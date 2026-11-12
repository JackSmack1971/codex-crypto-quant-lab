# User-facing changes — P0–P2 integration

**Integrated into `main`:** November 12, 2026, merge commit `ea98440ed79600c7aea2384c851a0b34eb84146e`. This page describes user-observable interfaces and compatibility changes, not a claim of production or trading readiness.

## At a glance

| Area | What users can now do | Important difference or limitation |
| --- | --- | --- |
| Provider acquisition (P0) | Use bounded retry, provider cooldown, circuit breakers and optional SQLite cache | Cache is opt-in; stale cached results are explicitly labeled; quotas/breakers are per process |
| Evidence verification (P0) | Check archive-resolved sources and recompute supported metric transforms | A local archive/hash is not independent source authentication; stricter validation may reject old claims |
| Mission execution (P1) | `scripts/mission.py` `create`, `status`, `advance`, `resume`, `cancel` | Input specification and files must be pinned; coordinator is local, not a Codex agent scheduler or provider fetcher |
| Experiment governance (P1) | Register train-only selection and govern holdout exposure with `ExperimentRegistry` | Independence means local-registry-relative, never proven independent samples |
| Intelligence reports (P2) | Generate evidence-linked Markdown/HTML and report companions through mission `RENDERING` | Authored interpretation remains unauthenticated; supported charts require archived samples |
| History (P2) | Inspect immutable thesis revisions and compare verified mission evidence | Companion-free or unverifiable legacy bundles are readable but not trusted for numerical comparison |
| Benchmarks (P2) | Execute `scripts/benchmark.py --repeat 2` and inspect the mandatory gate | Current 31 synthetic/offline cases grade engineering correctness, not alpha |

## New mission workflow

A mission is created from `research-mission-execution.v1` with pinned local inputs, advanced through seven deterministic stages, and finalized to a `research-mission.v2` bundle. Stage success, process completion and scientific research state are separate. `COMPLETED` does **not** mean `VALIDATED`; the coordinator does not assign `VALIDATED`.

```text
python scripts/mission.py --workspace local/work create spec.json
python scripts/mission.py --workspace local/work status MISSION_ID
python scripts/mission.py --workspace local/work advance MISSION_ID --all
python scripts/mission.py --workspace local/work resume MISSION_ID
```

`resume` is for reconciliation of interrupted/claimed work, not a generic forced retry. A terminal `BLOCKED` or failed mission may require a new mission identity and an explicit `supersedes` relationship. Codex agent handoffs and provider attempt records are not authenticated by their presence in local files; no new network or agent runtime is hidden inside these commands.

## Verification has stricter exit semantics

The updated `scripts/verify_mission.py` distinguishes **full verification** from **partial inspection**. Use `--archive` with the correct evidence store when qualifying evidence-bearing bundles.

| Exit | Status | User impact |
| --- | --- | --- |
| `0` | `VERIFIED` | Archive-dependent evidence and report-companion checks passed |
| `1` | `FAILED` | Some required material is invalid, incomplete, blocked, or failed |
| `2` | `PARTIALLY_VERIFIED` | Checks needing archived evidence or semantically verified companions were not performed; **not a pass** |

`--allow-partial` may force exit 0 for a *partial* inspection but does not change the JSON verdict. Never use it when full verification is required. The output keeps the legacy completion `status` alongside the new `verification_status`, `evidence_verification`, `exit_code`, and `not_performed` fields. This may change existing scripts that previously treated any zero exit without an archive as fully verified.

The `research-mission.v2` integrity contract requires archive evidence and supported metric lineage. Existing assertions of `VALIDATED` can fail strict historical/chronological checks; this is intentional, not a new trading strategy judgment.

## Reports and charts

The mission `RENDERING` stage now creates `report.md`, `report.html`, `report_content.json`, and `report_manifest.json`. Companion verification semantically checks archived numeric claims rather than relying only on self-consistent hashes. Reports distinguish **[R]** retrieved, **[D]** calculated, **[I]** inferred, **[U]** unavailable.

The supported chart families are `metric_history`, `normalized_comparison`, and `drawdown`. The standalone `scripts/research_explorer.py` remains available with its `research-explorer-input.v1` shape; `scripts/render_report.py` remains the simpler observation-envelope renderer. Do not interchange their JSON inputs without conversion.

History comparison only produces authoritative numerical deltas from qualifying, archive-backed bundles. Legacy or unknown companion provenance is explicitly refused, rather than quietly treated as verified. Authored scenario/falsifier text is still not independently authenticated.

## Experiment registry and research utilities

`research_lab/experiment_registry.py` adds durable preregistration and a one-use holdout exposure policy for fixed candidate studies. Its `independent_evaluation` boolean is retained for compatibility but accompanied by `independence_scope` and `independence_limitations`. Even a true value refers only to matches visible to the local registry under declared dataset identity and digest; relabelled or shifted datasets can defeat this check.

The older `scripts/research_ops.py freeze`/`test` commands remain available as utilities; they do **not** replace registry-mediated exposure governance. A caller-supplied digest by itself does not prove preregistration.

## CI and benchmarks

The merged repository has an offline GitHub Actions workflow for Python 3.11 and 3.13 standard-library checks, plus a Python 3.13 full suite with direct dependency constraints. The final merged run was reported green: [Actions run 43263065915](https://github.com/JackSmack1971/codex-crypto-quant-lab/actions/runs/43263065915).

`python scripts/benchmark.py --repeat 2` runs the 31-case B01–B31 suite using synthetic fixtures. `mandatory_gate` is the coverage/correctness gate for the **full** suite; selected subsets can leave dimensions untested. `canonical_digest` may include environment details; `artifact_digest` provides a narrower reproducible artifact comparison. Neither reports predictive financial performance.

A correction to the CI workflow YAML was made in commit `62d9933`, following an invalid step name introduced in commit `31bd326`. The earlier zero-job failure was a workflow configuration problem, not evidence of failing tests. The corrected workflow and post-merge CI subsequently ran successfully.

## Compatibility and migration cautions

- **Legacy render@1:** Existing correctly recorded v1 render outputs can finalize under their declared v1 contract; they are not silently rewritten as v2 reports. Already-terminal failures may need a new mission id (`supersedes`). Tests of P1-era artifacts used simulated fixtures rather than archived real v1 outputs.
- **Companion-free bundles:** Readable for historical inspection, but not considered semantically verified. Stripping companions plus their attestation can resemble genuine legacy files, so history refuses authoritative claims from unverified bundles.
- **Archived evidence and timestamps:** Equivalent UTC spelling is reconciled by instant, without changing the original stored timestamp text or content digests. Implausible chronology or conflicting same-instant observations may now be excluded or rejected.
- **Versioned operations:** `render.local_report@2` and `finalize.bundle@2` use companion-aware contracts; manually constructed outputs must follow the declared version.
- **Manifest changes:** All documentation/package file changes require updating `PACKAGE-MANIFEST.json` with SHA-256 values and the exact managed file set before `scripts/verify_control_plane.py` will pass. Use the repo's `_managed_files(ROOT)` implementation; do not weaken or bypass validation.

## Scope that remains unverified

No live-provider reliability, Windows Codex MCP handshake, executable market liquidity, automated source-availability certification, independently authenticated reviewer identity/authored conclusions, production security assurance, trading capability, or predictive edge is established by the offline tests. Holdout independence is local-registry-relative; benchmark mutation coverage is incomplete and legacy fixtures are simulated. The CI fixture's use of `--allow-partial` is deliberately **not** a substitute for a full archive-backed verification.

See [README](../README.md), [implementation status](IMPLEMENTATION-STATUS.md), [mission completion contract](../control-plane/mission-completion.md), and [report contract](../control-plane/report-contract.md) for authoritative interfaces and limitations.
