# agnoclaw — Project Memory

## What This Is

`agnoclaw` is a hackable, model-agnostic agent harness built on the Agno framework.
It takes Claude Code's prompt wisdom, OpenClaw's UX patterns (heartbeat, workspace, SKILL.md),
and LangChain DeepAgents' middleware insights — and runs them on Agno's production-ready engine.

## Tech Stack

- **Runtime**: Python 3.12, UV package manager
- **Framework**: Agno v2.5.x (`pip install agno`)
- **CLI**: Click + Rich + prompt-toolkit
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
└── cli/
    └── main.py       # Click CLI entry point
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
uv run agnoclaw chat                    # interactive chat session
uv run agnoclaw run "task description" # one-shot task
uv run agnoclaw skill list             # list available skills
uv run agnoclaw heartbeat start        # start heartbeat daemon
```

## Import Patterns

```python
# Core
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
```
