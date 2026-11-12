# Crypto Quant Research Control Plane — 12-axis package

Includes preserved original three Skills, expanded research Skills, five bounded Codex research roles, minimal trusted-project configuration, 12-axis policy documents, and a local keyless read-only public-data MCP reference adapter. Designed for Windows 11 PowerShell / Python 3.11+ and current Codex CLI. No orders, wallets, payments, private APIs or autonomous publication.

**Recovered implementations:** `src/market_data.py`, `src/indicators.py`, `src/backtest.py`, their pytest tests, and standalone `src/edge_forensics.py` are now included from the separately supplied `tests(1).zip`. The original three `SKILL.md` files remain unchanged. See `docs/original-AGENTS.md` and `docs/free_crypto_quant_mcp_architecture_v1.md` for source inputs.

`python scripts/verify_control_plane.py` validates structure. `python -m unittest discover -s tests -p "test_core.py" -v` and the analogous `test_reporting.py` invocation validate stdlib-only components; with `test` extras installed, `python -m pytest tests -q` runs the combined suite (including legacy quantitative implementations). MCP requires installation of pinned SDK and live smoke verification. See `docs/INSTALL-WINDOWS.md`.

**Research baseline:** official Codex latest stable tagged `rust-v0.167.1` (Nov 10, 2026); current CLI installation/Windows behavior not verified. The local MCP implementation is a reference subset of the provided v1 architecture. Its feature coverage and gaps are documented in `docs/IMPLEMENTATION-STATUS.md`.

## Bundled quantitative scripts

Install `python -m pip install -e ".[test]"` for local tests, or `python -m pip install -e ".[quant]"` for data-fetch/indicators/backtest without pytest. From the package root, use the existing `crypto-ohlcv-fetch`, `crypto-indicators`, and `crypto-backtest` Skills for commands and verified CSV sidecar contracts. The MCP SDK dependency remains separate from the quantitative extras. The added research-only strategy replay and temporal validation tools are offline reference implementations; fitted-model CV, independently certified data timing, live network tests, and trade execution remain out of scope. `src/edge_forensics.py` is a standalone, network-using investigative script; inspect its parameters before running.

## v2.3 local research laboratory

The original twelve-axis control plane remains in place; there is no new LLM agent framework or broker integration. `research_lab/` now adds versioned MCP-to-observation mapping, a constrained read-only adapter bridge, immutable SQLite evidence archive, deterministic model catalog, composable score blending, next-bar research scenario simulation, rolling walk-forward and CPCV split indices, separate exploratory CSCV/PBO estimator, transactional SQLite hash-chained ledger, and an offline JSON run CLI. Refer to `control-plane/research-lab.md` for exact schemas and scientific limitations.

Run offline: `python scripts/run_research_lab.py research-input.json --archive local/evidence.sqlite --ledger local/ledger.sqlite --out local/result.json`. **No live results are generated without actual sourced inputs.** Missing timestamps prevent historical eligibility; no data is silently imputed. The new script is intentionally separate from the MCP server.

Validate: `python scripts/verify_control_plane.py` and `python -m pytest tests -q` (after installing test extras). `python -m unittest discover -s tests -p 'test_research*.py' -v` exercises the new dependency-free core. No external trading, live-paper execution, automatic model promotion, public publishing or full dashboard is implemented.

A fully synthetic, offline illustration is included at `examples/synthetic-research-run.json`. Run it with unique local output paths; synthetic values are **not market evidence**. Its PBO numbers demonstrate machinery only and must not be interpreted as investment results.

### v2.4 research correctness and intelligence extensions

New offline modules provide saved-task evaluation, train-only fixed-candidate freezing with explicit holdout gates, immutable SQLite thesis revisions, reconciliation-first persistent jobs, stress-cost grids, semantic fallback qualification, a compact crypto factor catalog, portfolio covariance diagnostics, and static accessible historical research comparisons. See `control-plane/research-lab.md` for APIs and scientific limitations; use `scripts/research_ops.py --help` for offline CLI operations. No live trading, external writes, replacement agent orchestrator, provider scheduling or holdout certification is introduced.
