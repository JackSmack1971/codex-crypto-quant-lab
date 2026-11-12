# Free Crypto Quant MCP Adapter Suite — Engineering Design v1.0

**Status:** Design specification; not implemented or runtime-tested.  
**Research date:** 2026-11-11 (UTC date).  
**Target:** Codex CLI, Windows 11 PowerShell, optional WSL2/Linux.  
**Scope:** Public read-only endpoints only. No credentials, wallet signing, order submission or paid plans.  
**Design constraint:** Provider failures must never produce plausible-looking fabricated numbers.

## 1. Architecture and decisions

Use **one local MCP process** with four separate provider adapters. Modules are independently testable, with a shared HTTP client, data contract, provenance, rate limits, cache, history store, and typed error taxonomy.

```
Codex CLI -- stdio --> quant_free_mcp (official Python MCP SDK v2)
                            |
                  read-only tool registry
                            |
       input validation -> capability router -> provider adapters
                            |               |               |
                  Binance spot/futures   DefiLlama   DEX Screener   mempool.space
                            |
                canonical observations + provenance
                            |
           Decimal math / units / source compatibility rules
                            |
           SQLite cache + observations + bounded diagnostics
                            |
            structured success / partial / unavailable responses
```

No authenticated routes, writes, scraping, browser automation, wallet operations, or paid API substitution. Do not proxy arbitrary user-supplied URLs. Use explicit domain allowlists and fixed route definitions. API keys are unnecessary for these specified free endpoints.

### Runtime and dependencies

- Python 3.12 target; require >=3.11; venv local to project.
- Official `mcp` Python SDK v2, pinned to a tested minor version and lockfile/constraints; check installed Codex MCP compatibility before upgrading, since MCP protocol revisions may affect clients.
- `httpx` (async transport, connection pooling, TLS, timeouts). Prefer no additional framework. `pydantic` only if consistent with SDK/runtime; stdlib `decimal`, `sqlite3`, `datetime`, `zoneinfo`, `hashlib`, `logging`.
- Default single process and stdio; stderr logging only, stdout reserved for MCP protocol.
- SQLite in `%LOCALAPPDATA%\quant-free-mcp\data.sqlite3` on Windows or configurable XDG data path. WAL, busy timeout, bounded retention. Use single-process writer; protect migration state and avoid concurrent writer corruption.
- Every outbound request constrained to documented read-only GET routes; public Binance WebSocket as a future optional ingestion process, not part of MVP.

## 2. Provider endpoint contract

| Provider | Free base / route examples | Core fields | Limit / caveat |
|---|---|---|---|
| Binance Spot | `https://data-api.binance.vision/api/v3/depth`, `/trades`, `/aggTrades`, `/klines`, `/ticker/bookTicker`, `/exchangeInfo` | Book levels, recent trades, candles, min notional/tick filters | Public no-key market-data-only API; request weights vary by endpoint and depth. |
| Binance Futures | `https://fapi.binance.com/fapi/v1/premiumIndex`, `/fapi/v1/openInterest`, `/fapi/v1/fundingRate`, `/futures/data/openInterestHist`, `/futures/data/globalLongShortAccountRatio`, `/futures/data/takerlongshortRatio` **only where docs confirm current route** | Mark/index, funding, OI, ratio, taker flow | Public endpoint access may be geoblocked or unavailable; history windows differ; mark unavailable instead of inferring. Do not silently mix USDT-M and COIN-M. |
| DefiLlama | `https://api.llama.fi/protocols`, `/protocol/{slug}`, `/v2/historicalChainTvl/{chain}`, `/overview/fees` | TVL, protocol/chain attribution, fees/revenue where exposed | Keyless core API. Free `/pools` and `/chart/{pool}` yield endpoints retire 2026-11-13: **do not implement**. Official DefiLlama MCP requires paid API; use local adapter. |
| DEX Screener | `https://api.dexscreener.com/latest/dex/pairs/{chainId}/{pairId}`, `/latest/dex/search?q=...`, `/token-pairs/v1/{chainId}/{tokenAddress}`, `/tokens/v1/{chainId}/{tokenAddresses}` | DEX price, volume, txn counts, liquidity USD, FDV, market cap, pair creation | Published 300 req/min for pair/search/token routes; 60 req/min for profiles/boosts. Never interpret marketing boosts as independent adoption. |
| mempool.space | `https://mempool.space/api/v1/fees/recommended`, `/api/mempool`, `/api/blocks/tip/height`, `/api/v1/mining/hashrate/{timePeriod}`, `/api/v1/mining/difficulty-adjustments/{interval}` | Sat/vB estimates, mempool count/vsize, tip, hashrate, difficulty | Public and variable policy; apply conservative client quotas. Bitcoin mainnet only by default; hashrate series may be weekly averages. |

**Documented sources** (verified via web documentation, not endpoint live tests):  
Binance market-only: https://developers.binance.me/en/docs/products/spot/faqs/market_data_only  
Binance futures market: https://developers.binance.me/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data  
DefiLlama free endpoints: https://github.com/DefiLlama/api-docs/blob/main/llms-free.txt and https://api-docs.defillama.com/  
DEX Screener: https://docs.dexscreener.com/api/reference  
mempool.space: https://mempool.space/docs/api/rest  
MCP SDK v2: https://py.sdk.modelcontextprotocol.io/

**Important validation gate:** Reconfirm each exact route, response shape, endpoint retirement, and IP rate limit against live official docs before integration. No implementation should rely on approximate endpoint names in this proposal.

## 3. Public MCP tool surface

Use namespaced tools with bounded arguments and output sizes. Expose a discoverable capability registry so callers do not guess provider support. Responses have the same envelope regardless of tool.

| Tool | Inputs | Output | Primary |
|---|---|---|---|
| `quant_capabilities` | optional category | supported metrics, endpoints, limitations, allowed fallbacks | Local |
| `quant_health` | `probe=false` default | passive last successes/errors; `probe=true` makes bounded public requests | Local |
| `market_spot_book` | venue=`binance`, symbol, depth=100 | bid/ask levels, spread, depth imbalance | Binance |
| `market_spot_trades` | venue, symbol, limit<=1000 | recent trades, directional volume, CVD **window-only** | Binance |
| `market_spot_candles` | venue, symbol, interval, limit<=1000 | typed OHLCV, quote volume, timestamps | Binance |
| `market_spot_microstructure` | symbol, depth band BPS, trade-window specification | composite spread/depth/trade flow if observations temporally aligned | Binance |
| `market_futures_snapshot` | symbol, venue, market=`usdm` | mark, index, funding, OI, instrument type | Binance Futures |
| `market_futures_history` | symbol, metric, interval, from/to, max_rows | historical funding/OI/ratios only if accessible | Binance Futures |
| `defi_protocol_snapshot` | protocol slug | current TVL, chains, source-attributed fee/revenue when available | DefiLlama |
| `defi_protocol_history` | slug, start/end, max_rows | TVL time series with timestamps | DefiLlama |
| `defi_chain_snapshot` | chain slug | TVL, history/changes with precise attribution | DefiLlama |
| `dex_token_pairs` | chain ID, token address, max_pairs<=50 | pools, liquidity, market cap/FDV, volumes, creation | DEX Screener |
| `dex_pair_snapshot` | chain ID, pair address | exact pool metrics | DEX Screener |
| `dex_search_pairs` | query, limit<=20 | discovery results, explicitly non-unique | DEX Screener |
| `btc_mempool_snapshot` | no input | mempool size/vsize/count and selected fee estimates | mempool.space |
| `btc_mining_snapshot` | fixed periods, optional lookback | hashrate/difficulty/block context and cadence | mempool.space |
| `quant_compare` | metric, two or more compatible source references | cross-source comparison; refuses incompatible definitions | Local |
| `quant_history` | metric, instrument canonical ID, start/end, max_rows | locally recorded observations, **not exchange-complete history** | SQLite |

Future tools require separate design approval, not speculative stubs.

## 4. Identity and provenance contract

Never normalize only by a bare ticker (`BTC`, `SOL`) because symbols collide and instrument type differs.

Canonical identities:
- Spot: `binance:spot:BTCUSDT`, with base and quote assets and filter metadata.
- Perpetual: `binance:usdm:BTCUSDT:perp`, with contract multiplier/settlement currency.
- DEX pair: `dexscreener:solana:<pairAddress>` and base/quote token addresses; never use project name as key.
- DeFi protocol: `defillama:protocol:<slug>`; mapping to token address is **not implicit**.
- Bitcoin network: `bitcoin:mainnet`.

Every successful/partial/unavailable response includes:

```json
{
  "schema_version": "1.0",
  "status": "ok",
  "metric": "spot.orderbook.imbalance",
  "instrument_id": "binance:spot:BTCUSDT",
  "value": "0.12345678",
  "unit": "ratio",
  "observation_time_utc": "2026-11-11T12:00:00Z",
  "retrieved_at_utc": "2026-11-11T12:00:01Z",
  "source": {
    "provider": "binance",
    "base_url": "https://data-api.binance.vision",
    "endpoint": "/api/v3/depth",
    "response_identifier": "lastUpdateId:123456"
  },
  "methodology": "top_N_book_levels_imbalance",
  "parameters": {"levels": 100},
  "freshness": {"age_ms": 1000, "ttl_ms": 1500, "stale": false},
  "quality_flags": [],
  "fallback_used": false,
  "coverage": {"scope": "single_venue_spot", "completeness": "snapshot"}
}
```

Example only, not a real market measurement. Prices/quantities/ratios serialized as decimal strings; UTC ISO-8601 timestamps; server observation timestamps kept distinct from retrieval times. If no source timestamp exists, set `observation_time_utc=null`, flag `source_time_unavailable`, and do not fabricate one. JSON `null` for missing values, not `0`. Include `as_of`, source definitions, sample size, units and method version for derived series. Never silently substitute stale cache.

Error statuses: `unavailable`, `partial`, `stale`, `invalid_request`, `rate_limited`, `upstream_error`, `schema_changed`, `unsupported`, `incompatible_sources`. Every error includes `retryable`, `reason`, `provider`, `last_verified_at` when known. Logs must not leak secrets or complete query contents unnecessarily.

## 5. Quantitative definitions

Calculations use `Decimal`; prohibit infinities and NaN; reject empty books, crossed books, nonpositive prices and divide-by-zero. Store calculation name/version.

1. **Mid**: `(best_bid + best_ask) / 2` in quote currency/base asset.
2. **Spread BPS**: `10000 * (best_ask - best_bid) / mid`, with spread >= 0.
3. **Depth imbalance**: `(sum_bid_base_qty - sum_ask_base_qty) / (sum_bid_base_qty + sum_ask_base_qty)` over the same explicit N levels or **symmetric** ±X BPS band from mid; range [-1,1]. Base-size and notional modes must not be mixed.
4. **Depth within band**: sum `price * quantity` for bids/asks within ±X BPS of mid, denominated in quote currency; one-time book snapshot, not executable liquidity promise.
5. **Taker buy volume**: for Binance spot `isBuyerMaker=false`, buyer is taker; taker sell if true. Compute base and quote notionals from the documented trade fields. If using aggregate trades, state that events are aggregated.
6. **Trade delta**: `sum(taker_buy_quote) - sum(taker_sell_quote)` over **explicit observed window**; `CVD` is a cumulative sum within that window, not lifetime market CVD.
7. **VWAP**: `sum(trade_price * trade_base_qty) / sum(trade_base_qty)`, only for the sampled covered trade window. Not equivalent to full-market VWAP if sampled/paginated data incomplete.
8. **Realized volatility**: `stdev(ln(close_t / close_{t-1})) * sqrt(K)`; K must identify bars per annualization period and session assumptions. Never label a 24/7 crypto series as calendar-day volatility without specifying sampling.
9. **Perp mark/index basis BPS**: `10000 * (mark-index)/index` for matching venue/contract at aligned timestamps; distinguish current perp basis from dated futures annualized carry.
10. **OI change**: `(OI_t / OI_{t-1} - 1)*100`, matching exchange/instrument and contract units; Binance OI units may not directly represent USD notional.
11. **TVL change**: `(TVL_t / TVL_{t-n} - 1)*100` when comparable protocol/method; beware double-counting, chain bridges, rebases and denomination changes.
12. **FDV/liquidity**: `FDV_USD / pool_liquidity_USD` for a specifically named pool; risk heuristic, not valuation or true portfolio liquidation capacity.
13. **Volume/liquidity**: `24h_reported_volume_USD / current_pool_liquidity_USD`; turnover proxy, not a verified wash-trading classifier.
14. **Pool age**: `retrieved_at - pairCreatedAt`; identify first **observed pool creation** timestamp, not token deployment.
15. **Bitcoin mempool pressure**: report vsize, count, fee estimates and changes separately; any composite regime score must be explicitly versioned with fixed thresholds and backtests.
16. **Fee-rate estimates**: sat/vB as quoted by mempool.space, not guaranteed inclusion or miner-confirmed fees.
17. **Hashrate**: identify period/weekly averages; don't present a weekly-average value as instantaneous mining rate.

## 6. Freshness, data quality and rate policies

These are **local design defaults**, not provider-promised refresh rates. Allow overrides via config but preserve tool metadata.

| Data family | Cache TTL | Maximum age for live-sensitive decision | Notes |
|---|---:|---:|---|
| Spot book | 1 s | 3 s | REST snapshot only; if older return stale/unavailable for execution-sensitive use |
| Recent trades | 2 s | 10 s | Distinguish returned sample from full bounded interval |
| 1m candles | 15 s | 90 s | Mark incomplete latest bar |
| Perp mark/funding | 10 s | 30 s | Funding event timestamp distinct from sample timestamp |
| Futures OI | 30 s | 90 s | Expiry/contract type required |
| DEX pair stats | 30 s | 120 s | Some rolling metrics updated less frequently upstream |
| DeFi TVL | 15 min | 24 h for fundamental screening | Historical points may lag; inspect upstream time |
| DeFi fees/revenue | 30 min | 48 h for fundamental screening | Fiscal-like periods must be explicit |
| Bitcoin mempool/fees | 15 s | 60 s | Source time may not be supplied |
| Bitcoin mining/difficulty | 30 min | 24 h (dependent on series) | Period averages are not live signals |

- Provider-level leaky/token-bucket with configurable limits below documented maxima. Track Binance request **weights**, not only request count, and honor `Retry-After` and exchange usage headers where present.
- DEX Screener: separate 300/min endpoint buckets from 60/min endpoints; set safer defaults (e.g. 120/min and 30/min) and allow reductions.
- DefiLlama/mempool.space: conservative adjustable quotas, because a universal free quota is not established here; do not assert unlimited access.
- Shared in-flight request deduplication; bounded concurrency per provider (default 2-4), max response size 5 MB, paging caps, connect/read/total timeouts; exponential backoff with bounded jitter for safe GET only.
- Honor 429 without bypassing quotas through other hostnames, rotated IPs or new processes. Circuit breaker after repeated provider failures; half-open bounded probe.
- Validate content type, JSON types, value ranges, units, duplicate timestamps, sort order and null handling. Reject ambiguous/changed schemas rather than extracting a plausible number.
- Only trust documentation-defined source timestamps. NTP/clock drift detection, timezone-aware UTC clock, no timestamp interpolation.

## 7. Fallback contract

Prefer **metric-consistent degradation** over forced completion. Fallback chain must be declared per tool and may end in `unavailable`.

| Requested datapoint | Primary | Permitted fallback | Forbidden substitution |
|---|---|---|---|
| Binance spot mid/spread | Binance bookTicker/book | Binance depth same market | DEX pool spot price labeled as Binance quote |
| Binance book imbalance | Binance depth | last-good **only if freshness SLA met** | DEX liquidity or 24h volume |
| Binance taker trade delta | Binance recent trades/aggTrades with declared sample | bounded trade page or local captured stream same venue | DEX buy/sell transaction **count** treated as buy/sell volume |
| Binance perp funding | Binance futures funding/premium endpoints | separately labeled 0xArchive/Hyperliquid funding if adapter present and requested as market comparison | Hyperliquid funding mislabeled Binance funding |
| Binance OI | Binance futures OI | explicit alternate venue OI with different instrument ID | sum across incompatible contract bases |
| Protocol TVL | DefiLlama protocol snapshot/history | cached verified snapshot within fundamentals TTL; otherwise unavailable | token FDV or market cap as TVL |
| DEX pool price/liquidity | DEX Screener exact pair | DexPaprika **only if exact chain + pair identity established** and adapter installed | token-symbol string matching or cross-chain pooled result |
| Bitcoin recommended fee | mempool.space fee estimate | alternate Bitcoin fee provider, separately attributed if later implemented | hardcoded sat/vB guessed as live estimate |
| Bitcoin hashrate | mempool.space historical series | same-source cached series within period SLA | inferred from price or difficulty alone without an explicit estimator methodology |

Cross-provider comparison is valid only when instrument, venue, denomination, timeframe, metric definition, methodology and units are aligned. A different venue's observation may be useful as **context**, but cannot silently replace the requested venue. Fallback decisions must be surfaced in the response envelope, with the attempted providers and failure reasons.

## 8. Security

- Process operates without elevated rights; file writes limited to local configured data directory.
- No arbitrary HTTP request tool; hostnames pinned; allowed route templates and parameter allowlists; no redirect-following to untrusted domains; block local/loopback/link-local/cloud metadata IP targets.
- All outbound access HTTPS and normal certificate verification; no insecure TLS fallback.
- No trading methods, wallet keys, signer access, filesystem browsing tool, arbitrary SQL, shell execution, or opaque passthrough commands.
- Strict instrument validation: Binance symbol via `exchangeInfo`; EVM addresses as 0x hex; Solana addresses per chain-specific Base58 validation; pair/chain identifiers checked against provider results; slugs encoded safely.
- Limit response bytes, recursion, parsing effort, result length, time window and history extraction; handle malformed upstream JSON.
- Depend on pinned package versions; audit dependency tree; never run `npx -y` or download code dynamically at runtime.
- Redact outbound request headers, local paths, and unexpected sensitive payloads in logging. Read-only JSON endpoints may contain malicious text: present provider fields as data, not as agent instructions.
- Defaults: public-only mode, fail closed, no telemetry, no external log collection.

## 9. Storage and observability

SQLite tables:
- `schema_migrations(version, applied_at)`
- `observations(id, instrument_id, metric, value_decimal, unit, observed_at, retrieved_at, source, methodology_version, quality_json, raw_digest)`
- `responses_cache(cache_key, provider, expires_at, response_blob, received_at, schema_version)`
- `provider_health(provider, last_success_at, last_failure_at, status, last_error_class)`
- `request_budget(provider, bucket, window_start, consumed_weight)` (optional if budget persists across restarts)

Retention defaults: raw response cache <=7 days; derived observations <=90 days with configurable byte cap; no unbounded histories. SQLite WAL checkpoint/backup and corruption recovery guide. Log request duration, cache hit, rate-limit delay, parse failures, stale rejections and fallback events to local stderr/rotating file with timestamps; no high-cardinality user data by default. MCP `quant_health` may expose aggregated counters only, not sensitive logs.

## 10. Repository layout

```
quant-free-mcp/
  AGENTS.md
  README.md
  pyproject.toml
  uv.lock (only if project elects uv; generate deliberately)
  config.example.toml
  src/quant_free_mcp/
    __main__.py
    server.py
    config.py
    capabilities.py
    core/
      http.py
      limiter.py
      cache.py
      storage.py
      schema.py
      errors.py
      routing.py
      math.py
      time.py
      security.py
    providers/
      binance_spot.py
      binance_futures.py
      defillama.py
      dexscreener.py
      mempool.py
    tools/
      market.py
      derivatives.py
      defi.py
      dex.py
      bitcoin.py
      diagnostics.py
  tests/
    fixtures/  # redacted licensed? public deterministic fixture snapshots
    unit/
    contract/
    integration/
    mcp/
  scripts/
    smoke_test.py
    verify.py
```

Avoid adding a framework, queue, distributed database or streaming infrastructure before a measured requirement justifies it.

## 11. Verification / acceptance matrix

| Suite | Checks | Gate |
|---|---|---|
| Unit | Decimal metrics, quote/base units, direction of `isBuyerMaker`, negative/zero quantities, timestamps | All pass |
| Provider contracts | Fixed mocked fixtures for valid, partial, renamed, missing, 429, 5xx, HTML, malformed JSON, huge responses | All pass |
| Cache | TTL boundary, clock skew, stale policy, cache key isolation, duplicate in-flight calls | All pass |
| Rate limits | Weighted Binance limit, separate DEX limit buckets, Retry-After, circuit breaker | All pass |
| Provenance | Every value has source, timestamp status, units, instrument, methodology | All pass |
| Fallthrough | Provider down, incorrect instrument match, stale fallback, incompatible units, no valid alternative | Never fabricate |
| Security | Reject arbitrary URLs, POST, redirect to local network, invalid symbols, shell/query injection | All pass |
| MCP | Tool listing, JSON schema, stdio initialization/compatibility, no stdout logging, result validation | All pass |
| Integration (opt-in) | Real GET on every configured provider; record HTTP status/shape, keyless access, region-specific failures | Report separately; not required for deterministic unit CI |
| Windows smoke | PowerShell launch, venv path, Codex MCP handshake/tool invocation, clean shutdown | All pass on target machine |

Property/metamorphic tests: imbalance remains in [-1,1]; uniform scale of both sides leaves imbalance unchanged; price scale changes spread BPS invariantly; zero trade count yields missing VWAP rather than zero; matching venue/side quantities produce delta zero; BTC mainnet series cannot be labeled Ethereum.

Suggested test commands (execute only after implementation):

```powershell
python -m pytest tests/unit tests/contract tests/mcp -q
python -m pytest tests/integration -q  # opt-in live integration, subject to public limits
codex mcp list
```

## 12. Codex configuration and local installation proposal

After publishing the local package and pinning dependencies, configure the Python executable from its venv (do not assume the path exists until deployment):

```toml
[mcp_servers.quant_free_local]
command = "C:\\path\\to\\quant-free-mcp\\.venv\\Scripts\\python.exe"
args = ["-m", "quant_free_mcp"]
startup_timeout_sec = 30.0
tool_timeout_sec = 45.0
```

Keep existing MCP entries unchanged; add this server after local implementation and smoke tests. Never point `command` at an installer or remote script. If using WSL, configure a separate launch wrapper only after confirming path and Codex's process environment.

## 13. Delivery phases and implementation stop points

**P0 — Core contract and read-only transport.** Create packaging, config, MCP server, canonical envelope/errors, safe HTTP, rate limiting, caching, fixture-based tests and one Binance tool. Verify SDK v2 compatibility with deployed Codex before expanding. **Stop** if handshake fails.

**P1 — All four provider adapters.** Add remaining GET-only tools, identity mapping, schema validation, regression fixtures and provenance. **Stop** if any requested endpoint requires billing/auth or returns incompatible schema; disable it and document.

**P2 — Quantitative layer.** Implement spread, imbalance, delta, VWAP, OI change, TVL change, pool metrics, series integrity; align sampling and time windows. Include property-based/metamorphic cases and fallback reporting.

**P3 — Durability and operations.** SQLite history, diagnostics, retention, Windows installer guide, controlled live probes, Codex smoke tests. Keep optional WebSocket capture and multi-venue synthesis out of v1 unless separately scoped.

### Explicit non-goals

- No order execution or trade recommendations gated on unavailable live depth.
- No exchange wallet/entity attribution from raw on-chain movements.
- No historical tick reconstruction before the adapter was running unless documented downloadable historical datasets are separately ingested.
- No free-tier circumvention or evasion of upstream usage limits.
- No promise of institutional completeness or guaranteed uptime of free services.

## 14. Unresolved implementation decisions

1. **MCP SDK/client compatibility:** Python SDK v2 has protocol changes; verify Codex client interoperability. Pin a compatible SDK revision if needed.
2. **Binance Futures availability:** Some markets/regions may be restricted; explicitly probe without assuming public endpoint reachability.
3. **Historical persistence budget:** Size and retention configurable; set measured defaults after observations of real request volume.
4. **DeFi revenue definition:** Fees, revenue, holder revenue are distinct concepts; do not merge them.
5. **Live microstructure:** REST snapshots cannot guarantee reconstructable order-book sequence; add snapshot+WebSocket diff synchronization as separate verified extension if true continuous L2 is required.

## 15. Verification status for this deliverable

- Official docs reviewed: yes (URLs in §2).
- Detailed architecture, formula rules, threat model, fallback matrix and acceptance criteria: specified.
- Source code written: no.
- Executable tests or live MCP handshakes: not run.
- Assumption that every public endpoint works from the target user's geography/network: **not established**.
