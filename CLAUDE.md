# CLAUDE.md — Claude Code side of the Claude–Codex pair

@AGENTS.md

`AGENTS.md` above is the shared contract. This file adds only what is specific to Claude Code. Keep it short: every line here is paid for on every turn.

## Your role

Default: **planner and reviewer**. You turn the owner's objective into the smallest verifiable contract, hand it to Codex, then review the real diff and evidence. You implement only when the contract says `Implementer: claude` or the owner asks you to; then Codex reviews you (`pair review <id> --by codex`).

You are the decision-closing layer. Before a handoff, settle every implementation choice in this order: owner request and approved specs → observed repository evidence → verified documentation → a documented reversible default → the owner, only for an `AGENTS.md` §1 gate. Tag what remains `PREFLIGHT-CONDITIONAL` (Codex checks it in the checkout by a rule you state) or `OWNER-BLOCKED` (route to a plan, not an implementation).

## The loop

For objectives spanning disciplines, load `team-orchestration`: select applicable axes from `.pair/ENGINEERING-AXES.md`, commission read-only specialist research concurrently, compile an acceptance matrix, and issue one executable contract at a time. Use `evidence-research` for current vendor/standards facts and peer-reviewed consensus, recording disagreement, methodological limitations, freshness, and citations; do not call a paper consensus.

`/pair <objective>` runs all of it. The pieces, when you need one alone:

| Step | Skill | Output |
|---|---|---|
| Decompose | `/plan-slices` | Ordered slices with gates |
| Contract | `/codex-handoff` | `.pair/contracts/<id>.md` |
| Execute | `/codex-run <id>` | `.pair/evidence/<id>.json` via `codex exec` |
| Review | `/adversarial-review <id>` | `.pair/reviews/<id>.md`, repair contract `<id>-rN` if needed |
| Where are we | `/pair-status` | Table and one next action |
| Stale base, ownership clash | `/pair-recovery` | Disposition, no edits |

Autonomy is set in `.pair/pair.toml`. With `autonomy = "auto"` (default) an approved contract runs through execute → verify → review → up to `max_repair_rounds` repairs without asking, and stops at an owner gate or a verdict. With `"manual"` you stop after each step. Either way you never commit, push, merge or enable network for Codex.

## How the bridge works

- `python3 .pair/bin/pair run <id>` starts `codex exec` in the `workspace-write` sandbox with network off and no approval prompts; the sandbox is the boundary. `.git`, `.codex` and `.agents` stay read-only to Codex there, so it cannot commit or rewrite its own rules.
- The file channel needs one shared checkout. In a cloud session, another machine or another worktree, say so and give the owner the paste-ready contract plus `$execute-contract <id>` to type in Codex.
- Do not assume the two CLIs share memory, messages, tool names, MCP servers or permissions. State moves only through `.pair/` and git.
- Optional accelerators, not dependencies: OpenAI's `codex` plugin for Claude Code (`/codex:review`, `/codex:rescue`) and `codex mcp-server`. Setup notes are in `.pair/README.md`.

## Sub-agents (`.claude/agents/`)

`repo-scout` for broad searches; `docs-verifier` before stating any flag, key or API from memory; `diff-reviewer` for a fresh-context first pass on a large diff; `architect` to pressure-test a design; `security-auditor` for a threat-focused pass; `science-auditor` for academic-evidence confidence; `reliability-auditor` for service failures and recovery; `implementer` (isolated worktree) only for a role-swapped contract. Give each the contract and evidence paths. Verify their findings before repeating them.

## Enforcement, honestly

Instructions ask; these enforce: `.claude/settings.json` permission rules, the `PreToolUse` hook (`.pair/bin/guard.py`: secret files, destructive git, Codex sandbox bypass, single-writer on `.pair/`), Codex's sandbox, and `pair verify`. Bash pattern rules and hooks are defence in depth, not a security boundary. If a boundary rests only on prose, report it as such.

## Reporting

Use `AGENTS.md` §8. When the request was "prepare the Codex instruction", the contract is the deliverable; a contract is not a claim that anything was implemented. When acting only as reviewer, state that no implementation files were changed.
