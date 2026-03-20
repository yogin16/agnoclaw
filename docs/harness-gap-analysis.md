# Harness Gap Analysis (Claude Code + OpenClaw)

Unified comparison of `agnoclaw` harness core against:
- Claude Code (v2.1.50 patterns)
- OpenClaw (docs/repo patterns, February 2026)

Last updated: 2026-03-20

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
- Browser toolkit (Playwright-based, optional extra)
- MCP toolkit (stdio + SSE transports, optional extra)
- Media toolkit (image + PDF reading, optional extra)
- Notebook toolkit (Jupyter .ipynb read/edit/add)
- Plugin system (entry-point-based discovery + explicit module paths)
- Hierarchical workspace (global → project → workspace layering)
- Skill `context: fork` enforcement (routes to isolated subagent)
- Skill `command-dispatch: tool` enforcement (bypasses LLM, invokes tool directly)
- ClawHub integration (search, inspect, install community skills)
- Auto-skill selection (skill catalog injected into system prompt)
- Single runtime backend abstraction for built-in tools, skills, and browser

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
| Built-in runtime backend abstraction | Tool family can target one alternate runtime plane | Exec/files/browser/skills bind to sandbox/container workspace | **Implemented** (`RuntimeBackend`) | ~~High~~ Done | Harness |
| Sandbox provider abstraction | Multiple runtime permission/sandbox modes | Sandboxing modes (`workspace-write/read-only/full`) | **Partial** (single backend contract exists; first-party `LLMSandboxBackend` ships, other provider adapters still open) | High | Harness |
| Hook breadth and packaging | 17 events + multiple hook types | Plugin hook packs + lifecycle events | **Partial** (run + tool path) | High | Harness |
| Scheduler | None | Heartbeat + Cron | **Implemented** | Low | Harness |
| Persistent cron management | N/A | Durable job management | **Partial** (in-memory jobs) | Medium | Harness |
| Workspace core files | CLAUDE.md hierarchy | AGENTS/SOUL/IDENTITY/USER/etc | **Implemented** (OpenClaw-style files) | Low | Harness |
| Hierarchical workspace layering | Hierarchical CLAUDE.md/rules loading | Layered behavior files | **Implemented** (global → project → workspace) | ~~Medium~~ Done | Harness |
| Skill metadata compatibility | AgentSkills + CC frontmatter | OpenClaw metadata and install specs | **Implemented** | Low | Harness |
| Skill `context: fork` execution | Skill fork contexts | Fork-style skill execution | **Implemented** (routes to subagent) | ~~Medium~~ Done | Harness |
| Skill `command-dispatch` execution | Tool-level skill execution paths | Direct dispatch metadata | **Implemented** (direct tool invocation) | ~~Medium~~ Done | Harness |
| Structured plan UX tools | `AskUserQuestion`, `ExitPlanMode` tool | Guided workflow patterns | **Missing** | Medium | Harness |
| Notebook tools | Notebook read/edit tools | N/A | **Implemented** (NotebookToolkit) | ~~Medium~~ Done | Harness |
| MCP parity | MCP tools/resources/search | MCP integrations | **Implemented** (MCPToolkit: stdio + SSE) | ~~Deferred~~ Done | Harness |
| Browser tools | Playwright browser automation | Browser use tools | **Implemented** (BrowserToolkit, optional extra) | ~~N/A~~ Done | Harness |
| Media tools | Image/PDF reading | Document processing | **Implemented** (MediaToolkit, optional extra) | ~~N/A~~ Done | Harness |
| Plugin system | N/A | Plugin hook packs | **Implemented** (entry-point discovery) | ~~N/A~~ Done | Harness |
| ClawHub integration | N/A | SkillHub community registry | **Implemented** (search/inspect/install) | ~~N/A~~ Done | Harness |
| Auto-skill selection | Model self-selects skills | Agent-driven skill selection | **Implemented** (skill catalog in system prompt) | ~~N/A~~ Done | Harness |

---

## What Changed Recently

Newly closed or reduced gaps (v0.3):
1. **Browser toolkit** — Playwright-based with navigate, click, type, screenshot, snapshot, scroll, fill_form, close.
2. **MCP toolkit** — Connect to MCP servers via stdio or SSE transport; auto-discovers and registers tools.
3. **Media toolkit** — Read images (base64) and PDFs (text extraction with page range support).
4. **Notebook toolkit** — Read, edit cells, and add cells in Jupyter .ipynb files.
5. **Plugin system** — Entry-point discovery (`agnoclaw.plugins` group) + explicit module loading.
6. **Hierarchical workspace** — Global → project → workspace layering with child-overrides-parent.
7. **Skill `context: fork` enforcement** — Routes fork-context skills to isolated subagent execution.
8. **Skill `command-dispatch: tool` enforcement** — Bypasses LLM, invokes the specified tool directly.
9. **ClawHub integration** — HTTP client for community skill registry (search, inspect, download, install).
10. **Auto-skill selection** — Skill catalog injected into system prompt so model can self-select relevant skills.
11. **Single runtime backend propagation** — Built-in bash/files, trusted skill execution/install flow, browser tools, subagents, and team presets now share one injected runtime backend.

Previously closed:
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

3. Sandbox provider abstraction beyond tool backends
- Keep current guardrails and injected exec/files backends; add full runtime sandbox providers and mode semantics.

### Medium

1. Persistent scheduler state + cron CRUD surface
- Persist jobs and run metadata; add first-class CLI management.

2. ~~Skill runtime parity for `context: fork` and `command-dispatch`~~ **DONE**
- ~~Enforce parsed skill metadata in runtime execution.~~

3. ~~Hierarchical workspace context loading~~ **DONE**
- ~~Deterministic layer precedence (global -> project -> workspace/path).~~

4. Plan UX tooling
- Add harness-level tools/signals for structured user questions and explicit plan completion.

5. ~~Notebook tools~~ **DONE**
- ~~Add read/edit support for notebook-centric workflows.~~

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
