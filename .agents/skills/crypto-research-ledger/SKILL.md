---
name: crypto-research-ledger
description: Record and verify local hash-linked research results and point-in-time eligibility; use for repeat analysis, research history and historical replay qualification.
---
# Reproducible research records

1. Inspect the exact instrument, question, evidence envelope, available-at timestamps and calculation method versions before constructing a research record.
2. Use `research_lab.validate_replay` to fail closed when historical availability is unknown, later than the model's as-of timestamp, or inconsistent with the instrument. Passing this check establishes only point-in-time *input eligibility*, not backtest correctness or edge.
3. Keep source identifiers, hashes, method versions, report location, validation outcomes and missing evidence in the ledger payload. Do not store secrets, raw prompts or unrelated personal data.
4. Before appending, inspect and verify existing local chain with `ResearchLedger.verify`; append once with a unique run ID and never overwrite earlier entries. Retain the output file as a local artifact, with repository-relative path chosen by the operator.
5. Record that the local chain detects modifications only relative to retained hashes, and can be rewritten by a filesystem actor. It is not externally anchored or an immutable compliance ledger.
6. Compare like-for-like research runs with matching universe, horizon, instrument, source coverage and method. Do not equate historical fit with out-of-sample predictive performance.

Research-only: no autonomous strategy promotion, brokerage connectivity, scheduler or live trades.

For new runs, prefer `research_lab.archive.EvidenceArchive` and `research_lab.ledger_sqlite.TransactionalResearchLedger`. Call `verify()` before interpreting a chain. Export `anchor()` and retain it under separate governance; without an independent witness the chain can be reconstructed by anyone controlling the database. The archived observation release time must have an external basis. `scripts/run_research_lab.py` ties both stores to one offline scenario result but commits them separately; interrupted runs require reconciliation.
