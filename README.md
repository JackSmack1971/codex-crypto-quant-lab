# Crypto Quant Research Control Plane

A **research-only, twelve-axis Codex control plane** for cryptocurrency investigation, source reconciliation, reproducible quantitative analysis, and local evidence-bound reports. The repository includes 13 Codex Skills, five bounded research-agent roles, a keyless public-data MCP reference server, and an offline research laboratory with durable missions, experiment governance, reporting, and benchmarks.

**Not a trading system.** No order placement, wallet/signing operations, paid-provider bypass, unattended online publication, or verified predictive trading edge is provided. The deterministic mission coordinator does **not** invoke Codex agents or retrieve live provider data on its own.

> **Current integration:** P0 acquisition/evidence hardening, P1 mission execution and experiment registration, and P2 intelligence reporting/benchmarks are merged into `main` (merge commit `ea98440ed79600c7aea2384c851a0b34eb84146e`, November 12, 2026). The three jobs in post-merge offline CI run [43263065915](https://github.com/JackSmack1971/codex-crypto-quant-lab/actions/runs/43263065915) were reported successful. This is **offline verification**, not Windows/client conformance, live-provider qualification, production readiness, or demonstrated predictive performance.

## Start here

**Prerequisites:** Python 3.11+; Windows 11/PowerShell is a documented target, not an independently certified runtime. Codex CLI is optional for the deterministic offline scripts; starting the MCP server requires the dependency declared in `pyproject.toml`.

```powershell
# From the repository root, in PowerShell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe scripts/verify_control_plane.py
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe scripts/benchmark.py --repeat 2
```

For the MCP client configuration, permissions, optional dependencies, and Windows conformance checks, follow [docs/INSTALL-WINDOWS.md](docs/INSTALL-WINDOWS.md). Do not overwrite an existing global `~/.codex/config.toml` with the project config; the reference server must be registered deliberately. Live provider probes are separate, optional, and may be unavailable by jurisdiction.

## What you can do

| Capability | Entry point | Boundary |
| --- | --- | --- |
| Inspect the twelve-axis control plane | `control-plane/`, `.agents/skills/`, `.codex/agents/` | Instructions/roles are **not** runtime authorization or isolated sandboxes |
| Fetch public market measurements | `quant_free_mcp` | Fixed-route, HTTPS GET reference adapters; no automatic historical data certification |
| Replay a supplied research dataset | `scripts/run_research_lab.py` | Offline hypothetical analysis, not execution or validated alpha |
| Run a durable local research mission | `scripts/mission.py` | Pinned local inputs and stage transitions; no agents, network, or provider calls |
| Govern fixed-candidate holdout evaluation | `research_lab/experiment_registry.py` | Local-registry-relative exposure policy, not proof of independent samples |
| Compose local evidence-bound reports | Mission `RENDERING` stage; `research_lab/reporting.py` | Data-derived numeric claims; authored interpretations remain unauthenticated |
| Render a supplied observation ledger | `scripts/render_report.py` | Independent, simpler observation-envelope renderer; no invented scenarios |
| Explore supplied numerical series | `scripts/research_explorer.py` | Offline self-contained SVG and tables, no live updates |
| Inspect historical thesis revisions | `research_lab/intelligence.py`, `research_lab/history.py` | Comparisons require qualifying evidence; past `SUPPORTED` is not current truth |
| Run offline correctness benchmarks | `scripts/benchmark.py` | Fixture-based engineering tests, **not** predictive-performance measurement |

### 1. Run the standalone offline research replay

```powershell
.\.venv\Scripts\python.exe scripts/run_research_lab.py examples/synthetic-research-run.json --archive local/evidence.sqlite --ledger local/ledger.sqlite --out local/result.json
```

The example is **synthetic**. Use unique output paths and inspect the result's coverage, timing assumptions, and limitations. This is not a live market-data report. See [control-plane/research-lab.md](control-plane/research-lab.md).

### 2. Create and advance a durable research mission

The P1 coordinator takes a **versioned mission specification** (`research-mission-execution.v1`) referencing pinned local inputs, not an informal question alone. Supply an actual spec and matching input files following [control-plane/mission-completion.md](control-plane/mission-completion.md) and `research_lab/mission_spec.py`.

```powershell
.\.venv\Scripts\python.exe scripts/mission.py --workspace local/mission-workspace create path/to/mission-spec.json
.\.venv\Scripts\python.exe scripts/mission.py --workspace local/mission-workspace status YOUR_MISSION_ID
.\.venv\Scripts\python.exe scripts/mission.py --workspace local/mission-workspace advance YOUR_MISSION_ID --all
# Only when the mission reports interrupted/claimed work requiring reconciliation:
.\.venv\Scripts\python.exe scripts/mission.py --workspace local/mission-workspace resume YOUR_MISSION_ID
```

Stages are **PLANNED → ACQUIRING → RECONCILING → ANALYZING → VERIFYING → RENDERING → FINALIZING**. Execution lifecycle and scientific research status are separate; `COMPLETED` does not imply a validated hypothesis. A required provider operation without a supplied attempt record stays `UNATTEMPTED`. Reviewer handoffs are persisted assertions, not authenticated proof that reviewers ran. A terminal `BLOCKED` mission may require a new mission ID (`supersedes`) rather than an unsafe retry.

The coordinator emits `research-mission.v2` bundles but **never assigns `VALIDATED`**. Existing native Codex roles remain a separately invoked, bounded investigation workflow—not processes launched by this CLI.

### 3. Verify a mission bundle

Use the actual mission bundle directory and its matching evidence archive:

```powershell
.\.venv\Scripts\python.exe scripts/verify_mission.py path/to/finalized-bundle --archive path/to/evidence.sqlite
```

| Exit code | Verification status | Meaning |
| --- | --- | --- |
| `0` | `VERIFIED` | Required artifact checks and archive-dependent evidence/companion verification were performed and passed |
| `1` | `FAILED` | Incomplete, blocked, invalid, or a performed check failed |
| `2` | `PARTIALLY_VERIFIED` | Applicable checks passed, but archive or semantic companion verification was unavailable; **not** full verification |

The JSON keeps the existing completion `status` and separately reports `verification_status`, `evidence_verification`, and checks not performed. `--allow-partial` is a **local convenience** that can change the process exit code for a partial result, not its actual verification status; do **not** use it as a full-verification gate. Declared legacy `render@1` and companion-free bundles are not promoted to semantic verification. See [control-plane/mission-completion.md](control-plane/mission-completion.md).

### 4. Read the reports and explore the data

The P2 `RENDERING` stage produces `report.md`, `report.html`, and evidence-bound companions `report_content.json` and `report_manifest.json`. Reports label observations **[R]**, derived calculations **[D]**, interpretation **[I]**, and unavailable information **[U]**. Supported charts are `metric_history`, `normalized_comparison`, and `drawdown`; each is backed by numeric data and an accessible table. Unavailable metrics are not filled in or converted to zero.

The standalone envelope renderer remains available:

```powershell
.\.venv\Scripts\python.exe scripts/render_report.py path/to/findings.json reports/my-run
.\.venv\Scripts\python.exe scripts/research_explorer.py path/to/series.json reports/history.html
```

These use **different input schemas** from the mission report composer. The explorer requires timestamp-aligned, source-attributed series; neither command gathers its own market data. `scripts/research_ops.py` retains saved task evaluation, candidate freezing, and thesis utilities; governed holdout reuse should go through `ExperimentRegistry`, not the ungoverned illustrative `research_ops.py test` command.

### 5. Run deterministic benchmarks

```powershell
.\.venv\Scripts\python.exe scripts/benchmark.py --repeat 2
.\.venv\Scripts\python.exe scripts/benchmark.py --cases B01,B02
```

The current suite contains **31 offline cases (B01–B31)** spanning evidence provenance, mission recovery, holdout reuse, timestamp conflicts, companion verification, legacy rendering, and other engineering contracts. The full-suite mandatory gate checks dimension coverage; a subset can leave the gate `INCOMPLETE` even when the CLI returns `0` for no failing selected checks. Inspect the JSON `mandatory_gate` and `per_dimension` results rather than using the exit code alone to infer full-suite qualification. Benchmarks do **not** measure returns, forecasts, or predictive edge.

## Installation and compatibility notes

- `pyproject.toml` requires Python 3.11+ and pins the direct MCP dependency (`mcp==2.3.0`). Optional `[quant]` brings NumPy/Pandas, `[test]` also brings pytest. CI's `constraints-ci.txt` pins direct dependencies, not every transitive dependency.
- The MCP reference server supplies public keyless read-only measurements from Binance spot/USDM, DefiLlama, DEX Screener, and mempool.space, subject to region, availability, source formats, and declared limits. The local acquisition layer provides bounded retries, per-process quotas/circuit breakers and optional SQLite caching; it does **not** coordinate quotas across processes.
- The original `src/market_data.py`, `src/indicators.py`, and `src/backtest.py` and their three Skills remain bundled; `src/edge_forensics.py` is a separate network-using investigative script. Inspect them before execution.
- Offline CI runs standard-library suites on Python 3.11 and 3.13 and the pinned-dependency suite on Python 3.13. Live-provider tests are opt-in and separate. No actual Windows MCP handshake or live provider reliability was certified by the merged offline run.

## User-facing changes since the original v2.3/v2.4 README

See [docs/USER-FACING-CHANGES.md](docs/USER-FACING-CHANGES.md) for workflows, behavior changes, migrations, and limits added by P0–P2 and the integration hardening.

## Scientific and operational limitations

This package is **not production-ready** and does **not demonstrate predictive trading edge**. In particular:

- Holdout independence is only `LOCAL_REGISTRY_RELATIVE` when so labeled; shifted timestamps or altered dataset identity/digest can evade local lineage matching. It cannot detect out-of-band data inspection.
- Archived numerical claims are rechecked when a suitable archive is available, but authored interpretations are hash-bound rather than independently authenticated. Local hashes do not attest source truth or reviewer identity.
- Legacy render-version fixtures used in testing are simulated. Older companion-free reports remain readable but are not eligible for authoritative historical comparison.
- Benchmark mutation coverage is incomplete; passing benchmark fixtures is not proof that all defects have been detected.
- CI uses a fixture with `--allow-partial` because no archive accompanies it; an archive-backed full-verification path is tested separately. Do not confuse this convenience check with full evidence verification.
- No unattended provider acquisition by the mission coordinator, certified historical point-in-time data, live trading/order execution, validated live fills, or automatic public publishing exists.

For detailed implemented/absent capabilities see [docs/IMPLEMENTATION-STATUS.md](docs/IMPLEMENTATION-STATUS.md), [control-plane/mission-completion.md](control-plane/mission-completion.md), and [control-plane/report-contract.md](control-plane/report-contract.md).
