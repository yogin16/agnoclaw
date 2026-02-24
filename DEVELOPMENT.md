# Development Guide

## Project structure

```
agnoclaw/
├── src/agnoclaw/          # Main package (src layout)
│   ├── agent.py           # AgentHarness — main entry point
│   ├── config.py          # HarnessConfig, HeartbeatConfig, StorageConfig
│   ├── workspace.py       # Workspace: context file loading, memory management
│   ├── memory.py          # MemoryManager + LearningMachine builders
│   ├── teams.py           # Pre-built team factories (research, code, data)
│   ├── prompts/           # System prompt assembly (SystemPromptBuilder)
│   ├── tools/             # Default tools (bash, files, web, tasks, subagent)
│   ├── skills/            # Skill registry, parser, loader (SKILL.md format)
│   ├── heartbeat/         # HeartbeatDaemon + CronJob (interval + cron expressions)
│   └── cli/               # Click CLI (chat, run, skill, heartbeat, workspace, init)
├── tests/                 # Test suite (223 tests, 6 skipped)
├── examples/              # 20 runnable examples (most work with Ollama, no API key)
├── skills/                # Bundled skills (shipped with package)
│   └── self-improving-agent/  # .learnings/ capture + workspace promotion
├── docs/                  # Extended documentation
│   └── harness-gap-analysis.md # Unified Claude Code + OpenClaw harness gap status
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

### Heartbeat + CronJob
`HeartbeatDaemon` runs as an asyncio event loop with two layers:

1. **Heartbeat** — interval-based (default 30m), runs on the main agent's session.
   Checks `HEARTBEAT.md` before each tick — skipped if no actionable content.
   `HEARTBEAT_OK` responses suppressed if under `ok_threshold_chars`.

2. **CronJob** — expression-based or interval-string scheduling, each job is an
   independent asyncio task:
   ```python
   CronJob(name="check", schedule="1h", prompt="...", isolated=False)
   CronJob(name="standup", schedule="0 9 * * 1-5", isolated=True)
   ```
   Schedule formats: `"30m"`, `"1h"`, `"2h30m"`, `"45s"`, cron expression (needs `croniter`).
   `isolated=True` creates a fresh `Agent` for the job (no conversation history).

3. **Service install** — `agnoclaw heartbeat install-service` registers a launchd
   LaunchAgent (macOS) or systemd user service (Linux) for always-on operation.

The CLI's `heartbeat start` runs the daemon until Ctrl+C.

### self-improving-agent skill
Bundled skill at `skills/self-improving-agent/SKILL.md`. When activated (user
correction, command failure, capability gap, or pre-compaction), it writes
structured entries to `.learnings/`:

- `LEARNINGS.md` — corrections + patterns (IDs: `LRN-YYYYMMDD-NNN`)
- `ERRORS.md` — command failures + workarounds (`ERR-YYYYMMDD-NNN`)
- `FEATURE_REQUESTS.md` — capability gaps (`FEAT-YYYYMMDD-NNN`)

Stable entries are promoted to workspace files: behavioral rules → `AGENTS.md`,
tool patterns → `TOOLS.md`, persona adjustments → `SOUL.md`, capabilities → `IDENTITY.md`.

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

## Adding a new file tool

`FilesToolkit` in `src/agnoclaw/tools/files.py`:

1. Add the method to the class
2. Register it: `self.register(self.my_new_tool)` in `__init__`
3. Add tests in `tests/test_tools.py`

`multi_edit_file` pattern for atomic multi-replacement:
```python
def multi_edit_file(self, path: str, edits: list) -> str:
    # Phase 1: validate ALL edits — fail fast if any old_string missing or non-unique
    # Phase 2: apply in sequence only after all pass
```

## Gap tracking

`docs/harness-gap-analysis.md` tracks the unified Claude Code + OpenClaw
harness parity status and roadmap. Check it before adding new tools or runtime
contracts to avoid drift from documented priorities.

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
