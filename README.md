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
| **Heartbeat** | No | Yes | Yes |
| **Workspace** | CLAUDE.md only | Full workspace | Full workspace |
| **Python-native** | No (TypeScript) | No | Yes |
| **Production patterns** | N/A | N/A | HITL, streaming, tracing, eval |

**TL;DR:** Claude Code is a consumer product. OpenClaw is a standalone app. agnoclaw is a library — embed it in anything, hack it for everything.

---

## Installation

```bash
# With uv (recommended)
uv add agnoclaw

# With pip
pip install agnoclaw

# With Postgres support
pip install "agnoclaw[postgres]"

# With all model providers
pip install "agnoclaw[all-models]"

# With local Ollama support (no API key needed)
pip install "agnoclaw[local]"
```

---

## Quick Start

```python
from agnoclaw import HarnessAgent

agent = HarnessAgent()
agent.print_response("Summarize the files in this directory", stream=True)
```

### With a different model

```python
agent = HarnessAgent(model_id="gpt-4o", provider="openai")
agent = HarnessAgent(model_id="gemini-2.0-flash", provider="google")
agent = HarnessAgent(model_id="llama3.2", provider="ollama")
agent = HarnessAgent(model_id="llama-3.3-70b-versatile", provider="groq")
```

### With a skill

```python
agent.print_response(
    "Research the state of fusion energy in 2026",
    skill="deep-research",
)
```

### Multi-agent team

```python
from agnoclaw.teams import research_team, code_team

team = research_team()
team.print_response("Compare the top AI agent frameworks in 2026", stream=True)
```

### Local inference with Ollama (no API key)

```python
agent = HarnessAgent(model_id="qwen3:8b", provider="ollama")
agent.print_response("Explain async/await in Python", stream=True)
```

### Async + streaming events

```python
import asyncio
from agno.run.agent import RunEvent

async def main():
    agent = HarnessAgent()
    async for event in agent.arun("Analyze this codebase", stream=True, stream_events=True):
        if event.event == RunEvent.run_content:
            print(event.content, end="", flush=True)

asyncio.run(main())
```

---

## CLI

```bash
# First-run onboarding wizard (persona, user identity, default model)
agnoclaw init

# Interactive chat
agnoclaw chat

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

# Trigger one heartbeat check immediately
agnoclaw heartbeat trigger
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

---

## Heartbeat

The heartbeat system runs the agent periodically to surface items needing attention.

```python
import asyncio
from agnoclaw import HarnessAgent
from agnoclaw.heartbeat import HeartbeatDaemon

agent = HarnessAgent()

def on_alert(message: str):
    # Send to Slack, email, desktop notification, etc.
    print(f"ALERT: {message}")

daemon = HeartbeatDaemon(agent, on_alert=on_alert)
daemon.start()

asyncio.run(asyncio.sleep(float("inf")))  # run forever
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

```toml
default_model = "claude-sonnet-4-6"
default_provider = "anthropic"
workspace_dir = "~/.agnoclaw/workspace"
session_history_runs = 10
enable_bash = true
enable_web_search = true
bash_timeout_seconds = 120

[storage]
backend = "sqlite"
sqlite_path = "~/.agnoclaw/sessions.db"

[heartbeat]
enabled = false
interval_minutes = 30
model = "claude-haiku-4-5-20251001"
```

### Key env vars

```bash
AGNOCLAW_DEFAULT_MODEL=claude-sonnet-4-6
AGNOCLAW_DEFAULT_PROVIDER=anthropic
AGNOCLAW_DEBUG=true

# Storage
AGNOCLAW_STORAGE__BACKEND=postgres
AGNOCLAW_STORAGE__POSTGRES_URL=postgresql://user:pass@localhost/agnoclaw

# Heartbeat
AGNOCLAW_HB_ENABLED=true
AGNOCLAW_HB_INTERVAL_MINUTES=30
```

---

## Advanced: Custom tools and extensions

```python
from agno.tools import tool
from agnoclaw import HarnessAgent

@tool(description="Query our internal API")
def query_internal_api(endpoint: str, params: dict = {}) -> str:
    """Query the internal analytics API."""
    # your implementation
    return "{...}"

agent = HarnessAgent(extra_tools=[query_internal_api])
```

### Custom system prompt section

```python
from agnoclaw import HarnessAgent
from agnoclaw.prompts import SystemPromptBuilder

agent = HarnessAgent()
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
from agnoclaw import HarnessAgent

@tool(requires_confirmation=True)
def deploy_to_production(service: str, version: str) -> str:
    """Deploy a service to production — requires human approval."""
    # deployment logic
    return f"Deployed {service}:{version}"

agent = HarnessAgent(extra_tools=[deploy_to_production])
response = agent.run("Deploy the auth service version 2.1.0 to production")

# Check if paused for approval
if response.active_requirements:
    for req in response.active_requirements:
        print(f"Approve: {req.tool_execution.tool_name}({req.tool_execution.tool_args})")
        if input("y/n: ") == "y":
            req.confirm()
        else:
            req.reject()

    # Resume after approval
    final = agent.underlying_agent.continue_run(
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

## Architecture

```
agnoclaw/
├── src/agnoclaw/
│   ├── agent.py           # HarnessAgent — main class
│   ├── workspace.py       # Workspace (~/.agnoclaw/workspace/)
│   ├── memory.py          # Memory management utilities
│   ├── config.py          # Settings (TOML + env vars)
│   ├── teams.py           # Pre-built team configurations
│   ├── prompts/
│   │   ├── sections.py    # Prompt sections (identity, tone, tasks, tools, security, git)
│   │   └── system.py      # System prompt assembler
│   ├── tools/
│   │   ├── bash.py        # Shell execution
│   │   ├── files.py       # Read/Write/Edit/Glob/Grep
│   │   ├── web.py         # WebSearch/WebFetch
│   │   └── tasks.py       # TodoToolkit + ProgressToolkit + SubagentTool
│   ├── skills/
│   │   ├── loader.py      # SKILL.md parser (AgentSkills standard)
│   │   └── registry.py    # Discovery + selective injection
│   ├── heartbeat/
│   │   └── daemon.py      # Asyncio heartbeat scheduler
│   └── cli/
│       └── main.py        # Click CLI (init, chat, run, skill, heartbeat, workspace)
├── skills/                # Built-in skills
│   ├── deep-research/
│   ├── code-review/
│   ├── git-workflow/
│   ├── daily-standup/
│   └── memory-manage/
└── examples/              # 17 runnable examples
    ├── ollama_local.py    # Local inference (no API key)
    ├── openclaw_style.py  # Full OpenClaw-style setup
    ├── openclaw_skills.py # Skill hub creation and usage
    ├── progress_tracking.py # ProgressToolkit lifecycle
    └── ...
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
