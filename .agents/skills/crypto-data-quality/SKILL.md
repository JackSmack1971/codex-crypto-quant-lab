---
name: crypto-data-quality
description: Audit crypto API observations for identity, freshness, schema, causality, compatible units, sampling gaps and fallback integrity before inference or a report.
---
# Data quality gate
Check canonical instrument ID, source provider/endpoint, UTC observation time vs retrieval time, clock skew, stale TTL by metric, units, sorted timestamps, coverage fraction, type/shape, null semantics, source-specific methodology, sample vs population and cache/fallback path. Prevent lookahead, survivorship and multiple-testing bias. Label incomplete candle, depth REST snapshot and funding-event cadence. Reject volume/count equivalence, TVL/market-cap equivalence, cross-chain address joins from ticker, and USD notionals inferred from contract quantity without verified multipliers. Return `PASS | DEGRADED | BLOCKED` with itemized evidence and concrete rerun path; never fill missing values.
