# Deterministic mission completion bundle

Each run directory contains `mission.json` with schema `research-mission.v1`, `completion_state` (`COMPLETE`, `INCOMPLETE`, `BLOCKED`), independent `research_state` (`NOT_EVALUABLE`, `INSUFFICIENT_EVIDENCE`, `EVALUABLE`, `VALIDATED`), an `artifacts` map, `missing_artifacts`, `blockers`, and `remaining_work`. Required artifact keys are defined by `research_lab.completion.ARTIFACTS`. Each maps to a relative in-directory `path` and lowercase SHA-256 digest. Required artifacts include a mission baseline, provider attempt ledger, raw evidence references, metrics, hypothesis assessment, evaluation result, thesis revision or explicit non-revision reason, committee verification, and Markdown and HTML reports.

The baseline lists `required_provider_operations`; the attempt ledger records a unique `operation_id` and `SUCCEEDED`, `FAILED`, or `UNATTEMPTED` state for each required operation. This makes omitted operations detectable relative to the declared baseline. The committee contract in `control-plane/orchestration.md` makes source verification and methodology review mandatory before finalization. The verifier requires both reviews to be recorded as `PASS` with evidence references. Independent investigative branches are conditional on the question and remain a research workflow choice; they are not required for every mission. The report artifact fulfills the report-editor deliverable. Committee records prove only that a persisted assertion exists, not who performed it or whether the review was substantive.

Provider attempts use `SUCCEEDED`, `FAILED`, or `UNATTEMPTED`; a failed request and an unattempted retrieval are distinct. `INCOMPLETE` describes non-empty remaining work; failed or unattempted operations and missing artifacts must be explicitly recorded when they apply. `BLOCKED` requires an attributable blocker and remaining work; each missing-artifact record must name its blocker ID, which must match an evidenced blocker record. `COMPLETE` requires valid references and hashes, both required review records, internally consistent research/evaluation states, no missing-artifact declarations, and no unfinished work. `NOT_EVALUABLE` research is compatible with a process-complete mission. `VALIDATED` requires a passing evaluation containing non-empty `research-crossvalidation.v1` walk-forward and CPCV fold outputs with disjoint train/test indices, regardless of process state. This checks persisted structure and does not establish that the experiment design or conclusion is scientifically sound; process completion alone never establishes research validity.

Run `python scripts/verify_mission.py <directory>`. Exit 0 means the persisted bundle supports a `COMPLETE` declaration. Exit 1 means the result is incomplete, blocked, or invalid. JSON output includes stable diagnostic codes, messages, and artifact identifiers. The verifier does not execute or authorize research phases, prove source truth, authenticate committee identities, or establish runtime tool use. Hashes check consistency against recorded values only; a separately trusted baseline, signature, or external anchor is required for authenticity.


## P0 integrity layers and schema v2

`verify(root, archive=None)` and `verify_mission.py --archive <EvidenceArchive.sqlite>` now return a `layers` object that keeps five claims separate; none of them is cryptographic authentication (`authenticity` is always `NOT_AUTHENTICATED`):

| Layer | Meaning | States |
| --- | --- | --- |
| `artifact_integrity` | Recorded SHA-256 digests match the files. | `VERIFIED`, `INCOMPLETE`, `INVALID` |
| `source_provenance` | Each evidence id resolves to an archived `OBSERVED` observation (digest-checked, matching any declared `sha256`/`instrument_id`/`metric`/`unit`) that was observed, retrieved and available no later than baseline `as_of_utc`. | `ARCHIVE_RESOLVED`, `UNKNOWN` (no archive, or availability time undocumented), `INCOMPLETE`, `INVALID` |
| `review_provenance` | A `PASS` record exists for each required review. Reviewer identity and substance are not attested. | `ASSERTED_UNAUTHENTICATED`, `INCOMPLETE`, `INVALID` |
| `quantitative_verification` | Each metric lists `inputs` (observation ids that are also in `evidence.json`) and a `transform` (`identity`, `sum`, `mean`, `difference`, `ratio`, optional `tolerance`); the Decimal result is recomputed from archived inputs. Same-series transforms require one instrument/unit (and one metric except `difference`). | `VERIFIED`, `NOT_APPLICABLE` (no metrics), `UNKNOWN` (no archive), `UNSUPPORTED` (legacy metrics without lineage or an undeclared transform), `INCOMPLETE`, `INVALID` |
| `scientific_validity` | Only gates a `VALIDATED` designation. `CLAIM_GATES_PASSED` means preconditions held; `established` is always `false`. | `NOT_CLAIMED`, `UNSUPPORTED`, `CLAIM_GATES_PASSED` |

Every diagnostic now carries a `state`: `UNKNOWN` (cannot be determined from what was supplied), `INCOMPLETE` (required material absent), `INVALID` (malformed, contradictory, future-dated or failing recomputation) or `UNSUPPORTED` (a claim the verifier cannot check).

**`VALIDATED` is stricter (all schemas).** In addition to a passing evaluation with disjoint `research-crossvalidation.v1` walk-forward and CPCV folds, it now requires: every evidence reference resolved in an archive; every metric `VERIFIED` (at least one); and `label_windows` (`[{start_utc,end_utc}]`, in the evaluation `validation` object or per result, strictly increasing starts) against which every fold is checked: indices in range, no training label interval overlapping a test label interval (`CV_LABEL_OVERLAP_LEAKAGE`), and for walk-forward every training label ending before the first test label (`CV_TRAIN_NOT_BEFORE_TEST`). Embargo width is not verifiable from windows and is not claimed. **Migration:** a previously accepted `VALIDATED` bundle without an archive or label windows now yields `VALIDATED_EVIDENCE_UNVERIFIED`, `VALIDATED_QUANTITATIVE_UNVERIFIED` or `CV_LABEL_WINDOWS_MISSING` and status `INCOMPLETE`; supply the archive and windows, or declare a non-`VALIDATED` research state.

**`research-mission.v2`** (opt-in; `research-mission.v1` remains accepted and unchanged for non-`VALIDATED` bundles) fails closed: an archive is required (`EVIDENCE_UNRESOLVED_NO_ARCHIVE`), baseline `as_of_utc` is required, every metric needs lineage (`METRIC_LINEAGE_MISSING`), and committee review evidence must be ids listed in `evidence.json` (`REVIEW_EVIDENCE_UNRESOLVED`). Passing an archive to a v1 bundle also resolves and rejects unknown, tampered, contradicted or future-dated evidence.

Limits: the archive and bundle are both local and can be rewritten consistently by whoever controls them; the verifier is not run against wall-clock time (future relative to the mission baseline only); it does not establish that a provider told the truth, that a model is sound, or that any edge exists.


## P1 coordinator mapping

`MissionCoordinator` assembles a `research-mission.v2` bundle in `<output_dir>/stages/FINALIZING/<op-key>/` and runs the same `verify()` described above, with the mission's own evidence archive, before it records `final_verification.json`. The bundle's `completion_state` is `COMPLETE` only when every provider attempt `SUCCEEDED`, both reviews are `PASS`, no evaluation `FAIL`ed and at least one eligible evidence reference exists; otherwise it is `INCOMPLETE` with the reasons in `remaining_work`. `metrics.json` carries no derived metrics in P1 (`NOT_APPLICABLE`), `thesis_revision.json` records a non-revision reason, and the coordinator never produces `research_state: VALIDATED`.

| Code | Meaning |
|---|---|
| `INVALID_MISSION_SPEC` (+ `SPEC_*` diagnostics) | Specification failed validation; nothing was created |
| `MISSION_ALREADY_EXISTS` | Same mission id, different specification; use a new id (identical re-creation is idempotent) |
| `OPERATION_IDENTITY_CONFLICT` | A stage was already bound to a different input digest |
| `STAGE_PRECONDITION_FAILED` | A pinned input differs from its digest, is malformed, or an operation is not permitted |
| `REQUIRED_EVIDENCE_UNAVAILABLE` | A declared input/handoff is absent (stage `BLOCKED`) or the finished bundle is honestly incomplete |
| `STAGE_OUTPUT_MISSING` / `STAGE_OUTPUT_DIGEST_MISMATCH` | A committed or on-disk artifact is absent or does not match its recorded/expected digest |
| `RECOVERY_RECONCILIATION_REQUIRED` | A stage is claimed or interrupted; run `reconcile_mission` (also returned with contradictory artifacts) |
| `UNSAFE_REPLAY_BLOCKED` | Recovery refused to replay over contradictory or unverifiable artifacts; evidence is retained |
| `STAGE_FENCED` | This executor lost its claim; its result was discarded |
| `MISSION_CANCELLED` | The mission was cancelled; an in-flight executor's commit is refused |
| `VERIFICATION_FAILED` | Archived evidence or the final bundle failed integrity verification |
| `COMPLETION_REQUIREMENT_UNMET` | Verified, but the research state is not an accepted one or a declared evaluation failed |
| `STAGE_POSTCONDITION_FAILED` / `OPERATION_ERROR` | An operation returned a wrong artifact set / failed unexpectedly (type name only is recorded) |

Crash windows covered by tests: before admission, after admission, after artifact write before state commit, after state commit before downstream work, during report generation, during final verification, and after the final stage commit before job completion. `UNSAFE_REPLAY_BLOCKED` accompanies `RECOVERY_RECONCILIATION_REQUIRED` whenever recovery finds contradictory or unverifiable artifacts: the operation is deterministic and local, but replaying it over contradictory bytes would destroy evidence, so recovery stops and the job is `BLOCKED` for manual resolution. An operation with an external side effect would have to return the same code instead of being replayed; none exists in P1.
