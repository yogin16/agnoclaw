# Development Guide

## Project structure

```
agnoclaw/
├── src/agnoclaw/          # Main package (src layout)
│   ├── agent.py           # AgentHarness — main entry point
│   ├── capabilities.py    # Immutable capability descriptors and registry
│   ├── capability_execution.py # Governed operation/materialization boundary
│   ├── capability_approval.py # Durable approval coordinator
│   ├── capability_runtime.py # AgentHarness governance/approval/effect composition
│   ├── context_management.py # Immutable context/archive/search domain
│   ├── context_runtime.py # AgentHarness session compaction/rehydration adapter
│   ├── session_commands.py # Elevated/session command routing + ownership
│   ├── config.py          # HarnessConfig + runtime-profile presets
│   ├── workspace.py       # Workspace: context file loading, memory management
│   ├── memory.py          # MemoryManager + LearningMachine builders
│   ├── teams.py           # Pre-built team factories (research, code, data)
│   ├── prompts/           # System prompt assembly (SystemPromptBuilder)
│   ├── tools/             # Default tools (bash, files, web, tasks, subagent)
│   ├── skills/            # Skill registry, parser, loader (SKILL.md format)
│   ├── heartbeat/         # HeartbeatDaemon + CronJob (interval + cron expressions)
│   ├── runtime/           # Lifecycle/effect/approval/store/security kernel contracts
│   │   ├── lifecycle.py   # Pure run states, commands, transitions, and invariants
│   │   ├── operations.py  # Intent, dispatch fencing, settlement, and reconciliation
│   │   ├── reconciliation.py # Bounded evidence-verifying host worker
│   │   ├── dead_letters.py # Scoped host inspection/replay + owner-bound cursors
│   │   ├── store.py       # RuntimeStore contract + SQLite schema-v10 authority
│   │   ├── postgres_store.py # Bounded-pool PostgreSQL schema-v11 authority
│   │   ├── artifacts.py   # Scoped content-addressed artifact contract
│   │   ├── recovery.py    # Exact-owner safe continuation and startup sweeps
│   │   ├── materialization.py # Profile resolution + run-resource factories
│   │   ├── agent_materialization.py # Run-owned Agno Agent construction
│   │   └── builtin_materialization.py # Local built-in tool factory + release scope
│   ├── cli/               # Click CLI + AsyncREPL (chat, run, tui, skill, heartbeat)
│   └── tui/               # v0.3 Textual TUI (optional: agnoclaw[tui])
│       ├── app.py         # AgnoClawApp — main Textual application
│       ├── driver.py      # AgentDriver — async streaming + heartbeat bridge
│       ├── events.py      # Custom Textual Messages
│       ├── screens.py     # Modal screens (skill picker, help)
│       └── widgets/       # ChatLog, InputBar, NotificationPanel, StatusBar, etc.
├── tests/                 # Unit, contract, integration, and example-oriented tests
├── examples/              # Runnable examples (many work with Ollama, no API key)
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
identity → tone → narration → tasks → executing_with_care →
blocked_approaches → tools → security → git → memory → skills →
[plan_mode] → [heartbeat] → [learning] → custom sections →
workspace context → active skill → extra context → datetime
```
Workspace context (AGENTS/SOUL/IDENTITY/USER/MEMORY/TOOLS/BOOT) is
injected near the end so it takes precedence over generic defaults.

### Skill injection (OpenClaw selective injection)
Before each response the agent has a list of skill descriptions in its
system prompt. It can activate at most one skill per turn by referencing
it. The full SKILL.md is only loaded when needed — keeping context lean.

Priority chain: workspace skills > user skills > extra dirs > bundled skills.

### Storage
Agno conversational history and personal/session learning use Agno's `SqliteDb` or
`PostgresDb` through `db=`. Canonical lifecycle, operation, lease, approval, event, and
outbox truth uses `RuntimeStore`; recoverable bytes use `ArtifactStore`; governed
institutional proposals use a learning ledger. Durable/service profiles require these
boundaries explicitly. Quick/legacy may use process-local control state only for their
documented ephemeral guarantees; no singleton may silently become durable authority.

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
   `isolated=True` creates a fresh `AgentHarness` for the job (no conversation history).

3. **Service install** — `agnoclaw heartbeat install-service` registers a launchd
   LaunchAgent (macOS) or systemd user service (Linux) for always-on operation.

The CLI's `heartbeat start` runs the daemon until Ctrl+C.

### Async REPL (v0.3)
`AsyncREPL` in `cli/async_repl.py` replaces the blocking Click REPL as the
default for `agnoclaw chat`. Uses `prompt_toolkit.PromptSession.prompt_async()`
+ `patch_stdout()` so HeartbeatDaemon notifications print above the prompt
without interrupting input. Heartbeat and cron jobs run in-process on the same
asyncio loop. Use `--sync` flag for the legacy blocking REPL.

### TUI Architecture (v0.3)
`AgnoClawApp` in `tui/app.py` is a Textual application. Single-process:
Textual's asyncio event loop hosts the TUI, HeartbeatDaemon, and agent calls.

**Layout:**
```
┌─────────────────────────────────────────────────┐
│ agnoclaw · model · session:abc                  │  HeaderBar
├──────────────────────────────────┬──────────────┤
│  ChatLog (streaming + markdown)  │ NOTIFICATIONS│
├──────────────────────────────────┴──────────────┤
│ > prompt input                                  │  InputBar
├─────────────────────────────────────────────────┤
│ ● heartbeat: 28m │ tools: 6 │ ready             │  StatusBar
└─────────────────────────────────────────────────┘
```

**Key components:**
- `AgentDriver` (`driver.py`) — bridges AgentHarness and Textual via custom Messages
- `ChatLog` (`widgets/chat_log.py`) — VerticalScroll with Static children; during
  streaming, a single Static is `update()`-d in place; on completion, re-rendered as
  Rich Markdown
- Custom Messages (`events.py`) — `StreamChunk`, `StreamDone`, `HeartbeatAlert`, etc.
- HeartbeatDaemon gets its own lightweight agent (haiku) to avoid contention

**Important Textual gotchas:**
- `App._driver` is reserved by Textual (terminal driver) — use `_agent_driver`
- `Static` subclasses must pass initial content to `__init__()`, not `on_mount()`
- Agno's `Agent.arun(stream=True)` returns an async generator directly, not a coroutine

### Packaging
Core `agnoclaw` is provider-neutral and has six direct dependencies: Agno, Pydantic,
Pydantic Settings, PyYAML, HTTPX, and SQLAlchemy (required by the default Agno SQLite
backend). Model SDKs, rich web parsing/search, scheduling,
CLI, TUI, servers, databases, browser automation, and media are optional extras.
`from agnoclaw import AgentHarness` succeeds from the core wheel. CI builds the actual
wheel/sdist, installs a clean core wheel, and enforces dependency, source-size,
artifact-size, import-time, and imported-module budgets through
`scripts/check_package_budget.py`.

### self-improving-agent skill
Bundled skill at `skills/self-improving-agent/SKILL.md`. When explicitly activated, it
can record a user correction, command failure, capability gap, or pre-compaction note as
a structured entry in `.learnings/`:

- `LEARNINGS.md` — corrections + patterns (IDs: `LRN-YYYYMMDD-NNN`)
- `ERRORS.md` — command failures + workarounds (`ERR-YYYYMMDD-NNN`)
- `FEATURE_REQUESTS.md` — capability gaps (`FEAT-YYYYMMDD-NNN`)

The current runtime does not automatically activate this skill on those events. Treat
promotion as a reviewed proposal: behavioral rules → `AGENTS.md`, tool patterns →
`TOOLS.md`, persona adjustments → `SOUL.md`, capabilities → `IDENTITY.md`. The target
provenance, validation, and promotion contract is in `docs/learning.md`.

## Running the full test suite

```bash
uv run pytest tests/ -q
```

Every pytest invocation treats `ResourceWarning`,
`pytest.PytestUnraisableExceptionWarning`, `DeprecationWarning`, and `FutureWarning` as
failures through the checked-in pytest configuration. Tests that intentionally exercise
a deprecated compatibility API must capture its exact warning contract. Tests that
directly construct SQLite stores/databases own those objects; the test resource registry
closes them after assertions. A harness owns only resources it creates. Injected
databases, runtime stores, command executors, workspace adapters, and browser backends
remain caller-owned.

When a resource failure needs an allocation traceback, rerun the smallest reproducer
with tracemalloc before changing cleanup code:

```bash
PYTHONTRACEMALLOC=10 uv run pytest tests/test_agent.py -q --tb=short
```

Do not weaken the warning filters or rely on `gc.collect()` as product cleanup. Add an
ownership regression that proves both created-resource closure and injected-resource
preservation.

Documentation contracts (local links, index coverage, landing-page size, and public API
examples) are executable:

```bash
uv run pytest tests/test_documentation.py -q
```

The durable approval slice is exercised by:

```bash
uv run pytest tests/test_approval_domain.py tests/test_runtime_approvals.py \
  tests/test_capability_approval.py tests/test_agent_capabilities.py -q
```

The finite operation-race matrix runs on SQLite by default. Point it only at an
isolated PostgreSQL test database to execute the identical transaction/effect contract
on both stores:

```bash
uv run pytest tests/test_operation_race_matrix.py -q

AGNOCLAW_TEST_POSTGRES_URL=postgresql://postgres:secret@127.0.0.1:5432/agnoclaw_test \
  uv run pytest tests/test_operation_race_matrix.py -q
```

Use `agnoclaw.testing.StoreFaultScript` for exact rollback sites,
`StoreBarrierScript` for cancellation-versus-commit ordering, and
`DeterministicEffectDriver` for pre-dispatch/before-effect/after-effect ordering. These
controls drive real stores and the real `OperationGateway`; do not replace the matrix
with sleeps or a fake backend.

The bounded live service probe requires the same loopback-only test database. It
creates 10,000 synthetic terminal rows under one random prefix, measures baseline and
noisy-neighbor p50/p95/p99, proves exact-owner reads, saturates a 2+2 bounded pool, runs
the process admission fairness oracle, and removes every prefixed row:

```bash
uv run python scripts/benchmark_postgres_runtime.py \
  --dsn postgresql://postgres:secret@127.0.0.1:5432/agnoclaw_test
```

The command refuses non-loopback hosts and databases whose name lacks `test`. Its
defaults fail at p99 above 25 ms or p95 slowdown above 4x. This is a repeatable local/CI
regression gate, not primary-failover, production-memory, or RPO/RTO certification.

Rehearse native PostgreSQL dump/restore only against one disposable, loopback test
container. The target database is dropped before and after the probe, so its name must
contain both `restore` and `test`, and destructive authority must be explicit:

```bash
uv run python scripts/postgres_backup_restore_probe.py \
  --source-dsn postgresql://postgres:secret@127.0.0.1:5432/agnoclaw_test \
  --target-dsn postgresql://postgres:secret@127.0.0.1:5432/agnoclaw_restore_test \
  --container agnoclaw-postgres-test \
  --allow-target-reset
```

The command compares every runtime row plus columns, indexes, constraints, and
sequences; checks ordered events and idempotency after restore; and attempts exact
dump, target, and marker cleanup even when the primary operation fails. It intentionally
advances native source sequences while deleting its marker rows. This is a local/CI
recovery regression gate—not a production backup system or a production RPO/RTO claim.

Run the bounded single-primary outage drill last because it deliberately stops the
exact container. It verifies the container/port/test DSN first, requires a finite typed
store failure while stopped, heals the container in cleanup, proves old/fresh pool
continuity and the two-connection bound, and removes its marker:

```bash
uv run python scripts/postgres_restart_probe.py \
  --dsn postgresql://postgres:secret@127.0.0.1:5432/agnoclaw_test \
  --container agnoclaw-postgres-test
```

This proves a local single-primary stop/start contract, not replica promotion, network
partition/split-brain handling, production resource budgets, or production RPO/RTO.

Run the independent two-node gate only on a Docker host where creating and removing a
disposable PostgreSQL topology is acceptable:

```bash
uv run python scripts/postgres_failover_probe.py \
  --allow-topology-create \
  --image postgres:17-alpine \
  --timeout 90 \
  --outage-timeout 1
```

The probe owns only UUID-named/labeled resources it successfully creates. It verifies
read-only standby behavior, forces measurable replay lag, waits for the last
acknowledged LSN, stops the old primary, proves both runtime and learning multi-host
read-write pools have no writable target, promotes, and proves existing/fresh pool
continuity. The learning ledger retains an evaluated candidate and commits a
post-promotion transition with contiguous events. The probe never restarts the old
primary. The gate therefore certifies a fenced, zero-observed-loss planned promotion
path for its asynchronous local topology; deployment-specific external fencing,
automatic failover, unplanned-loss RPO, split-brain injection, rewind/rejoin, and
production load/RTO remain separate release gates.

The synchronous companion proves that a disconnected required standby cannot create a
false application acknowledgement and that an already acknowledged manifest survives
an abrupt primary process loss:

```bash
uv run python scripts/postgres_synchronous_failover_probe.py \
  --allow-topology-create \
  --image postgres:17-alpine \
  --timeout 90 \
  --outage-timeout 1 \
  --blocked-observation 0.5
```

It uses `remote_apply`, disconnects/reconnects only the exact owned replication network,
terminates only the exact named replication sender after partition, asserts the actual
RuntimeStore connection policy, and later sends `SIGKILL` only to the exact owned
primary. The observed zero-loss result applies to acknowledged runtime state/events
under this one-standby/single-fault local topology. It is not a general zero-RPO,
availability, automatic election, or fencing claim. The default tag path refreshes and
resolves the image; for an already certified offline repeat, `--image` may instead be
the exact previously recorded `postgres@sha256:<digest>`, which must exist locally and
match byte-for-byte.

The round-trip companion proves that each fenced former writer can be rewound, rejoined
as a read-only synchronous standby, and later returned to the writer role without
losing acknowledged RuntimeStore state:

```bash
uv run python scripts/postgres_role_rotation_probe.py \
  --allow-topology-create \
  --image postgres:17-alpine \
  --timeout 90 \
  --outage-timeout 1
```

It requires checksums and `full_page_writes`, pins a checkpoint/replay boundary before
both promotions, runs `pg_rewind` only against exact stopped owned volumes, and writes
one password-free recovery connection string using a mode-0600 temporary password
file. The first cutover is an abrupt `SIGKILL`; the return cutover cleanly stops the
writer and requires `pg_rewind --no-ensure-shutdown`. A failed rewind target must be
discarded and rebuilt from a fresh base backup. This is local two-node recovery
evidence, not automatic election, external-fence, split-brain, multi-fault, production
endpoint-discovery, or production RPO/RTO certification.

The dual-writer companion intentionally promotes the standby without stopping the old
primary, proves unguarded RuntimeStore rows diverge, then certifies the optional
external-authority guard against the first-party etcd adapter and official immutable
etcd 3.6.14 image:

```bash
uv run python scripts/postgres_split_brain_authority_probe.py \
  --allow-topology-create \
  --image postgres:17-alpine \
  --etcd-image quay.io/coreos/etcd@sha256:dfd3941bf6ced5fdb700f9b2d98b22b7bca7ceee13aec16224f93ff30d9a59c4 \
  --timeout 90 \
  --outage-timeout 1
```

It requires stale-writer, stopped-authority, and over-lease transaction denial;
commit-boundary etcd revision change must roll back; only the named writer may commit;
pools stay at one connection; the exact etcd endpoint must recover; and all seven
resources must be removed. Run `scripts/etcd_writer_authority_probe.py` independently
to isolate gateway/revision/revocation/natural-expiry/loss behavior. This proves
application-level containment only.

The secure control-plane companion uses an owned three-voter topology, unique member
and peer certificates, client and peer certificate verification, TLS 1.2 minimum,
enabled RBAC, least-privilege exact-key users, endpoint-bound JSON-gateway tokens, and
real quorum loss/recovery:

```bash
uv run pytest tests/test_etcd_secure_quorum_probe.py -q
uv run python scripts/etcd_secure_quorum_probe.py \
  --allow-topology-create \
  --image quay.io/coreos/etcd@sha256:dfd3941bf6ced5fdb700f9b2d98b22b7bca7ceee13aec16224f93ff30d9a59c4 \
  --timeout 90 \
  --lease-ttl 15
```

It must reject a client without a certificate; prove the controller can read/write
only the authority key; return exact 403/code-7 denials for reader writes, adjacent-key
reads, and an unprivileged user; tolerate one stopped voter; fail closed within the
authority deadline after majority loss; recover on a different member endpoint; and
advance the fence. It also exercises public `EtcdGatewayCredentials`, whose token
exchange and single 401 refresh stay within one total adapter deadline. All three
containers, the exact network, and temporary certificate workspace must be removed.

This local gate is not a production deployment certificate: it uses ephemeral data and
does not inject an isolated network partition. A production claim still requires the
external controller/election and record-transfer owner, durable multi-AZ topology,
backup/restore and endpoint discovery, partition/latency/clock/certificate-rotation
chaos, production RPO/RTO evidence, host-pause injection, and watchdog/STONITH that
fences arbitrary clients—not only agnoclaw.

With the release coverage floor:

```bash
uv run pytest tests/ -q -m "not integration" --tb=short \
  --cov=agnoclaw --cov-report=term-missing --cov-fail-under=80
```

Blocking static gates:

```bash
uv run ruff check src/ tests/ scripts/
uv run mypy src/agnoclaw/ --ignore-missing-imports
```

The longer deterministic context certification is intentionally outside the fast
`not integration` loop:

```bash
uv run python scripts/long_run_continuity_probe.py \
  --turns 100 --restart-turns 30,70 --max-context-tokens 1800
```

Mypy checks unannotated function bodies. Ruff's full configured rule set is blocking;
do not substitute the former `F,B,E9` subset for release evidence.

A specific module:
```bash
uv run pytest tests/test_workspace.py -v
```

## Agno compatibility notes

The development lock is Agno 2.9.0. Agno 2.6.4 remains the minimum/legacy contract
lane, and 3.0.0a1 is a non-production preview as of 2026-08-10.
Use the compatibility gates in `docs/evaluation.md`; do not infer compatibility from
`agno>=2.6.4` or hide drift behind untested `hasattr` branches.

- `from agno.run.agent import RunOutput, RunEvent` — NOT `agno.run.response`
- `Agent(id=...)` — NOT `agent_id=`
- Storage via `db=` — NOT `storage=`
- `LearningMachine` is a `@dataclass` in the supported stable runtimes — no global `mode=`. Use per-store
  configs: `EntityMemoryConfig`, `LearnedKnowledgeConfig`, `DecisionLogConfig`
  from `agno.learn.config`
- Learned Knowledge requires `Knowledge` plus a vector DB. Pass it as
  `learning_knowledge=`; institutional learning now fails construction with
  `LEARNING_KNOWLEDGE_REQUIRED` when the prerequisite is absent. See
  `docs/learning.md` for the remaining scope/evaluation limitations.
- The supported stable `LearningMachine` exposes `.curator` but no portable
  `.optimize_memories()` contract; use the compatibility layer and explicit maintenance.

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

`docs/harness-gap-analysis.md` tracks implementation-aligned maturity and defects.
`docs/world-class-harness.md` owns product direction and roadmap;
`docs/architecture.md` owns target invariants; `docs/evaluation.md` owns completion
gates. Check them before adding tools or runtime contracts.

## Adding a new workspace file type

1. Add to `WORKSPACE_FILES` dict in `workspace.py`
2. Add to the `context_files()` loading order if it should be injected
3. Add a corresponding test in `tests/test_workspace.py`
4. Update `workspace show` in `cli/main.py` if it should be displayed

## Release process

The authoritative 0.12 gates and owners are in
[`docs/releases/v0.12.0-plan.md`](docs/releases/v0.12.0-plan.md); progress evidence is
recorded separately in [`docs/releases/v0.12.0-progress.md`](docs/releases/v0.12.0-progress.md).

1. Freeze the release candidate only after every required workstream is complete or an
   explicit, documented scope decision removes it.
2. Prove migration preflight, backup/restore, old-writer fencing, cutover, verification,
   and rollback against representative persisted data.
3. Pass the Python/Agno matrix, exact advertised provider and service integrations,
   PostgreSQL recovery/chaos, security, soak, and hosted-platform gates.
4. Pass full warning-clean tests/coverage, Ruff, mypy, documentation contracts, package
   budgets, Twine, clean installs of the exact wheel and sdist, and a
   resource-instrumented soak. Do not suppress delayed connection, event-loop, socket,
   process, browser, or file cleanup failures.
5. Update the version, changelog, migration guide, compatibility matrix, and release
   notes from archived evidence; rebuild and recheck the final artifacts.
6. Obtain release approval, then create and publish the signed tag through the protected
   release workflow. Never infer publish authorization from local release preparation.
