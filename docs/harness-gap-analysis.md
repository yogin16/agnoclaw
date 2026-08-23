# Harness gap analysis

Status: implementation-aligned audit

Last reviewed: 2026-08-23

Repository version: 0.12.0 development branch

Locked runtime reviewed: Agno 2.9.0 (2.6.4 compatibility; 3.0.0a1 preview)

This document records what the repository does today and what remains. It replaces the
old Claude Code/OpenClaw parity checklist, which treated the existence of code as proof
of production-ready behavior.

Use the following labels:

- **stable**: documented public behavior with production-oriented conformance coverage;
- **preview**: useful implementation, but incomplete production/compatibility contract;
- **experimental**: behavior or API may change and has material limitations;
- **planned**: design only.

The definitions and release gates are in [Harness evaluation](evaluation.md).

## Executive summary

`agnoclaw` is a configurable embedded Agno wrapper with runtime policy seams, a
coherent execution backend, workspace/skills, structured events, optional AgentOS
export, a preview lifecycle facade, two transactional RuntimeStores, an operation/effect
ledger, store-issued execution leases, artifact-backed result recovery, and exact
pre-model request-checkpoint continuation. On the certified Agno 2.9 path it also owns
per-provider operations/artifacts, validates Agno's native `tool-batch` checkpoint after
a governed registered capability, and reconstructs an earlier durable approval wait
from exact provider/approval/frozen-authority evidence. The
effect-safe model/capability slice can overlap through isolated Agno Agents; supported
host-local built-ins get fresh run-owned Agents/tools but retain single-flight effects. Registered capabilities have
durable exact approval-before-effect with external-host settlement;
stateful/custom/streaming overlap still rejects rather than risking shared mutation. It
is not yet safe to present the whole system as restart-continuable or institutionally
self-improving.

The immediate blockers are:

1. incomplete isolated execution: the supported local built-ins now materialize and
   release per run and their lifecycle effects settle durably, but effects plus
   optional/custom backends still serialize;
2. lifecycle/operation truth, execution leases, successful-result recovery, first-party
   per-tool settlement, an exact pre-model request checkpoint, and one governed Agno
   2.9 tool/approval continuation envelope exist, but nested/parallel, raw/extension,
   streaming, parser/output-model, direct-opaque-model, and custom-factory checkpoints,
   universal extension
   routing, and autonomous deployment scheduling do not; bounded exact-owner startup
   recovery and evidence-bound operation reconciliation are implemented;
3. host-local proactive/fenced reactive compaction and governed output spill exist, and
   one pinned local Ollama 100-turn tool-bearing run passes, but cloud-provider breadth,
   cross-process fencing, drift certification, and universal large-output routing do not;
4. learning promotion is scoped/evaluated and direct personal/session administration
   is post-verified, and schema-v12 durable scheduling can run consented work, but
   production reconciliation-worker database partition/failover/soak proof,
   service-wide deletion proof, production migration certification, and measured
   benefit remain; local and PostgreSQL learning/schedule migration lifecycles are
   implemented, with the service path still awaiting production certification;
5. skill activation now includes the governed `get_skill_instructions`
   progressive-disclosure tool; scaling evidence for it plus MCP
   authorization/extensions and real-network certification beyond the implemented
   modern tool ingress remain;
6. explicit registered host/plugin/pack capabilities and currently constructed
   first-party tools now route through the effect ledger. Raw caller, plugin, pack,
   context-provider, and caller-supplied MCP surfaces normalize to opaque evidence and
   are rejected by `start()` before run creation rather than misclassified. A governed
   run-owned read-query context-provider adapter is implemented; provider writes/tools,
   skill/independent-child/job, and richer MCP adapters remain open.
7. content-free durable OpenTelemetry logs/metrics and exact-owner read-only run
   inspection are implemented; live spans/trace correlation, exporter-health SLOs,
   support bundles, Collector outage/backpressure soak, and production
   cardinality/retention/key-rotation drills remain.

## Current capability matrix

| Area | Current behavior | Maturity | Key limitation / next proof |
|---|---|---|---|
| Embedded `run/arun` | Explicit `quick`/`durable`/`service` calls adapt through `start()` plus `wait()`; async raw streams attach a bounded lifecycle presentation; sync calls reuse a harness-owned event loop and sync streams detach without cancelling their run; named legacy retains direct behavior; string/default models and digest-bound `AgnoModelFactory` custom transports are fresh/run-owned; effect-safe model/capability runs overlap and supported host-local built-ins including configured MCP are run-owned | preview | One harness instance must use one loop-ownership style; cross-loop sync/async mixing fails closed. Direct opaque model objects and raw pack/plugin/context/caller-MCP ingress are rejected; only the named-legacy escape hatch retains specialized team/subagent paths outside lifecycle. |
| Lifecycle `start/get_run` | Preview `HarnessRun` with wait/status/events/output/cancel/command, typed reconciliation waits, artifact-backed provider-text replay, reattachment, exact service-wide session leases, bounded process admission with tenant round-robin fairness/metrics, shutdown ownership, trajectory projections, successful-result recovery, exact pre-model continuation, a certified Agno 2.9 governed tool/approval continuation envelope, and bounded exact-owner startup/reconciliation scanning | preview | No distributed weighted fairness, general nested/parallel/raw/streaming/parser/output-model continuation, autonomous owner enumeration, native non-text provider-event replay, or full client-adapter parity yet. |
| Runtime stores | Transactional SQLite and bounded-pool PostgreSQL schema-v12 ledgers for lifecycle, child lineage/join/cancellation, owner-authorized dead-letter audit/replay, retention, operations/reconciliation evidence, leases, artifacts, exact approvals/grants, scheduler jobs/attempts, and owner/age-keyset discovery | preview | The existing service matrix plus scheduler SQLite/PostgreSQL parity, a 64-worker SQLite single-occurrence race, and a real PostgreSQL multi-connection claim/reclaim/retry race pass. Existing backup/restart/promotion/synchronous-loss/role-rotation/writer-authority/secure-etcd evidence remains; production scheduler partition/soak, controller election, arbitrary-client/paused-host fencing, encrypted off-host/artifact/key/PITR recovery, and corruption gates remain open. |
| Operations/effects | Immutable intent/effect domain, fenced/idempotent settlement, explicit recovery, request/result artifacts, pre-model continuation, evidence-bound reconciliation/continuation, per-provider operations/artifacts, `start()` model-call integration, content-minimized Agno usage/cost evidence, registered-capability approval-before-effect, first-party/configured-MCP settlement, governed read-only provider queries, the eight-scenario capability-effect crash gate, the three-scenario outer Agno restart gate, a three-scenario Agno 2.9 tool-checkpoint gate, and an atomic approval-wait restart gate—all with zero duplicate calls/effects in their certified envelopes | preview | Raw custom/plugin/pack/context/caller-MCP tools fail lifecycle admission; effectful context, certified live-provider observers/receipts and billing reconciliation, richer authenticated MCP adapters, and nested/parallel/raw/streaming/parser/output-model continuation remain. |
| Artifacts | Scoped content-addressed local bytes, checksums, bounded paging, key-provider encryption seam, atomic result references/restart loading, and opt-in registered-capability/first-party-tool model-context spill | preview | Custom/dynamic/outer-model spill, remote object adapter, deletion/retention automation, and DR certification remain. |
| Capability descriptors/execution | Immutable `CapabilitySpec`, scope-filtered lazy registry, bounded catalog/selection, admission-bound executor, governed plugin/pack/read-context registrations, deterministic opaque normalization/lifecycle rejection for raw caller and extension tools, an explicit first-party effect manifest, and deferred MCP 2.0 tools | experimental | Effectful/provider-tool context, skill/independent-child/job, and richer MCP auth/extension ingress remains. |
| Model portability | Agno model strings/adapters and provider-specific prompt caching support | preview | Establish tested provider/version matrix and cache regression tests. |
| Dependencies/session state | Per-run values merge over defaults and use active run context; immutable construction seeds are copied into each isolated Agent and opaque values fail durable/service classification | preview | Arbitrary host-managed dependency factories and complete tool-observed overlap still need certification. |
| Tool argument binding | Hides caller-bound values from model schema and injects at dispatch | preview | Current implementation mutates live Function/Agent state during a run. |
| Runtime backend | One backend seam for shell/files/skills/browser; host and LLMSandbox paths | preview | Backend security/conformance matrix and recovery semantics are incomplete. |
| Files and shell | Rich local tools including background shell and atomic multi-edit | preview | Large outputs, effect metadata, idempotency, and durable task ownership are missing. |
| Browser/media/notebook | Optional toolkits | experimental | Need capability metadata, artifact handling, security suites, and backend parity. |
| Web | Multiple search backends and fetch | experimental | Output truncation, provenance/taint, citation evidence, SSRF/DLP hardening needed. |
| Skills | Discovery, precedence, explicit activation, governed `get_skill_instructions` model activation, trust levels, restrictions, fork/dispatch metadata | preview | Tool-discovery scaling evidence (docs/evaluation.md) is unrecorded; supply-chain manifest/signing and concurrency-safe scope needed. |
| Packs/plugins | Inspect/install/trust/load hooks, governed capability registrations, and fail-closed raw-tool lifecycle containment | experimental | Stronger pinning, checksums, signatures, isolated materialization, and migrations. |
| MCP tools | SDK 2.0 / `2026-07-28`; two-tool deferred discovery/call; stdio, Streamable HTTP, explicit legacy SSE; pagination, structured results, schema-digest drift checks, run-owned quick/legacy clients, conservative lifecycle settlement | preview | OAuth/enterprise identity, resources/prompts/subscriptions/Apps/Tasks/extensions/MRTR, real-network reference servers, reconnect/expired-auth, and hostile-server/soak gates remain. |
| Workspace | Hierarchical Markdown context, hooks, memory files | preview | Prompt size, authority/conflict rules, trust boundary, and context manifest needed. |
| Policies/permissions | Run/tool checkpoints, several permission modes, approver adapters, exact durable registered-capability requests/grants with final-boundary reauthorization, exact-decision retry idempotency, and certified Agno 2.9 approval-wait process restart for one governed sequence | preview | Legacy name-based ingress, permissive default/bypass migration, universal approval coverage, and restart continuation outside the certified envelope remain. |
| Guardrails | Path/network checks and sandbox modes | preview | Prompt-injection/taint, secret/DLP, symlink/backend red-team matrix incomplete. |
| Events | Lifecycle signals commit first as content-minimized `trajectory.*`; the outbox worker provides at-least-once batches, exact ack/deferral, bounded retry, one-at-a-time poison quarantine, and exact-CAS replay through owner/scope-authorized immutable service history | preview | Direct/native ingress, schema registry, independent audit anchoring/operator UI, tenant fairness, multi-destination state, client adapters, and production exporter soak remain. |
| Observability/inspection | Allowlisted schema-v1 durable projection with domain-separated HMAC IDs, registered event cardinality, flat OpenTelemetry logs, low-cardinality event/token/cost counters, exact-owner/scope SDK inspection, and read-only SQLite/PostgreSQL stable-JSON CLI | preview | Live GenAI spans/trace correlation, exporter-health SLOs, multi-destination receipts, support bundles/UI, Collector backpressure/queue-loss, production TLS/RBAC/key rotation/retention and volume/cardinality certification remain T13. |
| AgentOS | Optional adapter/export plus admin/debug surfaces | experimental | Verify against supported AgentOS versions and ensure all routes preserve harness runtime semantics. |
| Scheduler/heartbeat | Compatibility heartbeat/JSON cron plus schema-v12 SQLite/PostgreSQL job revisions, deterministic occurrences/attempts, database clock, renewable leases/fences, lifecycle identity binding/reattachment, bounded retries/jitter, misfire/overlap policy, concurrency groups, manual trigger/history, and explicit learning consent | preview | Trusted host administration only; no JSON apply/cutover, scheduler-history retention, OTEL/operator UI, certified learning observer wiring, or production PostgreSQL partition/failover/soak proof. |
| Migration | Versioned Python/CLI preflight and certified local apply/verify/cutover/rollback; streamed `REPEATABLE READ, READ ONLY` PostgreSQL 17 scanning and deterministic transformation; credential/scope/schedule/backup/evidence-bound service plans; provenance/control tables; batched resumable apply; independent source/target/unowned-write verification; receipt-only cutover; reverse-order drift-refusing rollback; dry-run and stable automation contracts; representative process-death plus 5,000-row/three-database/least-privilege drills | development lifecycle | Complete checkpoint-kill, rogue-writer, credential-rotation/TLS, native-backup/production-volume drills, encrypted off-host/artifact/key/PITR recovery, deployment-enforced writer fence, online reverse migration, production RPO/RTO, and certification remain T14b/T5b. |
| Subagents/teams | Host-declared capability-only child runs share the lifecycle kernel with durable lineage, deterministic identity, isolated session, subset grants, joins, recursive cancellation, handles, active-worker wall deadlines, settlement-time reported token/cost checks, declaration-bound structured results, typed partial-failure/artifact handoff, explicit governed synthesis, model-visible template capabilities, authenticated remote start/list/result, and fenced restart recovery with full-chain/depth-16 orphan reaping; explicit profiles omit/reject raw `spawn_subagent`, named subagents, and Agno Team presets before effects | preview foundation | Named legacy retains those raw migration paths. Operational meters are not persisted absolute deadlines or provider-preflight hard ceilings; first-party cross-child artifact reads, distributed orphan sweeps, production PostgreSQL partition/failover control-plane integration, and multi-worker soak remain. |
| Tool-result compression | Agno CompressionManager for tool results | experimental | This is not full conversation context management. |
| Session summaries | Agno summary manager and callbacks | experimental | Summary is not a complete resumable checkpoint or compaction record. |
| Context budget | Model-tokenizer or deterministic fallback counts with typed 80/90/97% recommendations plus opt-in 90/97% automatic replacement and 70% release; one pinned local Ollama run crosses prepare/compact/emergency thresholds through 100 turns | preview | Process-local admission works; cloud-provider breadth, cross-process fencing, production soak, and drift certification remain. |
| `summarize_session()` | Explicit summary-only compatibility operation | preview | A summary is not a replacement or recovery checkpoint. |
| `compact_session()` | Governed memory flush, tool-free/typed summary maintenance with deterministic rejection fallback, artifact-first full trajectory archive, original-intent plus spill-artifact/failure invariant retention, bounded typed goal/plan/progress/decision/approval/open-question/test/file/citation records, automatic source-bound initial goal, exact reviewed-record carry/supersession provenance, required-checkpoint-preserving narrative fitting to the automatic release boundary, token-efficient priority indexing, actual Agno history replacement, content-free manifest/checkpoint, bounded automatic/reactive orchestration, opt-in cooperative same-host reader/writer fencing, and passing deterministic plus pinned local-Ollama 100-turn/11-tool-call/repeated-compaction/reopen gates with typed canonical tool-result recovery | preview | Automatic extraction/merge/fidelity for non-goal fields, universal output spill, multi-host/non-cooperating-writer fencing, cloud-provider breadth, process-death/soak, and adversarial/model-backed drift proof remain. |
| Context search/rehydration | Exact-scope bounded lexical archive search, stable item provenance, newest-active deduplication of identical carried structured values, selective read or untrusted-data live injection | preview | Hybrid/semantic retrieval, remote index, retention automation, and behavioral recall quality gates remain. |
| User Profile/Memory | Explicit immutable store policies, per-run trusted scope, opaque Agno keys, consent/update bounds, and post-verified host read/replace/forget; legacy booleans deprecated | preview | Cross-process writer fencing, replica/backup purge, durable admin audit, and quality evals remain. |
| Session Context | Public explicit policy, stable session scope, and post-verified host read/replace/forget | preview | Cross-process writer fencing, behavioral long-run recall/delete, and compaction integration remain. |
| Entity Memory/Decision Log | Candidate-only institutional policy with tenant-safe namespace; direct save authority removed; SQLite/PostgreSQL candidates/evaluation/quarantine/events/outbox/reconciliation plus bounded observer coordination exist | preview foundation | Snapshot-aware reversible adapters, certified backend observers/durable workers, CRUD propagation, and outcome evaluation remain. |
| Learned Knowledge | Vector-backed `Knowledge`; search-only model path; artifact-backed SQLite/PostgreSQL candidates; reviewed reversible default adapter; evidence reconciliation; digest-bound exact-name Agno observer with content-free artifacts; automatic harness composition; verified coordinator; a dedicated owner-scoped database-clock leased/fenced/checkpointed worker with real independent-pool exclusion and child-process death/reclaim proof; schema-v6 exact-run application/outcome attribution with non-mutating effectiveness recommendations; and one exact Ollama/LanceDB model-backed no-learning benefit smoke | preview | Custom-backend observers, partition/failover/soak certification, migration/deletion proof, production vector-backend certification, and previous-version/multi-provider/long-duration benefit certification remain. |
| Learning maintenance | No implicit maintenance promise | honest removal | Add only a version-certified Curator operation with observable outcomes. |
| `.learnings/` skill | Human-readable correction/error/feature log instructions | experimental | Explicit skill only; no runtime trigger, provenance link, outcome eval, or unified promotion policy. |
| Evals | Contract/property/race/security suites; SQLite/PostgreSQL service probes; recovery-index and learning-archive scale gates; strict Docker containment; real-process SQLite transaction, capability-effect, outer `AgentHarness`/Agno model-stack, Agno 2.9 tool-checkpoint, and durable-approval restart recovery; immutable held-in/out/transfer improvement evaluation; local model-backed learning benefit; deterministic and pinned local-Ollama 100-turn context continuity/reopen plus exact Agno-native canonical tool-result evidence | preview certification system | Cloud-provider breadth, nested/parallel/raw/streaming/parser/output-model crash continuation, multi-host/network chaos, production-duration soak, richer statistical policy, and adversarial drift remain open. |

## Critical implementation evidence

### Shared mutable per-run state

`AgentHarness._apply_tool_scope()` still mutates live `Agent.tools`; run setup can also
swap prompt/session state. A certified path now materializes distinct Agno Agents for
the non-streaming model/capability slice, immutable dependency/session seeds, run-owned
Agno managers, and supported host-local built-ins. The built-in factory also closes its
command/tool resources in reverse order. Built-in effects and other paths use one process-local gate across sync, async, and full stream lifetime;
overlap fails with `HARNESS_RUN_IN_PROGRESS`. This prevents the known leak across a
meaningful first-party slice but is not complete service-mode isolation.

Required change: immutable `HarnessSpec` plus a fresh per-run Agent/runtime, or another
design with equivalent proven isolation. Add adversarial concurrency tests for prompt,
skill, schema, binding, session, dependency, permission, and tenant overlap.

### Run lifecycle and durability

`AgentHarness.start/get_run` and `HarnessRun` now expose a persisted preview lifecycle
with CAS transitions, terminal projections, cursor events, typed commands, exact-owner
reattachment, session lanes, and explicit shutdown policy. SQLite and PostgreSQL share
the same schema-v11 operation/lease/artifact/approval/export/child ledger. The outer model call crosses
`OperationGateway`; ambiguous non-repeatable failure/cancellation becomes
`waiting_for_reconciliation` instead of a false clean cancellation. A successful
result can be staged and recovered without redispatch through `LocalArtifactStore`.
The bounded reconciliation coordinator accepts only exact owner/revision/digest-bound,
physically readable observer evidence, CAS-settles the operation, and safely continues
a prior settlement without re-observing or redispatching.

For service storage, the optional `PostgresWriterAuthorityProvider` now fail-closes
every PostgreSQL store access against a fresh external generation, exact non-recovery
`cluster_name`, conservative relative TTL, server `transaction_timeout`, and
commit-boundary revalidation. The adversarial live gate creates two writable timelines,
proves unguarded divergence, and then proves stale/outage/commit-change containment
with the first-party etcd adapter. Controller election/record ownership, production
multi-AZ topology/backup, arbitrary-client and paused-host watchdog/STONITH fencing,
network partitions, latency and certificate/key rotation,
and production RPO/RTO remain open; the application seam is not mislabeled as physical
fencing.

Registered model-callable specs now bind to the executor/kernel with versioned policy
evidence, atomic durable approval waiting, exact grants, final-boundary lease/grant
reauthorization, and pre-persistence result policy. Another host can settle a live
worker's request. For the certified Agno 2.9 registered-capability sequence, process
death after the atomic wait reconstructs from the exact first-provider artifact,
request/decision/grant, and frozen authority/spec; after the tool result it continues
only from a valid native `tool-batch` checkpoint. This reconstructs a bounded sequence,
not a serialized Python stack. Explicit-profile `run/arun` now routes through lifecycle;
the required change is to route or reject every remaining legacy/nested/specialized
Agno ingress through explicit manifests; add protected command
payloads and certified provider observers; extend continuation to parallel/raw/
streaming/parser/output-model surfaces. Extend result artifacts to all output classes
and deletion/retention/search.
Pause/fork remain restricted to proven safe points. See
[Operations, effects, and recovery](operations-and-recovery.md).

### Context management

Current mechanisms solve parts of the problem:

- Agno CompressionManager reduces tool-result history;
- history limits can restrict prior messages/runs/tool calls;
- session summaries preserve a high-level recap;
- `inspect_context_budget()` reports exact-or-estimated usage and a typed action;
- `summarize_session()` is explicitly summary-only;
- `compact_session()` archives the complete trajectory before actual history replacement;
- `auto_compact_context` adds opt-in 90% proactive and deterministic 97% emergency
  replacement with a 70% release boundary and same-session run/stream admission;
- Agno-classified provider overflow gets one exact non-streaming invocation retry,
  fenced against tool activity, competing same-session work, streams, and retry loops;
- scoped lexical search and selective provenance-bearing rehydration recover archived
  items;
- registered capability and lifecycle first-party-tool results can opt into lossless
  artifact envelopes and bounded same-session paging; compaction retains spill/failure
  IDs and a recent artifact index. First-party tools also have explicit versioned effect
  declarations and exact intent/lease/policy/settlement ordering.

This is not yet a complete unattended long-running context lifecycle. There is no
transparent pre-provider spill for direct compatibility tools, governed context-
provider/raw-MCP adapters, or outer model output,
credentialed live-provider overflow proof, multi-host manifest fencing,
semantic/hybrid retrieval, or repeated-compaction drift certification. The controller is process-local;
its optional file lock coordinates only cooperating processes on one POSIX host. A
reviewed typed continuation deterministically keeps goal, plan, progress, approval,
decision, citation, test, file, and open-question invariants. Automatic compaction now
source-binds the first real user goal, carries the latest record with exact provenance,
and lets an explicit reviewed record supersede it. It does not yet extract, merge, or
fidelity-score the remaining fields from arbitrary model prose.

Required change: complete and certify the remaining pipeline in
[Context management](context-management.md) with long-run/overflow/fidelity scenarios.

### Agno learning

The v0.12 preview now provides:

- User Profile/User Memory when enabled;
- Entity Memory;
- Learned Knowledge when explicit vector-backed `Knowledge` is supplied;
- Decision Log;
- Session Context as a public policy/config option.

`LearningPolicy`/`LearningProfile` separate static intent from run-resolved
`LearningScope`; scope validation, consent, update budgets, deterministic opaque
tenant/user/session keys, per-run LearningMachine construction, safe event provenance,
and fail-closed configuration are implemented. The nonexistent optimizer path was
removed. Institutional direct writes are forbidden and the direct Agno view is
recall-only. SQLite/PostgreSQL previews add scoped artifact-backed candidates,
evaluation/export, immutable supersession, quarantine/tombstones, reversible Learned
Knowledge promotion/rollback, and evidence-backed manual reconciliation. The preview
self-improvement gate now binds causal clusters and component hypotheses to frozen
held-in/out/transfer, safety/privacy, resource, judge, novelty/diversity, and Pareto
evidence. Restart-safe scope-bound unknown-effect discovery and a bounded coordinator
for host observers are implemented; observations are revision-bound, evidence bytes
and owner scope are verified, and duplicate workers converge by CAS without effect
replay. A first-party preview evaluator now runs fresh-resource baseline/candidate pairs,
verifies exact scoped upstream artifacts, stages per-case evidence, enforces experiment
budgets, and applies paired confidence bounds. A provider-neutral adapter now constructs
a host-supplied fresh Agno Agent per rollout and retains only JSON-like public content
plus bounded token/cost evidence for the independent verifier. A content-free corpus
manifest and exact-scope evidence chain now fail before model construction on duplicate
payloads, cross-split lineage, exposure/authority drift, or known/unresolved overlap;
the default gate rejects ungoverned experiments. Rejected and inconclusive ledger
evaluations now have a content-free, exact-owner SQLite/PostgreSQL query by stable gate
reason/evaluator/mechanism/target/safety fields with a scope-bound keyset cursor. A
schema-v5 typed projection and bounded PostgreSQL 17 gate cover 10,000 evaluations for
the queried owner plus a 10,000-evaluation noisy neighbor without JSON-filter scans.
Runner schema 1.2 now adds paired local process/state/fault isolation, redacted contract
digests, empty-by-default environment, bounded streams, and POSIX descendant reaping.
The strict Docker subject now adds an immutable exact-platform image, no network/host
environment/mounts, read-only non-root execution, zero capabilities, built-in seccomp,
resource limits, and exact-owner cleanup, with unit and live-daemon proof. VM and
provider credential/egress isolation, managed corpus registry/ACL/near-duplicate
operations, richer statistical policy, production archive/failover certification, custom exact-state observers,
production reconciliation-worker database partition/failover/soak, reversible Entity/Decision adapters,
cross-process/replica/backup deletion proof (personal/session active-database
read/replace/forget is post-verified), production migration certification, and measured
benefit remain T8/T12/T14b work. The local and PostgreSQL learning/schedule migration
lifecycles are implemented.

Required change: finish the candidate, scoped CRUD, behavioral, and evaluation pipeline
described in
[Learning and self-improvement](learning.md).
The exact implemented evaluation boundary is in
[Evidence-gated harness self-improvement](self-improvement-evaluation.md).
Personal/session administration and its point-in-time deletion boundary are documented
in [Personal and session learning administration](learning-administration.md).

### Skill activation

The registry loads only skill metadata into the catalog. Activation is explicit
(`skill=` or CLI/TUI selection) or model-driven: the governed
`get_skill_instructions` progressive-disclosure tool lets the model select one
eligible local skill and load its bounded `SKILL.md` content inside the active
governed turn (see `docs/skills.md`). Catalog visibility alone is still not
activation.

Remaining change: record the tool-discovery scaling evidence defined in
`docs/evaluation.md` (10 -> 1,000 tools) before claiming activation quality at
catalog scale.

### MCP

`MCPToolkit` now exposes one bounded `search_mcp_tools`/`call_mcp_tool` pair across all
configured servers. SDK 2.0 negotiates the current stateless protocol and older
fallback; schemas stay out of the base prompt; calls refresh discovery and require an
exact schema digest. Stdio, Streamable HTTP, explicit legacy SSE, pagination,
structured content, private-metadata withholding, async loop ownership, and distinct
connection/discovery/call failures have focused contracts. During `start()`, configured
MCP calls use the first-party effect gateway and are conservatively non-repeatable.

Required change: add brokered OAuth/enterprise identity and selected current extensions
(resources, prompts, subscriptions, Apps, Tasks, MRTR) behind the same lazy registry;
certify real stdio/HTTP servers, reconnect, auth expiry, malicious servers, and soak.
Roots, Sampling, and Logging are deprecated in `2026-07-28` and are not new mandatory
parity work. See [MCP client and governed tool ingress](mcp.md).

### Subagents

The capability-only declared-child preview now represents host-coordinated delegation as
ordinary parent-linked runs. Schema v11 stores exact lineage and a bounded immutable
grant; creation/settlement events, joins, recursive cancellation, isolated sessions,
owner inheritance, handles, rollback, and SQLite/PostgreSQL parity cases are present.
An active child worker now commits an authoritative timeout cancellation before local
task cancellation. Successful outer-model settlement records stable Agno usage/cost,
then writes an idempotent budget observation; reported excess fails the child, and
missing provider metrics remain explicitly unverified.
Direct parents now collect stable typed outcomes, partition pending/success/failure,
prepare deterministic bounded synthesis input, and page owner-authorized direct-child
artifacts. Oversized results require a complete operation-result artifact and are never
silently shortened. Child declarations may now digest-bind a bounded object result
schema; content-free validation occurs after operation/budget settlement and repeats on
known-success recovery. Explicit governed synthesis defaults to all-success, bounds and
injection-frames the untrusted evidence snapshot, remains idempotent while pending, and
runs as another capability-only declared child.

The model-visible `spawn_subagent` compatibility tool remains only on named legacy and
returns at most 8,000 characters. Explicit profiles omit it and reject named raw
subagents before construction. New deployments instead register
`DeclaredChildTemplate`, which exposes only bounded task/delegation identity through
capability governance and also powers authenticated remote child handles. Recovery now
restores the exact durable declaration before safe pre-model continuation, certifies
the full ancestry, and reaps terminal-ancestor orphans without dispatch. Required next
work is to add restart-safe database-clock deadlines and provider-preflight
reservations/receipts, add a first-party governed cross-child artifact reader, and
certify distributed sweeps/multi-worker failover. See
[Declared child runs](child-runs.md).

## Keep

- the small Python-first embedding API;
- Agno-native runtime and optional AgentOS boundary;
- model portability;
- coherent `RuntimeBackend` ownership;
- public policy/permission/guardrail/hook/event seams;
- workspace and Agent Skills compatibility;
- model-hidden dependency and tool-binding support;
- optional extras rather than one mandatory platform stack.

## Change next

Order is deliberate:

1. Correct public claims and establish Agno compatibility CI.
2. Remove shared per-run Agent mutation through an isolated run kernel.
3. Add `RunHandle`, trajectory, snapshots, effects, artifacts, and real context
   management.
4. Fix and validate learning, including Session Context and vector-backed Learned
   Knowledge.
5. Add typed capability/effect metadata, actual Skill activation, lazy tool search, and
   complete modern MCP authorization/extensions beyond the implemented tool slice.
6. Finish remaining child and scheduler production certification: persisted child
   deadlines/hard ceilings, old-schedule cutover, tenant administration, retention,
   observability health/SLOs, and partition/soak proof.
7. Complete live span correlation, support bundles, and production Collector
   certification on the implemented safe telemetry/inspection foundation.
8. Promote capabilities to stable only through [Harness evaluation](evaluation.md).

## Deliberate platform boundary

Keep these outside the core library unless a concrete embedded use case proves
otherwise:

- channel gateways and Slack/Discord/Telegram/voice adapters;
- organization provisioning and identity federation;
- hosted fleet routing and operations UI;
- a proprietary public skill marketplace;
- mandatory workflow-graph authoring;
- a mandatory external durable-execution service.

Adapters may provide these concerns. They must preserve core identity, policy, events,
and run semantics.

## Current comparator lessons

The detailed sources and feature synthesis are in
[World-class harness strategy](world-class-harness.md). The most relevant present-day
competitive changes are:

- Pi 0.84.1 remains the benchmark for a minimal extensible core and now adds released
  lane/session/operation/storage contracts, deterministic crash-boundary design, and
  unusually candid scaffold debt accounting; see the [Pi audit](pi-harness-audit.md).
- Claude Code and OpenClaw show mature automatic context lifecycle and session lanes.
- Codex App Server shows a shared harness protocol with durable thread/turn/item and
  bidirectional approval primitives.
- LangGraph remains a benchmark for checkpoints, interrupts, replay, and forks.
- Pydantic AI Harness now provides composable compaction, lossless tool-output spill,
  step/effect persistence, conversation search, memory, guardrails, and durability
  adapters. This is a direct 2026 benchmark for an embeddable harness.
- Agno itself has advanced from the repository's 2.6.4 lock to stable 2.9.0, while
  3.0.0a1 previews normalized run storage and a durable queue. Wrapping and validating
  first-party capabilities is preferable to duplicating them; see the
  [release-practice audit](agno-release-practices.md).

## Research scope

Primary-source review covered Agno, Pi, Claude Code/Agent SDK, OpenAI Codex/App Server
and Symphony, OpenClaw, LangGraph/Deep Agents, Pydantic AI Harness, Letta, Agent Skills,
MCP `2026-07-28` plus SDK 2.0/extensions, and OpenTelemetry GenAI conventions. Source links are
maintained in [World-class harness strategy](world-class-harness.md).
