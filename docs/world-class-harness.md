# World-class harness strategy

Status: architecture and product direction

Research date: 2026-08-09; Agno/Weng live-source recheck: 2026-08-17; Pi recheck: 2026-08-13;
OpenTelemetry GenAI recheck: 2026-08-14

Applies to: the embeddable `agnoclaw` library, its optional AgentOS adapter, and its
CLI/TUI clients

## Executive decision

`agnoclaw` should be a **small, profile-driven harness with a durable run kernel**.
The public experience should stay close to:

```python
harness = AgentHarness(
    model="anthropic:claude-sonnet-4-6",
    config=HarnessConfig.local_safe(profile="durable"),
    runtime_store=runtime_store,
    artifact_store=artifact_store,
)
result = await harness.arun("Review this repository")
```

The power should come from composable runtime contracts below that API, not from a
larger constructor or an ever-growing default prompt.

The competitive target is not a list of tools. It is a set of behavioral guarantees:

1. A run can be observed, steered, cancelled, paused, resumed, forked, and recovered.
2. Concurrent runs cannot see or mutate each other's prompts, tools, identity, or state.
3. Context remains bounded without silently losing the information needed to finish.
4. Tools declare effects and are selected lazily, policy-checked, and auditable.
5. Learning is scoped, evidence-backed, reversible, and measured against a no-learning
   baseline.
6. Short tasks pay almost no orchestration tax; long tasks gain durability and
   checkpoints without changing the agent's programming model.
7. Every important trajectory can be replayed in an evaluation and explained to an
   operator.

This is a stronger and more durable differentiator than feature parity with any one
coding agent.

## Honest current position

`agnoclaw` already has a valuable foundation:

- a compact Python embedding surface over Agno;
- model/provider portability;
- a coherent backend seam for shell, files, skills, and browser execution;
- workspace, skill, pack, plugin, hook, policy, permission, and guardrail concepts;
- per-run dependency and session-state injection;
- model-hidden tool argument binding;
- structured events and structured output;
- optional AgentOS export.

It is not yet a worry-free long-running harness. The highest-impact gaps are behavioral,
not cosmetic:

| Priority | Current finding | Why it matters |
|---|---|---|
| P0 | Explicit profiles drive immutable spec/resource classification. The effect-safe non-streaming model/capability slice overlaps; first-party tools, including configured MCP, get fresh resources where certified, explicit versioned effects, lifecycle intent/lease/policy/settlement, and conservative single-flight admission. | Object-state leakage and unfenced first-party dispatch are blocked without mislabeling effects as concurrent-safe. World-class service concurrency still requires custom backend/pack/plugin/context materializers, richer remote adapters, and the complete overlap matrix. |
| P0 | Preview `start/get_run`, `HarnessRun`, typed commands/reconciliation waits, exact service-wide session leases, bounded process-local tenant round-robin admission with timeout/overload/metrics, explicit shutdown ownership, exact pre-model/result/evidence continuation, bounded owner-scoped startup/reconciliation recovery, lifecycle-governed async REPL/TUI presentation, and artifact-backed cursor-replayable provider-text segments exist. A repeatable SQLite gate passes 50/50 real ungraceful exits across run, transition, and operation transaction boundaries. A separate two-ledger capability gate passes eight real before-dispatch/after-provider-commit crashes across every effect class with zero duplicate external effects and zero blind ambiguous redispatches. A three-scenario real-process gate kills the production `AgentHarness.start()`/Agno model stack while planned, in provider dispatch, and settled before run completion. Two additional Agno 2.9 gates now prove a governed two-provider/one-tool sequence across a native `tool-batch` checkpoint and reconstruct an atomic approval wait before that checkpoint, with ambiguous later provider dispatch still blocking and zero duplicate calls, requests, or effects. | Distributed weighted fairness, nested/parallel or raw/extension tool graphs, parser/output-model and persisted-stream recovery, certified live-provider observers/receipts, full native event replay, legacy sync-chat/child routing, safe-point command continuation, autonomous owner enumeration, host-power-loss proof, multi-host recovery, and production recovery soak/failover are not yet certified. |
| P0 | PostgreSQL service storage optionally binds every access to a fresh external writer grant, exact non-recovery server identity, monotonic generation, a server deadline inside lease TTL, and pre-commit revalidation. The first-party etcd v3 adapter pins cluster identity, brackets lease inspection with linearizable reads, derives fences from `mod_revision`, and passes live revocation/expiry/loss plus real dual-writer commit-rollback drills. A three-voter gate additionally proves TLS 1.2 client/peer mTLS, exact-key RBAC, endpoint-bound gateway tokens, one-member availability, majority-loss denial, alternate-endpoint recovery, and fence advancement. | External controller election/record ownership, durable multi-AZ quorum, network-partition/latency/certificate-rotation proof, endpoint automation, production backup/RPO/RTO, and watchdog/STONITH for arbitrary clients or paused hosts remain mandatory production gates. |
| P0 | Manual plus opt-in automatic/reactive compaction archives the full scoped trajectory and replaces Agno history; internal synthesis produces a bounded typed continuation through a tool-free model turn, accepts only exact source spans, and falls back deterministically; exact-scope search/rehydration deduplicates identical carried active state; registered-capability and lifecycle first-party-tool spill, same-session paging, spill/failure invariant retention, a bounded typed continuation record for goal/plan/progress/decision/approval/open-question/test/file/citation state, automatic source-bound first goal plus exact carry/supersession provenance, and optional cooperative same-host POSIX reader/writer fencing are implemented. The fresh deterministic 100-turn real-Agno/SQLite gate passes 11/11 native calls, 14 compactions, 14 integrity loads, and three reopens. Its pinned local Ollama companion passes 100 provider-backed turns, 11/11 native calls, 14 compactions, three reopens, and exact typed head/middle/tail input plus canonical tool-result recovery. The certified Agno 2.9 recovery envelope additionally survives process death across one governed tool and its approval wait. | Complete custom/dynamic/outer-model output routing, multi-host/non-cooperating-writer fencing, cloud-provider/tool-model breadth, nested/parallel or raw extension tool recovery, production-duration soak, and adversarial/model-backed continuation-drift certification still block worry-free unattended long runs. |
| P0 | Learned Knowledge requires vector-backed Agno `Knowledge`; the model path is recall-only; scoped candidates, events/outbox, review, promotion, rollback, restart-safe discovery, digest-bound exact-name Agno observation, content-free evidence, bounded verified coordination, and a dedicated owner-scoped database-clock leased/fenced/checkpointed worker exist on SQLite/PostgreSQL. Schema-v6 append-only records now distinguish retrieval from application, bind exact runs/targets/evidence, attach one independent outcome, and produce a non-mutating effectiveness recommendation without contending on promotion CAS. Independent pools cannot steal a heartbeat-renewed slow observation, and an actual dead child is reclaimed only after expiry with a higher fence. One frozen local Ollama/LanceDB gate now proves 6/6 objective wins against an identical no-learning control across held-in/out/alias-transfer in three consecutive runs. | Silent/direct shared writes and blind ambiguous retries are contained; custom-backend observers, partition/failover/soak certification, production vector-backend/all-store deletion proof, and previous-version/multi-provider/long-duration benefit certification remain. |
| P0 | Schema-v12 SQLite/PostgreSQL scheduling now has deterministic occurrence/attempt identity, database-clock atomic claims, renewable leases and fences, lifecycle binding/reattachment, bounded retries/jitter, misfire and overlap policy, concurrency groups, and explicit learning consent. | Legacy JSON apply/cutover, tenant-aware administration, retention/OTEL/operator UX, and production scheduler partition/failover/soak certification remain. Learning reconciliation uses its smaller dedicated worker rather than disguising observation as a model job. |
| P0 | The nonexistent `LearningMachine.optimize_memories()` maintenance path has been removed. | No silent maintenance promise remains; a Curator operation must pass version-specific behavioral certification before introduction. |
| P1 | Session Context is exposed through immutable `LearningPolicy` profiles and the deprecated compatibility flag. | Run admission requires stable scope, and update work is bounded. Behavioral long-run certification remains. |
| P1 | Skills use Agno-native progressive disclosure: the model sees a trust-filtered catalog, calls governed `get_skill_instructions`, receives one bounded eligible local skill, and continues under its final-boundary tool allowlist. | Community skills, model/context/schema-changing skills, and inline commands remain explicit-only; arbitrary resource preloading and production provider breadth remain uncertified. |
| P1 | Configured MCP servers use SDK 2.0 and the `2026-07-28` protocol through two deferred tools with stdio/Streamable HTTP, structured results, schema-digest drift checks, run ownership, and governed conservative effects. | OAuth/enterprise identity, resources, prompts, subscriptions, Apps, Tasks, extensions, multi-round-trip input, real-network interoperability, and hostile-server/soak certification remain. |
| P1 | First-party effects are explicit and unknown additions fail closed. Plugins and packs share the `CapabilitySpec` path; configured MCP discovery/call and explicitly attested read-only provider queries are governed; raw extension/provider/caller-MCP tools fail lifecycle admission. | Effectful/provider-tool context ingress still needs explicit reconciliation; trusted MCP installations also need server-policy metadata before any call can be classified less conservatively. |
| P1 | Host-declared capability-only children now have durable handles, bounded grants, joins, recursive cancellation, active-worker deadlines, reported token/cost settlement checks, typed partial-failure collections, lossless result-artifact handoff, model-visible `DeclaredChildTemplate` tools, and authenticated remote start/list/result ingress. Explicit profiles omit/reject raw subagents and Agno Team presets; named legacy alone retains them. | Host, model, and remote orchestration use the same authority-bound declaration; the raw migration surface cannot accidentally enter an explicit profile. |
| P1 | Legacy JSON scheduling remains compatibility-only; schema-v12 SQLite/PostgreSQL scheduling provides leased/fenced durable execution, deterministic idempotency, bounded retry, and stale-attempt recovery. | JSON requires migration, while the durable path still needs tenant administration, retention/OTEL/operator UX, and production partition/failover/soak proof. |
| P1 | Lifecycle, operation, and learning ledgers persist ordered events/cursors/outboxes; lifecycle observers commit first as minimized `trajectory.*`, and a leased worker adds at-least-once delivery, bounded retry, poison isolation/quarantine, and exact-CAS replay through an owner/scope-authorized audited host API. | Reconnect/recovery/evaluation export works, but native/direct ingress, independent audit anchoring/operator UI, multi-destination state, schema registry, and client adapters remain open. |
| P1 | A content-free schema-v1 projection now converts durable events into HMAC-linked OpenTelemetry logs and low-cardinality event/token/cost counters; exact-owner/scope SDK plus read-only stable-JSON CLI explain a run without copying content. | Live GenAI spans/trace correlation, exporter-health SLOs, support bundles, Collector backpressure/queue-loss, and production volume/TLS/RBAC/key-rotation/retention certification remain T13. |
| P1 | Local SQLite/JSON migration has preflight plus plan/apply/verify/cutover/rollback; service migration now has streamed PostgreSQL 17 scanning, an evidence-bound plan, bounded deterministic transformation, provenance/control tables, idempotent batched apply, independent verification, cutover receipts, reverse-order drift-refusing rollback, automation-safe CLI dry-runs, representative process-death resume, and a 5,000-row/three-database/least-privilege matrix. | Deployment-enforced writer fencing, complete checkpoint-kill/rogue-writer/credential-rotation/TLS/native-backup/production-volume matrices, artifacts/keys/PITR, RPO/RTO, and production certification remain T14b/T5b. |
| P2 | The default permission mode is `bypass`, the full default tool set is eager, and prompt-injection defense is mainly instructional. | The easiest embedding path has a broader trust surface and higher context cost than a safe default should. |
| P2 | The development lock is Agno 2.9.0; 2.6.4 remains the legacy lane and 3.0.0a1 is quarantined preview-only after a release-by-release audit. | Hosted certification, future release monitoring, and evidence-gated Agno 3 adoption remain required as upstream moves quickly. |

The implementation evidence and corrected capability matrix live in
[Harness gap analysis](harness-gap-analysis.md). The target contracts are specified in
[Harness architecture](architecture.md).

## What the leading harnesses get right

The important lesson is that the leaders optimize different layers. `agnoclaw` should
combine the strongest patterns without copying their product boundaries.

| System | Strongest pattern to adopt | Pattern not to copy blindly |
|---|---|---|
| Pi 0.84.1 | A minimal coding-agent core plus a new lane-based session contract: immutable conversation tree, per-lane durable operation records, global facts, shared sequence, atomic JSONL/SQLite backends, deterministic effect stepping, deferred-provider handles, and candid uncovered-test accounting. | The promoted durable `AgentHarness` is explicitly still a scaffold, its serving model assumes one writer per session, and main-branch harness v3 is unshipped. Pi also deliberately omits permissions, MCP, and built-in subagents. Adopt its invariants and ergonomics, not its maturity or product boundary by assumption. |
| Claude Code / Agent SDK | Automatic compaction, a compaction thrash guard, deferred MCP tool schemas, real Skill and Task tools, isolated subagent context, resumable sessions. | Product-specific UX and provider coupling should stay outside the core. |
| OpenAI Codex / App Server | One common harness across clients; stable thread/turn/item protocol; capability handshake; durable thread operations; bidirectional approvals; deferred tools. | A server protocol should adapt the embedded kernel, not become the only runtime. |
| OpenAI Symphony | Repository-owned workflow policy, isolated workspaces, bounded concurrency, reconciliation, retries/backoff, stall detection, and issue/run status. | Issue-tracker orchestration is an optional control-plane adapter, not a core assumption. |
| OpenClaw | Gateway as session source of truth; serialized per-session lanes; append-only transcripts; overflow compact-and-retry; pre-compaction memory flush; operational retention controls. | A personal-agent gateway and channel ecosystem should not be pulled into the core library. |
| LangGraph | Checkpoints at step boundaries, durable interrupts, state inspection, replay/fork/time travel, and fault recovery. | Graph authoring should be optional; simple agents should not have to become graphs. |
| Pydantic AI Harness | Composable capabilities; tiered compaction; lossless tool-output spill; step/event persistence and effect ledger; conversation search; multiple durability integrations. | `agnoclaw` should remain Agno-native and avoid a compatibility layer that recreates another framework. |
| Letta | Explicit in-context memory blocks versus retrieved archives, persistent state as a service, and stateful/memory-specific evaluations. | Always-visible memory can consume too much context; scope and boundedness remain essential. |
| Agno | Model-agnostic Agent runtime, background/cancel/continue primitives, AgentOS, Learning Stores, HITL, traces, scheduler, and evaluations. | Do not duplicate Agno capabilities; validate, compose, and expose them through clearer harness profiles and contracts. |

Lilian Weng's July 2026 synthesis independently reinforces this direction: the harness
is an OS-like runtime, files preserve long-horizon evidence, child jobs must be explicit,
and self-improvement needs component/experience/decision observability with permissions
and evaluators outside the loop. The reconciliation added itemized context evolution,
falsifiable change hypotheses, causal failure mining, immutable evaluator controls,
held-out transfer, diversity, and Pareto acceptance. See the
[full research audit](lilian-weng-harness-audit.md).

## The target product: one API, three profiles

Profiles should be immutable, inspectable bundles. A profile chooses defaults; every
component remains replaceable.

| Profile | Intended use | Default behavior |
|---|---|---|
| `quick` | One-shot extraction, classification, or answer | No persistence, minimal prompt, caller-selected tools, no learning, low overhead. |
| `durable` | Hours/days-long work and resumable automation | Persistent event log, checkpoints, effect ledger, retries, artifacts, pause/resume/fork, budgets, recovery. |
| `service` | Multi-user embedded or AgentOS deployment | Fail-closed identity/tenant scope, concurrency isolation, external event and artifact stores, OpenTelemetry, no implicit global learning. |

Session continuity is selected with `session_id`; it is not a runtime profile.
`HarnessConfig.local_safe(...)` is an orthogonal posture preset that can strengthen
`quick` or `durable`. `HarnessConfig.legacy()` is the only compatibility preset.

Avoid `advanced=True` style flags. Profiles should be plain data:

```python
config = HarnessConfig.durable()
harness = AgentHarness(
    model=model,
    config=config,
    runtime_store=store,
    artifact_store=artifacts,
)
```

## The small public grammar

An ordinary developer learns three runtime objects:

1. **`AgentHarness`** configures the reusable, immutable harness and starts work.
2. **`HarnessSession`** starts related runs under one durable conversation/work ID.
3. **`HarnessRun`** exposes `wait`, `status`, `events`, `output`, `cancel`, and one typed
   `command` entry point.

Profiles are `quick`, `durable`, and `service`. Extension authors additionally see one
`RuntimeStore`, one `ArtifactStore`, and one `CapabilitySpec`. The current 0.12 preview
also exposes `CapabilityRegistry`/`CapabilityExecutor` as an advanced host seam.
`AgentHarness(capabilities=[...])` now owns the normal registered-capability
composition and automatic version-pinned Agno binding; the runtime ledger, outbox, snapshots,
OperationGateway, policy engine, reducers, projections, workers, and per-run Agno
materializer must not become peer abstractions every ordinary caller assembles.

Scheduler, AgentOS, CLI, TUI, remote protocol, and distributed durable-execution
providers are adapters around this kernel.

## Non-negotiable behavioral contracts

### 1. Concurrency isolation

- A configured harness is immutable after construction.
- Each run gets a fresh runtime view or cloned Agno Agent.
- No run mutates shared prompt, tools, schema, argument bindings, session identity,
  approval cache, or step state.
- Same-session turns are serialized by a lane; different sessions may run concurrently
  under explicit global and tenant limits.
- Concurrency is tested with adversarial overlap, not inferred from `contextvars`.

### 2. Durable lifecycle

Use a stable identity hierarchy:

```text
tenant -> user -> session -> run -> attempt -> effect/tool_call
                              \-> child run(s)
```

Every run has a typed state machine:

```text
created -> queued -> running -> waiting/paused -> completed
                         |           |          -> cancelled
                         |           +----------> waiting_for_reconciliation
                         +----------------------> failed / failed_with_unknown_effects
```

Durability must distinguish:

- conversation continuation from execution resumption;
- safe settled snapshots from interrupted snapshots;
- a failed tool call from an unknown side effect after a crash;
- read-only reconstruction from effect-capable fork/re-execution.

### 3. Context lifecycle

Context management is a pipeline, not one summarization flag:

1. Keep system and capability prefixes stable for provider caching.
2. Load skills and large tool schemas on demand.
3. Deduplicate superseded file reads and clear old tool results cheaply.
4. Spill large tool outputs losslessly to artifacts with bounded paged reads.
5. Preserve tool-call/result pairing and recent turns.
6. Summarize older state into a structured continuation record containing goal,
   progress, decisions, open questions, tests, and relevant files.
7. Persist the compaction boundary and retain searchable full history.
8. On provider overflow, compact once and retry with a thrash guard.

The runtime should expose a context manifest showing tokens by prompt layer, skills,
tool definitions, messages, tool results, retrieved memory, and reserved output.

The current v0.12 preview implements artifact-first manual replacement plus an opt-in
90% proactive / 97% deterministic emergency controller with a 70% release boundary
and process-local session admission. It also retries one exact non-streaming model
invocation after an Agno-classified overflow, unless a tool call was observed. Lossless
pre-provider spill is implemented for governed host/plugin/pack registered-capability
and lifecycle first-party-tool results—including configured MCP calls—with bounded
same-session paging and artifact/failure invariant retention. Governed context/raw-MCP adapters, direct
compatibility tools, outer-model output, automatic typed-continuation extraction and
merge beyond the source-bound goal/carry path, live-provider breadth, multi-host
save-boundary fencing, and adversarial/model-backed drift certification remain
required before this target is complete.

### 4. Capabilities and tools

Every tool needs machine-readable metadata:

- input and output schemas;
- `read_only`, `destructive`, `idempotent`, and `open_world` effects;
- required scopes and secrets;
- timeout, retry, and concurrency policy;
- sandbox/runtime requirements;
- whether the result can be cached;
- whether execution can become a durable task.

Tool catalogs should be searchable and lazily loaded when large. Below roughly ten
small tools, eager schemas remain faster and simpler. Unknown or untrusted tool
metadata is a hint, never an authorization decision.

MCP support should target the current protocol rather than “tool calls over SSE”:
Streamable HTTP, authorization, capability negotiation, resources, prompts, tools,
structured content/output schemas, pagination, notifications, reconnect/resume,
sampling, roots, elicitation, and durable tasks where supported.

### 5. Safety and trust

Adopt **model-last security**:

- deterministic code establishes identity, tenant, scopes, sandbox, path/network
  boundaries, and approvals;
- the model proposes actions inside those bounds;
- untrusted external content carries provenance/taint and cannot rewrite policy;
- secrets are injected only at the execution boundary and redacted from prompts,
  events, artifacts, and errors by default;
- side-effecting tools use idempotency keys and an effect ledger;
- approval means approval of an exact normalized action, not a broad tool name.

Bypass remains an explicit legacy/development permission choice, never a runtime profile
or the default for embedded or service use.

### 6. Human steering

A run handle keeps one compact command grammar:

```python
from agnoclaw.commands import Fork, Pause, Respond, Resume, Steer

run = await harness.start("Investigate the outage", session_id="incident-42")
await run.command(Steer("Prioritize database evidence"))
await run.command(Pause())
await run.command(Respond({"approved": True}))
await run.command(Resume())
await run.cancel()
fork = await run.command(Fork(from_step=17))

# Follow-up work is a new immutable run in the same session.
follow_up = await harness.session("incident-42").start(
    "Prepare a customer-safe summary"
)
```

Approvals and elicitation are typed waiting states. They must survive client
disconnects and process restarts in durable profiles. Terminal runs are immutable;
resume never rewrites a completed or failed run.

### 7. Subagents

Subagents are child runs, not string-returning helper calls. They inherit a bounded
subset of parent identity, policy, budget, backend, and cancellation. They get:

- explicit purpose and output schema;
- independent context and checkpoint lineage;
- maximum depth, fan-out, time, tokens, cost, and tool effects;
- lossless artifact/evidence handoff;
- status, steering, cancellation, and deterministic synthesis hooks.

Use delegation only when context isolation, parallelism, or specialization creates
measurable value. A single agent with good tools stays the default.

Implementation checkpoint (2026-08-11): schema-v11 capability-only declared children
now have durable lineage, exact owner/context inheritance, isolated sessions,
deterministic delegation idempotency, subset-only grants, join policies, recursive
cancellation, parent settlement events, handles, rollback tests, and store parity code.
Active workers now commit a wall-time cancellation before local task cancellation;
successful model settlements record stable Agno usage/cost, idempotently observe the
budget, fail on reported excess, and mark missing provider dimensions unverified.
Direct parents now receive deterministic typed outcome sets, explicit partial-failure
helpers, lossless large-result artifact pointers, and scope-checked artifact paging;
child-spec `1.1` can digest-bind a bounded structured result schema, emits content-free
validation evidence, and re-applies the contract during known-success recovery. An
explicit `synthesize_children()` helper injection-frames bounded untrusted evidence and
launches another ordinary capability-only child; it defaults to all-success and grants
no ambient artifact access. `DeclaredChildTemplate` now exposes only bounded task and
delegation identity through the ordinary capability gateway, while AgentOS registers
the same host-owned declarations for authenticated remote child handles and typed
collections.
Recovery now certifies the entire durable ancestry under a fenced claim, restores the
exact child declaration before safe pre-model continuation, preserves its timeout and
output/budget contracts, and reaps terminal-ancestor orphans without model dispatch;
the maximum depth-16 tree passes on both stores. Persisted absolute deadlines,
provider-preflight hard token/cost controls and receipt reconciliation, a first-party
governed cross-child artifact reader, and live PostgreSQL multi-worker/primary-failover/
soak proof remain before this target is complete. The current 36-case PostgreSQL 17
transaction suite, six exact-container bounded single-primary stop/start drills, and
five consecutive 10,000-row owner-isolation/noisy-neighbor/p50-p95-p99/pool-saturation
passes succeed. Five owned two-node drills also pass positive lag, acknowledged-LSN
catch-up, explicit old-primary fencing, planned promotion, and read-write pool
reattachment. A synchronous companion passes five completed exact-standby drills proving
false-ack prevention and zero observed acknowledged-state/event loss after primary
`SIGKILL`; a third gate completes five double-rewind round trips, rejoining both former
writers as read-only synchronous standbys while preserving the exact ledger and fences.
Production controller/fence integration, true split brain, multiple faults, managed
endpoint/rejoin automation, sync-latency availability, and multi-worker soak remain.
Acquired connection loss is a typed non-retryable reconciliation
boundary instead of a raw driver error.
Five consecutive post-service local native dump/restore rehearsals also preserve every
runtime row plus logical schema, index, constraint, sequence, ordered-event, and
idempotency evidence exactly. Encrypted off-host/artifact/key/PITR recovery, corruption
drills, and measured production RPO/RTO remain intentionally unclaimed.
See
[Declared child runs](child-runs.md).

### 8. Learning and self-improvement

Learning is not conversation storage. It is a governed promotion pipeline:

```text
observe -> candidate -> validate -> approve/promote -> recall -> measure -> decay/remove
```

Each learning records scope, source run, evidence, confidence, author, schema version,
created/last-confirmed timestamps, expiry, conflicts, and feedback. Learned behavior
must be reversible and evaluable against a no-learning control.

Use Agno stores by intent:

- User Profile: stable structured personal facts.
- User Memory: useful unstructured observations about one user.
- Session Context: goals, plan, and progress for a long-running session.
- Entity Memory: facts/events/relationships about named entities.
- Learned Knowledge: reusable cross-session insight, backed by `Knowledge` and a
  vector database.
- Decision Log: consequential decisions, rationale, and later outcome.

Never use a default global namespace for multi-tenant data. See
[Learning and self-improvement](learning.md) for the current defect, target profiles,
and validation gates. The preview component/hypothesis and held-in/out/transfer/Pareto
contract now includes an executable scoped paired runner, a fresh Agno-Agent subject
adapter, a runner-schema-1.2 fresh-process boundary, a content-free governed-corpus/
decontamination boundary, a strict immutable/no-network Docker profile with resource
bounds, a confidence-aware gate, and one exact local Learned Knowledge versus
no-learning outcome smoke. It does not yet provide VM/provider-egress
certification, managed/enforced sealed-corpus operations, semantic near-duplicate
certification, or broad/previous-version model-backed benefit certification; see
[Evidence-gated harness self-improvement](self-improvement-evaluation.md).

### 9. Observability and evaluation

The append-only trajectory is the common substrate for:

- live streaming and reconnect;
- traces and metrics;
- recovery and read-only reconstruction;
- audit and incident investigation;
- offline and online evaluations;
- learning outcome attribution.

Events require schema version, monotonically increasing sequence, timestamp, session,
run, parent run, step, tool call, trace/span, model, capability version, policy
decision, usage/cost, and redaction metadata where applicable. Export OpenTelemetry
GenAI conventions, but keep a stable agnoclaw-native event schema because those
conventions are still evolving.

Release quality is determined by the gates in [Harness evaluation](evaluation.md),
not by unit-test count or a capability checklist.

## Keep, change, remove, add

### Keep

- `AgentHarness` as the canonical embedded entry point.
- Agno as the underlying model/session/learning/AgentOS runtime.
- `RuntimeBackend` as the coherent execution-plane seam.
- workspace and Agent Skills interoperability.
- policy, permission, guardrail, hook, event, dependency, and argument-binding seams.
- optional adapters and optional dependencies.

### Change

- Split immutable configuration from per-run mutable state.
- Replace `run/arun`-only lifecycle with `start -> HarnessRun`; preserve `run/arun` as
  convenience methods.
- Replace eager default capabilities with profile-selected and on-demand capabilities.
- Replace warning-only context budgets with tiered automatic management.
- Replace name-based tool categories with declared effects.
- Replace direct child response truncation with artifacts plus bounded previews.
- Keep JSON scheduling explicitly compatibility-only; use the implemented schema-v12
  scheduler interface over durable run handles for new unattended work.
- Make safe embedding the default and keep bypass explicit.

### Remove or stop claiming

- “Automatic compaction” until history is actually rewritten/persisted and overflow is
  recovered.
- unqualified “auto-skill selection” beyond the governed single-skill progressive-
  disclosure path and its explicit trust/tool boundaries;
- “Institutional learned knowledge” until a vector-backed Agno `Knowledge` is required
  and tested.
- “MCP parity” for stdio/legacy-SSE tool discovery.
- parity matrices that mark a feature done without a behavioral conformance test.
- any silent or capability-probed learning maintenance path without observable,
  version-certified behavior.

### Add

- an immutable validated internal harness spec derived from existing configuration and a
  per-run runtime factory;
- an evolved `HarnessRun`, RuntimeStore, snapshots, operation ledger/outbox, artifacts,
  lineage;
- typed steering/pause/resume/respond/fork commands and waiting states;
- tiered compaction, lossless output spill, conversation search, context manifest;
- the implemented governed Skill capability plus future lazy search across very large
  catalogs;
- complete remaining MCP authorization, resources/prompts, subscriptions, Apps, Tasks,
  MRTR, and extension capability set;
- typed tool effects, idempotency, default secret/DLP redaction;
- learning profiles, tenant-safe scope keys, promotion workflow, and learning evals;
- the implemented content-free OpenTelemetry logs/metrics and read-only run inspection,
  plus remaining live spans, exporter SLOs, support bundles, reconnect cursors, budgets,
  and durable child-run certification;
- the optional first-party etcd or deployment-supplied PostgreSQL writer-authority adapter,
  with watchdog/STONITH explicitly remaining an infrastructure responsibility;
- tested Agno compatibility matrix and automated upstream contract tests.

## Phased roadmap

Sequence matters. Adding more tools before fixing state ownership and learning truth
would increase risk.

These phases are the internal delivery gates for the complete `0.12.0` release described
in [the versioned release plan](releases/v0.12.0-plan.md). They are not separate final
releases and do not move any core harness work past final `0.12.0`.

### Phase 0 — truth and compatibility

- Publish this strategy, architecture, learning guide, and evaluation contract.
- Correct README and gap claims.
- Turn the complete [Agno release-practice audit](agno-release-practices.md) into tested
  adopt/adapt/avoid decisions. Develop against 2.6.4 legacy, 2.9.0 primary, and a
  non-production newest-Agno-3 preview; generate the final range from RC evidence.
- Add known-limitations and capability maturity labels: stable, preview, experimental,
  planned.

Exit gate: every public claim maps to a test or is explicitly marked planned.

### Phase 1 — isolated run kernel

- Derive one immutable internal spec from the existing public configuration and create a
  fresh per-run Agent/runtime; do not add a peer public configuration hierarchy.
- Implement same-session lanes plus bounded/time-limited tenant-fair cross-session
  admission; expose content-free load/overload metrics.
- Evolve `HarnessRun` with durable events, cancellation, status, and one typed command
  entry point. Follow-up creates a new run through `HarnessSession.start()`.
- Route existing `run/arun` through the new kernel.

Exit gate: concurrency isolation suite passes under prompt/tool/schema/session overlap;
existing public API compatibility suite passes.

### Phase 2 — trajectory, artifacts, and real context management

- Persist append-only step events, settled snapshots, lineage, and effect states.
- Add in-memory/SQLite/PostgreSQL RuntimeStore implementations and one
  content-addressed ArtifactStore interface.
- Implement tiered compaction, output spill, structured continuation summaries,
  history search, overflow compact-and-retry, and compaction thrash protection.
- Add typed pause/resume/respond/steer/fork commands and reconnect cursor semantics.

Exit gate: kill/restart recovery and 100+ turn soak tests complete without orphaned tool
pairs, duplicate known side effects, or unrecoverable context overflow.

### Phase 3 — correct learning

- Add static `LearningPolicy`, run-resolved `LearningScope`, and
  `knowledge=`/vector-store integration.
- Expose Session Context for durable profiles.
- Require tenant/agent/user/session scope keys; remove implicit cross-tenant global use.
- Add candidate/provenance/feedback/promotion/expiry contracts around Agno stores.
- Replace maintenance no-op with version-aware, tested maintenance operations.

Exit gate: write, recall, isolation, conflict, deletion, and benefit evals pass; learned
configuration fails fast when prerequisites are missing.

### Phase 4 — capability plane and safety

- Introduce capability manifests and tool effect metadata.
- Add real skill activation and lazy tool search.
- Modernize MCP and integrate MCP annotations as untrusted hints.
- Add default taint/provenance, secret redaction, exact-action approval, idempotency, and
  policy conformance tests.

Exit gate: untrusted-content and cross-tenant red-team suites pass; tool selection quality
does not regress as the catalog scales.

### Phase 5 — durable delegation and adapters

- Complete the declared-child preview with restart-safe absolute deadlines,
  provider-preflight token/cost reservation and receipt reconciliation,
  governed cross-child artifact reads, distributed orphan sweeps, and multi-worker proof. Keep
  the implemented model/remote template ingress, declaration-bound schemas, and
  governed synthesis under their compatibility/adversarial gates.
- Finish the implemented durable scheduler's release proof: legacy cutover, tenant
  administration, retention/exporter-health/operator UX, certified learning-observer integration,
  and production PostgreSQL partition/failover/soak.
- Add optional Temporal/DBOS/Prefect-style durability adapters only where Agno/AgentOS
  does not provide the needed guarantee.
- Version the remote/App Server adapter around session/run/item primitives while mapping
  external thread terminology at the protocol boundary.

Exit gate: multi-process and multi-worker recovery tests pass; embedded quick profile
latency remains within its budget.

## Staying current without chasing fashion

No harness can be “worry-free forever.” The durable answer is an update system:

- weekly upstream release-delta triage and continuous compatibility CI against Agno and
  MCP SDKs;
- quarterly primary-source harness landscape review;
- a public capability maturity and conformance matrix;
- versioned prompts, capability manifests, event schemas, and migrations;
- representative trajectory evals checked into the repository;
- removal budgets and deprecation windows, so the default stays small;
- a six-month architecture review driven by measured failures and costs, not feature
  announcements.

This creates a low-regret harness that can absorb future advances without rewriting the
embedding API.

## Primary research sources

### Harness and long-running-agent design

- [OpenAI: Harness engineering](https://openai.com/index/harness-engineering/)
- [OpenAI: Unlocking the Codex harness](https://openai.com/index/unlocking-the-codex-harness/)
- [OpenAI Codex App Server protocol](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
- [OpenAI Symphony specification](https://github.com/openai/symphony/blob/main/SPEC.md)
- [Anthropic: Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [Claude Code: How it works](https://code.claude.com/docs/en/how-claude-code-works)
- [Claude Code: Tool search](https://code.claude.com/docs/en/agent-sdk/tool-search)
- [Pi 0.84.1 release](https://github.com/earendil-works/pi/releases/tag/v0.84.1)
- [Pi released durable harness design](https://github.com/earendil-works/pi/blob/v0.84.1/packages/agent/docs/harness-v2.md)
- [Pi promotion test matrix](https://github.com/earendil-works/pi/blob/v0.84.1/packages/agent/docs/harness-v2-test-matrix.md)
- [Pi 0.84 research reconciliation](pi-harness-audit.md)
- [OpenClaw session management and compaction](https://github.com/openclaw/openclaw/blob/main/docs/reference/session-management-compaction.md)
- [OpenClaw queue](https://docs.openclaw.ai/queue)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [Pydantic AI Harness](https://pydantic.dev/docs/ai/harness/)
- [Pydantic AI Harness step persistence](https://pydantic.dev/docs/ai/harness/step-persistence/)
- [Pydantic AI durable execution with Temporal](https://pydantic.dev/docs/ai/capabilities/durable_execution/temporal/)

### Learning, skills, interop, and observability

- [Lilian Weng: Harness Engineering for Self-Improvement](https://lilianweng.github.io/posts/2026-07-04-harness/)
- [Lilian Weng research reconciliation](lilian-weng-harness-audit.md)
- [Agno Learning Machines](https://docs.agno.com/learning/overview)
- [Agno learning modes](https://docs.agno.com/learning/learning-modes)
- [Agno Learned Knowledge example](https://docs.agno.com/examples/learning/basics/learned-knowledge)
- [Agno release-practice audit](agno-release-practices.md)
- [Letta context hierarchy](https://docs.letta.com/guides/core-concepts/memory/context-hierarchy)
- [Letta stateful-agent evaluations](https://docs.letta.com/guides/evals/concepts/overview)
- [Agent Skills specification](https://agentskills.io/specification)
- [Agent Skills client implementation guide](https://agentskills.io/client-implementation/adding-skills-support)
- [MCP 2026-07-28 release](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- [MCP Python SDK v2 client](https://py.sdk.modelcontextprotocol.io/client/)
- [MCP Python SDK v2.0.0](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0)
- [MCP tool annotation trust](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/)
- [OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/)
- [OpenTelemetry GenAI agent spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md)
- [Agnoclaw observability contract](observability.md)
- [Agno 2.9.0](https://github.com/agno-agi/agno/releases/tag/v2.9.0)
- [Agno 3.0.0a1](https://github.com/agno-agi/agno/releases/tag/v3.0.0a1)
