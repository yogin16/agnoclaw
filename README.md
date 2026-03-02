# agnoclaw

**A hackable, model-agnostic agent harness built on Agno.**

Distills the best ideas from Claude Code's system prompt architecture, OpenClaw's UX patterns (heartbeat, workspace, SKILL.md), and LangChain DeepAgents' middleware insights — running them on Agno's production-ready, model-agnostic engine. Built to be owned, forked, and extended.

---

## Why agnoclaw?

| | Claude Code harness | OpenClaw | agnoclaw |
|---|---|---|---|
| **Model support** | Anthropic only | Any | Any (30+ via Agno) |
| **Embeddable as library** | No (CLI app) | No (standalone app) | Yes (`pip install agnoclaw`) |
| **Hackable agent loop** | No | No | Yes (Agno pre/post hooks, guardrails) |
| **Multi-agent** | Task tool only | No | Native (coordinate, route, broadcast) |
| **SKILL.md system** | Yes | Yes | Yes (compatible with both) |
| **Heartbeat + Cron** | No | Yes | Yes (interval strings + cron expressions) |
| **Workspace** | CLAUDE.md only | Full workspace | Full workspace |
| **Self-improving** | No | Community skill | Bundled `self-improving-agent` skill |
| **Atomic multi-edit** | Yes (MultiEdit) | No | Yes (`multi_edit_file`) |
| **Service install** | No | Yes (launchd/systemd) | Yes (`install-service`) |
| **Python-native** | No (TypeScript) | No | Yes |
| **Textual TUI** | No | No | Yes (streaming, notifications, skill picker) |
| **Async REPL** | No | No | Yes (heartbeat alerts while typing) |
| **Production patterns** | N/A | N/A | HITL, streaming, tracing, eval |

**TL;DR:** Claude Code is a consumer product. OpenClaw is a standalone app. agnoclaw is a library — embed it in anything, hack it for everything.

`HarnessAgent` is available as a backward-compatible alias for `AgentHarness`.

---

## Installation

```bash
# Core library only (for embedding — zero CLI/TUI deps)
pip install agnoclaw

# With CLI (async REPL, heartbeat notifications)
pip install "agnoclaw[cli]"

# With full Textual TUI
pip install "agnoclaw[tui]"

# Personal-assistant setup (TUI + cron)
pip install "agnoclaw[full]"

# With local Ollama support (no API key needed)
pip install "agnoclaw[local]"

# With Postgres support
pip install "agnoclaw[postgres]"

# With all model providers
pip install "agnoclaw[all-models]"

# With uv (recommended)
uv add agnoclaw
uv add "agnoclaw[tui]"   # for TUI
```

---

## Quick Start

```python
from agnoclaw import AgentHarness

agent = AgentHarness()
agent.print_response("Summarize the files in this directory", stream=True)
```

### With a different model — `"provider:model_id"` format

```python
agent = AgentHarness("anthropic:claude-sonnet-4-6")
agent = AgentHarness("openai:gpt-4o")
agent = AgentHarness("google:gemini-2.0-flash")
agent = AgentHarness("groq:llama-3.3-70b-versatile")
agent = AgentHarness("ollama:qwen3:8b")   # local, no API key
```

The model string `"provider:model_id"` is parsed natively by Agno — no separate `provider=` needed. Legacy `model_id=` + `provider=` kwargs still work.

### Try it now (zero config)

```python
from agnoclaw import AgentHarness

# Works out of the box with any Agno-supported model
agent = AgentHarness("ollama:qwen3:8b")  # local, no API key needed
agent.print_response("What files are in this directory?")
```

### With a skill

```python
agent.print_response(
    "Research the state of fusion energy in 2026",
    skill="deep-research",
)
```

### Use a SkillHub / community skill

Skills from [ClawHub](https://clawhub.dev) or any AgentSkills-compatible repo
work out of the box — just drop the skill directory in your skills folder:

```bash
# Install a community skill (e.g. from SkillHub)
git clone https://github.com/clawhub/skills-collection /tmp/skills-collection
cp -r /tmp/skills-collection/summarize-pr ~/.agnoclaw/skills/summarize-pr
```

```python
from agnoclaw import AgentHarness

agent = AgentHarness()
agent.print_response(
    "Summarize PR #42 in the agno-agi/agno repo",
    skill="summarize-pr",
)
```

Or register a skills directory programmatically:

```python
agent = AgentHarness()
agent.skills.add_directory("/path/to/clawhub-skills", trust="community")
agent.print_response("Run the summarize-pr skill", skill="summarize-pr")
```

### Multi-agent team

```python
from agnoclaw.teams import research_team, code_team

team = research_team()
team.print_response("Compare the top AI agent frameworks in 2026", stream=True)
```

### Local inference with Ollama (no API key)

```python
agent = AgentHarness("ollama:qwen3:8b")
agent.print_response("Explain async/await in Python", stream=True)
```

### Async + streaming events

```python
import asyncio
from agno.run.agent import RunEvent

async def main():
    agent = AgentHarness()
    async for event in agent.arun("Analyze this codebase", stream=True, stream_events=True):
        if event.event == RunEvent.run_content:
            print(event.content, end="", flush=True)

asyncio.run(main())
```

---

## Tools

agnoclaw ships a Claude Code-compatible tool set. Key tools:

| Tool | Method | Notes |
|---|---|---|
| `Read` | `read_file()` | Line offset/limit support |
| `Write` | `write_file()` | Creates parent dirs |
| `Edit` | `edit_file()` | Unique string replacement |
| `MultiEdit` | `multi_edit_file()` | Atomic multi-replacement — validates all before applying |
| `Glob` | `glob_files()` | Pattern matching, sorted by mtime |
| `Grep` | `grep_files()` | Regex search with context lines |
| `LS` | `list_dir()` | Directory listing with sizes |
| `Bash` | `bash()` | Shell execution with timeout |
| `WebSearch` | `web_search()` | Auto-detects best backend: Tavily → Exa → Brave → DDGS |
| `WebFetch` | `web_fetch()` | URL fetch + HTML-to-text extraction |
| `TodoWrite/Read` | `create_todo()` / `list_todos()` | Task management |
| `Task` | `spawn_subagent()` | Subagent spawning |

For the unified Claude Code + OpenClaw gap status (implemented/remaining roadmap), see [`docs/harness-gap-analysis.md`](docs/harness-gap-analysis.md).

---

## CLI

```bash
# First-run onboarding wizard (persona, user identity, default model)
agnoclaw init

# Interactive chat (async REPL with heartbeat notifications)
agnoclaw chat

# Legacy blocking REPL
agnoclaw chat --sync

# Full Textual TUI (requires agnoclaw[tui])
agnoclaw tui

# One-shot task
agnoclaw run "Find and fix the bug in src/auth.py"

# With model override
agnoclaw run "Summarize the README" --model gpt-4o --provider openai

# With Ollama (local, no API key)
agnoclaw run "Summarize the README" --model qwen3:8b --provider ollama

# With skill
agnoclaw run "Research quantum computing trends" --skill deep-research

# List available skills
agnoclaw skill list

# Inspect a skill
agnoclaw skill inspect deep-research

# Install a skill from a local directory
agnoclaw skill install path/to/my-skill/

# Initialize workspace (non-interactive)
agnoclaw workspace init

# Show workspace files
agnoclaw workspace show

# Start heartbeat daemon (runs until Ctrl+C)
agnoclaw heartbeat start

# Start with custom interval
agnoclaw heartbeat start --interval 15

# Trigger one heartbeat check immediately
agnoclaw heartbeat trigger

# Install as a persistent service (starts on login, survives terminal close)
# macOS → launchd LaunchAgent; Linux → systemd user service
agnoclaw heartbeat install-service

# Uninstall the service
agnoclaw heartbeat install-service --uninstall
```

---

## Workspace

The workspace is your agent's home directory. Default: `~/.agnoclaw/workspace/`

```
~/.agnoclaw/workspace/
├── AGENTS.md       ← behavioral guidelines
├── SOUL.md         ← persona and tone
├── IDENTITY.md     ← capabilities, limitations, specializations
├── USER.md         ← user preferences (create this yourself)
├── MEMORY.md       ← long-term memory (agent-maintained, first 200 lines loaded)
├── TOOLS.md        ← tool policy (allowed commands, restrictions)
├── HEARTBEAT.md    ← heartbeat checklist
├── BOOT.md         ← startup sequence (run on session start)
├── skills/         ← workspace-level skill overrides (highest priority)
└── memory/         ← daily session logs (YYYY-MM-DD.md)
```

All workspace files are plain Markdown — grep-able, git-backup-friendly, fully transparent.

Populate them with `agnoclaw init` (interactive wizard) or write them directly.

### Example SOUL.md

```markdown
# Soul

You are a direct, technically precise assistant.
You prefer Python. You work in the ML/AI space.
Never use emojis. Keep responses concise.
```

### Example USER.md

```markdown
# User

Name: Alice
Timezone: US/Pacific
Preferred language: Python (not JavaScript)
Communication style: Direct, no fluff
Current focus: Building ML infrastructure
```

### Example HEARTBEAT.md

```markdown
# Heartbeat Checklist

- Check for unreviewed GitHub PRs
- Alert if disk usage > 80%
- Remind about daily standup if it's before 10am

If nothing needs attention, reply HEARTBEAT_OK.
```

---

## Skills

Skills extend the agent with domain-specific instructions. Compatible with the [AgentSkills standard](https://agentskills.io) and the Claude Code / OpenClaw SKILL.md format.

### Built-in skills

| Skill | Description |
|---|---|
| `deep-research` | Multi-source research with structured findings |
| `code-review` | P0/P1/P2/P3 priority code review |
| `git-workflow` | Safe git operations with guardrails |
| `daily-standup` | Generate standup from git history and todos |
| `memory-manage` | Read/update/summarize long-term memory |
| `self-improving-agent` | Record corrections/errors/feature-requests in `.learnings/`; promote stable patterns to workspace files |

### Using skills

```python
# Via API
agent.print_response("Review src/auth.py", skill="code-review")

# Via CLI
agnoclaw run "Review src/auth.py" --skill code-review

# Via chat slash command
/skill code-review
Review the authentication module.
```

### Writing a skill

Create `~/.agnoclaw/workspace/skills/my-skill/SKILL.md`:

```markdown
---
name: my-skill
description: What this skill does and when to use it
user-invocable: true
allowed-tools: bash, web_search
argument-hint: "[optional arg hint]"
---

# My Skill

Instructions for the agent when this skill is active.

Use $ARGUMENTS to access what the user passed.
Use !`git status` to inject dynamic context.
```

Skill precedence (highest → lowest):
1. `~/.agnoclaw/workspace/skills/` — workspace overrides
2. `~/.agnoclaw/skills/` — user-level skills
3. Built-in skills (shipped with agnoclaw)

### Skill security

Skills are classified by **trust level** based on their source:

| Level | Source | Inline exec (`!`cmd``) | Install specs |
|---|---|---|---|
| **builtin** | Shipped with agnoclaw | Allowed | Auto-approved |
| **local** | Workspace or `~/.agnoclaw/skills/` | Allowed | User approval required |
| **community** | External (ClawHub, git clone) | Blocked | User approval + validation |

Package names in install specs are validated against shell metacharacters, URL-based
installs, and path traversal before any command runs. See `docs/skills.md` for details.

---

## Heartbeat and Cron

The heartbeat runs the agent on a schedule to surface items needing attention.
Cron jobs extend this with precise expression-based scheduling — inspired by
OpenClaw's CronManager.

Heartbeat runs on the **main agent's session** (full workspace context). Cron jobs
can run in the main session or in an **isolated session** (fresh, no prior context).

```python
import asyncio
from agnoclaw import AgentHarness
from agnoclaw.heartbeat import HeartbeatDaemon, CronJob

agent = AgentHarness()

def on_alert(message: str):
    # Send to Slack, email, desktop notification, etc.
    print(f"ALERT: {message}")

daemon = HeartbeatDaemon(agent, on_alert=on_alert)

# Add a cron job: daily standup at 9am Mon-Fri (isolated session)
daemon.add_cron_job(CronJob(
    name="daily-standup",
    schedule="0 9 * * 1-5",         # cron expression
    prompt="Run the daily standup.",
    skill="daily-standup",
    isolated=True,                   # fresh session, no conversation history
))

# Or use interval syntax: "30m", "1h", "2h30m"
daemon.add_cron_job(CronJob(
    name="disk-check",
    schedule="1h",
    prompt="Check if disk usage exceeds 80% and alert if so.",
))

asyncio.run(daemon.run_forever())  # blocks until Ctrl+C
```

For always-on operation (survives terminal close), install as a system service:

```bash
# macOS → launchd LaunchAgent; Linux → systemd user service
agnoclaw heartbeat install-service --interval 30
```

Configuration (`.agnoclaw.toml` or env vars):

```toml
[heartbeat]
enabled = true
interval_minutes = 30
active_hours_start = "08:00"
active_hours_end = "22:00"
model = "claude-haiku-4-5-20251001"  # cheaper model for heartbeat
ok_threshold_chars = 300
```

---

## Configuration

Priority order (highest → lowest):

1. Environment variables (`AGNOCLAW_*`)
2. `~/.agnoclaw/config.toml` (user-level)
3. `.agnoclaw.toml` in current directory (project-level)
4. Defaults

### Example `.agnoclaw.toml`

See [`.agnoclaw.toml.example`](.agnoclaw.toml.example) for the full annotated template with all options documented.

Quick start — copy and customize:

```bash
cp .agnoclaw.toml.example .agnoclaw.toml
# or for user-level config:
cp .agnoclaw.toml.example ~/.agnoclaw/config.toml
```

Minimal config (most fields have sensible defaults):

```toml
default_model = "claude-sonnet-4-6"
default_provider = "anthropic"

[storage]
backend = "sqlite"

[heartbeat]
enabled = false
```

### Key env vars

```bash
# Model
AGNOCLAW_DEFAULT_MODEL=claude-sonnet-4-6
AGNOCLAW_DEFAULT_PROVIDER=anthropic

# Storage
AGNOCLAW_STORAGE__BACKEND=postgres
AGNOCLAW_STORAGE__POSTGRES_URL=postgresql://user:pass@localhost/agnoclaw

# Learning + context
AGNOCLAW_ENABLE_LEARNING=true
AGNOCLAW_LEARNING_MODE=agentic
AGNOCLAW_ENABLE_COMPRESSION=true
AGNOCLAW_ENABLE_SESSION_SUMMARY=true

# Heartbeat
AGNOCLAW_HB_ENABLED=true
AGNOCLAW_HB_INTERVAL_MINUTES=30
AGNOCLAW_HB_MODEL=claude-haiku-4-5-20251001

# Runtime governance
AGNOCLAW_PERMISSION_MODE=default
AGNOCLAW_GUARDRAILS_ENABLED=true

# TUI
AGNOCLAW_THEME=textual-dark

# Debug
AGNOCLAW_DEBUG=true
```

---

## Advanced: Custom tools and extensions

```python
from agno.tools import tool
from agnoclaw import AgentHarness

@tool(description="Query our internal API")
def query_internal_api(endpoint: str, params: dict = {}) -> str:
    """Query the internal analytics API."""
    # your implementation
    return "{...}"

agent = AgentHarness(tools=[query_internal_api])
```

### Custom system prompt section

```python
from agnoclaw import AgentHarness
from agnoclaw.prompts import SystemPromptBuilder

agent = AgentHarness()
agent._prompt_builder.add_section("""
# Company Context

You are assisting engineers at Acme Corp.
Internal Jira: jira.acme.internal
Internal docs: confluence.acme.internal
Follow the Acme style guide for all code changes.
""")
```

### Using Agno's HITL approval

```python
from agno.tools import tool
from agnoclaw import AgentHarness

@tool(requires_confirmation=True)
def deploy_to_production(service: str, version: str) -> str:
    """Deploy a service to production — requires human approval."""
    # deployment logic
    return f"Deployed {service}:{version}"

agent = AgentHarness(tools=[deploy_to_production])
response = agent.run("Deploy the auth service version 2.1.0 to production")

# Check if paused for approval
if response.active_requirements:
    for req in response.active_requirements:
        print(f"Approve: {req.tool_execution.tool_name}({req.tool_execution.tool_args})")
        if input("y/n: ") == "y":
            req.confirm()
        else:
            req.reject()

    # Resume after approval (use _agent directly for Agno continue_run)
    final = agent._agent.continue_run(
        run_id=response.run_id,
        requirements=response.requirements,
    )
```

---

## Multi-agent patterns

### Research → Code pipeline

```python
from agnoclaw.teams import research_team, code_team

# Phase 1: Research the solution space
research = research_team()
findings = research.run("What's the best approach for rate limiting in FastAPI in 2026?")

# Phase 2: Implement the chosen approach
code = code_team()
code.print_response(
    f"Implement rate limiting in FastAPI based on these findings:\n{findings.content}",
    stream=True,
)
```

### Custom team

```python
from agno.agent import Agent
from agno.team import Team, TeamMode
from agno.models.anthropic import Claude
from agnoclaw.tools import FilesToolkit, WebToolkit

security_reviewer = Agent(
    name="SecurityReviewer",
    role="Review code for OWASP Top 10 vulnerabilities and security anti-patterns",
    model=Claude(id="claude-sonnet-4-6"),
    tools=[FilesToolkit()],
)

perf_reviewer = Agent(
    name="PerfReviewer",
    role="Review code for performance issues: N+1 queries, memory leaks, unnecessary O(n²)",
    model=Claude(id="claude-sonnet-4-6"),
    tools=[FilesToolkit()],
)

review_team = Team(
    name="Review Team",
    mode=TeamMode.broadcast,  # both review in parallel
    members=[security_reviewer, perf_reviewer],
    model=Claude(id="claude-sonnet-4-6"),
)

review_team.print_response("Review src/database.py", stream=True)
```

---

## Multi-session project tracking (ProgressToolkit)

For complex projects that span multiple sessions or context windows:

```python
import json
from agnoclaw.tools.tasks import ProgressToolkit

toolkit = ProgressToolkit(project_dir=".")

# Define requirements upfront (all start as failing)
toolkit.write_features(json.dumps([
    {"id": "auth-01", "description": "Users can register"},
    {"id": "api-01",  "description": "GET /users/{id} returns profile"},
]))

# Mark features passing as you implement them
toolkit.update_feature_status("auth-01", "passing")

# Save progress before a session ends / context compacts
toolkit.write_progress(
    summary="Auth complete. Starting API layer.",
    next_steps="1. Implement GET /users/{id}\n2. Write tests",
    context="JWT secret in JWT_SECRET env var",
)

# Resume at the start of the next session
print(toolkit.read_progress())
print(toolkit.read_features())
```

---

## Context management for long-running sessions

AgentHarness includes built-in support for managing context in long-running or
multi-session agents. These features use Agno's native context management layer.

### Tool result compression

For sessions with many tool calls (file reads, bash output, web fetches), tool
results can dominate the context window. Enable compression to automatically
summarize older tool outputs:

```python
agent = AgentHarness(
    enable_compression=True,
    compress_token_limit=4000,  # compress when tool results exceed this
)
```

### Session summaries (cross-session continuity)

Session summaries capture the key decisions and state at the end of each run.
On the next run, the summary is injected into context so the agent picks up
where it left off:

```python
agent = AgentHarness(enable_session_summary=True)

# First session
agent.run("Implement user auth with JWT")

# Later session — the agent automatically gets the previous summary
agent.run("Continue: add refresh tokens")
```

### Memory hierarchy

| Layer | What | Scope | Storage |
|-------|------|-------|---------|
| **Workspace files** | MEMORY.md, AGENTS.md, SOUL.md | Per-workspace | Markdown files |
| **LearningMachine** (per-user) | User profile + observations | Per-user | SQL |
| **LearningMachine** (institutional) | Patterns, entities, decisions | Cross-user | SQL |

All SQL-backed memory goes through Agno's LearningMachine — a unified system
that handles both per-user facts and institutional knowledge in a single pass.

```python
# Full memory stack
agent = AgentHarness(
    user_id="alice",
    enable_user_memory=True,       # per-user profile + observations
    enable_learning=True,          # institutional knowledge
    learning_mode="agentic",       # agent decides when to learn
    enable_compression=True,       # keep context window manageable
    enable_session_summary=True,   # continuity across sessions
)
```

### Workspace bootstrap limits

Workspace files are capped to prevent context bloat:

- **MEMORY.md**: first 200 lines only (keep it as an index)
- **Per file**: 20,000 chars max
- **Total**: 150,000 chars max across all workspace files

### Environment variables

```bash
AGNOCLAW_ENABLE_COMPRESSION=true
AGNOCLAW_COMPRESS_TOKEN_LIMIT=4000
AGNOCLAW_ENABLE_SESSION_SUMMARY=true
AGNOCLAW_ENABLE_LEARNING=true
AGNOCLAW_LEARNING_MODE=agentic
```

---

## Architecture

```
agnoclaw/
├── src/agnoclaw/
│   ├── agent.py           # AgentHarness — main class (HarnessAgent = alias)
│   ├── workspace.py       # Workspace (~/.agnoclaw/workspace/)
│   ├── memory.py          # Memory management utilities
│   ├── config.py          # Settings (TOML + env vars)
│   ├── teams.py           # Pre-built team configurations
│   ├── prompts/
│   │   ├── sections.py    # Prompt sections (identity, tone, tasks, tools, security, git)
│   │   └── system.py      # System prompt assembler
│   ├── tools/
│   │   ├── bash.py        # Shell execution
│   │   ├── files.py       # Read/Write/Edit/MultiEdit/Glob/Grep/LS
│   │   ├── web.py         # WebSearch/WebFetch
│   │   └── tasks.py       # TodoToolkit + ProgressToolkit + SubagentTool
│   ├── skills/
│   │   ├── loader.py      # SKILL.md parser (AgentSkills + OpenClaw frontmatter)
│   │   └── registry.py    # Discovery + selective injection + gate checks
│   ├── heartbeat/
│   │   └── daemon.py      # HeartbeatDaemon + CronJob (interval + cron expressions)
│   ├── runtime/           # v0.2 runtime contracts
│   │   ├── hooks.py       # PreRunHook, PostRunHook
│   │   ├── policy.py      # PolicyEngine, PolicyDecision
│   │   ├── events.py      # EventSink (observability)
│   │   ├── guardrails.py  # Input/output guardrails
│   │   └── permissions.py # Permission modes (bypass, accept_edits, plan)
│   ├── cli/
│   │   ├── main.py        # Click CLI (init, chat, run, tui, skill, heartbeat, workspace)
│   │   └── async_repl.py  # Async REPL with heartbeat notifications
│   └── tui/               # v0.3 Textual TUI (requires agnoclaw[tui])
│       ├── app.py         # AgnoClawApp — main Textual application
│       ├── driver.py      # AgentDriver — async streaming + heartbeat bridge
│       ├── events.py      # Custom Textual Messages
│       ├── screens.py     # Modal screens (skill picker, help)
│       └── widgets/       # ChatLog, InputBar, NotificationPanel, StatusBar, HeaderBar
├── skills/                # Built-in skills
│   ├── deep-research/
│   ├── code-review/
│   ├── git-workflow/
│   ├── daily-standup/
│   ├── memory-manage/
│   └── self-improving-agent/  # Capture corrections/errors → .learnings/
├── docs/
│   └── harness-gap-analysis.md # Unified Claude Code + OpenClaw harness gap status
└── examples/              # 20 runnable examples
    ├── ollama_local.py    # Local inference (no API key)
    ├── openclaw_style.py  # Full OpenClaw-style setup
    ├── openclaw_skills.py # Skill hub creation and usage
    ├── progress_tracking.py   # ProgressToolkit lifecycle
    ├── cron_jobs.py           # CronJob scheduler + service install
    ├── self_improving_agent.py # .learnings/ capture + promotion pattern
    ├── claude_code_tools.py   # CC gap analysis + MultiEdit demo
    └── ...
```

---

## v0.2 Runtime Contracts

AgentHarness v0.2 adds a runtime governance layer between your code and the LLM.
Every `run()` / `arun()` call passes through this pipeline:

```
User message
  → PreRunHooks (transform input)
  → PolicyEngine.before_run (allow/deny/redact)
  → PolicyEngine.before_skill_load (gate skills)
  → PolicyEngine.before_prompt_send (inspect/redact prompts)
  → Model call (Agno Agent)
  → PostRunHooks (transform output)
  → EventSink (audit trail)
```

### EventSink (observability)

```python
from agnoclaw.runtime import EventSink

class MyEventSink(EventSink):
    async def emit(self, event):
        # Send to your logging/tracing/analytics system
        print(f"[{event['event_type']}] {event.get('payload', {})}")

agent = AgentHarness(event_sink=MyEventSink())
```

### PolicyEngine (governance)

```python
from agnoclaw.runtime import PolicyEngine, PolicyDecision

class CompliancePolicy(PolicyEngine):
    def before_run(self, run_input, context):
        if "DELETE FROM" in run_input.message.upper():
            return PolicyDecision.deny("SQL DELETE statements are blocked")
        return PolicyDecision.allow()

agent = AgentHarness(policy_engine=CompliancePolicy())
```

### Hooks (middleware)

```python
def log_inputs(run_input, context):
    print(f"User: {run_input.message[:100]}")
    return run_input  # pass through (or return modified)

def log_outputs(run_input, result, context):
    print(f"Output: {result.content[:100]}")
    return result

agent = AgentHarness(
    pre_run_hooks=[log_inputs],
    post_run_hooks=[log_outputs],
)
```

### Permission modes

```python
# Bypass all checks (development)
agent = AgentHarness(permission_mode="bypass")

# Require approval for edits (staging)
agent = AgentHarness(permission_mode="accept_edits")

# Plan-only mode (no writes, no shell)
agent = AgentHarness(permission_mode="plan")
```

---

## Usage Patterns

### 1. Personal assistant (OpenClaw-style, local)

Use agnoclaw as a personal agent that runs on your machine, connects to
messaging, monitors your workspace, and has cron-scheduled tasks:

```python
import asyncio
from agnoclaw import AgentHarness
from agnoclaw.heartbeat import HeartbeatDaemon, CronJob

agent = AgentHarness(
    "anthropic:claude-sonnet-4-6",
    enable_learning=True,
    enable_user_memory=True,
    user_id="me",
)

daemon = HeartbeatDaemon(agent, on_alert=send_to_slack)
daemon.add_cron_job(CronJob(
    name="daily-standup",
    schedule="0 9 * * 1-5",
    prompt="Run daily standup.",
    skill="daily-standup",
    isolated=True,
))
asyncio.run(daemon.run_forever())
```

For WhatsApp/Telegram/Slack integration, build a thin webhook handler that
calls `agent.arun(message)` and sends the response back. The messaging
layer is a platform concern — agnoclaw is the agent backend.

```bash
# Install as always-on service
agnoclaw heartbeat install-service
```

### 2. Embed in a SaaS product

Use agnoclaw as the AI backend in your existing product. The messaging
layer (WebSocket, REST API, etc.) is your platform code — agnoclaw provides
the agent harness:

```python
from agnoclaw import AgentHarness
from agnoclaw.runtime import PolicyEngine, PolicyDecision, EventSink

class TenantPolicy(PolicyEngine):
    def before_run(self, run_input, context):
        if context.tenant_id not in allowed_tenants:
            return PolicyDecision.deny("Tenant not authorized")
        return PolicyDecision.allow()

# One harness per tenant (or shared with tenant_id in context)
agent = AgentHarness(
    "anthropic:claude-sonnet-4-6",
    policy_engine=TenantPolicy(),
    event_sink=MyAnalyticsSink(),
    tenant_id="acme-corp",
    enable_learning=True,
    learning_namespace="acme",
)

# Your API endpoint calls this:
result = await agent.arun(user_message, user_id=user_id, session_id=session_id)
```

### 3. TUI mode (personal assistant)

The Textual TUI provides a full-featured terminal interface with streaming,
heartbeat notifications, skill picker, and a debug log viewer:

```bash
pip install "agnoclaw[tui]"
agnoclaw tui
```

Features: live token streaming with Markdown rendering, notification panel
(heartbeat/cron alerts), slash commands (`/skill`, `/clear`, `/help`),
Ctrl+N toggle notifications, Ctrl+S skill picker, Ctrl+L debug log.

### 4. CLI-only (no messaging layer)

The CLI works standalone — it IS the messaging layer. Use this for
development, debugging, and personal productivity:

```bash
agnoclaw chat                                    # async REPL (default)
agnoclaw chat --sync                             # legacy blocking REPL
agnoclaw run "Fix the bug in src/auth.py"        # one-shot
agnoclaw run "Review src/" --skill code-review   # with skill
```

---

## Testing

```bash
# Unit tests (no API key needed, ~0.8s)
uv run pytest tests/ -m "not integration" -q

# Integration tests with Ollama (local, no API key)
uv run pytest tests/test_integration.py -v

# Integration tests with a larger local model
AGNOCLAW_TEST_MODEL=qwen3:8b uv run pytest tests/test_integration.py -v

# Integration tests with Anthropic
AGNOCLAW_TEST_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-... pytest tests/test_integration.py
```

---

## License

MIT — fork it, hack it, ship it.

---

*Built with [Agno](https://github.com/agno-agi/agno). Inspired by Claude Code, OpenClaw, and LangChain DeepAgents.*
