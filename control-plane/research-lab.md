# Research laboratory — executable offline reference v2.3

**Purpose.** Extend the existing twelve-axis crypto research control plane without importing another agent runtime, brokerage framework, or live execution. Architecture is independently adapted from the educational [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) composable model/strategy/ledger ideas. No upstream source code was copied.

## Implemented interfaces

| Surface | File | Contract / boundary |
|---|---|---|
| Source observations | `research_lab/core.py` | Immutable `Observation`, `ResearchSignal`, hypothesis assessment, strict UTC and score checks; no implied probability |
| MCP normalization | `research_lab/normalization.py` | Local MCP envelope v1 → scalar `Observation`; exact fields, real observation time, source, field units; no automatic release-time inference |
| Provider bridge | `research_lab/provider_bridge.py` | Explicit allowlist of existing read-only `quant_free_mcp.adapters` functions; no arbitrary URLs, trading tools or new credentials |
| Historical archive | `research_lab/archive.py` | SQLite immutable observation ID/content binding, as-of eligibility, frozen snapshot ID/content checks; transactions and duplicate conflicts |
| Deterministic models | `research_lab/models.py` | Closed model registry: `signed_scalar`, `difference_scaled`; point-in-time, maximum age, method version and evidence references |
| Strategy engine | `research_lab/strategies.py` | Weighted model blend with coverage gate; deterministic next-bar close-to-close research scenario, stated bps costs; NO_DATA never zero |
| Temporal validation | `research_lab/validation.py` | Rolling walk-forward, CPCV label overlap purge/embargo, fixed-candidate fold evaluation, *separate* CSCV/PBO rank heuristic |
| Transactional audit | `research_lab/ledger_sqlite.py` | `BEGIN IMMEDIATE`, sequential hash chain, duplicate reject, tamper detection and exportable chain-tip receipt; requires independent storage for an anchor |
| End-to-end execution | `research_lab/workflows.py`, `scripts/run_research_lab.py` | Accept input JSON with explicit measured signals or deterministic models, strict eligibility, local evidence archive, scenario, optional validation, and recorded run |

`ResearchLedger` (the previous file-backed single-writer ledger) remains intact for compatibility. New work should use `TransactionalResearchLedger` for local multi-process write safety. Neither ledger protects against an adversary controlling the entire DB *and* the anchor; copy a verified tip receipt into a separately controlled store if this matters. SQLite is not a distributed database or third-party attestation system.

## Data invariants

- An `Observation` is a source-attributed value with metric, instrument, unit, URI, observed time, retrieved time, and separately justified earliest availability time. Unknown historical availability is not eligible for replay. A retrieved snapshot does **not** automatically establish availability at the historical time.
- Scalar conversion requires source observation UTC. Compound MCP values require explicit scalar field and field-specific unit. Unavailable envelopes remain unavailable; datasets are not silently filled or interpolated. Most public REST spot-book/quote data lack an exchange observation timestamp and cannot support historical replay without additional evidence.
- `ModelSpec` produces uncalibrated signed scores; `StrategySpec` is a model-weight configuration, not asset allocation. A predeclared score blend does not establish signal independence or predictive validity.
- `ClosedBar` has a declared close and availability time. Replay scores signal at the preceding bar's availability timestamp and evaluates the following bar's close-to-close move, after illustrative fee/spread/slippage costs. **Not an execution simulator**: excludes price impact, funding, borrow, real fills and latency; do not call its returns realized or executable.
- The `validate_replay` gate rejects calculated signals with future/incompatible evidence. Explicit insufficient-evidence signals remain missing and yield `NO_DATA`; incomplete research cannot be reported as a full simulation.
- A SQLite archive snapshot freezes observation *content*, not an authoritative source's full revision history or independently validated release timing.

## CLI

Run from the checkout with Python 3.11+:

```
python scripts/run_research_lab.py INPUT.json --archive LOCAL/evidence.sqlite --ledger LOCAL/research.sqlite --out LOCAL/run.json
```

`INPUT.json` uses `schema=research-run-input.v1`, `run_id`, `as_of_utc`, `observations` (`Observation` dictionaries), a `strategy` (`id`, `model_weights`), `bars` (`ClosedBar` dictionaries), and either explicit `signals` or `models` (model specifications). Optional `costs_bps` is mapped to `fee_bps`, `spread_bps`, and `slippage_bps`; optional `validation` provides `label_windows`, `candidate_period_returns`, `train_size`, `test_size`, `n_groups`, `n_test_groups`, `pbo_groups`, `embargo`. The temporal candidate series must be aligned to the label windows. `--out` uses exclusive creation; inputs and databases are never overwritten. No API fetch occurs unless the separate provider bridge is explicitly called.

A workflow interrupted between evidence archival and ledger commit may leave immutable cached observations without a run entry. Rerun is safe if the run ID has not yet been recorded. The archive and ledger are distinct SQLite transactions; they are not globally atomic.

## Validation limits and scientific interpretation

- `walk_forward_splits` is rolling (not expanding) and removes training labels overlapping the test start; embargo is measured in observations.
- `cpcv_splits` enumerates nonempty contiguous-group train/test splits and purges all intersecting training label windows. It **does not** construct independent CPCV paths, fit models, or eliminate candidate-selection and feature engineering leakage.
- `cscv_pbo` uses symmetric train/test group combinations and midrank OOS relative ranking with **mean return**, not Sharpe. Its output is an exploratory candidate-selection overfitting diagnostic, not a calibrated probability of future profit. Stable candidate definitions, aligned outcomes, sufficient sample size, regime checks and independent OOS verification remain necessary.
- `select_and_evaluate_folds` scores **precomputed fixed candidates**, not trainable models. CPCV test windows may repeat across folds, so its mean test score is not a tradable equity curve.
- All numerical claims and rich reports must continue to follow `control-plane/report-contract.md`, and use the original source-quality rules and independent methodology reviewer.

## Reference methods

- Bailey, Borwein, López de Prado and Zhu, *The Probability of Backtest Overfitting*, 2017: https://escholarship.org/uc/item/4w1110bb (CSCV/PBO theoretical basis; this package implements an explicitly limited diagnostic).
- Temporal purge/embargo and CPCV ideas are described in López de Prado, *Advances in Financial Machine Learning*, ch. 7. This implementation is a bounded basic splitter; no claim of replication of the complete original method.

## Not implemented or verified

No external signed/notarized audit anchoring, real execution/fill model, full historical provider dataset acquisition, provider-side revision history, universal source availability proof, automated strategy search, causal identification, trained/fitted model CV, CPCV OOS path construction, calibrated predictive probabilities, full dashboard, independent production CI, Windows installed-client MCP handshake, live provider uptime or production trading. Report them as gaps, not as successful features.

An executable no-network synthetic fixture is included in `examples/synthetic-research-run.json`; its values and outcomes are illustrative and should never be represented as real provider evidence.

## v2.4 evidence-driven research extensions

| Component | Contract | Limit |
|---|---|---|
| `research_lab.evals` | Saved-artifact task replay with required evidence/capabilities, numeric tolerances and `PASS` / `FAIL` / `NOT_EVALUABLE` | Recorded `used_capabilities` must be sourced from observed execution, not agent assertions |
| `research_lab.experiments` | Predeclared fixed-candidate train selection, purged boundary, deterministic freeze digest, explicit holdout evaluation | External pre-registration and reuse policing still required; not estimator fitting |
| `research_lab.intelligence` | Immutable SQLite thesis revisions, search, parent linkage, evidence/falsifiers, comparison | Revision state is an author's classification; retrieving does not revalidate evidence |
| `research_lab.jobs` | Transactional state machine, no blind restart | No external scheduler/process coordination; compare state on resume |
| `research_lab.strategies.cost_sensitivity` | Hypothetical cost-grid replay, preserves incomplete evidence | Not actual market depth, fills, or exchange fees |
| `research_lab.provider_policy` | Exact metric/instrument/venue/method/interval qualification and observed provider health | Provider data contracts must be supplied correctly; no network scheduler/rate limiter |
| `research_lab.factors` | Decimal-backed fixed formulas, explicit input/availability descriptions | Caller must certify source timestamps and commensurate units |
| `research_lab.risk` | Aligned sample covariance, portfolio variance contributions and correlations | Historical descriptive statistics, not a forecast |
| `research_lab.mission_spec` | Immutable `research-mission-execution.v1` contract: canonical digest, strict UTC, safe relative paths (no traversal/symlinks), fixed operation allowlist, no embedded commands | Validates shape and path syntax only; says nothing about whether inputs are true |
| `research_lab.mission_coordinator` | Seven-stage deterministic local engine on `JobStore`: transactional claims, fencing, immutable digest-checked artifacts, restart reconciliation | Not an agent runtime or scheduler; performs no provider calls; never emits `VALIDATED`; reviewer handoffs are unauthenticated assertions |
| `research_lab.experiment_registry` | Durable preregistration from the training partition, atomic single-use holdout claim, `REGISTERED`/`EVALUATING`/`CONSUMED`/`INVALIDATED` | Cannot stop out-of-band inspection of the dataset; a valid process is not a credible inference or an edge |
| `scripts/research_explorer.py` | Accessible no-JavaScript HTML charts and source-linked sample tables | Input series and their per-sample evidence IDs must be independently validated |

A frozen selection is not an untouched holdout unless the digest is registered **before** holdout access. The `freeze_candidate` function only reads training candidate returns; `evaluate_frozen` explicitly requires a registered hash, but cannot stop a user from repeatedly exploring the same holdout. Candidate returns are fixed inputs and no parameter fitting occurs. Likewise, provider health is an observation of event logs, not a reliable forecast of future uptime, and `qualified_fallback` must not be generalized across instruments/venues.

Local CLI utilities (all offline): `python scripts/research_ops.py eval SPEC.json ARTIFACT.json`; `freeze PLAN.json DATASET.json`; `test PLAN.json DATASET.json FROZEN.json`; `thesis-add DB.sqlite REVISION.json`; `thesis-search DB.sqlite QUERY`. Store freeze JSON independently before the holdout is unsealed. `python scripts/research_explorer.py SERIES.json reports/explorer.html` produces a static history viewer and rejects existing output files.

## Experiment governance (P1)
Use `ExperimentRegistry` instead of passing a digest to `evaluate_frozen`. `register` accepts **only the training partition** (`train_end` values per candidate), builds the freeze with the unmodified `freeze_candidate`, and stores the plan, partitions, embargo, label-window digest, candidate universe, dataset identity/version and a **mandatory dataset digest** (`dataset_digest(windows, candidates)`, binding the holdout values), declared costs and evaluation method. Registration also refuses a plan that could never be evaluated (no valid validation periods). `evaluate` first re-verifies the request against that record (rejecting a changed plan, windows, dataset identity, universe, training partition or dataset digest as `EXPERIMENT_MODIFIED`, and non-finite or misaligned streams as `EXPERIMENT_INVALID`, before the claim and before any holdout statistic is computed), then claims the holdout atomically (`REGISTERED` -> `EVALUATING`), evaluates, and records the result (`CONSUMED`). A repeat request returns the same stored, digest-checked result with `reused: true` and `independent_evaluation: false`. A crash after the claim leaves `EVALUATING`: the holdout is exposed, `HOLDOUT_CLAIM_UNCERTAIN` blocks retries, and `reconcile_evaluation` either completes it (recorded as `recovered`, never independent, exposure count incremented) or `INVALIDATE`s it. No call returns a claimed holdout to `REGISTERED`. A modified experiment requires a new `experiment_id`, but a new id never refreshes a holdout: the exposure budget is keyed by the holdout itself (its label windows on a dataset id/version), so registering or evaluating a second experiment over an already-exposed holdout is refused with `HOLDOUT_ALREADY_EXPOSED`. A rejected reconciliation adds no exposure; only a recorded result does. Limits: the registry is local, cannot prevent copying or inspecting the dataset elsewhere, and a valid registration is neither evidence of credible inference nor of predictive edge.
