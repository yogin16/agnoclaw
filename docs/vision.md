# agnoclaw — Vision

## What agnoclaw is

agnoclaw is a **general-purpose agent harness** — a foundation layer for building any kind of AI assistant, copilot, or autonomous agent.

It wraps the Agno framework with opinionated defaults drawn from Claude Code's prompt engineering, OpenClaw's UX patterns, and production middleware insights. The result is a harness that works out of the box for common cases while remaining fully customizable for specialized ones.

## Who it's for

### Developers embedding AI agents

Use agnoclaw as a library in your product. Import `AgentHarness`, configure it with a `HarnessConfig`, and you have a production-ready agent with tools, skills, memory, and workspace — without building any of that plumbing yourself.

```python
from agnoclaw import AgentHarness
from agnoclaw.config import HarnessConfig

harness = AgentHarness(config=HarnessConfig(
    model="openai:gpt-4o",
    enable_browser=True,
    enable_media_tools=True,
))
response = await harness.arun("Analyze this contract PDF")
```

### Non-technical users creating their own agents

agnoclaw is designed so that **anyone who can write a config file and a markdown document can create a useful agent**. No Python required.

1. **Config file** (`.agnoclaw.toml`): Set the model, enable tools, configure behavior
2. **Skill files** (`skills/my-skill/SKILL.md`): Plain markdown with YAML frontmatter — describe what the agent should do when invoked
3. **Workspace files** (`AGENTS.md`, `SOUL.md`, `USER.md`): Plain markdown personality and context that shape agent behavior

A non-technical user can:
- Write a `SOUL.md` that defines the agent's personality and expertise
- Create skill files that give the agent domain-specific instructions
- Install community skills from ClawHub with `agnoclaw hub install skill-name`
- Configure everything in a single TOML file
- Run it with `agnoclaw chat` or `agnoclaw tui`

### Teams building specialized copilots

Use agnoclaw as the core of a vertical copilot:
- Legal contract analysis (see `examples/legal_rag/`)
- Code review and development
- Customer support
- Data analysis and reporting
- Any domain where an AI assistant adds value

The harness handles the hard parts (tool execution, memory, context management, scheduling) so teams can focus on the domain expertise captured in skills and configs.

## Design principles

### Config-driven, not code-driven

The primary interface for customization is configuration, not code. A `.agnoclaw.toml` file controls which tools are enabled, what model to use, how the agent behaves, and what skills are available. Code is needed only for custom tools or deep integration.

### Skills as the unit of expertise

Skills are plain markdown files with YAML frontmatter. They're the primary way to teach an agent new capabilities:
- Easy to write (just markdown)
- Easy to share (just files)
- Easy to discover (ClawHub registry)
- Compatible with the broader OpenClaw/ClawHub ecosystem

### Transparent by default

All agent state is stored as plain markdown files in the workspace directory. No opaque databases, no proprietary formats. Everything is grep-able, git-trackable, and human-readable.

### Model-agnostic

agnoclaw works with any model provider supported by Agno: OpenAI, Anthropic, Google, Ollama (local), and others. Switch models by changing one config line.

### Progressive complexity

- **Simple**: `agnoclaw chat` with default config
- **Moderate**: Custom skills, workspace files, scheduled tasks
- **Advanced**: Custom tools, plugins, multi-agent teams, embedded in products
- **Expert**: Runtime hooks, policy engines, guardrails, MCP integrations

Each level builds on the previous one without requiring knowledge of the layers below.

## What makes it different

| Concern | Raw Agno | agnoclaw |
|---------|----------|----------|
| System prompt | Manual string | Layered composition from workspace files |
| Tools | Register manually | Batteries-included + config toggles |
| Skills | N/A | SKILL.md format + registry + ClawHub |
| Memory | Manual file I/O | Workspace hierarchy (AGENTS/SOUL/USER/MEMORY.md) |
| Scheduling | HTTP callback server | In-process daemon with active hours |
| Config | Code only | TOML + env vars + pydantic-settings |
| Plugins | N/A | Entry-point discovery |
| Browser/MCP/Media | Build it yourself | Optional extras, config-enabled |

agnoclaw doesn't replace Agno — it wraps it with the patterns and conventions that make agents actually useful in practice.
