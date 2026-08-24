# agnoclaw — Vision

## What agnoclaw is

agnoclaw is a **general-purpose agent harness** — a foundation layer for building any kind of AI assistant, copilot, or autonomous agent.

It wraps the Agno framework with opinionated defaults drawn from Claude Code's prompt engineering, OpenClaw's UX patterns, and production middleware insights. The result is a harness that works out of the box for common cases while remaining fully customizable for specialized ones.

The long-term boundary is **embeddable library first, server-capable when needed**.
`AgentHarness` remains the canonical runtime. Optional server, remote, AgentOS, and
pack surfaces adapt to the harness; they do not replace it.

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

### Products exposing agents as services

Use agnoclaw as the harness boundary and export it through an optional server runtime
when a product needs hosted sessions, streaming, approvals, scheduler, observability,
or external clients. AgentOS is the preferred adapter target for this mode, but it is
not required for core embedded use.

```python
from agnoclaw import AgentHarness
from agnoclaw.runtime.agentos import create_agentos_app

harness = AgentHarness(name="deal-agent")
app = create_agentos_app([harness], scheduler=True, approvals=True)
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

The harness should own the hard runtime contracts (tool execution, state isolation,
context lifecycle, policy, learning scope, and observability) so teams can focus on the
domain expertise captured in skills and configs. Some of those long-running guarantees
are target architecture rather than stable current behavior; the distinction is tracked
in [Harness gap analysis](harness-gap-analysis.md).

## Design principles

### Tiny surface, explicit guarantees

The default API stays small. Profiles choose coherent behavior for quick, session,
durable, local-safe, and service use instead of forcing users to coordinate dozens of
booleans. Power lives in composable capabilities and a per-run runtime below the API.

Public claims describe observable guarantees—concurrency isolation, cancellation,
recovery, context bounds, effect policy, learning scope—not the mere presence of a code
path.

### Config-driven, not code-driven

The primary interface for customization is configuration, not code. A `.agnoclaw.toml` file controls which tools are enabled, what model to use, how the agent behaves, and what skills are available. Code is needed only for custom tools or deep integration.

### Skills as the unit of expertise

Skills are plain markdown files with YAML frontmatter. They're the primary way to teach an agent new capabilities:
- Easy to write (just markdown)
- Easy to share (just files)
- Easy to discover (ClawHub registry)
- Compatible with the broader OpenClaw/ClawHub ecosystem

### Transparent by default

Workspace instructions and curated memory are plain Markdown and remain human-readable,
grep-able, and git-trackable. Exact sessions, learning stores, scheduler records, event
trajectories, and artifacts may use databases or append-only formats appropriate to their
durability and privacy needs. The documentation must identify the canonical owner,
retention, and export path for every state type.

### Model-agnostic

agnoclaw works with any model provider supported by Agno: OpenAI, Anthropic, Google, Ollama (local), and others. Switch models by changing one config line.

### Progressive complexity

- **Simple**: `agnoclaw chat` with default config
- **Moderate**: Custom skills, workspace files, scheduled tasks
- **Advanced**: Custom tools, plugins, multi-agent teams, embedded in products
- **Expert**: Runtime hooks, policy engines, guardrails, context providers, MCP integrations, optional AgentOS/server export

Each level builds on the previous one without requiring knowledge of the layers below.

## What makes it different

Agno evolves quickly and increasingly supplies first-party skills, learning, scheduling,
AgentOS, HITL, tracing, and evaluation primitives. A static “raw Agno versus agnoclaw”
feature checklist goes stale and encourages duplicate implementations.

`agnoclaw` should differentiate through the quality of its composition:

- one embeddable, profile-driven API with optional CLI/TUI/server adapters;
- a coherent execution backend and policy boundary across first-party capabilities;
- transparent workspace conventions and interoperable Agent Skills;
- stronger run lifecycle, concurrency, context, effect, and learning guarantees;
- an evidence-backed conformance suite across supported Agno versions;
- defaults with excellent taste: small for short tasks, durable when requested, safe for
  embedding.

`agnoclaw` does not replace Agno. It turns selected Agno primitives into a stable,
opinionated harness contract and delegates platform concerns back to Agno/AgentOS when
that is the stronger boundary.

## Current direction

The durable product direction is defined by:

- [World-class harness strategy](world-class-harness.md) — research, product decision,
  keep/change/remove/add analysis, and roadmap;
- [Harness architecture](architecture.md) — target kernel and invariants;
- [Learning and self-improvement](learning.md) — correct Agno store usage and promotion
  model;
- [Harness evaluation](evaluation.md) — evidence required to call a capability stable;
- [Harness gap analysis](harness-gap-analysis.md) — implementation-aligned current truth.

The older [v0.8 direction](../spec/v0.8-harness-sdk-server-packs.md) remains a historical
design record for context providers, AgentOS export, packs, and SDK ergonomics. Those
surfaces adapt to the new run kernel; they do not replace it.
