# agnoclaw

**A small, embeddable, model-agnostic agent harness built on Agno.**

`agnoclaw` combines Agno's model portability with an opinionated workspace, Agent
Skills, policy and permission hooks, a transactional run lifecycle, scoped artifacts,
and governed learning. It is a Python library first: no required gateway, editor, or
hosted control plane.

> **0.12 development status:** security, lifecycle, effects, artifacts, approvals,
> governed-learning, and durable-scheduler foundations are implemented and tested.
> Schema-v12 SQLite/PostgreSQL scheduling has deterministic occurrences, leased/fenced
> attempts, lifecycle reattachment, retries, misfires, overlap policy, and learning
> consent. Artifact-first context, governed spill, declared capabilities, deferred MCP 2.0, and read-only Agno context
> queries work; raw extensions are rejected by `start()`. Authenticated AgentOS remote
> lifecycle parity is implemented; JWT/proxy certification and live-provider proof remain
> open. Improvement evidence, exact pre-model recovery, fresh-process subjects, and a strict no-network Docker evaluation profile
> work; VM/provider-egress certification and arbitrary mid-model/tool-stack
> continuation remain open. Follow the [live implementation record](docs/releases/v0.12.0-progress.md) for evidence and limitations.

## Install

Python 3.11–3.14 is supported. The core is provider-neutral; install only the extras you use.

```bash
pip install agnoclaw                    # core; strings or an AgnoModelFactory
pip install "agnoclaw[anthropic]"       # recommended Claude setup
pip install "agnoclaw[local]"           # local Ollama
pip install "agnoclaw[cli]"             # CLI and async REPL
pip install "agnoclaw[tui]"             # Textual TUI
pip install "agnoclaw[postgres]"        # PostgreSQL runtime stores
pip install "agnoclaw[mcp]"             # MCP 2.0 deferred tool ingress
pip install "agnoclaw[server]"          # AgentOS + remote lifecycle HTTP edge
pip install "agnoclaw[full]"            # Claude + web + scheduler + TUI
```

## Run an agent

The default model is Claude, so this example requires `ANTHROPIC_API_KEY` and the `anthropic` extra.

```python
from agnoclaw import AgentHarness

harness = AgentHarness()
result = harness.run("Summarize the files in this directory")
print(result.content)
```

Use any Agno-supported model with `"provider:model_id"`:

```python
harness = AgentHarness("openai:gpt-4o")
harness = AgentHarness("google:gemini-2.0-flash")
harness = AgentHarness("ollama:qwen3:8b")  # local; Ollama must be running
```

## Choose runtime semantics

The 0.12 preview exposes `quick`, `durable`, and `service` profiles; the no-argument
default remains `legacy` only for migration compatibility. Start new short-running
work with `HarnessConfig.quick()` or `AgentHarness(profile="quick")`. Durable/service
construction fails early unless its required runtime and artifact stores are explicit;
service additionally requires PostgreSQL runtime and Agno storage. Profile defaults,
prerequisites, and current certification limits are in the
[configuration reference](docs/configuration.md#runtime-profiles).

For streaming compatibility:

```python
async for event in harness.arun(
    "Analyze this repository",
    stream=True,
    stream_events=True,
):
    print(event)
```

Read the [getting-started tutorial](docs/getting-started.md) for provider setup, trusted execution context, sessions, cleanup, and expected failure behavior.

## Control a run

`start()` returns the preview lifecycle facade without waiting for completion:

```python
run = await harness.start(
    "Investigate the incident",
    session_id="incident-42",
    idempotency_key="incident-42:v1",
)
async for event in run.events():
    print(event.sequence, event.event_type)
result = await run.wait()
await harness.aclose(policy="drain")
```

The lifecycle persists intent, state, terminal results, and content-minimized normalized trajectory through a `RuntimeStore`; ambiguous outcomes are never blindly retried. Recovery continues from settled pre-model/result/evidence boundaries; exact-owner startup and reconciliation scans do not promise general mid-model restart. See [run lifecycle](docs/runtime-lifecycle.md),
[operations and recovery](docs/operations-and-recovery.md), and [artifacts](docs/artifacts.md).

With a durable artifact store, `start(..., persist_output=True)` uses bounded provider
streaming and `run.output()` replays authorized text segments by cursor. Authenticated
remote lifecycle starts default this option to true. The final `wait()` result remains
authoritative; output replay does not pretend an interrupted provider call is safe to
resume.

## Embed with trusted identity

Resolve identity in the host, then pass one immutable context to the harness:

```python
from agnoclaw import AgentHarness, ExecutionContext

harness = AgentHarness(
    workspace_dir="/srv/agent/workspace",
    permission_mode="default",
    permission_require_approver=True,
)
context = ExecutionContext.create(
    tenant_id="acme",
    user_id="user-42",
    session_id="case-7",
    workspace_id="support",
    roles=["analyst"],
    scopes=["agents.run"],
)
result = await harness.arun("Continue the case", context=context)
```

The effect-safe model/capability slice can overlap through isolated Agno Agents. Local
built-ins get fresh run-owned tools but remain typed single-flight with custom/streaming paths. Read the
[embedding guide](docs/embedding/README.md) before service deployment.

## Use Agno learning safely

Learning is intent- and scope-driven. Personal/session stores can write directly with
explicit consent; institutional observations become governed candidates first.

```python
from agnoclaw import AgentHarness, LearningProfile

harness = AgentHarness(
    learning=LearningProfile.personal_and_session(
        user_profile="always",
        user_memory="agentic",
        session_context="always",
        max_updates_per_run=5,
    )
)
result = await harness.arun(
    "Remember that I prefer concise incident summaries",
    context=context,
    learning_consent=True,
)
```

Scoped read/replace/forget administration is post-verified. Candidate capture,
evaluation, promotion/rollback, unknown-effect discovery, and evidence-bound
reconciliation plus an owner-scoped leased/fenced/checkpointed maintenance worker are implemented on SQLite and PostgreSQL.
Automatic promotion remains off until custom backend observers,
production worker certification, deletion proof, and model-backed no-learning benefit pass. Start with
[learning](docs/learning.md), [administration](docs/learning-administration.md), and [governed candidates](docs/learning-candidates.md).

## Skills, workspace, tools, and backends

- Workspace instructions and memory are plain Markdown with explicit size limits and
  precedence. See [workspace files](docs/workspace.md).
- Agent Skills are loaded progressively and carry trust/tool restrictions. See the
  [SKILL.md reference](docs/skills.md).
- Built-in shell, file, skill, and browser execution share one injectable runtime
  backend. See [runtime backends](docs/embedding/workspace-backends.md).
- Capabilities use immutable descriptors and an operation-gated executor. Specs passed
  through `AgentHarness(capabilities=[...])` receive version-pinned Agno binding,
  policy, durable approval-before-effect, active-lease fencing, and governed replay.
  Raw `tools=` are normalized as opaque, remain serialized only on named-legacy
  `run/arun`, and are rejected by `start()` and explicit-profile convenience calls
  until converted to explicit specs. See
  [capabilities](docs/capabilities.md).

```python
result = await harness.arun("Review the authentication module", skill="code-review")
print(result.content)
```

## CLI

```bash
agnoclaw init
agnoclaw chat
agnoclaw run "Review src/" --skill code-review
agnoclaw tui
agnoclaw heartbeat start
agnoclaw migrate 0.12 service --help
agnoclaw schedule worker --runtime-db ~/.agnoclaw/runtime.db
```

Install the relevant extra first. The [CLI reference](docs/cli.md) records command groups,
automation limits, and current durability boundaries; the [configuration
reference](docs/configuration.md) covers TOML, environment variables, and safe service defaults.
Use [durable scheduling](docs/durable-scheduling.md) for unattended jobs; JSON remains
compatibility-only. The PostgreSQL migration lifecycle needs `cli,postgres,scheduler`; production certification remains open.

## Why this shape

- **Tiny public grammar:** `run`, `arun`, `start`, `get_run`, `session`, and typed run
  controls instead of a second orchestration framework.
- **Short and long work, one kernel:** quick calls avoid unnecessary machinery while
  controllable work gains identity, intent, state, events, effects, and artifacts.
- **Truth before retries:** external effects have durable intent and explicit unknown
  outcomes; exactly-once external execution is never implied.
- **Learning is a governed data plane:** consent, scope, provenance, candidates,
  evaluation, reversible promotion, and deletion are separate concerns.
- **Progressive power:** local defaults stay approachable; service guarantees are
  enabled only with the stores, policy, and evidence they require.

The [architecture](docs/architecture.md) and [declared child-run contract](docs/child-runs.md) cover host, model-visible
`DeclaredChildTemplate`, and authenticated remote delegation. Competitive decisions
are in [World-class strategy](docs/world-class-harness.md), the [Agno release audit](docs/agno-release-practices.md),
and the [Lilian Weng research audit](docs/lilian-weng-harness-audit.md).

## Compatibility and quality

The current development lock is Agno 2.9.0; Agno 2.6.4 is the legacy lane and Agno
3.0.0a1 is a quarantined preview. Schema-v12 is current; retained cross-Python/preview
evidence predates its durable-scheduler tables and must rerun. Real-service, chaos,
hosted-CI, soak, migration, and release-candidate gates remain separately tracked—unit
count alone is not a production claim.

- [Compatibility matrix](docs/compatibility.md)
- [Evaluation and release gates](docs/evaluation.md)
- [Migration to 0.12](docs/migration-0.12.md)
- [Changelog](CHANGELOG.md)

## Documentation

Use the [documentation index](docs/README.md) for tutorials, how-to guides, reference,
explanations, operations, research, and release planning. `HarnessAgent`
remains a backward-compatible alias for `AgentHarness`.

## Development

```bash
uv sync --extra dev
uv run ruff check src/ tests/ scripts/
```
See [CONTRIBUTING.md](CONTRIBUTING.md), [support](SUPPORT.md), [security](SECURITY.md),
and [DEVELOPMENT.md](DEVELOPMENT.md).

## License

MIT — fork it, inspect it, and embed it.
