# Claude Code Gap Analysis

Comparison of agnoclaw vs Claude Code v2.1.50 (February 2026).

Research basis: official docs at code.claude.com, Piebald-AI/claude-code-system-prompts
reverse-engineering repo (updated Feb 20 2026), and live CC v2.1.50 inspection.

---

## Tool Inventory

### File System Tools

| CC Tool | agnoclaw Equivalent | Status | Notes |
|---|---|---|---|
| `Read` | `read_file()` | Implemented | CC adds PDF/image/notebook support |
| `Write` | `write_file()` | Implemented | Equivalent |
| `Edit` | `edit_file()` | Implemented | Same exact-string-replacement pattern |
| `MultiEdit` | `multi_edit_file()` | **Added** | Atomic multi-edit; fail-fast validation |
| `Glob` | `glob_files()` | Implemented | Both sort by modification time |
| `Grep` | `grep_files()` | Implemented | CC uses ripgrep; agnoclaw uses Python `re`. CC has `output_mode` (content/files_with_matches/count), `-A`/`-B`/`-C` context, `multiline` mode |
| `LS` | `list_dir()` | Implemented | Equivalent |
| `NotebookRead` | — | **Missing** | Read Jupyter cells — data science use case |
| `NotebookEdit` | — | **Missing** | Edit/insert/delete Jupyter cells (`cell_id`, `cell_type`, `edit_mode`) |

### Shell Execution

| CC Tool | agnoclaw Equivalent | Status | Notes |
|---|---|---|---|
| `Bash` | `bash()` | Implemented | CC adds `run_in_background`, `CLAUDE_ENV_FILE` for session hooks |
| `BashOutput` | — | **Missing** | Retrieve output from background bash tasks by task ID |
| `KillShell` | — | **Missing** | Kill a backgrounded process |

The background task system (Bash → background → BashOutput → KillShell) is a significant
CC subsystem enabling long-running builds/tests while the agent continues other work.

### Web Tools

| CC Tool | agnoclaw Equivalent | Status | Notes |
|---|---|---|---|
| `WebFetch` | `web_fetch()` | Implemented | Equivalent |
| `WebSearch` | `web_search()` | Implemented | CC supports `allowed_domains`/`blocked_domains` |
| `MCPSearch` | — | **Missing** | Lazy MCP tool loading at context threshold; not needed without MCP |

### Agent / Task Orchestration

| CC Tool | agnoclaw Equivalent | Status | Notes |
|---|---|---|---|
| `Task` | `spawn_subagent()` | Partial | CC supports named types (Explore/Plan/Bash/general-purpose), `isolation: worktree`, `model` override |
| `TodoWrite` | `create_todo()` | Implemented | CC uses atomic write-all; agnoclaw uses CRUD. Semantically equivalent |
| `TodoRead` | `list_todos()` | Implemented | Equivalent |
| `TaskUpdate` | — | **Missing** | Shared task list update for agent teams |
| `TaskOutput` | — | **Missing** | Retrieve subagent output by agent ID |

### UI / Interactive / Plan Mode

| CC Tool | agnoclaw Equivalent | Status | Notes |
|---|---|---|---|
| `AskUserQuestion` | — | **Missing** | Structured multi-choice prompts — critical for plan mode UX |
| `ExitPlanMode` | text instruction | Partial | CC exits plan mode via tool call; agnoclaw uses text instruction in PLAN_MODE section |
| `Skill` (tool) | prompt injection | Partial | CC has a `Skill` tool the model calls; agnoclaw injects skill content via system prompt |

---

## System Prompt Architecture

### Claude Code structure (~110+ conditional sections)

CC assembles sections conditionally based on environment, permission mode, and MCP state:

**Always present:**
- Identity (cwd, OS, shell, git state, model, permission mode)
- Tone and style
- Doing tasks (agentic workflow rules)
- Tool usage policy
- Security policies
- Git protocol
- Memory instructions (CLAUDE.md hierarchy)
- Skill instructions
- Runtime reminders (datetime, workspace, permission mode injected per-turn)

**Conditionally added:**
- Plan mode instructions
- Learning mode section (gradual rollout)
- Agent team instructions (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`)
- MCP server tool descriptions (one block per server)
- IDE tool descriptions (VS Code / JetBrains injection)
- CLAUDE.md content (hierarchical)
- rules/ files (with optional path-scoped frontmatter)
- ~40 system reminders (context nearing limit, file modified externally, etc.)

### agnoclaw structure

`SystemPromptBuilder` assembles in fixed order:
```
IDENTITY → TONE_AND_STYLE → DOING_TASKS → TOOL_GUIDELINES → SECURITY →
GIT_PROTOCOL → MEMORY_INSTRUCTIONS → SKILL_INSTRUCTIONS →
[PLAN_MODE] → [LEARNING_INSTRUCTIONS] →
workspace context (AGENTS/SOUL/IDENTITY/USER/MEMORY/TOOLS/BOOT) →
active skill → extra context → runtime (date, workspace, session_id)
```

**Gaps vs Claude Code:**
- No conditional per-turn system reminders (~40 in CC)
- No permission mode injection per turn
- No hierarchical CLAUDE.md loading (flat workspace files instead)
- No `@import` syntax in memory files
- No path-scoped conditional rules (`rules/*.md`)
- No MCP server description injection

---

## Memory System

### Claude Code

```
/Library/Application Support/ClaudeCode/CLAUDE.md   ← org-wide managed policy
~/.claude/CLAUDE.md                                   ← user-level
~/.claude/rules/*.md                                  ← user-level modular rules
./CLAUDE.md or ./.claude/CLAUDE.md                    ← project-level (committed)
./CLAUDE.local.md                                     ← project-local (gitignored)
./.claude/rules/*.md                                  ← project modular rules
<child_dir>/CLAUDE.md                                 ← loaded on demand
~/.claude/projects/<proj>/memory/MEMORY.md            ← auto memory (first 200 lines)
```

- `@path/to/file` import syntax (recursive, 5-hop limit)
- Path-scoped rules: `paths: ["src/api/**/*.ts"]` frontmatter
- `/memory` command for interactive editing
- `/init` command for project bootstrapping

### agnoclaw (3-tier)

**Tier 1 — Workspace files (Markdown):**
```
~/.agnoclaw/workspace/
├── AGENTS.md     ← behavioral guidelines
├── SOUL.md       ← persona and tone
├── IDENTITY.md   ← capabilities, specializations
├── USER.md       ← user preferences
├── MEMORY.md     ← curated long-term facts (first 200 lines)
├── TOOLS.md      ← tool configuration
├── BOOT.md       ← startup task execution
├── HEARTBEAT.md  ← heartbeat checklist
└── memory/       ← daily session logs (YYYY-MM-DD.md)
```

**Tier 2 — Agno MemoryManager:** structured per-user fact extraction to SQLite/Postgres.
Enable with `AgentHarness(user_id="alice", enable_user_memory=True)`.

**Tier 3 — Agno LearningMachine:** institutional cross-user knowledge.
Three stores: `entity_memory`, `learned_knowledge`, `decision_log`.
Enable with `AgentHarness(enable_learning=True)`.

**agnoclaw advantages:**
- `BOOT.md` startup execution (CC uses SessionStart hooks)
- `LearningMachine` institutional memory (CC has no cross-user learning)
- `ProgressToolkit` for multi-context-window project tracking
- `self-improving-agent` skill for `.learnings/` capture + promotion

---

## Plan Mode

### Claude Code

- Activated via `--permission-mode plan`, `/plan` command, or Shift+Tab
- Read-only tool enforcement (Write/Edit/Bash blocked at permission layer)
- `AskUserQuestion` tool gathers requirements interactively
- `ExitPlanMode` tool signals plan completion
- `Ctrl+G` opens plan in text editor
- Uses the `Plan` subagent type (read-only, prevents infinite nesting)

### agnoclaw

- Activated via `agent.enter_plan_mode()` or `include_plan_mode=True` in prompt builder
- Read-only enforced by text prompt instructions only (no permission layer)
- No `AskUserQuestion` tool
- No `ExitPlanMode` tool signal
- Plan written to `.plan.md` file

---

## Permission System

### Claude Code

| Mode | Behavior |
|---|---|
| `default` | Prompts on first use per tool category |
| `acceptEdits` | Auto-accepts file write/edit for session |
| `plan` | Read-only; blocks Write/Edit/Bash |
| `dontAsk` | Auto-denies unless pre-approved |
| `bypassPermissions` | Skips all prompts (container/VM only) |

Rule syntax: `Bash(npm run *)`, `Read(./.env)`, `WebFetch(domain:example.com)`, `Task(Explore)`

### agnoclaw

No runtime permission system. The PLAN_MODE prompt section is the closest concept.

---

## Hooks System

### Claude Code (17 hook events, 3 hook types)

**Events:** SessionStart, UserPromptSubmit, PreToolUse, PermissionRequest, PostToolUse,
PostToolUseFailure, Notification, SubagentStart, SubagentStop, Stop, TeammateIdle,
TaskCompleted, ConfigChange, WorktreeCreate, WorktreeRemove, PreCompact, SessionEnd

**Hook types:**
- `command` — shell script; receives JSON stdin; controls via exit codes + stdout JSON
- `prompt` — LLM single-turn evaluation (yes/no decision)
- `agent` — multi-turn subagent with tool access for verification

**Key capabilities:**
- `PreToolUse` can block, allow, modify tool inputs, or escalate
- `WorktreeCreate/Remove` — non-git VCS support for isolation
- `TeammateIdle/TaskCompleted` — quality gates for agent teams
- `PreCompact` — cleanup before context compaction
- `async: true` for non-blocking background hooks

### agnoclaw

No hooks system. Agno provides pre/post tool call hooks at the framework level, but
the CC-style 17-event hook system is not implemented.

---

## Scheduler (Heartbeat + Cron)

### Claude Code

No scheduler. Claude Code is a per-session CLI — purely reactive.

### OpenClaw (for reference)

Gateway daemon (launchd/systemd) with two schedulers:
- **Heartbeat**: interval-based, main session, HEARTBEAT_OK suppression
- **CronManager**: expression-based (`croner` lib), isolated or main session

### agnoclaw

Matches OpenClaw's model:

```python
from agnoclaw.heartbeat.daemon import HeartbeatDaemon, CronJob

daemon = HeartbeatDaemon(agent, on_alert=print)

# Interval-based cron job (main session)
daemon.add_cron_job(CronJob(name="check", schedule="1h", prompt="..."))

# Cron expression, isolated session (requires croniter: uv add croniter)
daemon.add_cron_job(CronJob(
    name="standup", schedule="0 9 * * 1-5",
    prompt="...", isolated=True,
))

# Service persistence (launchd/systemd)
# agnoclaw heartbeat install-service
```

Schedule formats: `"30m"`, `"1h"`, `"2h30m"`, `"45s"`, `"0 9 * * 1-5"` (needs croniter)

---

## MCP Integration

### Claude Code

- Tools appear as `mcp__<server>__<tool>`
- 3 scopes: local, project (`.mcp.json`), user
- OAuth 2.0 for remote servers
- Dynamic tool updates via `list_changed`
- `MCPSearch` for lazy loading
- MCP resources via `@server:protocol://path` mentions
- CC can act as MCP server: `claude mcp serve`

### agnoclaw

No MCP integration. The Agno framework has its own tool ecosystem.

---

## Skills System

### Claude Code

Full frontmatter: `name`, `description`, `disable-model-invocation`, `user-invocable`,
`allowed-tools`, `model`, `context` (fork), `agent`, `hooks`, `argument-hint`

Special syntax: `$ARGUMENTS`, `$ARGUMENTS[N]`, `$N`, `${CLAUDE_SESSION_ID}`, `` !`cmd` ``

- `context: fork` runs skill as isolated subagent
- Plugin distribution system
- Character budget: 2% of context window

### agnoclaw

Full AgentSkills + OpenClaw compatibility:
- `$ARGUMENTS`, `$ARGUMENTS[N]` substitution
- `` !`cmd` `` dynamic context injection
- OpenClaw frontmatter: `metadata.openclaw`, `metadata.clawdbot`, `metadata.clawdis`
- `anyBins` gating, `os` platform list, `emoji`, `command_dispatch`
- Priority: workspace > user (`~/.agnoclaw/skills/`) > bundled

**Gaps vs CC:**
- No `context: fork` execution (parsed in SkillMeta but not run)
- No `hooks` in skill frontmatter
- No character budget tracking
- No plugin distribution

---

## What agnoclaw Has That Claude Code Lacks

| Feature | Description |
|---|---|
| **BOOT.md startup** | Explicit startup task execution; CC uses SessionStart hooks |
| **LearningMachine** | Institutional cross-user memory; CC has auto memory but not this |
| **ProgressToolkit** | Multi-context-window feature tracking with `progress.md` + `features.md` |
| **self-improving-agent** | Structured `.learnings/` capture + workspace file promotion |
| **CronJob scheduler** | Cron expression + interval scheduling; CC has no scheduler |
| **install-service** | launchd/systemd registration for always-on operation |
| **3-tier memory** | Workspace files + MemoryManager + LearningMachine |
| **SOUL/IDENTITY/TOOLS.md** | Agnoclaw-specific workspace persona files |
| **Model-agnostic** | Any Agno-supported model; CC is Anthropic-only |
| **Python-native** | Embeddable library; CC is a standalone TypeScript CLI |
| **Multi-agent teams** | Native via Agno; CC's agent teams are experimental |
| **HITL patterns** | `requires_confirmation=True` tools; structured approval flow |

---

## Priority Implementation Roadmap

### High (most impactful for parity)

1. `AskUserQuestion` tool — structured multi-choice prompts, critical for plan mode
2. `ExitPlanMode` tool signal — proper plan mode exit (not just text instruction)
3. `NotebookRead` + `NotebookEdit` — Jupyter support for data science
4. Per-turn system reminders — context limit warnings, permission mode injection

### Medium

5. `BashOutput` + `KillShell` — background task management
6. Hierarchical workspace loading — load from current dir up to root
7. Permission modes (`plan`, `acceptEdits`, `dontAsk`) — runtime enforcement
8. `Skill` tool (model-invoked) — tool interface for skill invocation

### Low / Future

9. MCP integration — requires Agno MCP support
10. Hooks system (17 events) — major architecture addition
11. Agent teams — experimental in CC too
12. Plugin distribution system

---

*Last updated: 2026-02-23 | CC version: v2.1.50*
