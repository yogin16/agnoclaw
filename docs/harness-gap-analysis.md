# Harness Gap Analysis (Claude Code + OpenClaw)

Unified comparison of `agnoclaw` harness core against:
- Claude Code (v2.1.50 patterns)
- OpenClaw (docs/repo patterns, February 2026)

Last updated: 2026-05-26

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
- Pack system v1 preview (manifest inspection/loading, install/list/trust/remove CLI,
  trusted code registrations, hook and policy event emission)
- Agno context provider bridge (tools, instructions, lifecycle, dependencies)
- Optional AgentOS export (`as_agentos_agent`, `create_agentos_app`) with
  harness-owned admin/debug routes
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
| AgentOS runtime export | Hosted API/runtime adapter | Gateway/API runtime | **Partial** (AgentProtocol adapter + optional admin routes; approvals/scheduler/MCP reuse AgentOS) | Medium | Harness |
| Context provider bridge | N/A | Source-scoped integrations | **Implemented** (Agno providers expose bounded tools + instructions) | Low | Harness |
| Pack install/trust lifecycle | N/A | Plugin/package ecosystem | **Partial** (local/git install, inspect, trust, remove; no marketplace) | Medium | Harness |
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

Newly closed or reduced gaps (v0.8 preview):
1. **Agno context providers** — `AgentHarness` accepts providers, adds bounded
   provider tools/instructions, supports async setup/close, and preserves policy,
   permission, guardrail, and event boundaries.
2. **AgentOS export** — Harnesses can be wrapped as AgentOS-compatible agents and
   registered through `create_agentos_app()` without bypassing `AgentHarness`.
   `approvals=True` installs an AgentOS-backed permission approver, scheduler
   metadata emits a harness event, and runtime metadata records AgentOS
   scheduler/MCP/approval configuration.
3. **Harness admin/debug routes** — Optional `/agnoclaw` routes expose capabilities,
   runtime metadata, in-memory events, sandbox listing/download/snapshot/reset,
   skills, packs, policies, and permissions. Routes include AgentOS auth plus
   agnoclaw admin/debug scope checks when authorization is enabled. Reset routes
   through harness permission, policy, and event emission.
4. **Pack v1 preview** — Pack manifests can be inspected without executing code,
   installed from local paths or `git+` URLs, trusted explicitly, removed, and
   loaded into the harness. Pack-provided hooks and policies emit lifecycle
   events, and pack policies compose after the harness policy engine.
5. **SDK ergonomics** — `AgentHarness.create()`, `session().send()`, and remote
   client helpers provide a programmatic harness-shaped API over existing
   `run/arun` behavior.

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
4. Persistent scheduler backend abstraction now exists for embedded runtimes
   (`SchedulerBackend`, `InMemorySchedulerBackend`, `JsonSchedulerBackend`).

Still open in this area:
- Built-in interactive approval adapter and elevated command flow
- Cross-session/background task persistence and queueing semantics
- First-class scheduler CLI/API management beyond heartbeat daemon helpers

---

## Remaining Backlog (Non-MCP)

### High

1. Elevated execution path
- Add explicit elevated command contract, approval gate, and audit event schema.

2. Hook-pack system + broader lifecycle coverage
- Add workspace/project hook discovery and more checkpoints
  (session/message/compact/worktree equivalents). Pack-provided run hooks now emit
  events, but broader lifecycle coverage remains open.

3. Sandbox provider abstraction beyond tool backends
- Keep current guardrails and injected exec/files backends; add full runtime sandbox providers and mode semantics.

### Medium

1. Persistent scheduler state + cron CRUD surface
- Persisted job/run storage and daemon CRUD helpers are implemented through the
  scheduler backend contract; first-class CLI management remains open.

2. ~~AgentOS approval bridge~~ **DONE**
- ~~Map AgentOS approval records/resolution into `PermissionController` approval
  requests so hosted approvals can satisfy harness permission prompts.~~

3. ~~Skill runtime parity for `context: fork` and `command-dispatch`~~ **DONE**
- ~~Enforce parsed skill metadata in runtime execution.~~

4. ~~Hierarchical workspace context loading~~ **DONE**
- ~~Deterministic layer precedence (global -> project -> workspace/path).~~

5. Plan UX tooling
- Add harness-level tools/signals for structured user questions and explicit plan completion.

6. ~~Notebook tools~~ **DONE**
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

Agno:
- https://docs.agno.com/agent-os/overview
- https://docs.agno.com/agent-os/mcp/mcp
- https://docs.agno.com/agent-os/multi-framework/overview
- https://docs.agno.com/hitl/approval
- https://docs.agno.com/runtime/context
- https://docs.agno.com/runtime/scheduling

---

This file replaces:
- `docs/claude-code-gaps.md`
- `docs/openclaw-gaps.md`
