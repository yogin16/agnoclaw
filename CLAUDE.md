# agnoclaw — Project Memory

## What This Is

`agnoclaw` is a hackable, model-agnostic agent harness built on the Agno framework.
It takes Claude Code's prompt wisdom, OpenClaw's UX patterns (heartbeat, workspace, SKILL.md),
and LangChain DeepAgents' middleware insights — and runs them on Agno's production-ready engine.

## Tech Stack

- **Runtime**: Python 3.12, UV package manager
- **Framework**: Agno v2.5.x (`pip install agno`)
- **CLI**: Click + Rich + prompt-toolkit (optional extra: `agnoclaw[cli]`)
- **TUI**: Textual >= 0.85 (optional extra: `agnoclaw[tui]`)
- **Storage**: SQLite (dev), PostgreSQL (prod) — via Agno's `SqliteDb` / `PostgresDb`
- **Scheduling**: asyncio (heartbeat daemon + cron jobs)
- **Frontmatter**: python-frontmatter (SKILL.md parsing)

## Package Layout

```
src/agnoclaw/
├── agent.py          # AgentHarness — main class, wraps Agno Agent
├── workspace.py      # Workspace: ~/.agnoclaw/workspace/
├── memory.py         # Memory hierarchy loader (AGENTS.md, SOUL.md, USER.md, MEMORY.md)
├── config.py         # Settings via pydantic-settings + TOML
├── teams.py          # Pre-built team factories (research, code, data)
├── prompts/
│   ├── system.py     # System prompt assembler (layered composition)
│   └── sections.py   # Sections: identity, tone, narration, tasks, care, blocked, tools, security, git, memory, skills, plan, heartbeat, learning
├── tools/
│   ├── bash.py       # BashToolkit (bash, bash_start, bash_output, bash_kill)
│   ├── files.py      # FilesToolkit (read, write, edit, multi_edit, glob, grep, list_dir)
│   ├── web.py        # WebToolkit (web_search, web_fetch)
│   └── tasks.py      # TodoToolkit, ProgressToolkit, SubagentTool
├── skills/
│   ├── loader.py     # SKILL.md frontmatter + content parser
│   └── registry.py   # Skill discovery and selective injection
├── heartbeat/
│   └── daemon.py     # asyncio-based HeartbeatDaemon + CronJob scheduler
├── runtime/          # v0.2 runtime contracts (hooks, policy, events, guardrails)
│   ├── hooks.py      # PreRunHook, PostRunHook
│   ├── policy.py     # PolicyEngine, PolicyDecision
│   ├── events.py     # EventSink (observability)
│   ├── guardrails.py # Input/output guardrails
│   ├── permissions.py # Permission modes (bypass, accept_edits, plan)
│   ├── context.py    # RunContext
│   └── errors.py     # Runtime error types
├── cli/
│   ├── main.py       # Click CLI entry point (chat, run, tui, skill, heartbeat)
│   └── async_repl.py # Async REPL with prompt-toolkit + heartbeat notifications
└── tui/              # v0.3 Textual TUI (optional: agnoclaw[tui])
    ├── app.py        # AgnoClawApp — main Textual application
    ├── driver.py     # AgentDriver — async bridge: streaming + heartbeat
    ├── events.py     # Custom Textual Messages (StreamChunk, HeartbeatAlert, etc.)
    ├── screens.py    # SkillPickerScreen, HelpScreen modals
    └── widgets/      # ChatLog, InputBar, NotificationPanel, StatusBar, HeaderBar, LogViewer
```

## Key Design Decisions

- `AgentHarness` wraps Agno's `Agent` but adds: workspace awareness, skill injection, memory loading
- System prompt is assembled from sections, with memory files injected last
- SKILL.md follows the AgentSkills standard (compatible with ClawHub format)
- Selective skill injection: only one skill's content loaded per turn
- Heartbeat: asyncio-based, HEARTBEAT_OK suppression, active hours, configurable model
- All workspace files are plain Markdown — transparent, grep-able, git-backup-friendly

## File Naming Conventions

- Tool functions: `snake_case`, registered as Agno `@tool` or `Toolkit`
- Config keys: `snake_case` in TOML, `AGNOCLAW_` prefix for env vars
- Workspace files: `SCREAMING_SNAKE_CASE.md` (matches OpenClaw convention)

## Common Commands

```bash
uv run agnoclaw chat                    # async REPL with heartbeat notifications
uv run agnoclaw chat --sync             # legacy blocking REPL
uv run agnoclaw tui                     # full Textual TUI (requires agnoclaw[tui])
uv run agnoclaw run "task description" # one-shot task
uv run agnoclaw skill list             # list available skills
uv run agnoclaw heartbeat start        # start heartbeat daemon
```

## Import Patterns

```python
# Core (zero CLI/TUI deps)
from agnoclaw import AgentHarness
from agnoclaw.config import HarnessConfig

# Tools
from agnoclaw.tools import get_default_tools, BashToolkit, FilesToolkit, WebToolkit

# Skills
from agnoclaw.skills import SkillRegistry

# Workspace
from agnoclaw.workspace import Workspace

# Heartbeat
from agnoclaw.heartbeat import HeartbeatDaemon, CronJob

# Runtime contracts (v0.2)
from agnoclaw.runtime import EventSink, PolicyEngine, PolicyDecision

# TUI (requires agnoclaw[tui])
from agnoclaw.tui import AgnoClawApp

# Async REPL (requires agnoclaw[cli])
from agnoclaw.cli.async_repl import AsyncREPL
```

## Packaging Extras

Core `agnoclaw` has zero CLI/TUI deps. Install extras for interfaces:

- `agnoclaw[cli]` — Click + Rich + prompt-toolkit (async REPL)
- `agnoclaw[tui]` — Textual TUI (includes cli deps)
- `agnoclaw[full]` — TUI + croniter (personal-assistant setup)
- `agnoclaw[local]` — Ollama support
- `agnoclaw[postgres]` — PostgreSQL storage
- `agnoclaw[all-models]` — All model providers
- `agnoclaw[dev]` — Development tools (pytest, ruff, etc.)
