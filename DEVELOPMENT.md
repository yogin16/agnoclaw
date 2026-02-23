# Development Guide

## Project structure

```
agnoclaw/
├── src/agnoclaw/          # Main package (src layout)
│   ├── agent.py           # HarnessAgent — main entry point
│   ├── config.py          # HarnessConfig, HeartbeatConfig, StorageConfig
│   ├── workspace.py       # Workspace: context file loading, memory management
│   ├── memory.py          # MemoryManager + LearningMachine builders
│   ├── teams.py           # Pre-built team factories (research, code, data)
│   ├── prompts/           # System prompt assembly (SystemPromptBuilder)
│   ├── tools/             # Default tools (bash, files, web, tasks, subagent)
│   ├── skills/            # Skill registry, parser, loader (SKILL.md format)
│   ├── heartbeat/         # Heartbeat daemon (asyncio-based scheduler)
│   └── cli/               # Click CLI (chat, run, skill, heartbeat, workspace, init)
├── tests/                 # Test suite (206 tests, 11 skipped)
├── examples/              # Runnable examples (require API key)
├── skills/                # Bundled skills (shipped with package)
└── pyproject.toml
```

## Architecture decisions

### System prompt assembly
`SystemPromptBuilder` layers sections in order:
```
identity → tone → tasks → tools → security → git → memory →
skills → [plan_mode] → [learning] → workspace context →
active skill → extra context → datetime
```
Workspace context (AGENTS/SOUL/IDENTITY/USER/MEMORY/TOOLS/BOOT) is
injected near the end so it takes precedence over generic defaults.

### Skill injection (OpenClaw selective injection)
Before each response the agent has a list of skill descriptions in its
system prompt. It can activate at most one skill per turn by referencing
it. The full SKILL.md is only loaded when needed — keeping context lean.

Priority chain: workspace skills > user skills > extra dirs > bundled skills.

### Storage
All persistence (session history, memory, knowledge) goes through
Agno's `SqliteDb` or `PostgresDb` via the shared `db=` parameter.
Never store agent state in Python process memory (no singleton patterns).

### Heartbeat
The heartbeat daemon runs as an asyncio task. It checks `HEARTBEAT.md`
before each tick — if the file has no actionable content (headers only),
the tick is skipped (zero cost). HEARTBEAT_OK responses are silently
suppressed if under `ok_threshold_chars`. The CLI's `heartbeat start`
command runs the daemon in an asyncio event loop until Ctrl+C.

## Running the full test suite

```bash
uv run pytest tests/ -q
```

With coverage:
```bash
uv run pytest tests/ --cov=agnoclaw --cov-report=term-missing
```

A specific module:
```bash
uv run pytest tests/test_workspace.py -v
```

## Key Agno v2.5.x notes

- `from agno.run.agent import RunOutput, RunEvent` — NOT `agno.run.response`
- `Agent(id=...)` — NOT `agent_id=`
- Storage via `db=` — NOT `storage=`
- `show_tool_calls` does NOT exist in v2.5.3 (removed)
- `LearningMachine` is a `@dataclass` — NO global `mode=`. Use per-store
  configs: `EntityMemoryConfig`, `LearnedKnowledgeConfig`, `DecisionLogConfig`
  from `agno.learn.config`

## Adding a new workspace file type

1. Add to `WORKSPACE_FILES` dict in `workspace.py`
2. Add to the `context_files()` loading order if it should be injected
3. Add a corresponding test in `tests/test_workspace.py`
4. Update `workspace show` in `cli/main.py` if it should be displayed

## Release process

1. Bump version in `pyproject.toml`
2. Update `CHANGELOG.md` (if it exists)
3. Tag: `git tag v0.x.y && git push origin v0.x.y`
4. CI publishes to PyPI automatically on tags (once configured)
