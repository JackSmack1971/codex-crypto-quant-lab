# Bounded task DAG
Supervisor identifies decision question and evidence windows, checks tool capability registry, then fans out at most three independent investigations by domain: spot/derivatives, onchain/DeFi, and official/source verification. Each worker returns typed provenance + failure/unknown ledger. A synthesis barrier merges only definitionally compatible data; alternatives are context not drop-in equivalents. Methodology reviewer adversarially checks time alignment, sample bias, nulls, lookahead and claims; report editor drafts final local deliverable. A verifier checks actual citations and generated artifacts. No nested delegation. Native Codex roles are behavioral threads sharing parent permission/MCP state, not independently sandboxed processes or worktrees. Subagent disagreements are resolved by sources/tests, never majority vote.

## Hypothesis competition (optional, evidence-led)
For conflicting crypto explanations, supervisor predefines alternatives, falsifiers and essential metric coverage. Delegate bounded source gathering using existing roles; synthesize at one barrier. `crypto-hypothesis` consumes explicit evidence assignments; no role is a vote and no duplicate observation is independent corroboration. Use `research_lab.evaluate_hypothesis` only after source-quality and instrument-compatibility checks. The methodology reviewer assesses contradictory and missing evidence. Do not add a second LLM orchestration runtime. See `research-lab.md`.


## Research experiment path (v2.3)
Only after a source-quality synthesis barrier, use the `crypto-strategy-lab` Skill to bind verified observations to deterministic models and a declared `StrategySpec`. `crypto-temporal-validation` checks time windows and selection bias; the independent methodology reviewer inspects actual folds, evidence timing, incompatible metrics, and hypothetical cost assumptions. Persist locally via `EvidenceArchive` and `TransactionalResearchLedger`, and retain separately anchored tip receipts when audit requirements justify them. Do not treat research signals as executable positions, automated trading recommendations or authorizations.

## Research committee contract (v2.4)

An analytical committee is a *task DAG*, not another agent runtime or permanent set of personas. The primary Codex supervisor defines one decision question, as-of UTC, instrument and venue identities, mandatory evidence, shared-source dependency risks, and explicit falsifiers. Delegate only when the question has independent branches:

1. `market_researcher`: spot/derivatives and cross-asset observations with timestamped source identities.
2. `onchain_researcher`: chain/network observations with entity-attribution limitations.
3. **Synthesis barrier:** reconcile source overlap, vendor dependencies, conflicting data and definition mismatches before conclusions.
4. `source_verifier`: adjudicate missing primary sources, quote accuracy, availability and provenance.
5. `methodology_reviewer`: challenge selection bias, label leakage, cost assumptions and inferential leaps; record dissent as evidence-linked hypotheses.
6. `report_editor`: prepare the local evidence-bound report; the supervisor owns final verdict and stop/go criteria.

Bull and bear briefs are task assignments within these roles, not votes. Max three independent concurrent investigative workers; no nested fan-out; one author owns any overlapping mutable artifact. Every worker supplies `task_id`, exact input baseline, observed source/UTC, unknowns, method, conflicts and evidence IDs. A committee cannot convert absent data into consensus. Use `research_lab.evals.evaluate_task` to grade saved artifacts; only a runtime can attest what tools were actually used.

**Durable jobs:** `research_lab.jobs.JobStore` records task transitions. A recovered `RUNNING` job is *not* `COMPLETED`; reconcile local files, database rows and downstream effects before moving `INTERRUPTED → RECONCILING → RUNNING`. No automatic retries of non-idempotent actions. Job records do not create Codex sessions or authorize external effects.
