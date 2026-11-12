# Twelve-axis effective control-plane design (2026-11-11)

| Axis | Concrete artifact and decision | Enforcement/evidence boundary |
|---|---|---|
| 1 Identity/trust | `identity.json`; canonical repo, trust checkpoint, no assumed Git baseline | Runtime trust and actual path/checkout must be resolved locally |
| 2 Configuration | `.codex/config.toml` minimal additive project defaults; `docs/global-config-original.toml` preserved | Managed requirements and effective `codex` settings override desired prose |
| 3 Instructions | short `AGENTS.md`; scoped Skills | Instructions do not grant authority |
| 4 Model policy | `model-policy.md`; no silent model fallback; capture actual model | Catalog listing != account entitlement or successful execution |
| 5 Sandbox | `.codex/config.toml` :workspace intention; no YOLO | Test actual profile and OS boundaries, not prompt claims |
| 6 Approvals | `authority.md`; user authorizes remote/publication separately | Native approval + provider scopes; `never` != unrestricted |
| 7 Tool capabilities | `capabilities.json`; source identity, fallback, health, auth surface | Registration != enablement != exposure != invocation permission |
| 8 Environment | `runtime.md`, packaging and install guide | Lock dependency/env/version before deployment; Windows target not tested |
| 9 Network | `network.md`; fixed HTTPS destinations, no open URL tool | Domain allowlist only enforced by verified proxy/OS policy; MCP/shell separate |
| 10 Orchestration | role TOMLs, `orchestration.md` | Native subagents share permission context; not isolated worktrees |
| 11 Source control | `source-control.md`; dirty-state preservation, local-only | Branch/commit/push are distinct permissions and effects |
| 12 Governance | `report-contract.md`, `telemetry.md`, validator, local audit manifest | Evidence ledger is custom, not native immutable Codex audit |

## Delivery limits
The package implements a local GET-only reference adapter and report scaffolding, and now bundles the recovered historical fetch, indicators, and SMA-cross backtester, but NOT hosted dashboards, production-grade data warehousing, exchange-complete history, a real execution/fill simulation engine, an enforcing proxy, or a CI-tested Windows Codex runtime. Those remain separately verified integration steps.

## Cross-axis research extension (v2.3)
`control-plane/research-lab.md` extends axes 3, 7, 8, 10 and 12 through offline observation/signal/hypothesis contracts, point-in-time SQLite archives, versioned deterministic models, hypothesis evaluation, research-only strategy replay, temporal folds and validation, and a transactional local research ledger. Axis 6 still governs external effects; axis 5 continues to deny trade execution. No thirteenth axis or second orchestrator was added.
