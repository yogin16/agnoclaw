# Harness Gap Analysis (Claude Code + OpenClaw)

Unified comparison of `agnoclaw` harness core against:
- Claude Code (v2.1.50 patterns)
- OpenClaw (docs/repo patterns, February 2026)

Last updated: 2026-02-24

---

## Scope

This document tracks **harness-core parity** only.

In scope:
- Runtime policy, permission modes, hooks, guardrails
- Tool behavior and lifecycle
- Scheduler, workspace loading, skill execution semantics

Out of scope (enterprise/platform repo):
- Gateway protocol server and channel adapters
- Multi-tenant control plane, org routing, hosted operations
- Browser/canvas/node orchestration services

Deferred by design:
- MCP-specific parity

---

## Current Baseline

`agnoclaw` now includes:
- Typed runtime contracts (`ExecutionContext`, hooks, events, policy contracts)
- Tool-boundary policy checkpoints (`before_tool_call`, `after_tool_call`)
- Runtime guardrails (path + network)
- Runtime permission modes (`bypass`, `default`, `accept_edits`, `plan`, `dont_ask`)
- Plan-mode runtime enforcement (read-only tool behavior)
- Background bash lifecycle tools (`bash_start`, `bash_output`, `bash_kill`) via opt-in `BashToolkit`
- Heartbeat + cron scheduler with launchd/systemd install support

---

## Unified Capability Matrix

| Capability | Claude Code Pattern | OpenClaw Pattern | agnoclaw Status | Gap Severity | Repo Owner |
|---|---|---|---|---|---|
| Permission modes | `default/acceptEdits/plan/dontAsk/bypassPermissions` | Approval + exec modes | **Implemented (core)** | Medium | Harness |
| Plan-mode runtime read-only | Permission-layer enforcement | Permission/safety enforcement | **Implemented** | Low | Harness |
| Interactive approval UX | Native in-product approval flow | Approval + elevated flow | **Partial** (approver interface exists; no built-in interactive adapter in harness) | Medium | Harness |
| Background shell lifecycle | `BashOutput`, `KillShell` | Exec task lifecycle | **Implemented (opt-in)** (`bash_start/output/kill`) | Medium | Harness |
| Elevated execution path | Bypass/elevation semantics | Elevated mode (`!`) | **Missing** | High | Harness |
| Tool boundary policy interception | Pre/post tool controls | Hooks around command/tool lifecycle | **Implemented** | Low | Harness |
| Runtime guardrails | Permission/sandbox controls | Sandboxing/security controls | **Implemented (path/network)** | Low | Harness |
| Sandbox provider abstraction | Multiple runtime permission/sandbox modes | Sandboxing modes (`workspace-write/read-only/full`) | **Partial** (policy + guardrails, no provider backends) | High | Harness |
| Hook breadth and packaging | 17 events + multiple hook types | Plugin hook packs + lifecycle events | **Partial** (run + tool path) | High | Harness |
| Scheduler | None | Heartbeat + Cron | **Implemented** | Low | Harness |
| Persistent cron management | N/A | Durable job management | **Partial** (in-memory jobs) | Medium | Harness |
| Workspace core files | CLAUDE.md hierarchy | AGENTS/SOUL/IDENTITY/USER/etc | **Implemented** (OpenClaw-style files) | Low | Harness |
| Hierarchical workspace layering | Hierarchical CLAUDE.md/rules loading | Layered behavior files | **Missing** | Medium | Harness |
| Skill metadata compatibility | AgentSkills + CC frontmatter | OpenClaw metadata and install specs | **Implemented** | Low | Harness |
| Skill `context: fork` execution | Skill fork contexts | Fork-style skill execution | **Missing** (parsed, not enforced) | Medium | Harness |
| Skill `command-dispatch` execution | Tool-level skill execution paths | Direct dispatch metadata | **Missing** (parsed, not enforced) | Medium | Harness |
| Structured plan UX tools | `AskUserQuestion`, `ExitPlanMode` tool | Guided workflow patterns | **Missing** | Medium | Harness |
| Notebook tools | Notebook read/edit tools | N/A | **Missing** | Medium | Harness |
| MCP parity | MCP tools/resources/search | MCP integrations | **Deferred** | Deferred | Platform/Harness later |

---

## What Changed Recently

Newly closed or reduced gaps:
1. Runtime permission modes are now implemented in the harness core.
2. Plan mode now enforces read-only behavior at runtime, not prompt-only.
3. Background shell lifecycle tools now exist (`bash_start`, `bash_output`, `bash_kill`).

Still open in this area:
- Built-in interactive approval adapter and elevated command flow
- Cross-session/background task persistence and queueing semantics

---

## Remaining Backlog (Non-MCP)

### High

1. Elevated execution path
- Add explicit elevated command contract, approval gate, and audit event schema.

2. Hook-pack system + broader lifecycle coverage
- Add workspace/project hook discovery and more checkpoints (session/message/compact/worktree equivalents).

3. Sandbox backend abstraction
- Keep current guardrails; add pluggable runtime sandbox providers.

### Medium

1. Persistent scheduler state + cron CRUD surface
- Persist jobs and run metadata; add first-class CLI management.

2. Skill runtime parity for `context: fork` and `command-dispatch`
- Enforce parsed skill metadata in runtime execution.

3. Hierarchical workspace context loading
- Deterministic layer precedence (global -> project -> workspace/path).

4. Plan UX tooling
- Add harness-level tools/signals for structured user questions and explicit plan completion.

5. Notebook tools
- Add read/edit support for notebook-centric workflows.

---

## Platform-Only Backlog (Do Not Pull Into Harness)

1. Gateway protocol server (channel transport + routing layer).
2. Channel adapters (Slack/Discord/Telegram/voice/web).
3. Multi-user session routing and org-level orchestration.
4. Hosted policy admin, compliance workflows, tenant operations.

---

## Sources

Claude Code:
- https://docs.anthropic.com/en/docs/claude-code
- https://docs.anthropic.com/en/docs/claude-code/settings
- https://docs.anthropic.com/en/docs/claude-code/hooks

OpenClaw:
- https://docs.openclaw.ai/architecture
- https://docs.openclaw.ai/automation/hooks
- https://docs.openclaw.ai/automation/cron-jobs
- https://docs.openclaw.ai/tools/exec-tool
- https://docs.openclaw.ai/tools/approvals
- https://docs.openclaw.ai/tools/elevated-mode
- https://docs.openclaw.ai/security/sandboxing
- https://docs.openclaw.ai/skills
- https://docs.openclaw.ai/skills/skills-config
- https://docs.openclaw.ai/agents/sub-agents
- https://raw.githubusercontent.com/openclaw/openclaw/main/README.md

---

This file replaces:
- `docs/claude-code-gaps.md`
- `docs/openclaw-gaps.md`
