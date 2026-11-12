---
name: crypto-strategy-lab
description: Design and evaluate deterministic research-only crypto strategies from provenance-qualified observations with causal replay and explicit simulation costs. Use for strategy composition, offline scenario replay, or hypothesis evaluation.
---
# Crypto strategy laboratory

1. Inspect `control-plane/research-lab.md`, applicable instructions, and source metric contracts; preserve research-only boundaries.
2. Predeclare instrument, horizon, source metrics, units, maximum observation age, model kind, and score scale. Never choose these by reviewing future returns.
3. Normalize only actual scalar MCP observations with source times; separately document earliest availability (retrieval does not prove historical publication).
4. Freeze eligible evidence in `EvidenceArchive`, run deterministic models and a `StrategySpec` through `replay_strategy`, and keep outputs local.
5. Report NO_DATA rather than zero when required evidence is missing. Simulation is hypothetical close-to-close exposure, not fills. Disclose assumed fee, spread and slippage costs, omitted impact/funding, and survivorship bias.
6. Run focused tests and methodology review; do not create orders, wallets, brokers, publications or production permissions.
