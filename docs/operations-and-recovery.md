# Operations, effects, and recovery

Status: 0.12 development preview; operation domain, transactional ledgers, gateway,
store-issued execution leases, durable capability approval, artifact-backed result
recovery, exact pre-model request-checkpoint continuation, and lifecycle model-call
integration plus evidence-bound operation reconciliation implemented; a real-process
full-stack gate certifies planned, in-flight, and settled model-operation restart, and
Agno 2.9 gates certify one governed tool-batch plus approval-wait reconstruction path;
intents pre-provision exact result identities and successful outer model settlements
capture content-minimized stable Agno usage/cost evidence

Last verified: 2026-08-18

This document is the current contract for answering one deceptively hard question:
after interruption, did an external operation happen?

The design follows [ADR-0001](adr-0001-recovery-ownership.md). Stable Agno 2.x does
not expose a certified settled continuation boundary, and the tested Agno 3 preview
queue can repeat an external effect after process death between dispatch and
settlement. agnoclaw therefore owns a small operation ledger while continuing to use
Agno for agent/model/session/learning behavior.

## The invariant

Every operation admitted to this contract follows:

```text
persist intent + result slot -> claim a fenced dispatch -> perform external work
    -> fulfill that exact slot + persist settlement
```

The ledger never infers success from an attempted call, never treats task cancellation
as proof that an external effect was undone, and never blindly retries an ambiguous
effect.

The repeatable SQLite crash gate uses actual ungraceful child exits rather than Python
exceptions:

```bash
uv run python scripts/sqlite_runtime_crash_probe.py \
  --iterations 10 \
  --allow-process-crash
```

It kills a disposable worker inside run-create, lifecycle-transition, operation-prepare,
dispatch, and settlement transactions. On every reopen it requires rollback visibility,
one-and-only-one retry event, idempotent duplicate retry, SQLite integrity, and WAL
checkpoint success. The current evidence is 50/50 injected process deaths. This is an
atomicity/reopen proof, not evidence that a provider effect did or did not happen; the
operation reconciliation contract below still owns that ambiguity.

The companion capability-effect crash gate crosses the process/provider boundary with
two disposable SQLite databases: one for `OperationGateway` and one standing in for an
external provider ledger.

```bash
uv run python scripts/operation_effect_crash_probe.py \
  --iterations 1 \
  --allow-process-crash
```

It kills a real child immediately before the provider call and immediately after the
provider transaction commits but before gateway settlement, for every `EffectClass`.
The retained eight-scenario run proves that `read_only` and `idempotent` operations can
be reclaimed safely, the provider idempotency key prevents a duplicate commit, and
`compensatable` plus `non_repeatable` ambiguity requires reconciliation and refuses
blind redispatch. It reports eight process crashes, four safe retries, four
reconciliation blocks, zero duplicate external effects, zero blind ambiguous
redispatches, and valid runtime/provider integrity. This is real capability-effect
evidence for those exact boundaries; it is not a live SaaS-provider receipt, full Agno
multi-step model/tool continuation, host-power-loss, multi-host, or production soak
claim.

The full-stack companion drives a real Agno `Agent` and provider-neutral custom model
through the production `AgentHarness.start()` path using the public
`AgnoModelFactory`:

```bash
uv run python scripts/agno_stack_restart_probe.py \
  --allow-process-crash
```

Four disposable children call `os._exit(88)` only after an authoritative write: a
planned model intent before provider dispatch, an entered provider call while the
operation is `dispatching`, and a successful result-artifact/operation settlement
before the terminal run transition. The fourth stops at the planned boundary and is
reopened with a different factory implementation digest. After the minimum execution
lease expires, a new harness reopens the same Agno SQLite session database, runtime
ledger, and artifact store, then discovers the stranded run through the bounded,
owner-scoped `recover_pending_runs()` startup API rather than using its known ID as a
recovery shortcut. The retained gate proves one exact pre-dispatch
continuation, one fail-closed `waiting_for_reconciliation`, one completion from the
committed result without calling the provider again, and one
`RUN_RECOVERY_SPEC_MISMATCH` before provider dispatch. Across all four scenarios it
observes exactly three provider calls, only one after restart, zero duplicate calls,
zero blind ambiguous redispatches, thirteen unique factory-created transports,
five-of-five clean-recovery transport closures, and four-of-four startup-scan
recoveries. All twelve databases pass integrity checks; abrupt-child transport cleanup
is supplied by process termination rather than misrepresented as a graceful `close()`
call.

This closes the factory-backed outer single-operation `AgentHarness`/Agno
process-restart proof and binds recovery to the declared factory implementation. It
does not claim a cloud provider receipt, provider billing reconciliation, host
power-loss durability, multi-host fencing, or production-duration soak. The narrower
multi-step tool and approval envelope below has its own capability/version gate and
uses the same public factory ownership contract for its exact native-model path.

### Agno 2.9 tool-batch checkpoint restart

Run the post-tool process-death gate after changes to provider-call materialization,
Agno checkpoint detection/continuation, capability dispatch, or recovery:

```bash
uv run python scripts/agno_tool_checkpoint_restart_probe.py \
  --allow-process-crash
```

Each scenario uses a native Agno Agent, two provider calls, and one governed registered
capability. Three children die after the persisted `tool-batch` checkpoint, during the
second provider call, and after the second provider result/operation settlement but
before outer-run completion. A fresh harness validates the exact Agno checkpoint and
all provider-operation/artifact evidence before continuing. The retained gate reports
three crashes, two completions, one reconciliation wait, six provider calls, only one
post-restart provider call, three tool effects, zero duplicate provider calls/effects,
and valid integrity for every reopened runtime, Agno, and evidence database.

The ambiguous second-provider scenario does not continue merely because an earlier
tool checkpoint exists: the later `dispatching` non-repeatable provider operation wins
and parks the run at `waiting_for_reconciliation`. If the second provider settlement is
already durable, recovery replays its artifact and completes without redispatch.

### Durable approval restart before the tool checkpoint

Run the approval process-death gate after changes to approval transactions, provider
artifact replay, registered capabilities, or recovery:

```bash
uv run python scripts/agno_approval_restart_probe.py \
  --allow-process-crash
```

The child exits immediately after the transaction that inserts one approval request
and moves the run to `waiting_for_approval`. At that boundary the first provider call
is settled, no capability effect or second provider call has occurred, and no valid
Agno tool-result checkpoint exists. A fresh host loads the exact frozen request,
settles the approval, proves that an identical decision retry is idempotent, and calls
`recover_run()`. Recovery replays the first-provider artifact, reauthorizes the exact
approved request, executes the capability once, calls the second provider once, and
completes. The retained gate reports one process crash, one approval-wait recovery, two
provider calls (one after restart), one request, one approved record, one tool effect,
zero duplicates, and three intact databases. A conflicting decision retry remains a
typed settled-approval error.

These two gates certify only the explicit envelope: Agno 2.9 must expose the
`tool-batch` checkpoint/cancel/continue contract; Agnoclaw must materialize the native
Agent and a fresh public-factory model classified as run-owned, isolated, and
recreatable; execution must use non-streaming `start()` with durable runtime and Agno
databases plus a shared ArtifactStore; and model-callable effects must be governed
registered capabilities. Parser/output models, persisted presentation/stream paths,
directly injected opaque models, raw/custom/plugin/pack/context/
caller-MCP tools, live cloud receipts, host-power loss, multi-host/network partitions,
and production soak are not certified. Agno 2.6.4 lacks the required checkpoint
capability and keeps the conservative legacy behavior.

## Model transport ownership

Model lifecycle is explicit even when a run never enters durable recovery. A model
materialized from an agnoclaw-owned string/default specification is harness-owned: the
base Agent transport and every fresh per-run Agent transport are closed during normal,
error, cancellation, and construction-failure paths. A public `AgnoModelFactory`
extends that ownership to custom transports: the factory and credentials remain
host-only, its digest and declared identity enter the spec, and reused/mismatched
instances fail before dispatch. The adapter uses public
`close()`/`aclose()` contracts when available and a narrowly provider-specific fallback
for Ollama's current client, which owns an HTTPX transport without forwarding a close
method. The same owned cleanup applies to
`AgnoEvaluationSubject(close_agent=True)`.

A caller-injected Agno Model remains caller-owned, is never closed by the harness, and
is rejected by durable/service resource classification. Call
`AgentHarness.close()`/`aclose()` for owned harness resources and close directly
injected models according to their provider contract. Factory-backed process restart
is certified for the outer non-streaming operation, exact digest-drift boundary, and
the narrow Agno 2.9 native tool/approval continuation envelope above. Live Ollama runs
and the model-backed learning gate execute with `ResourceWarning` and unraisable
warnings treated as errors; this is resource-lifecycle evidence, not provider-effect
reconciliation.

Runtime schema v12 stores operation records, mutation-idempotency evidence,
store-issued execution leases, durable approvals/grants, and authorized artifact
references beside the run event stream. SQLite and PostgreSQL atomically append these
event types:

- `operation.planned`
- `operation.dispatching`
- `operation.recovery.planned`
- `operation.settled`
- `operation.reconciled` with the exact observer/evidence manifest
- `artifact.committed` when a result is externalized
- `approval.requested`, `approval.approved`, `approval.denied`,
  `approval.expired`, or `approval.cancelled` for governed capability approval

Each mutation uses revision compare-and-set. Dispatch and settlement additionally use
a monotonically increasing fence token so a stale worker cannot settle work reclaimed
by another worker.

`operation.settled` copies only allowlisted, bounded numeric token and micro-USD
measurements from the already content-minimized settlement evidence into the event.
Provider-private fields, account metadata, response data, and arbitrary usage keys are
discarded. This makes durable telemetry useful without making the outbox a second
provider-response store; see [Observability](observability.md).

`OperationIntent.result_slot_id` is a canonical content-independent projection of the
operation ID. It is present in `operation.planned` before dispatch and is carried into
artifact and settlement evidence. Successful settlement fills that exact slot; an
explicit mismatched slot raises `OPERATION_RESULT_SLOT_MISMATCH`, and either store
rejects a different artifact if the slot is already fulfilled. The eventual artifact
remains content-addressed—the slot identifies where the future result belongs, not
what its unknown bytes will be.

## Effect classes

`EffectClass` is declared, never guessed from a function name:

| Class | Meaning | Interrupted dispatch |
|---|---|---|
| `read_only` | No externally visible mutation | May be explicitly reclaimed and retried |
| `idempotent` | Provider guarantees the supplied key deduplicates the effect | May be explicitly reclaimed with the same key |
| `compensatable` | Mutation has a defined compensating operation, but is not replay-safe | Reconcile; do not retry automatically |
| `non_repeatable` | Cost-bearing or externally visible operation without certified deduplication | Reconcile; do not retry automatically |

Model calls are nondeterministic and usually billable. The current lifecycle adapter
therefore classifies each `AgentHarness.start()` model loop as `non_repeatable` unless
a future provider adapter can prove a stronger contract. “It is only inference” is not
an idempotency guarantee.

## Operation state

```text
planned -> dispatching -> succeeded
                      +-> failed
                      +-> unknown
                      +-> cancelled

dispatching/unknown -- independent evidence --> succeeded/failed

planned ----------------> cancelled  (provably before external dispatch)
```

- `planned` means intent is durable and external dispatch has not started.
- `dispatching` means a named worker owns a fence and dispatch may be in progress.
- `succeeded` and `failed` are known terminal outcomes.
- `unknown` means the outcome cannot be established safely from current evidence.
- `cancelled` means dispatch did not occur, or the gateway has a known cancelled
  settlement. It is not used for an ambiguous non-repeatable operation.

`OperationIntent.digest` excludes observation time (`prepared_at`) and the derived
result slot, but includes the semantic request. Reconstructing the same intent after
restart therefore reuses the original operation; changing its target, request digest,
effect contract, metadata, or other semantics raises an idempotency conflict. Older
development records without the additive slot field derive and validate the same value
when loaded, so idempotent preparation stays compatible.

## Current lifecycle behavior

`AgentHarness.start()` first settles the canonical message, full frozen execution
context/admission envelope, lifecycle keyword arguments, and harness-spec digest as the
content-addressed `run_request_checkpoint` artifact behind
`{run_id}:checkpoint:request:1`. It then creates `{run_id}:model:1`, commits its intent,
and invokes `arun()` only after the dispatch fence commits. The final model result is
then settled and the run completes.

On successful outer-model settlement, the gateway can record an optional bounded
provider request ID, stable Agno token counters/duration, and USD cost rounded upward
to integer microdollars. It never persists provider-private response data. Metrics
missing from the provider/Agno result are marked unreported rather than inferred. The
evidence callback is observability-only: if it raises or returns an invalid value, the
gateway commits success with a content-minimized `extraction_error` marker instead of
stranding an already successful external effect.

Cancellation is intentionally asymmetric:

| Cancellation point | Operation truth | Run truth |
|---|---|---|
| Queued/paused, before operation intent | No operation | `cancelled` |
| During intent or dispatch-fence database commit | Gateway finishes the commit, records pre-dispatch cancellation, and never calls the provider | `cancelled` |
| During a non-repeatable model dispatch | Outcome may be ambiguous; operation settles `unknown` | `waiting_for_reconciliation` |
| After external success but before its result reference is durable | Operation settles `unknown` | `waiting_for_reconciliation` |
| After the operation is durably `succeeded` | Known success remains in the ledger; the enclosing workflow may still honor cancellation | Never rewritten as unknown |

Provider/model exceptions are conservatively unknown today because the generic Agno
boundary cannot prove whether a request reached the provider. Safe adapters may later
classify authenticated provider errors that prove non-dispatch as known failures. The
operation retains only allowlisted diagnostic fields; raw exception messages are not
persisted. `wait()` raises typed `RUN_RECONCILIATION_REQUIRED` while the run is parked.

For explicit `quick`, `durable`, and `service` profiles, `run()` and `arun()` now enter
this lifecycle through `start()` plus `wait()` without changing the completed-result
shape. `quick` uses an ephemeral store. The lifecycle worker binds the logical run ID
before its one direct Agno call, avoiding adapter recursion and duplicate dispatch.
Only `HarnessConfig.legacy()` remains a direct compatibility route. Raw streaming is
attached as a bounded non-authoritative presentation. Explicit-profile synchronous
calls use one reusable harness-owned event loop: slow or closed iterators detach from
presentation without cancelling the run, iterator exhaustion reconciles to terminal
run truth, and `close()`/`aclose()` drain that loop before releasing owned resources.
Mixing a direct async `start()`/`arun()` call from another loop after synchronous
ownership is established fails before run creation with
`HARNESS_EVENT_LOOP_OWNERSHIP_CONFLICT`.

Lifecycle workers also claim two leases in one store transaction: the exact run and
the exact tenant/session lane. Both carry independent monotonically increasing fences,
one opaque claim, exact tokens, and a shared database-clock expiry in PostgreSQL. The
worker heartbeats both. Lease loss cancels dispatch; an in-flight non-repeatable model
operation still becomes unknown. Expired crash ownership can be reclaimed under a
higher fence, while an unexpired worker cannot be stolen.

`recover_run()` is the explicit current startup-recovery entry point. It acquires the
same fenced ownership and never replays ambiguous external work. If the model operation
is absent or only `planned`, a settled request checkpoint may relaunch the worker after
exact owner/session, authority-context, harness-spec, request-digest, checkpoint-
operation, artifact-purpose/metadata, and planned-model-intent verification. Interrupted
model dispatch enters `waiting_for_reconciliation`; known operation failure is
`failed`; a pre-dispatch run without a settled readable checkpoint fails with
`RUN_RECOVERY_CHECKPOINT_UNAVAILABLE`. Known operation success with a committed result
artifact is checksum-verified, loaded, and completed without redispatch. Without a
configured/shared ArtifactStore it fails with `RUN_RECOVERY_RESULT_UNAVAILABLE`.
Missing or tampered committed bytes fail as typed artifact corruption.

`recover_pending_runs()` is the bounded discovery layer over that exact primitive. The
SQLite and PostgreSQL stores filter by an explicit `RunOwner`, select only `queued`,
`running`, and `cancelling`, and paginate by immutable run ID. The owner-bound opaque
cursor cannot be reused across tenant/user scopes. Each candidate still passes through
`recover_run()` and the same lease claim; an unexpired claim becomes a content-minimized
`lease_busy` outcome, not a steal or failure. Per-item failures contain a typed safe code
and never raw exception text. Limits are 1–100 candidates and 1–32 concurrent claim
attempts. A full startup sweep must be restarted from `cursor=None` later to discover
newly admitted work and retry leases that were live during the prior sweep. The harness
defaults the minimum candidate age to its lease duration; each store applies that cutoff
with its authoritative clock so a newly queued run is not misclassified before its
original worker has time to claim.

Recovery discovery is bounded by partial indexes over only executable or reconcilable
states, not by a fold over terminal history. The run paths use
`runtime_runs_executable_owner_idx` and
`runtime_runs_reconciliation_owner_idx`; the operation paths use
`runtime_operations_dispatch_queue_idx` and
`runtime_operations_run_reconcile_idx`. Internal enum predicates are emitted as fixed
SQL literals so SQLite can prove the partial-index implication; owner, age, cursor, and
limit values remain parameters. Query-plan tests reject table scans and temporary sort
trees on every recovery path. A deterministic SQLite benchmark additionally compares
1,000 versus 10,000 terminal run/operation rows using 100 samples of 100 calls. Across
three consecutive certification runs, the largest p95 ratios were 0.988 for operation
discovery, 1.185 for run discovery, and 1.070 for reconciliation. These figures prove
history-size independence for this bounded local fixture, not a production latency SLO.
A fresh 2026-08-18 release-candidate run passed all three paths at 0.996, 0.993,
and 0.997 respectively, with no p95 above 0.044 ms.

The scanner is intentionally host-triggered. Automatically scanning every owner from a
library constructor would require an unsafe global tenant enumeration and unclear
worker ownership. A service should enumerate authenticated deployment scopes, run one
bounded cursor loop for each, admit new traffic only according to its own readiness
policy, and retain the returned metrics/outcomes in its operator audit system.

## Evidence-bound reconciliation worker

`reconcile_pending_operations()` is the bounded host-owned worker for runs parked at
`waiting_for_reconciliation`. It performs an exact-owner, database-clock-aged,
operation-ID keyset scan. The opaque cursor is bound to the tenant/user tuple; pages
are limited to 100 records and observer concurrency to 32 (defaults 25/4). There is no
global cross-tenant enumeration.

The host supplies a versioned read-only observer and its canonical `sha256:`
implementation digest. The observer may return `None` to defer or an
`OperationReconciliationObservation` bound to the exact operation ID, revision, and
record digest. A conclusive observation declares one of:

- `succeeded`, with a JSON result artifact that can complete the enclosing run;
- `failed`, when independent evidence proves the provider/effect failed; or
- `effect_absent`, when independent evidence proves the effect did not occur.

Every verdict requires 1–16 physically readable artifacts with the exact run owner
scope and purpose `operation.reconciliation.evidence`. Successful observations must
include the result reference among those artifacts. The store atomically fences the
old dispatcher, compare-and-sets the operation revision, commits evidence references,
records the observer digest/verdict in `operation.reconciled`, and emits its outbox row.
It never dispatches or compensates the original effect.

```python
from agnoclaw import (
    OPERATION_RECONCILIATION_EVIDENCE_PURPOSE,
    ArtifactScope,
    OperationReconciliationObservation,
    OperationReconciliationVerdict,
)

class ProviderObserver:
    async def observe(self, request):
        receipt = await provider.lookup_without_mutation(
            request.record.intent.idempotency_key
        )
        if receipt.pending:
            return None
        evidence = await artifacts.stage_json(
            {"content": receipt.result},
            scope=ArtifactScope(
                run_id=request.run_id,
                tenant_id=trusted_context.tenant_id,
                user_id=trusted_context.user_id,
            ),
            purpose=OPERATION_RECONCILIATION_EVIDENCE_PURPOSE,
        )
        return OperationReconciliationObservation(
            operation_id=request.record.intent.operation_id,
            expected_revision=request.record.revision,
            operation_digest=request.record.digest,
            verdict=OperationReconciliationVerdict.SUCCEEDED,
            evidence_artifacts=(evidence,),
            result_reference=evidence.artifact_id,
            provider_request_id=receipt.request_id,
        )

batch = await harness.reconcile_pending_operations(
    ProviderObserver(),
    observer_digest="sha256:<digest-of-observer-code-and-policy>",
    context=trusted_context,
)
```

Public reconciliation types:

| API | Contract |
|---|---|
| `OperationReconciliationObserver` | Async read-only host protocol; receives one exact `OperationReconciliationRequest` and returns an observation or `None`. |
| `OperationReconciliationRequest` | Immutable observer input containing the authoritative `OperationRecord` and run ID. |
| `OperationReconciliationObservation` | Exact revision/digest-bound verdict, physical evidence references, and optional successful result/provider receipt. |
| `OperationReconciliationVerdict` | `succeeded`, `failed`, or `effect_absent`; it never means “retry the original effect.” |
| `OperationReconciliationBatch` | Ordered page of item outcomes plus an owner-bound `next_cursor`; exposes `reconciled` and `continued` counts. |
| `OperationReconciliationItem` | Safe per-operation result with status, revisions, states, reconciliation ID, and optional safe error code. |
| `OperationReconciliationStatus` | Worker outcome: `reconciled`, `continued`, `deferred`, `stale`, `rejected`, or `failed`. |
| `OperationReconciliationCursorError` | Typed `OPERATION_RECONCILIATION_CURSOR_INVALID` failure for malformed or cross-owner cursors. |
| `OPERATION_RECONCILIATION_EVIDENCE_PURPOSE` | Required artifact purpose string for every observer evidence object. |

The host-oriented types above are exported from `agnoclaw`. Low-level store adapters
also use `agnoclaw.runtime.OperationReconciliation`, the immutable CAS command that
binds the reconciliation ID, exact operation revision/digest, observer digest, verdict,
and evidence artifact IDs. Most applications should call the harness worker rather
than construct that store command directly.

Ordered item states are `reconciled`, `continued`, `deferred`, `stale`, `rejected`, or
`failed`; errors expose only safe codes. `reconciled` means the operation CAS committed,
not that the business outcome was successful. `continued` means a prior reconciliation
had already committed and this sweep finished the run transition without calling the
observer again. This closes the crash window between operation settlement and run
continuation. If cancellation arrives during the database call, the worker waits for
the authoritative commit and then propagates cancellation; a fresh sweep safely
continues any settled/waiting run. Start each later sweep at `cursor=None` to find newly
parked or previously deferred work.

An observer is deployment/provider logic, not a model guess. It must query an
authoritative receipt, idempotency endpoint, or independently verifiable side effect.
If no such proof exists, return `None` and keep the run waiting. `effect_absent` becomes
a known failed operation and is not automatically retried; a separate authorized new
run is required for another attempt.

The request checkpoint remains one deliberately narrow continuation point immediately
before outer-model dispatch. Reconciliation adds a second boundary after independent
proof of the outer operation's outcome. Neither serializes an Agno/Python stack or
resumes a stream by itself. The separately certified Agno 2.9 path composes these
agnoclaw boundaries with exact per-provider artifacts, the approval ledger, and Agno's
validated `tool-batch` checkpoint for one governed capability sequence. Missing or
drifting evidence fails closed; it is not a generic nested-stack serializer. All of
these artifact classes require encryption, retention, access control, and deletion
suitable for their contents.

## `OperationGateway`

The async-first gateway can also be used by first-party adapters:

```python
from agnoclaw import EffectClass, OperationGateway, OperationIntent, OperationKind

intent = OperationIntent(
    operation_id="run-17:lookup:1",
    run_id="run-17",
    attempt_id="run-17:attempt:1",
    kind=OperationKind.CAPABILITY,
    target="inventory.lookup",
    request_digest="sha256:...",
    effect_class=EffectClass.READ_ONLY,
)

execution = await OperationGateway(
    store,
    worker_id="worker-3",
).execute(intent, dispatch)

assert execution.record.settlement.result_slot_id == intent.result_slot_id
```

Inside the runtime kernel (gateway and operation paths), synchronous store calls run
off the event loop; some `AgentHarness` lifecycle methods still call synchronous
stores directly on the loop, retained as post-0.12 robustness work (see
`docs/releases/v0.12.0-progress.md`). Mutation calls finish their database
commit even if the calling coroutine is cancelled, closing the race where a thread
commits after the task has already reported cancellation. Existing in-flight work is
never stolen implicitly; recovery is a separate explicit action.

Successful values live in a bounded per-process LRU cache. Without an ArtifactStore,
the default result reference is content-derived but cannot reconstruct the value; a
restart raises `OPERATION_RESULT_UNAVAILABLE` instead of calling external work again.
With `artifact_store=`, the gateway stages a scoped JSON result, and both first-party
RuntimeStores atomically commit its authorized reference with operation settlement.
A restarted gateway then verifies and loads it. Gateway-staged artifact metadata and
the `artifact.committed` event include the exact result slot; the ledger relationship,
not caller-supplied metadata alone, remains authoritative. See
[Durable artifacts](artifacts.md).
An optional `settlement_evidence=` callback is evaluated only after result staging and
before the atomic success settlement. Its failure is isolated as missing observability
evidence; it cannot change known external success into failure or ambiguity.

`start()` uses this authority for the outer model request, registered capabilities,
and every first-party function present when Agnoclaw constructs its tool surface. The
built-in declaration table is explicit and versioned in the harness-spec digest;
unknown additions fail with `BUILTIN_EFFECT_UNCLASSIFIED`. At an active call, Agnoclaw
temporarily wraps the native Agno entrypoint, verifies policy-version stability, renews
the store-issued run lease, dispatches sync work off-loop while observing completion,
applies after-tool policy before persistence, and restores the original entrypoint.
Direct compatibility `run()`/`arun()` does not install this operation wrapper.

## `CapabilitySpec`

`CapabilitySpec` is the single immutable descriptor intended for model, built-in,
custom, skill, context-provider, and MCP capabilities. It declares:

- stable name and version;
- capability kind and implementation digest;
- effect class and optional provider idempotency support;
- trust, lifetime, concurrency, and recovery class;
- input schema and required scopes;
- a live factory excluded from its persisted manifest/digest.

Durable/service validation rejects opaque live-only resources and an unreconcilable
non-repeatable capability. `operation_intent()` binds the descriptor digest and profile
to an `OperationIntent` so a later implementation change cannot silently masquerade as
the original operation.

Specs supplied through `AgentHarness(capabilities=...)` for model-callable kinds are now
converted into version-pinned Agno functions and dispatched through a separate
capability operation inside the model loop. Their policy evidence, active run/session
lease fences, provider idempotency key, approval request/decision/exact grant, and
governed result settlement are durable. A required approval and
`waiting_for_approval` state commit together before materialization. Approval is
reauthorized after lease renewal at the final no-effect boundary, so argument,
capability, policy, identity, authority, scope, expiry, or effect drift prevents
dispatch.

The approval row does not serialize the suspended Agno/Python stack. A different host
process can inspect and settle the request while the live worker polls the authoritative
store. On the certified Agno 2.9 envelope, process-death recovery before the tool-result
checkpoint reconstructs only after validating the request checkpoint, settled first-
provider artifact, exact decision/grant, frozen authority/spec, and provider-operation
ordinal. It then replays the provider artifact into a fresh native Agent; after the
tool result, continuation requires Agno's exact `tool-batch` checkpoint. Any unsupported
surface or evidence drift retains the conservative classification.
Caller `tools=` entries are normalized into stable opaque/live-only/non-repeatable
spec evidence, but never inferred into a trustworthy effect contract. Named-legacy
`run/arun` keeps them behind serialized compatibility ownership; `start()` and all
explicit-profile convenience calls reject them before run creation. Currently
constructed first-party tools now have a versioned effect
manifest and lifecycle operation ingress. Plugins and packs can publish explicit
`CapabilitySpec` registrations into that same governed path. Configured MCP discovery
and calls join first-party ingress with conservative effects. Run-owned, explicitly
attested read-only context queries join the registered path. Raw/effectful provider,
pack, plugin, and caller-supplied MCP tools are inventoried separately and rejected by
`start()` before run creation; their named-legacy behavior remains. Do not
claim universal per-tool exactly-once behavior yet: non-repeatable ambiguity still
requires reconciliation, and named-legacy execution remains outside this adapter.

## Recovery decision matrix

`recovery_action(record)` is pure and storage-independent:

| Stored evidence | Decision |
|---|---|
| `planned` | dispatch is permitted |
| `dispatching` + `read_only` | explicit retry after fenced recovery |
| `dispatching` + keyed `idempotent` | explicit retry with the same provider key |
| `dispatching` + `compensatable` or `non_repeatable` | reconciliation required |
| `unknown` | reconciliation required |
| any known terminal settlement | do nothing |

`recover_interrupted()` never auto-runs the dispatch and never reclaims ambiguous work.
It only moves a safely replayable interrupted operation back to `planned` under a newer
fence. The caller must make a separate deliberate `execute()` decision.

Scheduled execution applies the same truth boundary at a different layer. A scheduler
lease reclaim keeps the same occurrence and attempt; after lifecycle admission it also
keeps the same `runtime_run_id`. Only a known retryable terminal lifecycle failure may
create another scheduler attempt. Store loss, a stale scheduler fence, or
`RUN_RECONCILIATION_REQUIRED` detaches the same attempt instead of redispatching the
effect. See [Durable scheduling](durable-scheduling.md).

## Finite race conformance

The opt-in `agnoclaw.testing` controls exercise the real gateway and stores. A shared
SQLite/PostgreSQL matrix now proves:

- `StoreFaultScript` rollback and exact retry at prepare, dispatch, settlement,
  recovery, and reconciliation transaction checkpoints;
- close/reopen truth for `planned`, `dispatching`, `succeeded`, `failed`, `unknown`,
  and `cancelled`, including one and only one committed mutation/event;
- `read_only`, keyed `idempotent`, `compensatable`, and `non_repeatable` behavior at
  pre-dispatch, immediately-before-effect, and after-effect boundaries;
- safe interrupted effects stay fenced for explicit recovery, while ambiguous effects
  become `unknown` and require evidence-bound reconciliation;
- cancellation after the dispatch transaction but before the callable settles known
  `cancelled`, while a success transaction already inside its commit barrier remains
  authoritative `succeeded` even if its waiter is cancelled.

`StoreBarrierScript` blocks one exact store checkpoint with a finite timeout, so these
commit orders need no fake store or timing sleep. This matrix certifies the current
operation entry type; each future provider-handle, remote task, or scheduler operation
must join it before promotion.

## Store and retention guarantees

Both first-party stores preserve:

- operation intent before dispatch;
- canonical result-slot identity before dispatch and exact slot fulfillment on success;
- exact mutation-idempotency digests;
- revision and fence checks;
- safe terminal settlement, optional provider request ID, and content-minimized usage/
  cost fields populated when the adapter reports them;
- transactional run event/outbox emission;
- atomic artifact/result reference, `artifact.committed`, operation settlement, and
  outbox emission after external bytes are staged;
- operation evidence required to replay an idempotent mutation even after visible run
  events are pruned;
- post-crash listing of recoverable and reconciliation-wait operations;
- partial-index-backed run, operation, reconciliation, and child discovery whose query
  plans are contract-tested against growing terminal history;
- approval requests, decisions, and exact grants with owner isolation, revision CAS,
  one-decision/one-grant constraints, and replay-safe idempotency.

Operation lookup applies the owning run's tenant/user visibility check. Event retention
does not delete operation or operation-mutation evidence. Full operation retention,
legal deletion, and complete artifact/key co-retention policy remain T10b/T14 work.

## Still required for T6 certification

This is a real operation foundation, not final durable recovery. The open gate is:

- bind effectful/provider-tool context, elevated/child/job, and richer MCP auth/
  extension ingress to the executor path;
  raw caller and extension tools now normalize/reject before lifecycle state, while
  explicit host/plugin/pack/read-context registered model capabilities already cross
  `CapabilitySpec`, durable permission approval, policy, active-lease renewal, and the
  gateway with content-minimized request evidence;
- extend implemented durable approval-before-effect from explicitly registered model
  capabilities to every remaining effect ingress, and add service quotas plus data
  handling classification at those boundaries;
- add certified provider-specific request/receipt and pre-dispatch reservation adapters
  beyond the implemented generic Agno result metrics; prove usage/cost reconciliation
  against provider bills and hard-limit behavior where the provider supports it;
- extend the implemented request-checkpoint and durable-result ArtifactStore coverage
  to tool-output, media, child-run, and step checkpoints plus authorized deletion/
  retention automation;
- extend the implemented pre-dispatch run/session lease binding to atomic operation
  settlement checks and every remaining capability safe point;
- extend the implemented Agno 2.9 per-provider/tool-batch/approval reconstruction from
  one governed registered-capability sequence to nested or parallel tool graphs,
  parser/output-model and persisted-stream paths, and the remaining extension ingress;
- add certified provider-specific observers, operator audit UI, retention/deletion
  proof for evidence, and reconciliation soak/failover certification;
- extend the implemented before/during/after model-operation, capability-effect,
  tool-batch, and approval-wait process-kill gates to nested/parallel and raw extension
  sequences plus live-provider receipt observers;
- run duplicate delivery, lease loss, database partition/failover, and retry-storm
  chaos gates.

Until those pass, `start()` provides automatic restart only inside the explicitly
certified envelopes above. Everywhere else it remains a truthful crash/effect recorder
that refuses arbitrary or ambiguous continuation.

## Context-maintenance recovery boundary

Opt-in automatic context compaction always acquires a process-local, session-specific
maintenance lease before archive-first replacement. Any direct run or open stream in
that session causes `CONTEXT_SESSION_BUSY`; new same-session work while maintenance is
active gets `CONTEXT_MAINTENANCE_IN_PROGRESS`. Both are retryable and neither permits a
partial session-row mutation. When the host injects `LocalFileContextLockProvider`,
ordinary activity also holds a shared exact-scope OS lock and replacement holds an
exclusive lock across the final Agno save. A conflicting cooperating process fails
before model dispatch or session mutation; process exit releases the descriptor. The
archive can contain staged, unreferenced bytes after a later quality/database failure;
normal artifact grace-period collection owns them.

For Agno-classified context overflow, the sole active run may upgrade to owned
maintenance, perform deterministic emergency replacement, and retain the fence through
one retry of the exact model invocation. With the local provider it first converts the
sole shared reader into the exclusive writer; a competing process makes the retry fail
safe. A stream, an observed tool call, competing session work, or a second overflow
terminates without replay. This is separate from the store-issued durable execution
lease. The shipped adapter coordinates cooperating processes only on one POSIX host;
multi-host and arbitrary-client fencing remain open. See
[Context management](context-management.md) for the exact boundary.

## Large governed-result recovery boundary

`max_inline_output_chars` externalizes oversized registered-capability and lifecycle
first-party-tool results only after policy/redaction and authoritative operation
settlement. The model receives a
bounded envelope; `read_spilled_output` loads verified JSON in bounded pages without
redispatch. The producing run and later runs in the exact trusted tenant/user/session
may read it. Cross-session references fail with `OUTPUT_SPILL_SCOPE_MISMATCH`, and a
missing/tampered artifact fails through the normal artifact integrity contract.

Compaction retains the spill envelope as an artifact-reference invariant with stable
item ID and provenance. This supports conversation continuation, not interrupted
effect execution: it recovers settled output and never resumes or retries the original
capability or first-party tool. Configured MCP calls are covered; custom/plugin/context-
provider/caller-supplied MCP and outer-model output remain outside this guarantee. See [Durable
artifacts](artifacts.md#model-context-spill).

## Errors worth handling

| Code | Meaning |
|---|---|
| `OPERATION_ALREADY_DISPATCHING` | Another worker owns or may own the dispatch; do not steal it implicitly. |
| `OPERATION_RECONCILIATION_REQUIRED` | Outcome is ambiguous; inspect external evidence or request operator resolution. |
| `RUN_RECONCILIATION_REQUIRED` | The run is intentionally parked until independent evidence settles its ambiguous operation. |
| `OPERATION_RECONCILIATION_CURSOR_INVALID` | The cursor is malformed or belongs to another owner. |
| `OPERATION_RECONCILIATION_OBSERVATION_MISMATCH` | Observer output is not bound to the discovered operation revision/digest. |
| `OPERATION_RECONCILIATION_EVIDENCE_SCOPE_MISMATCH` | Evidence purpose or exact run/tenant/user scope is wrong. |
| `OPERATION_RECONCILED_EFFECT_ABSENT` | Independent evidence proved the effect did not occur; no automatic retry follows. |
| `OPERATION_RESULT_UNAVAILABLE` | Success is durable, but this process lacks the referenced result loader. |
| `OPERATION_RESULT_SLOT_MISMATCH` | Settlement named a future-result identity other than the one persisted before dispatch. |
| `OPERATION_RESULT_SLOT_ALREADY_FULFILLED` | The canonical slot is already linked to a different result artifact. |
| `ARTIFACT_CORRUPT` | A committed result's bytes, size, checksum, protection, or JSON failed verification. |
| `ARTIFACT_SCOPE_MISMATCH` | A staged artifact does not belong to the authoritative run owner. |
| `OPERATION_IDEMPOTENCY_CONFLICT` | A mutation/intent ID was reused with different semantics. |
| `OPERATION_REVISION_CONFLICT` | Operation state changed before compare-and-set. |
| `CHILD_RESOURCE_BUDGET_EXCEEDED` | Reported child token/cost usage exceeded its declared grant after provider settlement. |
| `BUILTIN_EFFECT_UNCLASSIFIED` | A first-party function lacks an explicit effect declaration. |
| `BUILTIN_ACTIVE_LEASE_REQUIRED` | First-party dispatch lacks the active store-issued run lease. |
| `BUILTIN_POLICY_DRIFT` | Policy/permission authority changed before external dispatch. |
| `BUILTIN_POLICY_CONSTRAINT_UNSUPPORTED` | A requested argument transformation is not explicitly supported by the built-in adapter. |
| `BUILTIN_RESULT_CONSTRAINT_UNSUPPORTED` | A requested result transformation is not explicitly supported by the built-in adapter. |
| `EXTENSION_TOOL_LIFECYCLE_UNSUPPORTED` | Raw extension or mutable dynamic-toolkit execution tried to enter `start()` without a capability manifest. |
| `OPERATION_FENCE_STALE` | A stale worker attempted settlement. |
| `RUN_FAILED_UNKNOWN_EFFECTS` | Terminal fallback for ambiguity that cannot remain in the active reconciliation workflow. |
| `RUN_RECOVERY_CHECKPOINT_UNAVAILABLE` | No settled readable pre-model request checkpoint exists. |
| `RUN_RECOVERY_CHECKPOINT_INVALID` | Checkpoint schema or operation evidence is invalid. |
| `RUN_RECOVERY_CHECKPOINT_SCOPE_MISMATCH` | Checkpoint authority or artifact scope does not match the run. |
| `RUN_RECOVERY_REQUEST_MISMATCH` | Checkpoint content and frozen request evidence disagree. |
| `RUN_RECOVERY_SPEC_MISMATCH` | The current harness specification differs from the checkpoint. |
| `RUN_RECOVERY_MODEL_INTENT_MISMATCH` | The planned model intent is not the certified request. |
| `APPROVAL_ALREADY_SETTLED` | A decision retry conflicts with the settled approval; an exact replay returns the existing decision without new evidence. |
| `RUNTIME_EVENT_IDEMPOTENCY_CONFLICT` | A projected event ID was reused with different semantics. |
| `RUNTIME_EVENT_TERMINAL_RUN` | A new observer projection arrived after terminal truth; only exact idempotent replay is allowed. |
| `RUNTIME_STORE_EVENT_PROJECTION_REQUIRED` | A lifecycle store does not implement authoritative runtime-event append. |
| `OUTBOX_LEASE_INVALID` | An exporter tried to acknowledge or defer an item without its current unexpired lease. |
| `CAPABILITY_ADMISSION_REQUIRED` | A durable/service capability call bypassed trusted admission. |
| `CAPABILITY_OPERATION_REQUIRED` | Capability recovery targeted a model or other non-capability operation. |
| `POSTGRES_WRITER_AUTHORITY_DENIED` | External writer authority is unavailable, stale, changed, too short, or does not name this exact writable server; the transaction did not commit. |

## Verification evidence

As of the date above:

- the refreshed primary non-integration lane passes 1,948 tests with 55 service/
  credential/provider/vector skips and 17 integration deselections in 173.28 seconds;
  the three current Agno restart integration files pass 3/3 separately in 21.03
  seconds. A fresh isolated minimum-Agno affected/documentation slice passes 99 tests with one
  intentional unavailable-checkpoint skip; the retained complete `[dev,server]`
  minimum-Agno lane passes 1,739 with 35 optional-environment skips and 10 integration
  deselections. Retained Python 3.11/3.12/3.14 and Agno 3 preview evidence predates
  schema v12 and must rerun;
- the Agno 2.9 tool-checkpoint process gate passes all three crash scenarios with two
  completions, one reconciliation wait, six provider calls, one after restart, three
  tool effects, zero duplicates, and database integrity; the separate approval-wait
  gate passes its atomic-commit crash with two provider calls, one request/approved
  record/effect, exact decision replay, zero duplicates, and database integrity;
- the focused artifact/operation/checkpoint/recovery/lifecycle/trajectory gate passes:
  90 passed; 11 additional outbox-worker contracts pass;
- the focused capability executor file passes: 14 passed;
- the current combined service lane passes 60/60 against disposable PostgreSQL 17:
  57 PostgreSQL-backed runtime/learning/race/migration cases plus three deliberate
  SQLite race repetitions. It
  includes atomic output commit, depth-16 child recovery parity, forced backend-loss
  rollback/reconnect through non-retryable `RUNTIME_STORE_CONNECTION_LOST`, every-state/
  both-order effect races, indexed recovery plans over 10,000 terminal rows, and five bounded
  noisy-neighbor/p50-p95-p99/owner-isolation/saturation passes;
- six exact-container single-primary stop/start drills return a typed bounded outage in
  at most 1.003 seconds, reconnect the existing pool in at most seven observations,
  preserve acknowledged state/events/fence, observe at most two application
  connections, heal the container, and remove the marker. This is not replica
  promotion, network-partition/split-brain, or production RPO/RTO evidence;
- five owned two-node PostgreSQL 17 drills prove strict standby write rejection,
  positive replay lag, exact acknowledged-LSN/run/lease-fence catch-up, a bounded
  no-writer interval after the old primary is stopped, promotion only after fencing,
  existing/fresh `target_session_attrs=read-write` pool recovery, contiguous events,
  and complete six-resource cleanup. This is planned fenced-promotion evidence—not a
  production failover controller, external-fence certification, unplanned-loss RPO,
  split-brain/partition proof, old-primary rejoin, or production RTO;
- five completed owned `remote_apply` drills disconnect only the exact standby
  replication network, terminate only its exact named sender, assert the RuntimeStore
  connection policy, and prove a commit is not acknowledged during the observation
  window; rejoin must restore `streaming/sync` and replay before acknowledgement. They
  verify an exact acknowledged state/event/lease-fence manifest, send `SIGKILL` to the
  exact old primary, promote while it remains fenced, and recover through old/new pools
  under the two-connection cap with zero observed acknowledged-state/event loss. This
  is one two-node/single-fault RPO regression path—not automatic failover, external-
  fence certification, true split brain, simultaneous/multiple failure, or a
  production availability SLO;
- five completed round-trip drills require checksums/`full_page_writes`, pin an exact
  checkpoint/replay boundary before both promotions, abruptly fence the first writer,
  cleanly fence the second, and use two exact stopped-volume `pg_rewind` helpers to
  rejoin both former writers read-only and synchronous. Recovery configuration must
  contain exactly one password-free `primary_conninfo`; a mode-0600 temporary password
  file is removed by the helper, and any failed rewind target requires a fresh base
  backup. Both bounded no-writer checks, existing/fresh pool recovery, revision/fence
  3, seven contiguous events, zero observed acknowledged loss, the two-connection cap,
  and eight-of-eight cleanup pass. This is exact local recovery evidence—not automatic
  election/fencing, split-brain/quorum, production endpoint/rejoin automation, large-
  data/archive recovery, or a production RPO/RTO result;
- the first hardened real-etcd application-authority drill intentionally keeps the old writer live while
  promoting its standby, prove unguarded one-connection stores create divergent
  histories, then use official etcd 3.6.14 and the first-party adapter to admit only
  the exact current `cluster_name`. Stale server and stopped-authority access fail
  closed; same-endpoint restart recovers; a 500 ms PostgreSQL transaction deadline
  aborts over-lease work; and a lease/revision change immediately before commit rolls
  back the injected row. Stale denial takes 0.004 seconds, outage denial 0.002–0.003
  seconds, timeout observation 0.516 seconds, and seven-of-seven cleanup passes. The
  standalone live-etcd gate also proves revocation, natural expiry, and loss. This is
  Agnoclaw-client containment. A separate owned three-member gate uses unique client
  and peer certificates, TLS 1.2 minimum, mTLS, enabled RBAC, endpoint-bound gateway
  credentials, exact-key positive/negative permissions, one-member loss, majority-loss
  fail closure, different-endpoint recovery, and fence advancement. Its first run
  passes in 7.328 seconds, denies majority loss in 1.003 seconds, advances fence 3→4,
  and removes four of four owned resources plus temporary certificates. This is still
  not production controller election, durable multi-AZ quorum, network-partition/
  latency/certificate-rotation chaos, arbitrary-client watchdog/STONITH, or RPO/RTO
  certification;
- five consecutive post-service loopback PostgreSQL native dump/restore rehearsals
  exactly preserve 18 runtime tables, 117 rows, two sequences, logical schema/index/
  constraint metadata, ordered events, and start idempotency; the worst local
  dump+restore+verify window was 0.352 seconds. The
  probe cleans its exact dump, target database, and source marker independently and is
  a local/CI regression gate, not production backup or RPO/RTO certification;
- tests cover intent-before-dispatch, exact replay, store rollback, SQLite and
  PostgreSQL contention, stale fences, safe recovery, raw-error redaction,
  all-effect exception/timeout/cancellation, both database commit orders, every
  terminal-state transaction rollback/reopen/retry, cancellation after external
  success, artifact rollback/tamper/GC/encryption,
  exact pre-model request continuation, restart result recovery without redispatch,
  lifecycle unknown-effect projection, bounded owner-bound trajectory append,
  raw-content minimization, observer failure isolation,
  atomic approval waiting, raw-response bypass rejection, exact grants, late-decision
  rejection, cancellation tombstones, successful-settlement usage evidence, extractor
  failure isolation, child overage failure, and timeout/reconciliation truth.

These are local/CI contract results, not production failover or long-duration soak
certification.
