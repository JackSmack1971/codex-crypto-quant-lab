---
name: crypto-report
description: Create a local evidence-bound cryptocurrency quantitative research report with traceable numeric claims, executive narrative, quality matrix, scenario analysis, accessible tables and a source/uncertainty ledger. Use for rich crypto research reporting.
---
# Rich report generator
1. Validate research notes and instrument/source/time ledger; block significant unsourced numeric claims.
2. Produce `reports/<run-id>/report.md` and `manifest.json` locally under the source-control/report policy. The user may request HTML; produce only accessible self-contained HTML with numeric data-derived plots, never decorative pretend charts.
3. Structure: title; UTC as-of; concise thesis; evidence table with columns metric/observed UTC/retrieved UTC/instrument/source/value/unit/status; competing hypotheses; scenario triggers and falsification; uncertainty and data gaps; limitations; method; bibliography; decision grade.
4. Every value has source and methodology; flag proxies and non-executable liquidity. Do not use an alternate venue as a same-venue fallback. If primary support missing, show `INSUFFICIENT_EVIDENCE` or `NO_TRADE` and identify acquisition path.
5. Capture actual verification and file digest in manifest. Report only performed checks. Never publish without explicit separate authority.
