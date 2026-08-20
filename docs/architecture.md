# Harness architecture

Status: target architecture; not all components are implemented

Last reviewed: 2026-08-18

This document defines the architecture `agnoclaw` should converge on. For the
implementation-aligned current audit, see [Harness gap analysis](harness-gap-analysis.md).
For product rationale and sequencing, see
[World-class harness strategy](world-class-harness.md).
The frozen admission, classification, policy-evidence, key, diagnostic, and threat
contracts are in [Security foundation](security.md).

## Design goal

Keep the embedding surface extremely small while making the runtime safe and capable:

```python
harness = AgentHarness(
    model=model,
    profile="durable",
    runtime_store=runtime_store,
    artifact_store=artifact_store,
)
result = await harness.arun("Analyze the incident")
```

The target convenience contract is equivalent to:

```python
run = await harness.start("Analyze the incident")
result = await run.wait()
```

Callers opt into control without adopting a different execution engine:

```python
run = await harness.start(
    "Analyze the incident",
    session_id="incident-42",
    dependencies={"tenant_id": "acme"},
)

async for event in run.events(after=last_sequence):
    ...

await run.command(Steer("Focus on database evidence"))
await run.cancel()
```

## Current and target boundary

The current 0.12 branch has begun this split. Explicit `quick`/`durable`/`service`
profiles now compile into the immutable spec; `legacy` remains the preview compatibility
default. The non-streaming model/registered-capability slice, immutable seeds, and
run-owned compression/summary managers materialize a fresh Agno `Agent` and can
overlap. The supported quick/legacy host-local built-in suite also gets a fresh Agent,
fresh tools, and reverse-order release per call. During `start()`, its declared
file/shell/web/child effects cross operation settlement while remaining conservatively
single-flight. Other custom, dynamically discovered, and streaming calls remain on the
compatibility gate. `start/get_run`, the pure lifecycle reducer, and a
transactional SQLite run/event/outbox authority are implemented as preview contracts.
Exact same-session lanes, a hard process queue/concurrency/time bound, round-robin
tenant admission, content-free admission metrics, store-issued service-wide run/session
leases, explicit drain/detach/cancel shutdown ownership, and an explicit admission-bound
capability executor are also implemented. Process fairness does not imply distributed
weighted fairness. Specs passed through
`AgentHarness(capabilities=...)` now get version-pinned Agno adapters, async governance,
active-lease binding, and governed result settlement. An exact content-addressed
pre-model request checkpoint can now relaunch a run whose model operation is absent or
still planned after worker loss. On the narrowly certified Agno 2.9 path, every
provider call has its own durable operation/artifact; a governed registered-capability
result continues from Agno's validated `tool-batch` checkpoint, while an earlier
approval wait reconstructs from the exact first-provider artifact and approval ledger.
Nested/parallel, raw/extension, streaming, parser/output-model, directly injected
opaque-model, and normalization of remaining legacy capability surfaces are still
target work. Public-factory models now have real-process same-digest recovery,
changed-digest refusal, and the exact narrow Agno 2.9 tool/approval continuation:

```text
                         immutable
                 +-----------------------+
                 | HarnessConfig ->      |
                 | immutable runtime spec|
                 | model/capabilities/   |
                 | policy/stores/profile |
                 +-----------+-----------+
                             |
                      creates per run
                             v
 +-----------+      +--------------------+      +------------------+
 |  Client   +----->|     RunRuntime     +----->| Agno Agent/Team  |
 +-----+-----+      | session/run state, |      | per-run instance |
       ^            | context, budgets,  |      +------------------+
       | events     | capability view    |
       |            +---+------------+---+
       |                |            |
       |                v            v
       |          +-----------+  +----------------+
       +----------+RuntimeStore|  | RuntimeBackend |
                  | + Artifact |  | shell/files/...|
                  +-----------+  +----------------+
```

The target `RunRuntime` powers embedded calls, CLI/TUI, a remote protocol, scheduler
jobs, child runs, and AgentOS export. No stable adapter may bypass it to call the
underlying Agent. In the current preview, explicit-profile non-interactive CLI,
heartbeat, and scheduler calls enter `start()/wait()`. Async chat/TUI uses the same
lifecycle model operation plus a bounded, process-local presentation attachment; slow
displays detach without stalling or cancelling the run and terminal truth remains
authoritative. Extracted provider text also enters bounded scoped artifacts referenced
by content-free runtime events and replays through the existing run handle. Named-legacy
sync chat and raw `spawn_subagent` remain labeled compatibility paths; explicit
profiles fail them closed. Host-coordinated,
capability-only declared children now enter this same lifecycle/store kernel with
schema-v11 lineage, joins, recursive cancellation, independent handles, active-worker
wall deadlines, settlement-time reported token/cost assessment, declaration-bound
structured result validation, typed lossless handoff, and explicit governed synthesis;
host-owned templates now expose this path to models through capability governance and
to remote clients through authenticated lifecycle routes. This does not
infer durability for raw child tools merely because the host profile says `durable`,
or turn post-response accounting into a prepaid provider ceiling.

## Core types

### `HarnessConfig` and internal runtime spec

Existing public configuration resolves to one immutable, serializable internal
definition. It is not a second peer public configuration system:

- stable harness and agent identity;
- model/model policy;
- prompt layers;
- capability manifests;
- runtime backend factory;
- policy, permissions, and guardrails;
- learning profile;
- runtime, artifact, and learning stores;
- budgets and concurrency limits;
- event exporters.

Construction validates the graph: duplicate tool names, missing learning prerequisites,
unsafe profile combinations, unserializable durable dependencies, unsupported model
features, and incompatible capability versions fail before a run starts.

The T3 implementation compiles `_HarnessSpec` as deep-frozen JSON-like intent with a
deterministic `0.12a2` digest bound to profile and every resource guarantee; live
objects never enter it. Each retained live
resource instead has one materializer descriptor:

| Dimension | Values |
|---|---|
| lifetime | `run`, `session`, `process_pool` |
| concurrency | `isolated`, `immutable_shared`, `host_managed_shared`, `serialized` |
| recovery | `recreatable`, `checkpointable`, `reconcilable`, `live_only` |
| trust | explicit factory, explicit immutable, host-managed, legacy serialized |

Factories must produce isolated resources. Immutable and host-managed sharing must be
declared explicitly. Opaque caller objects are serialized only in the compatibility
profile and fail `durable`/`service` classification with
`UNCLASSIFIED_RUNTIME_RESOURCE`; a failed `deepcopy()` or an upstream clone fallback
never silently turns into a concurrency guarantee. `AgentHarness.runtime_manifest()`
exposes a content-minimized typed view of the profile, complete-spec digest, and these
resource declarations without exposing settings or live objects. Supported host-local
built-ins now have a run factory and explicit release scope; their surface-changing
configuration is bound into the complete digest. T3 is not complete until their effects
join the gateway, optional/custom backend and sandbox resources plus packs, plugins,
effectful context providers and richer MCP gain certified materializers, and the full adversarial
overlap suite passes without the compatibility gate.

Every `run/arun` call also resolves one `AdmissionEnvelope`. `ExecutionContext` is now
its mutable compatibility projection for hooks and Agno; the frozen envelope remains
attached and is serialized into internally generated tool-hook metadata with its exact
per-field provenance. The envelope has a stable digest so an operation can bind its
authority evidence without persisting identity or metadata content.

### Session

A durable sequence of related work. A session owns:

- session ID and tenant/user scope;
- ordered turns and branches;
- active context view;
- compaction records;
- session-scoped Session Context;
- a serialized execution lane.

A session is not a process, an asyncio task, or an Agno Agent instance.

The embedded preview enforces this lane within one harness process. It takes a session
lane before global capacity so siblings from a hot session cannot starve unrelated
sessions. The service guarantee will be ledger-issued leases with attempt fencing; no
process-local mutex is documented as distributed serialization.

### `HarnessRun`

One logical requested execution and its control surface. Recovery attempts retain the
same run ID and receive a new attempt ID:

```python
class HarnessRun(Protocol):
    id: str
    session_id: str

    async def wait(self) -> object: ...
    async def status(self) -> RunStatus: ...
    async def events(self, *, after: str | None = None): ...
    async def cancel(self): ...
    async def command(self, command: RunCommand): ...
```

Quick runs may store this ephemerally and make no restart/cursor-after-process promise.
Durable runs back the same state/event contract with the runtime ledger, checkpoints,
operations, and artifacts. The typed command variants are `Pause`, `Resume`, `Respond`,
`Steer`, and `Fork`. Follow-up work starts a new immutable run through
`HarnessSession.start()`; terminal runs never reopen.

The implemented preview and its precise safe-point/recovery limitations are documented
in [Run lifecycle and RuntimeStore](runtime-lifecycle.md).

### `CapabilitySpec`

A capability is more than a list of functions. It may contribute:

- tool definitions and effect metadata;
- prompt instructions;
- model settings;
- pre/post model and tool hooks;
- context reducers;
- state with snapshot/restore methods;
- runtime requirements;
- evaluation fixtures.

Skills, MCP servers, browser access, planning, compaction, learning, and subagent
delegation are capabilities. One inspectable `CapabilitySpec` is the public extension
contract; capabilities are selected by profiles and may be loaded on demand.

The first data-plane slice is `CapabilityExecutor`: it composes the registry and
`OperationGateway`, reauthorizes the exact run owner, enforces admission scopes and
budgets, materializes run/session/process resources, serializes only resources that
declare it, and settles artifact-backed results. This explicit host API is implemented;
wrapping all existing Agno `Function`/`Toolkit` ingress remains T9b work.

Caller `tools=` now crosses a normalization boundary even on the named-legacy path.
Each advertised function becomes a deterministic opaque/live-only/nonrepeatable spec
without an executable factory. Named-legacy `run/arun` keeps the already-declared
serialized behavior; `start()` and explicit-profile convenience calls reject that
inventory before run creation. This preserves a deliberate migration escape hatch
without laundering an unclassified callable into a durable guarantee.

`DurableApprovalCoordinator` is the small control-plane seam for registered capability
effects. It persists one exact request with the lifecycle wait, accepts only an
owner/authority-matched callback or host decision, produces a bounded authorization
grant, resumes from the exact decision digest, and revalidates that grant after lease
renewal immediately before dispatch. It does not own policy and cannot turn an old
approval into a global allowlist.

### `RuntimeStore`

The transactional source of truth for what happened. One interface stores run state,
append-only events, settled snapshots, operation records, leases, waiting inputs, and a
transactional outbox. It is not a debug log bolted onto execution.

Minimum backends:

- memory for tests and `quick`;
- SQLite for single-node durable usage;
- PostgreSQL for service and multi-worker usage.

JSONL may be an export/debug format, not a peer authoritative store. Every backend must
pass the same atomicity, compare-and-set, event-cursor, lease-fencing, tenant-isolation,
migration, backup/restore, and crash tests.

The current first-party stores also expose exact-owner keyset discovery for only
`queued`, `running`, and `cancelling` runs. `AgentHarness.recover_pending_runs()` applies
the existing fenced `recover_run()` classifier to one bounded page. It never enumerates
owners globally, wakes intentional wait/pause states, or steals a live lease.
An independent exact-owner scanner handles only reconciliation waits. It validates a
versioned host observer, exact operation revision/digest, and scoped physical evidence,
then CAS-settles the operation or continues a previously committed settlement without
redispatch.

The current SQLite and PostgreSQL stores atomically commit run/operation state,
monotonic events, terminal projections, execution leases, authorized artifact
references, content-minimized provider usage/cost settlements, and outbox evidence.
Every normalized event emitted inside lifecycle
execution also commits as a bounded, content-minimized `trajectory.*` projection before
compatibility observers are notified. This observer trajectory is query/evaluation
evidence; it does not replace lifecycle or operation state as effect truth. Store-issued
ownership, result-artifact recovery, durable approval requests/decisions/grants,
reopen, fault-injection, exact-owner, cursor, idempotency, contention, and exact
pre-model request-continuation tests pass. A loopback local/CI PostgreSQL rehearsal
also proves native dump/restore parity for all runtime rows, columns, indexes,
constraints, sequences, ordered events, and idempotency behavior. Because it excludes
external artifact bytes/keys and production topology, it does not satisfy the complete
DR or RPO/RTO contract. The PostgreSQL adapter also bounds each libpq connect attempt,
classifies acquired-connection loss as a non-retryable re-read/reconciliation boundary,
and passes repeated exact-container single-primary stop/start recovery with fence/state/
event continuity and a two-connection cap. An owned two-node PostgreSQL 17 gate also
proves positive replica lag, exact acknowledged-LSN catch-up, old-primary fencing,
planned promotion, bounded no-writer behavior, and existing/fresh read-write pool
reattachment. A synchronous companion proves a required standby partition withholds
RuntimeStore acknowledgement, rejoin releases only after replay, and an exact
acknowledged state/event/fence manifest survives abrupt primary `SIGKILL` and explicit
promotion with zero observed loss. A double-rewind companion then rejoins each former
writer as the exact read-only synchronous standby and rotates the writer role back while
preserving state, event ordering, and monotonic fences. The optional PostgreSQL
writer-authority seam accepts a fresh external linearizable grant, binds it to the exact
non-recovery `cluster_name` and monotonic fence token, installs a server-enforced
transaction deadline inside the remaining lease, and revalidates before commit. The
first-party etcd v3 adapter brackets live-lease inspection with linearizable exact-key
reads, pins the etcd cluster identity, and derives fencing from `mod_revision`. Its
endpoint-bound JSON-gateway credentials exchange, cache, and refresh RBAC tokens inside
the same total deadline without trusting redirects or certificate Common Names. A real
two-writable-timeline gate proves stale denial, authority-process-loss fail closure and
same-endpoint recovery, and commit-boundary revision-change rollback. The adapter is a
read-only authority consumer; controller ownership, renewal, and transfer deliberately
remain outside the harness. An owned three-voter gate now proves mTLS for client and
peer paths, least-privilege exact-key RBAC, one-member availability, majority-loss fail
closure, alternate-member recovery, and fence advancement. This contains clients using
the adapter but does not replace controller election, a durable multi-AZ production
quorum, network-partition/certificate-rotation chaos, watchdog/STONITH, or fencing for
arbitrary SQL clients and paused hosts. Production endpoint discovery/
rejoin automation, multiple/simultaneous faults, sync latency/availability, and true
partition/physical-fence behavior remain separate gates.
General post-dispatch checkpoints beyond the certified Agno 2.9 registered-capability
envelope, universal legacy-tool capability routing, native provider-event ingestion,
destination adapters,
independent dead-letter audit anchoring/operator UI, certified provider reconciliation observers,
evidence retention/deletion, and production chaos/DR gates still block the complete
`durable`/`service` claim.

### `ArtifactStore`

Stores content that should not remain in model context or event attributes:

- large tool returns and logs;
- media and binary payloads;
- subagent evidence;
- generated reports;
- workspace snapshots where a runtime supports them.

Artifacts are content-addressed, scoped, checksummed, size-bounded, and read through
paged tools. Events contain handles and safe previews, not full sensitive payloads. The
implemented local store and schema-v11 operation-result/export/child integration are documented in
[Durable artifacts](artifacts.md). Manual and opt-in automatic context archives use
that byte store. Governed registered-capability and lifecycle first-party-tool results
can opt into bounded model-facing envelopes plus same-session verified paging. The
first-party dispatch adapter commits declared effect intent, renews the active lease,
applies result policy before persistence, and replays by exact call identity. Plugin
and pack capabilities use the registered path; configured MCP calls and attested
read-only provider-query factories use governed ingress. Direct compatibility tools,
raw/effectful provider and raw-MCP adapters, outer-model results, remote context indexing, and
retention/deletion automation remain T6/T7/T14 work.

## Identity and lineage

Identity must be explicit and stable:

| Level | Purpose | Example |
|---|---|---|
| harness | configuration/product identity | `repo-assistant` |
| agent | role/model policy identity | `security-reviewer:v3` |
| session | durable conversation/work line | `incident-42` |
| run | one logical requested execution | `run_01...` |
| attempt | one leased worker execution/recovery try | `attempt_03...` |
| turn | one user/agent interaction in a run | `turn_0007` |
| step | settled model/tool boundary | `step_0019` |
| tool call | one proposed external effect | provider call ID |
| artifact | immutable externalized content | content hash/ID |

Declared child runs carry `parent_run_id`, `root_run_id`, `child_depth`, immutable
delegation/budget/capability/learning policy, and optional parent step/tool-call links.
The active worker enforces the declared wall duration through authoritative
cancellation, and a successful model settlement records reported Agno tokens/cost
before child completion. Child-spec schema `1.1` can digest-bind a bounded object result
schema; validation occurs after operation/budget settlement and before completion, and
known-success recovery repeats it. Direct parents can collect typed terminal/pending
outcomes, apply explicit partial-failure policy, prepare deterministic synthesis input,
page lossless direct-child result artifacts without widening owner scope, or execute
that snapshot as another capability-only declared synthesis child. The synthesis
boundary defaults to all-success, bounds total evidence, treats child content as
untrusted data, and grants no ambient artifact reads. Absolute restart-safe deadlines,
provider reservation, a first-party cross-child reader, and pre-spend hard ceilings
remain distinct target controls.
Forks carry
`forked_from_run_id` and `forked_from_step`. Conversation sequence and parent/child
hierarchy are separate axes.

## Run state machine

```text
created -> queued -> running <-------------------------+
                      |  |  \                          |
                      |  |   -> waiting_for_input -----+
                      |  +----> waiting_for_approval --+
                      |  +----> waiting_for_reconciliation
                      |  +----> paused ----------------+
                      |  +----> cancelling -> cancelled
                      +-------> completed / failed / failed_with_unknown_effects
```

Rules:

- transitions are events and compare-and-set store updates;
- cancellation is cooperative first and escalates only through backend-specific policy;
- `waiting` survives client and worker loss in durable profiles;
- a recovered run resumes from a settled boundary;
- interrupted side effects enter `waiting_for_reconciliation`; after escalation policy
  they may produce terminal `failed_with_unknown_effects`, while later effect
  reconciliation never rewrites that terminal run state;
- terminal states are immutable; follow-up or fork creates a new run with explicit
  lineage.

For the implemented registered-capability path, entering
`waiting_for_approval` and inserting its exact request are one transaction. Returning
to `running` requires the matching settled decision digest. Cancellation/failure/expiry
tombstones a pending request in the same run transition. The durable wait is therefore
authoritative, but the row is not a serialized model/Python continuation. A live worker
can consume a cross-process decision. On the certified Agno 2.9 registered-capability
path, a replacement worker may reconstruct only after the request checkpoint, settled
first-provider artifact, decision/grant, frozen authority/spec, and provider ordinal all
validate; an identical decision retry is idempotent and a conflict fails closed. After
the tool result, continuation requires Agno's exact `tool-batch` checkpoint. Unsupported
surfaces or missing, drifting, or ambiguous evidence retain conservative recovery.

## Event contract

All event types share an envelope:

```json
{
  "schema_version": "1.0",
  "sequence": 42,
  "event_id": "evt_...",
  "event_type": "tool.call.completed",
  "occurred_at": "2026-08-02T10:15:22.123Z",
  "harness_id": "repo-assistant",
  "session_id": "incident-42",
  "run_id": "run_...",
  "attempt_id": "attempt_...",
  "parent_run_id": null,
  "turn_id": "turn_0007",
  "step_id": "step_0019",
  "tool_call_id": "call_...",
  "trace_id": "...",
  "span_id": "...",
  "payload": {},
  "usage": {},
  "redaction": {"applied": true},
  "metadata": {}
}
```

Requirements:

- `sequence` is monotonic within a run and is a reconnect cursor;
- producers never rewrite prior events;
- payload schemas are versioned per event type;
- arguments/results are redacted or replaced with artifact handles by default;
- event storage failure follows the profile's durability policy;
- exporters consume persisted or committed events and may be best-effort;
- OpenTelemetry spans correlate to native IDs but do not replace the native log.

The implemented schema-v12 outbox is at-least-once and authoritative. Its optional
telemetry projection is intentionally narrower than the target envelope shown above:
registered enums and numeric usage/cost only, with domain-separated HMAC IDs and no
content fields. `RuntimeRunInspector` is a separate exact-owner/scope read model, not an
exporter or mutation API. Live trace/span correlation remains target work. See
[Observability and safe run inspection](observability.md).

Required event families include run, turn, model, tool, context, compaction, capability,
skill, policy, permission, waiting/input, snapshot, artifact, learning, delegation,
scheduler, usage/budget, and recovery events.

## Snapshots and operation gateway

A snapshot is safe to continue only when:

- every model tool call has a matching result or explicit cancellation record;
- capability state can be serialized;
- required dependencies are serializable or recoverable by key;
- pending external operations are recorded.

Every nondeterministic external operation—including provider/model requests and
effectful tool calls—crosses one `OperationGateway`. It records request digest,
idempotency/reconciliation identity, provider/tool identity, attempt, status, usage and
cost where available, safe response evidence, and settlement policy. Operations use
states such as:

```text
proposed -> approved -> started -> completed
                          |          |
                          |          +-> compensated (optional)
                          +-> failed
                          +-> unknown_after_crash
```

The key includes tenant, run, operation kind, logical call ID, and normalized request
digest. Idempotent operations may also accept a caller-visible idempotency key. Retries
never assume a timed-out model request or external effect did not occur; ambiguous
results reconcile or enter `unknown_after_crash` under an explicit provider/tool
policy.

Provider evidence extraction is downstream of the external effect and therefore cannot
be allowed to create a new post-effect failure window. The implemented gateway treats
an extractor exception or invalid value as content-minimized unreported evidence and
still commits known success. Certified provider adapters must reconcile these generic
Agno metrics with provider receipts before advertising billing-grade accuracy.

## Context pipeline

The context manager receives a token budget and builds a manifest:

```text
reserved output/reasoning
system policy and identity
active capability instructions
active tool schemas
current user input
recent conversational tail
structured continuation summary
retrieved session/user/entity/learned memory
selected artifact excerpts
```

The default reducer order is:

1. clamp an oversized individual generation;
2. externalize oversized tool/media returns at production time;
3. remove superseded file reads;
4. clear old tool results while preserving a useful preview;
5. keep a complete recent tail at tool-pair boundaries;
6. summarize older context into a typed continuation schema;
7. retain full history in the RuntimeStore and expose bounded search;
8. compact once and retry on provider overflow;
9. stop with a typed `ContextExhausted` error if still over budget.

Compaction creates a trajectory event and a new context view. It does not destroy the
underlying trajectory. Repeated compaction without sufficient reclaimed budget trips a
thrash guard.

The implemented preview covers deterministic accounting, artifact-first full-session
archival, actual manual replacement, 90/97% opt-in preflight triggers, a 70% release
boundary, a deterministic emergency summary, process-local session admission,
content-free digest manifests, exact-scope lexical search, selective rehydration, and a
bounded explicit continuation schema for goal, plan, progress, decisions, approvals,
open questions, tests, files, and citations.
The exact-invocation reactive retry is now implemented for Agno-classified overflows,
with stream/tool/attempt fences. Registered capabilities can losslessly spill large
results to bounded envelopes, page them within the trusted session, and retain
artifact-reference/failure invariants through compaction. The ordered reducer,
live-provider certification, automatic continuation-schema extraction/merge/fidelity,
cross-process compaction lease/CAS, universal output routing, and
repeated thrash/drift gates remain the target contract. See
[Context management](context-management.md).

## Capability discovery

Small profiles may advertise all tools. Large catalogs use two layers:

1. compact capability and tool metadata available to the model;
2. a search/activation capability that loads full instructions and schemas.

An actual Skill capability supports:

- explicit caller activation;
- model-driven `activate_skill(name)`;
- one-time full `SKILL.md` disclosure;
- resource listing and relative path resolution;
- activation deduplication and protection through compaction;
- project trust checks and capability/effect restrictions;
- optional child-run delegation.

Skill metadata follows the Agent Skills specification. `allowed-tools` is an
authorization upper bound only when established by trusted policy; untrusted skill
content cannot grant itself capabilities.

## Policy architecture

Policy evaluation occurs at boundaries, not only around the top-level model call:

```text
request identity
  -> before run
  -> before context retrieval / learning recall
  -> before prompt/model request
  -> before capability activation
  -> before exact tool approval request
  -> durable decision + least-privilege grant
  -> lease renewal + grant reauthorization
  -> before tool execution
  -> after tool result / before model ingestion
  -> before final output
  -> before learning candidate/promotion
  -> before event/artifact export
```

Tool decisions use typed effects plus normalized arguments. Unknown custom tools use a
conservative default. MCP annotations improve UX but are untrusted hints.

The runtime keeps these controls separate:

- authorization: who may do what;
- sandboxing: where code may execute;
- network/path guardrails: what resources are reachable;
- approval: a human decision on an exact proposed action;
- redaction/DLP: what data may cross a boundary;
- model instructions: desired reasoning behavior, never the enforcement layer.

Approval requests bind capability/version digest, effect category, effective-argument
digest, policy version, authority, owner/session/run, expiry, and nonce. Grant
reauthorization occurs at the last no-effect boundary so time, policy, argument, or
identity drift fails before materialization. The administration API is a host control
plane and is never model-callable.

## Learning plane

The learning plane consumes completed trajectory and outcome evidence. It is not allowed
to mutate the active run invisibly.

```text
trajectory + explicit feedback + outcome
                 |
                 v
          LearningCandidate
        /       |         \
   reject   hold/review   promote
                           |
                           v
                scoped Agno Learning Store
                           |
                    recall with provenance
                           |
                 measured task outcome
```

Profile configuration selects Agno stores and their modes. Learned Knowledge requires an
Agno `Knowledge` with vector storage. Multi-tenant scope is a structured key, not an
unqualified string default. High-stakes promotion is enforced in code; Agno Propose mode
is useful UX, not a security boundary.

Ambiguous promotion/rollback effects enter fenced unknown states. Owner-bound keyset
discovery feeds a bounded `LearningReconciliationCoordinator`; a host observer may
inspect the exact external key but may never redispatch the effect. Before CAS
settlement, the coordinator binds the observation to one candidate digest/revision and
verifies immutable evidence bytes plus purpose/tenant/user scope. Concurrent observers
can repeat reads but cannot overwrite a winner. Backend-specific exact observers and
durable worker scheduling/leases remain adapter and operations responsibilities.

See [Learning and self-improvement](learning.md).
Exact host read/replace/forget behavior for identity-keyed personal/session stores is in
[Personal and session learning administration](learning-administration.md).

### Harness improvement boundary

The learning plane may propose a change to one or more observable harness components:
system prompt, tool description, tool implementation, middleware, skill, subagent
configuration, or long-term memory. A versioned component manifest makes attribution
and rollback possible without adding seven public configuration systems.

```text
read-only evidence -> causal diagnosis -> bounded ChangeHypothesis
                                             |
           exact scoped artifacts + fresh baseline/candidate pair
                                             |
              immutable verifier/model/config/budget -> held-in/out/transfer + confidence
                                             |
                                             v
                               reject/archive or explicit promotion
```

Raw traces, verifiers, benchmark cases, accepted budgets, model configuration, identity,
policy, permissions, and secrets are control-plane data outside the edit sandbox. A
candidate cannot authorize or evaluate itself. Component, experience, and decision
provenance are linked through immutable artifact IDs and digests. The preview runner
creates a fresh owned resource per rollout and closes it, but hostile/shared-state
subjects still require a separate process/container/VM boundary supplied by the host.
See the
[Lilian Weng research reconciliation](lilian-weng-harness-audit.md) and the implemented
[evidence-gated evaluation contract](self-improvement-evaluation.md).

## Short and long runs use the same kernel

| Concern | `quick` | `durable` / `service` |
|---|---|---|
| runtime record | in-memory RuntimeStore | transactional SQLite/PostgreSQL RuntimeStore |
| snapshots | optional end-of-turn | every settled step |
| operations | in-memory audit | persistent model/tool operation settlement and reconciliation |
| context | bounded history, optional compaction | tiered compaction, artifacts, search |
| waiting | process-local | durable across restart |
| retry | request/model retry | classified workflow/activity retry |
| dependencies | arbitrary Python values | serializable values or durable references |
| workspace | host/sandbox | isolated workspace plus snapshot policy |

The model/tool loop stays Agno-native. Durability wraps nondeterministic operations and
persists boundaries; it does not force every user to author a workflow graph.

## Adapter boundaries

### First-party process adapters

Profile-aware routing is intentionally smaller than a second execution framework.
Internal functions return the existing `HarnessRun`: explicit quick/durable/service
work uses `start()`, and named-legacy compatibility work wraps the direct result or
stream of `arun()` in the same facade. Non-interactive CLI, heartbeat, shared/isolated
local schedules, async REPL, and TUI now use this route.

Interactive explicit-profile calls add one private `LiveRunPresentation` attachment to
the lifecycle worker. The worker drains Agno's stream inside the already persisted
model operation, publishes raw display events through a bounded single-consumer queue,
builds terminal content, and settles normally. Queue overflow or consumer closure
detaches presentation without blocking/cancelling model execution. Independently, a
segment writer batches at 8,192 characters or 32 deltas, stages the content in the
run-owner `ArtifactStore`, then atomically commits its content-free reference, runtime
sequence, and outbox row. `HarnessRun.output()` verifies scope, purpose, artifact
binding, checksum, content length, and segment order. `RuntimeStore`,
`HarnessRun.events()`, and terminal settlement remain authoritative.

Schema v12 makes scheduled work a first-class consumer of this kernel. SQLite and
PostgreSQL atomically create one deterministic occurrence/attempt, advance the job's
nominal clock, and issue a database-clock lease with a monotonic fence. A stable
lifecycle idempotency key binds the attempt to `AgentHarness.start()`; the resulting
runtime run ID is a relationship, not an identity replacement. Expiry or deliberate
detachment reclaims the same attempt and increments its fence. Only a known retryable
terminal failure creates a new attempt for the same occurrence. Misfire, deterministic
jitter, bounded concurrency-group backlog, job-revision snapshots, and terminal skip/
dead-letter history are part of the store contract.

Cancelling only the scheduler waiter records `detached` once a lifecycle run exists;
cancellation does not silently become a run transition. Store loss, ambiguous completion
acknowledgement, or lifecycle reconciliation also preserves the same attempt rather than
fabricating a retry. Raw exception bodies do not enter history, and its bounded output is
only a marked preview. The full contract is in [Durable scheduling](durable-scheduling.md).

The compatibility boundary is equally explicit: sync streaming and raw
`spawn_subagent` remain named-legacy adapters. Explicit profiles reject sync raw
streaming, omit the raw subagent default tool, reject named raw subagents and Agno Team
presets, and reject specialized fork/command skills before run creation. Host code,
model tools, and remote clients can use
host-owned `DeclaredChildTemplate` entries on the lifecycle kernel instead. Segmented
output limits process-loss presentation RPO to one partial
segment, but it cannot reconstruct an interrupted Agno/model stack or prove a provider
effect. Moving the remaining paths requires capability/effect and continuation
contracts, not a broad `hasattr(start)` fallback.

### AgentOS

Use AgentOS for hosted APIs, auth middleware, scheduler, approvals, traces, and platform
operations where its guarantees are sufficient. The adapter maps AgentOS requests into
the internal runtime-spec/`HarnessRun` semantics and must never bypass harness policy or
events. New durable clients use the authenticated `/agnoclaw/v1` start, status, result,
bounded lifecycle/output cursor, cancel, and typed-command routes through
`RemoteHarnessClient`.
AgentOS's native `/agents/{id}/runs` route remains an explicit completed-response/raw-
stream compatibility surface. Both are adapters over the same harness kernel, never a
second execution authority. Disconnect is not cancellation; every reattachment repeats
claims-first owner authorization.

### Durable execution engines

Temporal, DBOS, Prefect, Restate, or similar integrations are optional. They should map:

- the deterministic coordinator to workflow code;
- model requests, tools, MCP, and external storage I/O to activities/steps;
- waiting states to signals/events;
- stable agent/capability/tool IDs to versioned activity names.

The local/SQLite durable profile remains useful without an external orchestrator.

### Remote protocol

A remote protocol exposes capability handshake plus session/run/item operations:

- initialize and negotiate schema/capabilities;
- session create/get/archive and start a related run;
- run start/get/wait/status/events/output/cancel/typed command;
- event stream from cursor;
- typed approval/input requests;
- artifact metadata and bounded download.

It is an adapter over the embedded types, not a second runtime.

## Dependency and version discipline

Agno is a fast-moving substrate. `agnoclaw` should maintain:

- a locked, fully tested Agno production version (2.9.0 primary during development);
- a documented supported range;
- CI against the 2.6.4 legacy lane, primary stable, and newest Agno 3 prerelease;
- contract tests for Agent signatures, event mapping, learning store behavior,
  cancellation/continuation, compression, and AgentOS adapter behavior;
- feature detection only when both branches have tests;
- explicit deprecation/migration notes instead of silent `hasattr` no-ops.

See [Agno release-practice audit](agno-release-practices.md) for the release-by-release
adopt/adapt/avoid decisions and the Agno 3 storage/queue adoption gate.

The same discipline applies to MCP protocol/SDK revisions and OpenTelemetry semantic
conventions.

## Architectural invariants

Changes are not complete unless these remain true:

1. `run()` and `arun()` route through one runtime contract.
2. No shared mutable run state crosses concurrent runs.
3. Same-session order is deterministic.
4. Every external effect passes policy and ledger boundaries.
5. Every durable waiting state and settled step survives restart.
6. Tool-call/result pairs are never orphaned by context reduction or recovery.
7. Large content is preserved by artifact handle before truncation.
8. Tenant/user/session scope is required before recall or learning.
9. A learning can name its evidence and be removed.
10. Public capability claims have conformance tests and maturity labels.
11. Optional adapters cannot weaken core policy, identity, or event guarantees.
12. The `quick` path stays dependency-light and observably low-overhead.
13. A self-improvement proposal cannot mutate its evaluator, evidence, model/config,
    budget, identity, policy, permission boundary, or raw run history.
14. No learned or harness change is promoted from self-reflection alone; acceptance is
    evidence-backed, held-out tested, versioned, and reversible.
