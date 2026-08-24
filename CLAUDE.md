# agnoclaw — Project Memory

## What This Is

`agnoclaw` is a hackable, model-agnostic agent harness built on the Agno framework.
It takes Claude Code's prompt wisdom, OpenClaw's UX patterns (heartbeat, workspace, SKILL.md),
and LangChain DeepAgents' middleware insights — and runs them on Agno's production-ready engine.

## Tech Stack

- **Runtime**: Python 3.11-3.14, UV package manager
- **Framework**: Agno >=2.6.4,<2.9; lockfile 2.8.7. Agno 2.6.4 is the legacy lane,
  2.8.7 is the primary stable lane, and 3.0.0a1 is a quarantined preview as of
  2026-08-07. Update through `agnoclaw.compat` and the compatibility suite, never by
  assuming minor-version behavior.
- **CLI**: Click + Rich + prompt-toolkit (optional extra: `agnoclaw[cli]`)
- **TUI**: Textual >= 0.85 (optional extra: `agnoclaw[tui]`)
- **Storage**: SQLite (dev), PostgreSQL (prod) — via Agno's `SqliteDb` / `PostgresDb`
- **Scheduling**: asyncio (heartbeat daemon + cron jobs)
- **Frontmatter**: safe PyYAML parsing with an agnoclaw-owned delimiter reader

## Package Layout

```
src/agnoclaw/
├── agent.py          # AgentHarness — main class, wraps Agno Agent
├── capabilities.py   # Immutable capability descriptor and bounded registry
├── capability_execution.py # Governed materialization and operation dispatch
├── capability_approval.py # Durable exact approval coordination
├── capability_runtime.py # Policy/approval/lease/effect composition for AgentHarness
├── compat.py         # Central Agno version/capability compatibility boundary
├── workspace.py      # Workspace: hierarchical (global → project → workspace)
├── memory.py         # Memory hierarchy loader (AGENTS.md, SOUL.md, USER.md, MEMORY.md)
├── config.py         # Settings via pydantic-settings + TOML
├── teams.py          # Pre-built team factories (research, code, data)
├── models/
│   ├── __init__.py   # materialize_model — provider-agnostic adapter dispatch
│   └── anthropic.py  # CacheAwareClaude (conversation-prefix caching), effort gating
├── plugins.py        # PluginLoader + PluginManifest — entry-point-based plugin system
├── prompts/
│   ├── system.py     # System prompt assembler (layered composition)
│   └── sections.py   # Sections: identity, tone, narration, tasks, care, blocked, tools, security, git, memory, skills, plan, heartbeat, learning
├── tools/
│   ├── bash.py       # BashToolkit (bash, bash_start, bash_output, bash_kill)
│   ├── files.py      # FilesToolkit (read, write, edit, multi_edit, glob, grep, list_dir)
│   ├── web.py        # WebToolkit (web_search, web_fetch)
│   ├── tasks.py      # TodoToolkit, ProgressToolkit, SubagentTool
│   ├── browser.py    # BrowserToolkit — Playwright-based (optional: agnoclaw[browser])
│   ├── mcp.py        # MCPToolkit — Model Context Protocol client (optional: agnoclaw[mcp])
│   ├── media.py      # MediaToolkit — image/PDF reading (optional: agnoclaw[media])
│   └── notebook.py   # NotebookToolkit — Jupyter .ipynb editing
├── skills/
│   ├── loader.py     # SKILL.md frontmatter + content parser
│   ├── registry.py   # Skill discovery, selective injection, hub install
│   └── hub.py        # ClawHubClient — HTTP client for ClawHub skill registry
├── heartbeat/
│   └── daemon.py     # asyncio-based HeartbeatDaemon + CronJob scheduler
├── runtime/          # Runtime kernel contracts and transactional authorities
│   ├── lifecycle.py  # Versioned run state/reducer
│   ├── operations.py # Effect intent/settlement domain
│   ├── gateway.py    # Async-first fenced OperationGateway
│   ├── store.py      # RuntimeStore protocol + SQLite authority
│   ├── postgres_store.py # Bounded PostgreSQL service authority
│   ├── leases.py     # Run/session execution ownership and fences
│   ├── artifacts.py  # Scoped content bytes, integrity, paging, encryption seam
│   ├── approvals.py  # Immutable request/decision/approval lifecycle domain
│   ├── security.py   # Admission, authority, grants, labels, safe diagnostics
│   ├── hooks.py      # PreRunHook, PostRunHook
│   ├── policy.py     # PolicyEngine, PolicyDecision
│   ├── events.py     # EventSink (observability)
│   ├── guardrails.py # Input/output guardrails
│   ├── permissions.py # Permission modes (bypass, accept_edits, plan)
│   ├── context.py    # RunContext
│   └── errors.py     # Runtime error types
├── cli/
│   ├── main.py       # Click CLI entry point (chat, run, tui, skill, heartbeat, hub)
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
- Skill discovery: when no skill is active, available descriptions may be injected into
  the prompt. Full activation is currently explicit (`skill=` or CLI/TUI selection);
  there is no model-callable activation tool yet.
- Skill enforcement: `context: fork` routes to an isolated subagent;
  `command-dispatch: tool` bypasses the LLM but still traverses governed Agno Function
  guardrail/policy/permission/event hooks; plain callables are rejected.
- Hierarchical workspace: global (~/.agnoclaw/global) → project (.agnoclaw/) → workspace; child overrides parent
- Plugin system: Python entry-point-based discovery (group: `agnoclaw.plugins`) + explicit module paths
- ClawHub integration: HTTP client for community skill registry (search, inspect, install)
- Heartbeat: asyncio-based, HEARTBEAT_OK suppression, active hours, configurable model
- All workspace files are plain Markdown — transparent, grep-able, git-backup-friendly

## Architecture and quality direction

- `docs/world-class-harness.md` — primary-source research, product strategy, and roadmap
- `docs/architecture.md` — target immutable spec/per-run runtime architecture
- `docs/harness-gap-analysis.md` — implementation-aligned current truth
- `docs/operations-and-recovery.md` — implemented effect/operation/cancellation contract
- `docs/artifacts.md` — implemented durable result artifact and recovery contract
- `docs/capabilities.md` — capability descriptor/registry and bounded discovery contract
- `docs/learning.md` — verified Agno learning behavior and target profiles
- `docs/evaluation.md` — maturity definitions and release gates

Do not mark a capability complete from a code-path checklist. Use the maturity labels
and conformance evidence in `docs/evaluation.md`. In particular, do not claim automatic
conversation compaction, automatic skill activation, MCP parity, isolated concurrent
shared-harness execution, or measured institutional self-improvement until their listed
gates pass. Current overlap is safe by typed single-flight rejection. Registered
capability approval evidence is durable, but it does not serialize an arbitrary
suspended Agno/model continuation after worker-process death.

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
uv run agnoclaw hub search "query"     # search ClawHub skill registry
uv run agnoclaw hub inspect skill-name # inspect a hub skill
uv run agnoclaw hub install skill-name # install a hub skill locally
uv run agnoclaw hub categories         # list hub skill categories
```

## Import Patterns

```python
# Core (zero CLI/TUI deps)
from agnoclaw import AgentHarness
from agnoclaw.config import HarnessConfig

# Tools
from agnoclaw.tools import get_default_tools, BashToolkit, FilesToolkit, WebToolkit

# Browser (requires agnoclaw[browser])
from agnoclaw.tools.browser import BrowserToolkit

# MCP (requires agnoclaw[mcp])
from agnoclaw.tools.mcp import MCPToolkit

# Media (requires agnoclaw[media])
from agnoclaw.tools.media import MediaToolkit

# Notebook
from agnoclaw.tools.notebook import NotebookToolkit

# Skills
from agnoclaw.skills import SkillRegistry, ClawHubClient

# Provider model adapters (prompt caching, effort — provider-agnostic dispatch)
from agnoclaw.models import materialize_model

# Workspace
from agnoclaw.workspace import Workspace

# Plugins
from agnoclaw.plugins import PluginLoader, PluginManifest

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

Core `agnoclaw` has zero CLI/TUI deps. Install extras for interfaces and capabilities:

- `agnoclaw[cli]` — Click + Rich + prompt-toolkit (async REPL)
- `agnoclaw[tui]` — Textual TUI (includes cli deps)
- `agnoclaw[full]` — TUI + croniter (personal-assistant setup)
- `agnoclaw[local]` — Ollama support
- `agnoclaw[postgres]` — PostgreSQL storage
- `agnoclaw[all-models]` — All model providers
- `agnoclaw[browser]` — Playwright-based browser automation
- `agnoclaw[mcp]` — Model Context Protocol server connectivity
- `agnoclaw[media]` — Image + PDF reading (PyMuPDF)
- `agnoclaw[rag]` — LanceDB + Tantivy + PyMuPDF for knowledge bases
- `agnoclaw[notebook]` — Jupyter notebook editing (nbformat)
- `agnoclaw[dev]` — Development tools (pytest, ruff, etc.)
