# Migrating to 0.12

Status: development preview; local SQLite/JSON cutover is implemented, final production
release gates remain open

Last updated: 2026-08-24

The migration tool exposes `check`, `plan`, `apply`, `verify`, `cutover`, and `rollback`
for certified local legacy shapes. The PostgreSQL/service path now has a public,
digest-bound control-plane schema for credential references, schedule maps, backup
receipts, endpoint/table evidence, and reviewed plans; a streamed read-only scanner now
proves exact source/target evidence, learning-scope coverage, schedule-map coverage, and
in-flight state before the only public plan factory will proceed. A deterministic,
bounded-memory transformation preview then rechecks the exact plan and source evidence
without writing either database. See the normative [service
runbook](migration-service-0.12.md). Non-interactive service `check`, `plan`, `preview`,
`apply`, `verify`, `cutover`, and `rollback` commands now implement the provenance-owned
lifecycle with stable JSON and semantic exits. The service workflow remains a
development preview until its production failure/security/scale matrices pass.
Artifacts/keys, online reverse migration, production RPO/RTO, and complete service
certification remain open.

## Before upgrading

1. Record the current agnoclaw, Agno, Python, provider, database, and optional-extra
   versions.
2. Back up Agno session/memory tables, workspace files, `.learnings`, schedules, and
   artifacts. Test that the backup can be read.
3. Inventory every call that shares an `AgentHarness`, every `enable_learning` flag,
   session-listing adapter, AgentOS identity source, sandbox admin route, plugin, pack,
   custom tool, MCP server, and direct `underlying_agent` access.
4. Do not change the package, runner, store schema, learning namespace, and scheduler
   writer in one unreviewed step.

## Immediate source/config changes

### Select a runtime profile

The development preview now implements the `quick`, `durable`, and `service` profile
grammar. The implicit default remains `legacy` during migration; it preserves existing
permission/storage behavior and serializes opaque resources. New code should select a
profile explicitly:

```python
short = AgentHarness(model, config=HarnessConfig.quick())
durable = AgentHarness(
    model,
    config=HarnessConfig.durable(),
    include_default_tools=False,
    runtime_store=runtime_store,
    artifact_store=artifact_store,
)
```

`durable` and `service` fail before workspace/model construction when required stores
are absent. `service` additionally requires `PostgresRuntimeStore` and Agno PostgreSQL
storage. Opaque caller tools/dependencies/session values fail explicit durable/service
classification; convert effectful tools to `CapabilitySpec` factories or retain the
temporary `HarnessConfig.legacy()` path while migrating. `HarnessConfig.local_safe()`
is a posture preset for quick/durable, not another profile. See
[Configuration](configuration.md#runtime-profiles).

Direct `underlying_agent` access now fails with
`UNDERLYING_AGENT_PROFILE_UNSUPPORTED` under `durable` and `service`, because raw Agno
calls cannot preserve lifecycle, effect, or recovery authority. Replace it with the
narrow harness accessors or lifecycle API. The deprecated escape hatch remains only
under `quick`/`legacy` for the 0.12 migration window and is scheduled for removal in
1.0.

### First-party CLI, chat/TUI, heartbeat, and scheduler routing

The selected profile now changes how non-streaming first-party process adapters enter
the harness:

- `agnoclaw run`, heartbeat, and local schedules use `start()` plus `wait()` under
  explicit `quick`/`durable`/`service` profiles;
- the same paths retain direct compatibility execution only under named `legacy`;
- async chat and TUI messages use the lifecycle model operation plus a bounded live
  presentation stream under explicit profiles and retain raw streaming under named
  `legacy`;
- `chat --sync` uses the same lifecycle through a reusable harness-owned event-loop
  coordinator; raw-tool child harnesses remain named-legacy-only and explicit profiles
  use declared child runs.

If automation previously parsed token-by-token output from `agnoclaw run`, keep it on
named `legacy` temporarily or migrate to `HarnessRun.events()`/the AgentOS lifecycle
client. Durable/service CLI `run` prints the final settled content rather than the raw
provider stream. Treat this as a deliberate semantics upgrade, not formatting parity.

The async chat/TUI presentation attachment remains process-local and single-consumer. A
slow or closed display detaches instead of stalling or cancelling the logical run; the
client waits for and reconciles to authoritative final content. Provider text consumed
inside this lifecycle path is now also stored as bounded, scoped segments. Reattach
with `HarnessRun.output(after=cursor)` locally or through `RemoteHarnessRun.output()`;
use `HarnessRun.events()` separately for gap-free content-minimized lifecycle events.
Embedders opt in with `start(..., persist_output=True)`, while authenticated lifecycle
HTTP starts default it to true. Set `persist_output=False` for structured-output runs;
segmented replay currently preserves plain text only and fails closed rather than
changing a typed terminal result into text.

Segments contain at most 8,192 characters or 32 consumed provider deltas; remote pages
contain at most 50 segments. Normal completion and cancellation flush the partial
segment. Abrupt process loss can lose the one uncommitted in-memory segment and still
leaves a post-dispatch provider operation ambiguous. Replay is durable presentation,
not permission to redispatch or continue an unknown provider call.

New unattended jobs should use `--runtime-db` or
`RuntimeSchedulerBackend(RuntimeStore)`. Schema v12 persists job revisions, deterministic
occurrences/attempts, database-clock due state, leases/fences, lifecycle bindings,
retries, jitter, misfires, overlap policy, and bounded concurrency-group backlog. The
default JSON file remains compatibility-only and has no interprocess ownership.

Scheduler records now carry `runtime_run_id` directly. Preserve it during export and
use it for trusted reattachment/recovery. The `detached` status means supervision ended
after lifecycle admission; it does not prove process survival. Ambiguous store or
lifecycle acknowledgement reclaims the same attempt, while a known retryable terminal
failure creates a later attempt. Failure history stores a stable error code instead of
raw exception text. See [Durable scheduling](durable-scheduling.md).

### Concurrent calls

Current isolation permits overlapping non-streaming runs with the effect-safe model/
registered-capability slice, immutable dependency/session seeds, and run-owned Agno
compression/session-summary managers. Supported quick/legacy host-local built-ins now
receive fresh tools and Agents with explicit release, but their effects remain one
active run per harness. Optional/custom backends, packs/plugins/caller-supplied MCP/context
providers, mutable opaque inputs, hooks/policies/exporters, skills, and streams do the same:

```python
try:
    result = await harness.arun(message)
except HarnessError as exc:
    if exc.code == "HARNESS_RUN_IN_PROGRESS":
        # Retry later, or route to another independently constructed harness.
        ...
```

Always exhaust or `close()`/`aclose()` a stream. Same-session lifecycle lanes are
implemented. First-party functions constructed by `get_default_tools()` now carry a
versioned effect declaration and cross operation settlement when invoked by `start()`;
their direct `run()`/`arun()` behavior is unchanged. Custom/plugin/context-provider,
administrative and nested child capability ingress, untrusted job administration, and optional/custom backend
materialization remain T3/T6/T9/T11 work. Outer durable/service heartbeat and
scheduler requests enter lifecycle start/wait, and schema-v12 RuntimeStore workers add
distributed PostgreSQL leasing/fencing. JSON scheduling does not. Plugin and pack registrations should move from raw `tools` to
`PluginManifest.capabilities` or `[provides].capabilities`; these use the ordinary
`CapabilitySpec` contract. Raw plugin/pack/context-provider tools and caller-supplied
mutable MCP toolkits now fail `start()` before run creation with
`EXTENSION_TOOL_LIFECYCLE_UNSUPPORTED`. Do not build an undocumented global mutex into
application persistence.

### MCP 2.0 and deferred tools

Install `agnoclaw[mcp]`; the supported SDK range changed to `mcp>=2,<3`. Configured
servers no longer register one changing Agno function per remote tool. The model sees
only `search_mcp_tools` and `call_mcp_tool`, and every call must carry the schema digest
returned by search. Code that invoked a generated remote wrapper directly must migrate
to the search/call pair or use the MCP SDK as an explicitly host-owned client.

Remote `url` entries default to Streamable HTTP. For compatibility, an omitted
transport on a URL path ending in `/sse` infers legacy SSE; an explicit transport
always wins. Async applications must use `await toolkit.aconnect()` and
`await toolkit.aclose()`; `connect()` no longer attempts to drive a running loop.
Configured endpoints pass the harness HTTPS/private-host/allowlist posture before tool
construction. Add an explicit `network_allowed_hosts` entry for each service and verify
the worker's real egress policy.

Static `headers` remain available as a bridge, but 0.12 does not provide OAuth/CIMD or
token-refresh flows. Move committed bearer tokens into a trusted broker before cutover.
MCP resources, prompts, subscriptions, Apps, Tasks, and MRTR are not exposed yet. See
[MCP client and governed tool ingress](mcp.md).

### Skills, packs, workspace hooks, and outbound URLs

0.12 tightens several host-execution boundaries. Apply these changes deliberately:

1. Reinstall Hub skills so they live below the `.community/` quarantine and carry
   source/digest provenance. Existing community directories are rediscovered after a
   restart but never inherit local trust.
2. Replace any automation that expected `hub install` or `skill inspect` to render a
   skill. Both are now parse-only; explicitly activate a reviewed skill when execution
   is intended. Community inline commands remain literal until promoted to local trust.
3. Re-trust every reviewed pack after upgrade. Pack trust now lives outside the payload
   and binds the canonical installed identity to the exact pack digest. Delete any
   legacy in-pack `.agnoclaw-trust.json`; it no longer grants authority. Packs with
   Python registrations always require host trust, and untrusted pack skills are
   community content.
4. If workspace hooks are required, opt in through
   `AgentHarness(allow_workspace_hooks=True)` and explicitly list inherited variables
   in `workspace_hook_env_allowlist`. A repository configuration cannot self-enable
   hooks; commands use `shell=False` and a minimal environment.
5. Review every network allowlist. URLs now require HTTPS, reject credentials and
   ambiguous/legacy or private addresses, resolve DNS before admission, and revalidate
   redirects. `web_fetch` and ClawHub pin connections to the admitted address; the
   default browser rechecks every request. Custom browser backends must enforce the
   supplied policy. Search SDK and MCP transports still require a deployment egress
   proxy/firewall when DNS rebinding or route compromise is in scope.

The dependency lock was refreshed and the all-extras export has zero known
vulnerabilities under the pinned CI `pip-audit` gate. Reproduce the supported
environment with `uv sync --frozen --all-extras`; do not substitute an unconstrained
dependency upgrade during migration.

### Automatic context compaction

Automatic replacement is new, explicit, and disabled by default. Migrate a host-local
long session only after injecting recoverable artifact storage and the real provider
budget:

```python
harness = AgentHarness(
    artifact_store=artifact_store,
    max_context_tokens=120_000,
    auto_compact_context=True,
)
```

The next `arun()` at 90% performs archive-first replacement and must release below 70%;
the 97% emergency path makes no additional model call. An Agno-classified provider
overflow gets one exact model-invocation retry under the same emergency path, but only
for non-streaming work with no observed tool call. Same-session runs and open streams
block replacement retryably. Use `arun()` in async applications: sync `run()`
at a compaction boundary inside an event loop raises
`CONTEXT_AUTOMATION_ASYNC_REQUIRED`. This controller is process-local and is not a
distributed compaction lease. Keep it off in
multi-process unattended deployments until those release gates pass.

### Large governed results

Lifecycle capabilities and first-party tools can now opt into lossless model-context
spill:

```python
harness = AgentHarness(
    capabilities=[report_capability],
    runtime_store=runtime_store,
    artifact_store=artifact_store,
    max_inline_output_chars=8_192,
)
```

Start with explicitly registered `CapabilitySpec` values and Agnoclaw-owned tools
invoked by `start()`. Small values stay unchanged; large values become bounded envelopes
after governed redaction and atomic artifact settlement. The model pages verified content with the reserved
`read_spilled_output` capability. A later run may read the artifact only inside the
same trusted tenant/user/session; a different session is denied. Existing caller
capabilities using that reserved name must be renamed before enabling the setting.

Do not remove application truncation/externalization around direct compatibility tools,
context-provider and caller-supplied raw MCP functions, or outer-model output yet;
those paths are not covered by this slice. Configured MCP calls use the first-party
spill path during lifecycle execution. Plugin/pack capabilities are covered only
after migration to `CapabilitySpec`; raw registrations cannot enter `start()`.
Review context budgets and ArtifactStore capacity/retention before rollout. See
[Durable artifacts](artifacts.md#model-context-spill).

### Preview lifecycle API

`AgentHarness.start()` and `get_run()` now expose the preview `HarnessRun` contract over
the transactional runtime ledger. Existing `run/arun` behavior is unchanged:

```python
run = await harness.start("Investigate", idempotency_key="incident-42:v1")
result = await run.wait()
```

An injected `SQLiteRuntimeStore` persists lifecycle/event/result records. Adding a
shared `LocalArtifactStore` now permits verified completion of an already-successful
outer model operation after process restart, without redispatch. A settled
pre-model request checkpoint can continue explicitly when provider dispatch has
not begun; owner/session, full authority context, harness spec, request digest,
operation intent, and artifact bytes are verified first. This includes raw request
context, so use suitable ArtifactStore encryption and retention.

Custom provider transports should migrate from a directly injected `Model` to
`AgnoModelFactory`. The declared model/provider identity and canonical implementation
digest then bind the persisted request. A replacement host can reconstruct the same
factory for the certified outer-operation path; implementation drift terminates with
`RUN_RECOVERY_SPEC_MISMATCH` before a provider call. The exact governed Agno 2.9
multi-step tool/approval checkpoint envelope is also factory-backed; unsupported
raw/nested/parallel/streaming/parser/output-model paths remain conservative.

On Agno 2.9, one additional path is now certified: an agnoclaw-materialized native
Agent with a fresh public-factory model using non-streaming `start()`, both durable
databases, a shared
ArtifactStore, and governed registered capabilities persists each provider call
separately. Recovery after the tool result requires Agno's exact `tool-batch`
checkpoint; an earlier approval wait reconstructs from the settled first-provider
artifact and exact request/decision/grant. Raw/custom/nested/parallel tools,
streaming/persisted presentation and parser/output models do
not enter that path. Agno 2.6.4 keeps conservative recovery.

This slice adds no runtime-schema migration, but preexisting in-flight runs do not gain
missing provider/checkpoint evidence retroactively. During a rolling upgrade, drain or
leave them under their old conservative recovery policy; do not fabricate artifacts or
rewrite operation rows. All processes that may recover a new run must use identical
harness/capability policy and a shared ArtifactStore. Durable prompt reconstruction now
anchors time to the persisted run creation time and omits process-local sandbox text on
this no-built-in-tool path, preventing harmless host/path drift from changing the
provider request digest.

Do not migrate production job ownership until live-provider reconciliation,
recovery soak/failover, and the required deployment-specific continuation gates pass. A
host may run bounded exact-owner startup sweeps with
`recover_pending_runs()`; this never enumerates tenants automatically. See [Run lifecycle and RuntimeStore](runtime-lifecycle.md) and
[Durable artifacts](artifacts.md).

### Declared child output contracts and synthesis

The declared-child record was introduced in runtime-store schema v11 and is retained in
schema v12, but its embedded
child-spec document advances additively from `1.0` to `1.1`. New readers accept both;
new declarations emit `1.1` and include `result_schema` (including explicit `null` when
the host does not require one). No SQL migration is needed for this field because the
complete versioned declaration is already stored as canonical JSON and bound by its
digest.

Add `result_schema=` when a child is expected to produce a bounded structured object:

```python
child = await parent.child(
    child_harness,
    "Return the reviewed incident classification.",
    context=trusted_context,
    delegation_id="classify-v2",
    purpose_code="classification",
    result_schema={
        "type": "object",
        "properties": {"severity": {"enum": ["low", "medium", "high"]}},
        "required": ["severity"],
        "additionalProperties": False,
    },
)
```

Configure Agno native structured output on `child_harness` where supported, but do not
treat provider parsing alone as the durable contract. Agnoclaw revalidates normalized
content after successful operation settlement and reported-usage assessment, before
child completion, and repeats that check during known-success recovery. Existing 1.0
children remain schema-less. A mismatch now produces a failed child with
`CHILD_OUTPUT_SCHEMA_MISMATCH` while retaining any authorized operation-result
artifact; applications that previously assumed every successful provider response
became a completed child must handle this state explicitly.

Hosts may replace a manual `synthesis_payload()` plus ungoverned model call with
`await parent.synthesize_children(...)`. The new helper requires terminal inputs,
defaults to all-success, bounds inline and total JSON evidence, frames results as
untrusted data, and starts synthesis as a normal declared child. Partial failures need
`allow_partial_failures=True`. Large result pointers do not grant model access: provide
an explicitly governed, owner-checking reader capability if the synthesis model must
dereference them.

Model-directed and remote delegation must migrate from raw `spawn_subagent` or raw Agno
Team presets to one host-owned `DeclaredChildTemplate`. Explicit profiles omit the raw
default tool and fail named raw subagents/Team presets before construction; only
`HarnessConfig.legacy()` retains the old surface. Pass the template directly in the parent
`AgentHarness(capabilities=...)`; agnoclaw binds it to an idempotent, reconcilable
`child_run` capability whose model schema contains only bounded `task` and
`delegation_id`. Use a distinct child harness instance. Custom `CapabilitySpec` values
with `kind="child_run"` are now model-exposed only if they satisfy the same
host-managed/run-scoped/isolated/idempotent/reconcilable contract; mislabeled legacy
specs fail startup with `CHILD_CAPABILITY_DECLARATION_INVALID`.

For hosted use, pass the same templates to
`create_agentos_app(child_templates={agent_id: {name: template}})`. New additive
version-1 lifecycle routes start/list direct children and return bounded typed results.
Remote run snapshots now include `parent_run_id`, `root_run_id`, and `child_depth`;
custom strict decoders must accept those additive fields. The child-start request does
not accept model, budget, tools, learning, schema, or persistence settings. Ensure all
replicas deploy the same catalog before enabling remote delegation, and grant
`agents:run`, `agents:read`, plus any template-specific scopes as appropriate.

During a rolling deployment, upgrade every process that may decode `runtime_children`
before any process creates a 1.1 child. Once a 1.1 declaration exists, do not roll back
to an older 1.0-only application reader; first drain/fence old workers or restore the
reviewed pre-change package-and-data snapshot. Reusing a delegation ID with a changed
message, schema, evidence snapshot, budget, or grant intentionally fails idempotency;
create a new versioned delegation ID for new logical work.

Lifecycle-emitted normalized events now commit to the ledger first as bounded,
content-minimized `trajectory.*` evidence with an atomic outbox row. Treat the immediate
`EventSink` as a best-effort compatibility notification for lifecycle runs: its failure
no longer fails or rewrites a committed run, even if legacy fail-closed mode is set.
Named-legacy direct `run/arun` sink behavior is unchanged. Explicit-profile
convenience calls now enter lifecycle and receive ledger-first trajectory/outbox
behavior. Exporters should consume the runtime outbox rather than depending on callback
delivery.

The public `RuntimeOutboxWorker` now supplies that generic delivery loop. Downstream
export must be batch-atomic or tolerate partial remote success and deduplicate by
`event_id`; multiple workers can complete batches out of order, so consumers use
`(run_id, sequence)` for reconstruction. Configure its delivery deadline below its
lease. Schema v8 adds bounded-attempt poison isolation and safe-coded quarantine.
Schema v10 adds exact-owner, scope-authorized inspection/replay and content-minimized
audit history. Independent audit anchoring, multi-destination acknowledgement, and
concrete OTEL/client adapters are not yet release-certified.

The preview also includes `PostgresRuntimeStore` in the `postgres` extra. Runtime
schema v12 includes transition/start event evidence, event-retention watermarks, the
operation intent/mutation/fence/settlement ledger, and store-issued run/session
execution leases. Version 6 adds scoped artifact metadata and operation-result
relationships. Version 7 adds immutable capability approval requests, decisions, exact
authorization grants, and approval lifecycle events. Version 8 adds dead-letter
timestamps/reason codes and ready-queue indexes without storing exporter exceptions.
Version 9 adds a store-authoritative recovery timestamp and owner/state/age/run-ID
index. Existing runs are conservatively stamped at migration time, so they become
scanner-eligible only after the configured minimum age; caller clocks cannot bypass it.
Version 10 adds the owner-scoped, content-minimized `runtime_dead_letter_audit` ledger.
Inspection is audited, and exact-CAS requeue plus mutation-idempotency/audit evidence
commit atomically. Audit records intentionally do not foreign-key to runs or outbox rows,
so operational retention cannot erase replay history.
Version 11 adds `runtime_children`, explicit parent/root/depth snapshot lineage, unique
parent/delegation identity, declaration digests, and indexed child lookup. Creation,
parent/child events, idempotency, join settlement, and descendant cancellation remain
transactional. Existing root snapshots decode with empty lineage; the v10→v11 migration
test proves run, event, and outbox preservation.
Version 12 adds `runtime_scheduler_jobs` and `runtime_scheduler_runs`, including exact
job revisions/digests, occurrence/attempt identity, lifecycle binding, database-clock
availability, renewable claim evidence, and concurrency/history indexes. It does not
import the legacy JSON file; that remains an explicit T14b cutover.
Both first-party stores migrate under a transaction (PostgreSQL takes an advisory
migration lock). Back up preview databases before upgrading and do not run an older
writer after v12 migration begins.
Custom `RuntimeStore` adapters must add `list_recoverable_runs(owner=...,
after_run_id=..., minimum_age_seconds=..., limit=...)`, enforce exact tenant/user
equality and the age cutoff using the store's authoritative clock, return only queued,
running, and cancelling snapshots in ascending run-ID order, and bound `limit` to
1–1,000. Do not implement this as an unscoped table scan.
They must also implement `list_reconciliation_operations()` with exact owner,
store-clock age, operation-ID keyset, and reconciliation-wait filtering, plus
`reconcile_operation()` as one owner-checked revision CAS that advances the dispatch
fence and atomically records evidence artifact references, `operation.reconciled`, and
outbox evidence. Exact idempotent replay must reauthorize the owner before returning a
prior decision. These additions reuse the tables introduced through schema v11; a custom adapter may not split
the mutation across best-effort writes.
Custom adapters must also accept the optional `child_spec` on `create_run()` and expose
owner-authorized, bounded `list_children()` plus `get_child_spec()`. Child creation must
validate authoritative parent state/owner/lineage, descendant non-escalation, depth and
fan-out; atomically commit relation, both event streams, outbox, and start idempotency;
enforce join policy on parent completion; append one parent settlement event with the
child terminal transition; and propagate descendant cancellation in the parent
transition transaction. See [Declared child runs](child-runs.md).
They must preserve the child-spec JSON and version exactly: current code reads both
`1.0` and `1.1`, while the complete canonical document remains part of creation and
idempotency digests. Adapters must not drop an unknown field or rewrite `null` before
digest comparison.
Custom adapters must replace the old unaudited `list_dead_letters()` surface with
`inspect_dead_letters(owner, operator_digest, authority_digest, reason_code, ...)`, add
`list_dead_letter_audit(owner, ...)`, and implement the expanded
`requeue_dead_letter(...)`. Inspection selection and its audit append are one
transaction. Requeue must exact-match owner and quarantine timestamp, globally reserve
`mutation_id`, bind its semantic digest, and atomically append replay evidence. An
identical retry returns the prior decision and different semantic reuse fails closed.
Adapters must not provide global owner enumeration or duplicate event payloads in the
audit table. Host applications should migrate callers to `RuntimeDeadLetterAdmin` and
its three explicit scopes; direct store administration is an internal adapter boundary.
Back up artifact bodies and matching key generations with the ledger. The service
store's loopback exact-manifest PostgreSQL restore rehearsal is automated, but it does
not cover external artifact/key generations, old-writer fencing, or production
topology. Its production RPO/RTO gate is still open; follow the
[PostgreSQL operations guide](postgresql-runtime-store.md).

```python
from agnoclaw import AgentHarness, LocalArtifactStore, SQLiteRuntimeStore

harness = AgentHarness(
    model=model,
    runtime_store=SQLiteRuntimeStore("/var/lib/agent/runtime.db"),
    artifact_store=LocalArtifactStore("/var/lib/agent/artifacts"),
)
```

`start()` now records its model execution as a non-repeatable operation. A cancellation
or generic provider exception after dispatch now parks the run at
`waiting_for_reconciliation`; `wait()` raises `RUN_RECONCILIATION_REQUIRED` instead of
the older unsafe `RUN_CANCELLED`/`RUN_FAILED` projection. Repeated cancellation cannot
erase the ambiguous effect. Applications must stop blindly retrying and run an
exact-owner `reconcile_pending_operations()` sweep with a versioned, read-only provider
observer and scoped evidence. See
[Operations, effects, and recovery](operations-and-recovery.md).

### Registered-capability approvals

`permission_durable_approvals` now defaults to `true`. For a registered capability on
the lifecycle path, a required approval transitions the run to
`waiting_for_approval` and persists an exact request before an approver callback or
external host decision. Existing callback implementations still work, but their
decision is now durable before continuation. Custom approvers continue to require a
stable `approval_version`.

Services that approve outside the worker should migrate to
`capability_approvals()` and `decide_capability_approval()` and pass the original
trusted `ExecutionContext`. Do not expose those methods as model tools. Raw
`Respond(..., {"approved": true})` no longer resumes an approval wait; the store
requires the exact durable decision digest.

Operator/API retries may repeat the exact approved flag, issuer, reason, and grant
scope: the existing decision is returned without a second event or grant. A conflicting
retry fails with `APPROVAL_ALREADY_SETTLED`; callers should treat that as a request to
re-read authoritative approval state, never as permission to create a new decision.

Set these explicitly during rollout:

```toml
permission_durable_approvals = true
permission_approval_ttl_seconds = 900
permission_approval_poll_interval_seconds = 0.25
```

Setting `permission_durable_approvals = false` temporarily preserves callback-only
compatibility, but removes durable wait/grant evidence and is not the recommended
service posture. Schema-v7 approval data alone does not make a suspended Python stack
restartable. The certified Agno 2.9 envelope above reconstructs one governed sequence
only from the complete provider/request/approval/authority evidence; missing, older,
unsupported, or ambiguous records still use conservative operation recovery.

### Raw `tools=` on lifecycle runs

Caller-supplied raw tools remain available on named-legacy `run()/arun()`, where their
live resources are serialized. Explicit-profile convenience calls enter lifecycle and
reject them. Construction records each advertised function as a
bounded opaque/live-only/nonrepeatable capability manifest; it does not infer safety
from function or Toolkit names.

`AgentHarness.start()` now rejects constructor or per-run caller `tools=` before it
creates a run or model-operation intent:

```python
# Compatibility only: serialized and not restartable per tool.
harness = AgentHarness(config=HarnessConfig.legacy(), tools=[legacy_tool])
result = await harness.arun("Use it")

# Durable lifecycle: declare the exact effect/recovery contract.
harness = AgentHarness(capabilities=[legacy_tool_spec])
run = await harness.start("Use it", context=trusted_context)
```

Migrate each raw function to `CapabilitySpec` with an explicit version,
implementation digest, input schema, trust, lifetime, concurrency, effect class,
recovery class, scopes, and factory. Do not bulk-label unknown writes as read-only or
idempotent merely to bypass admission. `admin_harness_capabilities()` exposes the
normalized raw-tool inventory to drive the migration.

For a provider whose complete `aquery()` path is independently known to be read-only,
`context_provider_capability()` supplies the narrow factory-owned adapter. Keep raw
`context_providers=[...]` only on named-legacy direct `run/arun` until migrated.
Explicit-profile convenience calls enter lifecycle and reject that raw ingress before
run creation. Provider updates,
`mode=tools`, and providers whose nested agents can reach unclassified effects need a
normal `CapabilitySpec` with truthful reconciliation; never infer safety from
`read=True` or a `query_*` name. See [Agno context providers](context-providers.md).

Same-tenant/session lifecycle runs now serialize within one harness process, while
different sessions overlap up to `runtime_max_concurrency` (default `16`). Ready
sessions rotate by tenant; `runtime_max_waiting` (default `1024`) and
its per-tenant/per-session subsets (defaults `256`/`32`) prevent one scope from owning
the queue. `runtime_admission_timeout_seconds` (default `30`) replaces unbounded waits
with `RUNTIME_ADMISSION_OVERLOADED`. If you intentionally retry a terminally rejected
submission, use a new idempotency key. At shutdown, prefer explicit async ownership:

```python
await harness.aclose(policy="drain")  # or "detach" / "cancel"
```

The default is `drain`; optional `runtime_close_timeout_seconds` bounds caller waiting
without abandoning the supervised workers. `close()` now fails clearly when called
inside a running event loop—replace it with `await aclose()` there.

### Learning

Previously nonfunctional institutional configuration now fails early:

```python
from agno.knowledge import Knowledge

harness = AgentHarness(
    enable_learning=True,
    learning_knowledge=Knowledge(vector_db=vector_db),
    learning_namespace="tenant/agent-version",
)
```

For User Profile/User Memory without institutional stores:

```python
harness = AgentHarness(
    user_id="user-123",
    enable_user_memory=True,
    enable_learning=False,
)
```

Do not reuse the old implicit `global` namespace for tenant-sensitive knowledge. The
final preflight will inventory, classify, quarantine, map, verify, and optionally roll
back legacy learning rows before new scoped writes are enabled.

The governed candidate ledger and self-improvement evaluator are additive preview
surfaces; they do not reinterpret existing Agno memory rows. New deployments use
learning-ledger schema v6 (v3 candidate events/outbox, the v4 reconciliation-worker
lease/fence/cursor table, the v5 content-free evaluation-archive projection, and v6
append-only application/outcome attribution) and
should submit a typed gate result with
`record_learning_candidate_evaluation()` rather than flattening its held-in/out/
transfer and frozen-control provenance into ad hoc metrics. No persisted-candidate
migration is promised until the final preflight/cutover tool below is available.

Schema v6 is additive: migration creates `learning_applications` and
`learning_outcomes` with candidate foreign keys and a unique application-to-outcome
constraint. It does not synthesize historical attribution from conversations or Agno
knowledge rows; doing so would falsely claim that a retrieved learning was applied.
Existing candidate revisions and v5 evaluation projections are unchanged. Back up the
ledger, run the normal idempotent migration, and verify schema version 6 before enabling
the outcome processor.

The optional `ImprovementEvaluationRunner` is also additive. Existing manually
constructed `ImprovementEvaluation` values remain accepted without paired statistics.
Runner-produced reports add mandatory `paired_statistics` and `runner_digest`, exact
scoped artifact checks, and confidence-aware rejection reasons. Code that serializes
these preview records should tolerate the new fields and continue treating the schema
as pre-freeze.

Personal/session store administration is also additive. `forget_learning_data()`
deletes only the exact opaque v0.12 identity-keyed row and post-verifies the active
database. Do not run it against legacy raw/global IDs or treat its point-in-time receipt
as backup/replica purge proof. The final migrator must map legacy identities first and
issue retention-complete deletion evidence separately.

### Identity and session administration

- Treat `ExecutionContext` as authoritative. Remove conflicting `user_id` or
  `session_id` arguments.
- Pass verified JWT claims to `AgentOSContextAdapter` only at a trusted host edge.
  Client metadata is never a claims channel.
- Ensure session-list records carry exact `tenant_id` and `user_id`; legacy unowned
  records will not be returned.
- Bind sandbox harnesses to one session and owner before enabling admin routes.

### AgentOS and remote lifecycle clients

- Install `agnoclaw[server]`; 0.12 includes `python-multipart` because AgentOS native
  run routes require it at application construction.
- Keep `RemoteHarnessClient.arun()` only for completed-response or raw-stream consumers.
  An explicit-profile server now routes it through lifecycle, but the wire wrapper does
  not expose control. Migrate controllable work to `start()` and reattachment to
  `get_run()`, then use the same `status/wait/events/output/cancel/command` grammar as local
  `HarnessRun`.
- Configure an OS security key, JWT middleware, or verified service accounts. The
  versioned lifecycle routes reject an otherwise open anonymous AgentOS. Grant
  `agents:read` for status/result/events and `agents:run` for start/cancel/commands.
- Pass an origin-only HTTP(S) base URL. Credentials in URLs, base paths, queries,
  fragments, redirects, and unsafe path identifiers are rejected by the new client.
- Treat caller timeout, task cancellation, and network disconnect as observation loss,
  not run cancellation. Persist the last run-bound event cursor, call `get_run()`, and
  resume with `events(after=cursor)`. Call `cancel()` only for an intentional state
  transition.
- Handle `RemoteHarnessError` for versioned server errors,
  `LifecycleProtocolError` for malformed peers, `RunWaitError` for safe terminal
  failures, and `RunReconciliationRequiredError` for ambiguous effects.

Full setup, route, scope, limit, and compatibility details are in
[AgentOS and remote lifecycle adapter](embedding/agentos-adapter.md).

## Persisted-data preflight

The read-only checker is implemented:

```bash
agnoclaw migrate 0.12 check \
  --learning-db ~/.agnoclaw/sessions.db \
  --schedules ~/.agnoclaw/schedules.json \
  --json
```

It fingerprints legacy Agno SQLite learning tables and scheduler JSON, reports ownership
gaps/collisions, requires institutional map-or-quarantine and schedule
timezone/misfire/fence decisions, and returns exit `3` while blockers remain. It never
mutates a source and always reports `apply_allowed: false`. See the complete
[migration preflight contract](migration-preflight.md).

## PostgreSQL/service migration lifecycle

Use `agnoclaw migrate 0.12 service check` to collect content-free source/target evidence,
`service plan` to rescan and write the reviewed mode-0600 plan, and `service preview` to
compile every learning/schedule/history destination without writes. Preview binds the
exact plan and private schedule map, repeats the source/target scan, streams a fresh
source snapshot, rejects source drift and post-rekey collisions, and emits only counts
and digests. A successful plan reports `apply_available: false`; a successful preview
reports `apply_available: true` with the exact confirmation values required by apply.

`service apply` then revalidates the plan, source, targets, backup receipt, and writer
fence under target-schema advisory locks before bounded provenance-owned writes.
`service verify` opens fresh connections and checks transformed rows, provenance, and
both owned and unowned target evidence. `service cutover` records a deployment-owned
receipt without changing routing. `service rollback` requires stopped writers, refuses
post-cutover or target drift, removes only exact inserted rows in reverse target order,
and preserves identical preexisting rows. Every mutating command supports `--dry-run`,
never prompts, and requires exact confirmations. Credential options accept
environment-variable names only.

Exit `3` identifies semantic blockers, `4` integrity/drift failures, `75` retryable
database failures, and `78` configuration/driver failures. Follow the [service migration
runbook](migration-service-0.12.md) for commands, control files, backup receipts, and the
remaining production-certification boundary. Do not invoke the local SQLite/JSON
`apply` command with a service plan.

## Local persisted-data cutover

The local SQLite/JSON migrator now prints and persists:

- source and target format versions;
- row/artifact/job counts and content hashes;
- tenant/user/session ownership gaps and collisions;
- legacy global learning namespaces and proposed quarantine/map decisions;
- schedule timezone, misfire, duplicate-fire, and old-writer fencing decisions;
- backup identifier, reversible boundary, and exact rollback command.

`plan` is content-free and checksum-bound. `apply` requires the exact reviewed digest,
explicit targets, and a writers-stopped assertion; it fences sources and creates
verified preimages before mutation. `verify` independently compares identities,
counts, normalized values, and behavior/logical digests. `cutover` records an explicit
marker. `rollback` is crash-resumable and refuses to discard target writes.

Follow [Operate the local 0.12 data migration](migration-apply-0.12.md). Do not extend
its claims to PostgreSQL, live services, arbitrary old writers, or production data while
the broader alpha release gates remain open.

## Improvement-evaluation runner 1.1 and governed corpus

The preview improvement runner moves from evidence schema 1.0 to 1.1. Existing 1.0
case artifacts remain immutable historical evidence; do not rewrite them or present
them as governed-corpus runs. Re-run an experiment when it needs 1.1 qualification.

Runner 1.1 adds `corpus_manifest_digest` to its digest and case artifacts and adds
`corpus_manifest_digest` plus `corpus_evidence_artifact_ids` to
`ImprovementEvaluation`. The default `EvaluationGatePolicy` now sets
`require_governed_corpus=True`, so an older/manual report without those fields receives
the hard rejection reason `governed_corpus_required`.

To migrate an experiment:

1. preserve the old report, case artifacts, and verdict as historical evidence;
2. curate an ordered `EvaluationCorpusManifest` with distinct proposer/curator
   identities, content-free payload/lineage digests, exact source artifacts, and
   development/sealed exposure;
3. stage the schema-1.0 source-provenance and decontamination records described in
   [Evidence-gated harness self-improvement](self-improvement-evaluation.md);
4. add their exact scoped references to `upstream_artifacts`, pass
   `corpus_manifest=...`, and execute fresh baseline/candidate rollouts; and
5. retain the new policy, corpus, runner, evaluator, model, and permission digests with
   the decision.

`EvaluationGatePolicy(require_governed_corpus=False)` is a deliberate compatibility
escape hatch for legacy/manual analysis only. It must not be enabled by a candidate,
must be versioned as a control-plane change, and must not be described as governed or
production qualification.

## Improvement-evaluation runner 1.2 and process subjects

Runner evidence schema 1.2 is additive over 1.1. Existing 1.0/1.1 reports and case
artifacts remain immutable historical evidence; do not rewrite them to imply process
isolation. Re-run an experiment when it needs the new claim.

Schema 1.2 adds `baseline_subject_contract_digest` and
`candidate_subject_contract_digest` to `ImprovementEvaluation`, the runner digest, and
each case artifact. It also adds one `subject_isolation_digest` shared by both sides.
`process_evaluation_subject_factory()` binds each command digest to its exact argv and
binds the isolation digest to protocol, environment-value digests, working-directory
mode, I/O limits, termination grace, and process-group policy. Environment values
themselves are not serialized. An experiment may use two ordinary in-process factories
or two bound factories; mixing one bound and one unbound side, or mismatching isolation
policy, fails before a subject is created.

To migrate an experiment to the local process boundary:

1. preserve the old report, artifacts, decision, and runner version;
2. package one absolute worker entry point using
   `run_process_evaluation_worker()` and pin its implementation/image digest into the
   existing baseline or candidate implementation digest;
3. pass only explicitly reviewed environment entries, keep secrets out of argv, and
   use the fresh temporary working directory unless an independently managed absolute
   directory is required;
4. use `process_evaluation_subject_factory()` for both baseline and candidate with
   equivalent limits and containment class; and
5. execute fresh paired rollouts and retain the 1.2 report/case artifacts rather than
   copying a prior verdict.

The default child environment is empty, commands never cross a shell, request/stdout/
stderr are bounded, cleanup is mandatory, and POSIX children run in a fresh process
group. This isolates local process state and faults, not filesystem/network/kernel
authority. Use the strict Docker subject for first-party no-network Linux-container
containment; VM/provider-egress profiles and Windows process-tree containment remain
uncertified.

### Strict Docker evaluation subject

`docker_evaluation_subject_factory()` is an additive runner-1.2 subject. Existing
process reports remain truthful process-isolation evidence; never relabel them as
container-isolated. To migrate an experiment:

1. build and independently review one worker image, then pin its full image ID or
   repository digest and exact `linux/amd64` or `linux/arm64` platform;
2. remove image-declared writable volumes and ensure the worker can run with the forced
   entrypoint, numeric non-root user, read-only root, bounded `/tmp`, and no network;
3. construct one `DockerEvaluationPolicy` and use it for both baseline and candidate;
4. pass an absolute Docker CLI path and each exact in-container worker command to
   `docker_evaluation_subject_factory()`; do not pass host secrets or mounts; and
5. run `scripts/docker_evaluation_probe.py --allow-live-docker` against the deployment
   daemon before retaining fresh paired runner evidence.

The image/platform/resource/process policy produces the required-equal isolation digest;
the baseline and candidate container commands produce distinct subject-contract digests.
No automatic conversion, networked-provider credential path, or VM claim is implied.

### Evaluation archive read model

The owner-scoped evaluation archive does not change canonical candidate or evaluation
record serialization, but the scale-safe read path does require learning-ledger schema
v5. Both first-party ledgers add nullable, content-free owner/target/mechanism/verdict/
evaluator/safety columns, a bounded reason-code relation, and owner/filter/order
indexes. New evaluations write canonical JSON, the typed projection, and validated
reason codes in one transaction.

Opening a v4 ledger performs an idempotent v5 migration under the SQLite migration
transaction or PostgreSQL advisory migration lock. The migrator derives typed fields
from the canonical candidate/evaluation JSON and scans historical reason metadata in
bounded 1,000-row batches. A content-free `reason_codes_json` completion marker avoids
rescanning older evaluations that legitimately have no stable reason code. Only codes
matching the public stable-code grammar enter the reason relation; historical prose or
unsafe strings remain solely in canonical JSON. Invalid/corrupt canonical rows fail the
migration and roll it back rather than creating a partially trusted projection.

Embedding applications may call `query_learning_evaluation_archive()` immediately.
Rejected and inconclusive verdicts are the default; qualification history requires an
explicit verdict filter. Cursors are not portable across learning owners. Custom
`LearningLedger` implementations remain source-compatible because the base protocol did
not grow; implement optional `EvaluationArchiveLedger` to enable the query, or handle
`LEARNING_EVALUATION_ARCHIVE_UNSUPPORTED`.

Old evaluations without structured `metrics["gate"]` remain visible by verdict,
evaluator, mechanism, target, and safety result, but have empty stable reason codes and
no gate/runner/corpus digests. Do not rewrite historical metrics merely to populate the
projection. Candidate content, notes, metrics, control metrics, and artifact IDs are
never returned by this API.

SQLite and live PostgreSQL tests reconstruct a v4 ledger, migrate it, and verify the v5
projection and reason backfill. Because older binaries do not understand or maintain
the v5 projection, take a database backup and budget a maintenance window before
migrating a large production ledger; rollback across this boundary restores the v4
backup or keeps writers on the v5 binary. Do not run mixed v4/v5 writers. The measured
10,000-owner plus 10,000-noisy-neighbor PostgreSQL benchmark is a bounded development
gate, not production-volume or failover certification.

## Compatibility escape hatches

The alpha/RC legacy runner, if introduced, will reject concurrency and all new durable
features, emit a visible compatibility marker, and be removed before final 0.12. Final
rollback uses the previous package within the documented data boundary; it will never
silently reinterpret identity, learning scope, approvals, or unknown effects.
