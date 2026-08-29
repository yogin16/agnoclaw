# Run lifecycle and RuntimeStore

Status: 0.12 development preview; lifecycle, both store authorities, operation ledger,
store-issued execution leases, durable registered-capability approvals,
artifact-backed successful-result recovery, and conservative startup classification
implemented; exact pre-model request-checkpoint continuation plus a conditional Agno
2.9 per-provider/tool-batch/approval reconstruction path implemented; general nested,
parallel, raw/streaming/parser/output-model, and reconciliation continuation remain in progress; bounded
exact-owner startup scanning and bulk classification are implemented; declared-child
active-worker deadlines, settlement-time reported-usage checks, output-schema
validation, and governed synthesis execution are implemented

Last verified: 2026-08-18

This document describes implemented behavior. The complete target and release gates
remain in [Harness architecture](architecture.md) and the
[0.12 release plan](releases/v0.12.0-plan.md).

## Small public API

One-shot code remains valid:

```python
result = await harness.arun("Investigate the incident")
```

With an explicit `quick`, `durable`, or `service` profile, `run()` and `arun()` are
convenience adapters over `start()` plus `wait()` and retain their completed-result
shape. `quick` uses the same kernel with an ephemeral store. Only
`HarnessConfig.legacy()` retains direct execution. Raw streaming is a bounded,
process-local presentation attached to a lifecycle run; closing or falling behind the
presentation does not cancel the run. Explicit-profile synchronous calls use one
reusable event-loop coordinator owned by the harness. The iterator relays raw display
events and authoritative terminal failures while the lifecycle ledger remains the
source of truth. Close the harness to drain or cancel work and join that coordinator.
Once a harness has established synchronous ownership, do not call async `start()` or
`arun()` on another event loop; that fails before run creation with
`HARNESS_EVENT_LOOP_OWNERSHIP_CONFLICT`. Use one API style per harness instance.

Controllable work uses the same model/tool pipeline through a `HarnessRun`:

```python
run = await harness.start(
    "Investigate the incident",
    session_id="incident-42",
    idempotency_key="incident-42-investigation-v1",
)

async for event in run.events():
    print(event.sequence, event.event_type)

result = await run.wait()
```

The frozen lifecycle surface is intentionally limited:

```python
await run.wait(timeout=30)
await run.status()
run.events(after=cursor, follow=None)
await run.cancel()
await run.command(command)
await run.child(child_harness, message, context=context, delegation_id="task-v1",
                purpose_code="research", result_schema=result_schema)
await run.children()
results = await run.child_results(require_terminal=True)
artifact = results.outcomes[0].result_artifact
if artifact is not None:
    await run.read_child_artifact(artifact.artifact_id)
await run.synthesize_children(
    synthesis_harness,
    "Reconcile the child findings.",
    context=context,
    delegation_id="synthesis-v1",
    result_schema=synthesis_schema,
)
```

`child()` is the capability-only declared-child preview. It requires the parent to be
actively running and the child harness to share the same store; it does not relabel the
raw `spawn_subagent` compatibility tool. See [Declared child runs](child-runs.md).
That raw tool exists only on the named legacy profile; explicit profiles omit it.

`HarnessSession.start()` starts related work with the session's exact trusted context.
`HarnessSession.send()` remains the completed convenience operation. On explicit
profiles it inherits the lifecycle adapter; on `legacy` it retains the direct result or
raw-stream wrapper. Use `HarnessSession.start()` when the caller needs the run ID or
control methods.

### The same grammar over HTTP

`RemoteHarnessClient.start()` and `get_run()` return `RemoteHarnessRun`, which preserves
the local `id/result/status/wait/events/output/cancel/command` grammar over the authenticated
version-`1.0` AgentOS lifecycle routes. New remote lifecycle clients should use these
methods. `RemoteHarnessClient.arun()` remains a completed-response/raw-stream wire
wrapper. An explicit-profile server now executes that request through its lifecycle
kernel, but this wrapper does not expose durable controls; use `start()` when the
client needs them.

The HTTP adapter derives `ExecutionContext` from verified AgentOS request state,
requires `agents:read` or `agents:run`, rejects anonymous open-mode requests, and
reauthorizes owner identity on every reattachment. Events use bounded cursor pages; the
client performs the follow loop and proves run identity, gap-free sequence, and cursor
advancement on every page. A client disconnect or abandoned waiter does not call
`cancel()`.

See [AgentOS and remote lifecycle adapter](embedding/agentos-adapter.md) for setup,
routes, limits, compatibility guidance, and error behavior.

### First-party client routing

Small internal adapters return the existing `HarnessRun`; they do not add another
public run type. Explicit `quick`, `durable`, and `service` profiles enter `start()`;
only named legacy and unknown duck-typed compatibility clients retain direct `arun()`.
Interactive explicit-profile clients attach one bounded live presentation consumer to
the same lifecycle worker and still settle through the normal model operation gateway.
The current route matrix is:

| Source | Current route | Why |
|---|---|---|
| non-interactive CLI `run` in an explicit profile | lifecycle start/wait | preserve run identity, settlement, and safe shutdown |
| heartbeat and shared/isolated schedule execution in an explicit profile | lifecycle start/wait | unattended work needs reattachment and cancellation truth |
| schema-v12 scheduler worker | database-clock fenced attempt, then lifecycle start/wait | process loss must reclaim or reattach the same logical attempt |
| named-legacy CLI, heartbeat, and JSON schedules | direct compatibility call | preserve an explicit migration escape hatch |
| async REPL and TUI in an explicit profile | lifecycle start/wait plus bounded live presentation | keep token UX without bypassing model intent, lease, settlement, terminal truth, or explicit cancellation |
| async REPL/TUI and sync chat in named legacy | raw provider stream | preserve explicitly selected legacy behavior |
| sync chat in an explicit profile | lifecycle start/wait through one reusable harness-owned coordinator | preserve lifecycle authority without creating a temporary event loop per call; slow/closed presentation consumers detach without cancelling work |
| host-coordinated declared child | child harness `start()` on the shared lifecycle store | preserve lineage, authority, joins, cancellation, and independent child handles |
| model `DeclaredChildTemplate` tool | registered capability gateway, then declared child start/wait | expose only bounded task/delegation identity while host policy remains immutable |
| remote `DeclaredChildTemplate` route | authenticated parent reattachment, registered template start, ordinary child handle | preserve owner/scopes, immediate long-run control, typed child list/results, and template non-enumeration |
| raw `spawn_subagent` tool | available only in named legacy; omitted/rejected in explicit profiles | preserve a bounded migration path without claiming declared/durable child ingress |

The live presentation is non-authoritative and process-local. Its single-consumer queue
is bounded; overflow or consumer closure detaches without applying backpressure or
cancellation to the run. The connected client then waits for the terminal result.
Durable state and content-minimized, gap-free cursor events remain in `RuntimeStore`.
Provider `RunContent` text is separately batched into owner-scoped artifacts and
replayed with `HarnessRun.output()` or `RemoteHarnessRun.output()`. Embedders request
this path with `persist_output=True`; authenticated lifecycle HTTP starts default it
on and expose an explicit false value for structured-output runs.
The remaining compatibility rows are release debt, not hidden parity. Child harnesses
already close in `finally`; CLI-owned and isolated schedule harnesses close
deterministically.

One segment holds at most 8,192 characters or 32 deltas, and one remote page holds at
most 50 segments. Staged bytes become live only when their reference, runtime sequence,
and outbox row commit atomically. Replay verifies exact owner, purpose, content address,
checksum, length, run binding, and segment order. Normal completion and cancellation
flush the partial segment. A hard process loss may lose the single uncommitted partial
segment; it does not turn an ambiguous post-dispatch model operation into a resumable
one. The terminal result is still authoritative.

The schema-v12 scheduler stores a deterministic occurrence ID and attempt ID, then adds
the lifecycle `runtime_run_id` to running, completed, failed, or detached history. It
uses the store clock, renewable leases, and monotonic fences; a stale worker cannot bind
or settle after reclaim. If supervision is cancelled after lifecycle admission, the
attempt becomes `detached` and the underlying run is not implicitly cancelled. A later
worker reattaches or recovers that same run. Ambiguous store or lifecycle outcomes stay
on the same attempt; only known retryable terminal failures create a retry. Schedule and
heartbeat failures expose stable safe codes, not raw exception strings. See
[Durable scheduling](durable-scheduling.md).

## Implemented guarantees

- A run ID names one logical requested execution. Terminal state never reopens.
- State changes use compare-and-set revisions.
- Creation, each state change, its sequence-numbered event, and its outbox row commit
  in one SQLite transaction.
- Start idempotency is scoped to the tenant/principal tuple. The same key plus the same
  canonical request digest returns the original run; changed input or configuration
  fails with `RUN_START_IDEMPOTENCY_CONFLICT`.
- Event sequences are positive, monotonic, and gap-free within a run. Cursors are
  opaque and bound to one run ID.
- `wait()` does not consume the event stream. Cancelling or timing out one waiter does
  not cancel the underlying run.
- `cancel()` is explicit and idempotent. A caller disconnect has no cancellation
  meaning.
- `get_run()` reauthorizes exact tenant/user ownership and hides unauthorized IDs as
  `RUN_NOT_FOUND`.
- Completed values and safe terminal diagnostics are persisted. Raw exception messages
  are never copied into terminal failure records.
- Outbox consumers claim expiring leases and acknowledge with the exact lease token.
- A second harness using the same store can reattach to a completed run and read its
  persisted normalized result.
- Declared children have durable parent/root/depth lineage, isolated sessions,
  deterministic delegation identity, exact owner inheritance, subset-only descendant
  grants, atomic parent/child creation events, and independent run handles.
- Parent completion waits for direct children. `all_success` requires successful
  terminals; `collect` accepts every terminal state. Child terminal settlement is
  recorded atomically on the parent stream.
- Parent cancellation/failure/expiry requests cancellation across all live descendants
  in the same transaction. Active workers observe propagated cancellation after lease
  renewal; ambiguous non-repeatable effects still stop at reconciliation.
- A declared child's active worker turns its finite wall grant into an authoritative
  cancellation request. Successful child model settlements record stable Agno token/
  cost evidence before terminal completion; reported excess fails the child, while
  missing provider evidence is explicitly marked unverified rather than assumed zero.
- Direct parents can collect deterministic typed child outcomes without waiting or
  cancelling, select explicit pending/success/failure policy, replace oversized
  synthesis values with lossless result-artifact links, and page only direct-child
  owner-authorized artifacts. Collection itself never invokes a synthesis model.
- A child may bind a bounded object result schema into declaration/spec digest `1.1`.
  Normalized content is graph-bounded and validated after successful operation/budget
  settlement but before completion; the content-free decision is idempotent and
  known-success recovery re-applies both contracts. Stored spec `1.0` still decodes.
- `synthesize_children()` turns an explicit bounded result snapshot into another
  ordinary declared child. Failed sources require host opt-in, untrusted result JSON is
  injection-framed, oversized values stay lossless artifact pointers, total evidence is
  bounded, and the synthesis child receives no ambient artifact-read authority.
- Runs with the same exact tenant/session key serialize in-process; different sessions
  may overlap up to `runtime_max_concurrency`. Session keys are length-framed rather
  than delimiter-concatenated, and a hot session cannot consume global permits while
  it waits on its own lane.
- Before external work, a worker atomically claims store-issued run and exact-session
  leases with independent monotonic fences. It renews them while active and cannot
  steal an unexpired claim. PostgreSQL serializes cross-process session claims with
  database-clock expiry and sorted advisory transaction locks.
- `close()`/`aclose()` stop admission before handling existing runs. Shutdown ownership
  is an explicit `drain`, `detach`, or `cancel` policy; a close timeout never closes
  resources underneath a live worker.
- `start()` persists a non-repeatable model-operation intent before invoking Agno,
  claims dispatch with an exact fence, and settles success/failure/unknown evidence in
  the same run ledger. Cancellation before dispatch never calls the provider;
  cancellation during an ambiguous call never masquerades as clean cancellation.
- `recover_run()` acquires the same fenced ownership and classifies stranded work
  without blindly replaying an external call. A settled request checkpoint may continue
  only while the model operation is absent or still `planned`, after exact run-owner,
  session, harness-spec, request-digest, operation, and artifact verification. When the
  operation has a committed result artifact and the same ArtifactStore is configured,
  recovery verifies and completes the run without redispatch.
- On the certified Agno 2.9 path, every provider call is a separate non-repeatable
  operation with a stable ordinal/request digest and artifact. After a governed
  registered-capability result, recovery validates Agno's exact `tool-batch`
  checkpoint before continuing. A later ambiguous provider operation still parks at
  reconciliation; a later settled operation replays its artifact without redispatch.
- A required registered-capability approval atomically commits both the exact request
  and `waiting_for_approval`. Decisions and grants are owner-isolated, revisioned,
  expiry-bound, and content-minimized. The run resumes only from a matching settled
  decision digest; leaving the wait cancels the pending request in the same transaction.
- Repeating an exact settled approval decision is idempotent and creates no new event or
  grant; a conflicting retry fails. Before a tool-result checkpoint, the certified Agno
  2.9 path reconstructs only from the exact request checkpoint, first-provider artifact,
  decision/grant, frozen authority/spec, and provider ordinal.

These contracts have fault-injection, reopen, two-connection CAS, caller-cancellation,
cursor, owner, idempotency, and restart-continuation tests. In addition to the outer
pre-model/known-success gates, real-process tests certify one Agno 2.9 governed
two-provider/one-capability sequence and its pre-tool approval wait. They do not certify
arbitrary nested/parallel/raw/streaming/parser/output-model stacks.
The remote boundary additionally has cross-version AgentOS contracts for authentication,
scope, claims-first identity, bounded parsing, result polling, reattachment, exact event
cursors, lifecycle commands, malformed peer responses, and disconnect semantics.
First-party adapter contracts additionally prove profile routing, raw-stream rejection
at the non-streaming adapter, lifecycle-governed REPL/TUI presentation, authoritative
terminal reconciliation, bounded slow-display detachment, explicit interactive-worker
cancellation, durable CLI completion, schedule/runtime identity links,
waiter-cancellation detach semantics, safe scheduler diagnostics, same-loop CLI close,
and isolated-harness close on success/failure/cancellation.

## State model

The versioned states are:

```text
created -> queued -> running
                      |  +-> waiting_for_input
                      |  +-> waiting_for_approval
                      |  +-> waiting_for_reconciliation
                      |  +-> paused
                      |  +-> cancelling -> cancelled
                      +----> completed
                      +----> failed
                      +----> failed_with_unknown_effects

nonterminal waiting work may also become expired
```

Terminal states are `completed`, `failed`, `failed_with_unknown_effects`, `cancelled`,
and `expired`. A follow-up is a new run in the same session. A fork is also a new run
with explicit lineage; it never mutates its source.

The pure reducer lives independently of SQLite. Storage adapters must pass the same
transition matrix, terminal immutability, exact pending-request binding, steering safe
point, compare-and-set, and idempotency conformance tests.

## Typed commands

Commands are strict version-`1.0` data objects:

```python
from agnoclaw import Fork, Pause, Respond, Resume, Steer

await run.command(Pause("operator inspection"))
await run.command(Resume())
await run.command(Respond("approval-17", {"approved": True}))
await run.command(Steer("Prioritize database evidence"))
await run.command(Fork(from_step=17))
```

Unknown versions, command types, and fields fail parsing. Mutable response payloads are
deep-frozen at construction. Persisted transition evidence contains a digest rather
than raw steering or response content.

Current execution boundary:

- pause/resume is implemented only before provider dispatch and requires the harness
  instance that owns the live worker;
- steering is accepted only before the explicit steering-close transition and
  requires the harness instance that owns the pre-dispatch request buffer;
- a reattached handle on a non-owning harness fails pause, resume, and steering with
  `RUN_CONTROL_OWNER_UNAVAILABLE` before recording an accepted transition;
- a late pause or steer fails instead of pretending it affected the model;
- ordinary response IDs are bound by the lifecycle reducer. An approval response is
  stricter: the store requires the exact settled approval state and decision digest,
  so raw `Respond(..., {"approved": true})` cannot grant authority;
- fork currently fails with `RUN_FORK_CHECKPOINT_REQUIRED`: the request checkpoint is
  not an effect-capable step checkpoint and cannot safely support time travel.

These limitations are deliberate truthfulness, not the final 0.12 command contract.

## Events and cursors

`events(after=None, follow=None)` first reads a finite committed snapshot. If the run is
nonterminal, the default follows it through the first terminal event and then ends.

- `follow=False`: snapshot only.
- `follow=None`: snapshot plus follow until terminal.
- `follow=True`: continue polling after terminal until timeout or caller cancellation;
  typed `RunHeartbeat` values may be yielded and never consume sequence numbers.

Resume with a run-bound cursor:

```python
from agnoclaw.runtime import encode_event_cursor

cursor = encode_event_cursor(run_id=run.run_id, sequence=42)
async for event in run.events(after=cursor):
    ...
```

Lifecycle execution projects every emitted normalized `HarnessEvent` into the same
authoritative ledger as a content-minimized `trajectory.*` event. The projection is
bounded, binds a digest of the full authority context, retains only safe operational
identifiers, and digests raw prompts, model text, errors, arguments, and arbitrary
metadata. Its event, monotonic sequence, and outbox row commit atomically before the
compatibility `EventSink` is notified.

An immediate `EventSink` is therefore a best-effort observer during lifecycle runs;
its failure cannot rewrite already-committed run truth, even when legacy
`EventSinkMode.FAIL_CLOSED` was selected. Durable export and retry consume the outbox.
Direct `run()`/`arun()` compatibility calls retain their old sink behavior, including
fail-closed mode, and do not create ledger projections.

`RuntimeOutboxWorker` is the first-party at-least-once delivery boundary. It leases an
ordered batch, calls one async exporter under a deadline shorter than the lease,
acknowledges only after complete success, and lease-safely defers the whole batch after
failure, timeout, or cancellation. Consumers deduplicate by `event_id` and reconstruct
per-run order with `(run_id, sequence)`. See [Durable event export](event-export.md).

## RuntimeStore

`RuntimeStore` is the single public persistence boundary. The current
`SQLiteRuntimeStore` owns:

- schema migrations;
- run snapshots and revisions;
- transition idempotency evidence;
- immutable run events and sequences;
- bounded idempotent runtime-event proposals with exact owner checks, terminal fences,
  and atomic outbox projection;
- atomic terminal result/error projections;
- start-idempotency keys;
- transactional outbox rows, expiring exporter leases, and exact-token acknowledgement
  or bounded deferral;
- operation intents with pre-provisioned canonical result slots, revisions, dispatch
  attempts, fences, exact-slot safe settlements, optional provider request IDs,
  content-minimized usage/cost evidence, mutation idempotency, and operation-linked
  run events.
- store-issued run and exact-session execution leases, claim identities, expirations,
  releases, and independent monotonic fence tokens.
- scoped artifact metadata and operation-result relationships committed atomically with
  successful settlement after immutable bytes are staged externally.
- immutable approval requests, decisions, least-privilege authorization grants,
  approval events, and indexes for run/state/expiry and uniqueness.
- exact-owner, run-ID-keyset discovery of executable `queued`, `running`, and
  `cancelling` runs for bounded startup recovery.

`PostgresRuntimeStore` implements the same contract for service deployment with a
bounded Psycopg pool, row locks/CAS, database-clock leases, `SKIP LOCKED` outbox claims,
tenant-aware indexes, and migration arbitration. Its real-service conformance gate is
green; production failover/partition/RPO/RTO certification is still open. See
[PostgreSQL RuntimeStore operations](postgresql-runtime-store.md).

Inject it explicitly when persistence beyond the harness object is wanted:

```python
from agnoclaw import AgentHarness, SQLiteRuntimeStore

store = SQLiteRuntimeStore("/var/lib/my-service/agnoclaw-runtime.db")
harness = AgentHarness(model=model, runtime_store=store)
```

Without an injected store, `start()` lazily creates an in-memory SQLite authority. It
has the same transaction semantics but no process-restart retention. The caller owns an
injected store; the harness owns its lazy in-memory store.

Store-level owner checks are defense in depth. Application authorization must still
construct `ExecutionContext` from authenticated claims at a trusted boundary.

Both stores expose explicit terminal-run event retention. Pruning is blocked while any
affected outbox record is undelivered, preserves terminal and idempotency evidence, and
advances a durable watermark. Reads behind it raise `RUN_EVENT_CURSOR_EXPIRED` with an
opaque resume cursor instead of returning a misleading partial history.

## Failure and cancellation semantics

`wait()` returns the original in-process result when available. A reattached completed
handle returns the persisted normalized value. Failed, cancelled, terminal
unknown-effect, and expired waits raise `RunWaitError` with the authorized terminal
snapshot and safe error projection; `.result` remains `None`. A nonterminal
`waiting_for_reconciliation` run instead raises `RunReconciliationRequiredError` with
code `RUN_RECONCILIATION_REQUIRED` and remains inspectable/reconcilable.

The lifecycle adapter classifies its generic model call as non-repeatable because it is
nondeterministic and cost-bearing. A provider/model exception therefore settles the
operation `unknown` and parks the run at `waiting_for_reconciliation` unless a certified
provider adapter can prove non-dispatch. Only allowlisted operation diagnostics are
persisted; raw exception text is not.

Cancellation before operation dispatch settles `cancelled`. Cancellation during the
model call, or after external success but before its result reference is durable,
settles the operation `unknown` and parks the run for reconciliation. Repeated
cancellation returns the same waiting state; it cannot erase ambiguity. This is
deliberately stronger than Python task cancellation: cancelling a coroutine cannot
prove that a provider or tool did nothing. See
[Operations, effects, and recovery](operations-and-recovery.md).

A live capability approval wait occurs inside that surrounding model operation. The
pending approval is cancelled atomically and no capability factory is entered, but the
generic outer model call has already crossed a non-repeatable dispatch boundary.
Cancellation therefore truthfully leaves the run waiting for reconciliation, not
cleanly `cancelled`.

A declared child wall timeout follows the same truth rule. The worker first commits
`CHILD_TIMEOUT_EXCEEDED`, then cancels local execution. If provider dispatch may have
occurred, the model operation is `unknown` and the child waits for reconciliation.
Reported token/cost excess is detected after successful settlement and fails with
`CHILD_RESOURCE_BUDGET_EXCEEDED`; it is not a prepaid provider spending ceiling.
After a known successful child operation is restored from its verified result artifact,
recovery repeats reported-usage and declaration-bound output validation before applying
the terminal completion transition. A schema mismatch fails the child without changing
the already-settled successful operation.

## Recovery

Recovery is explicit and conservative:

```python
run = await harness.recover_run(run_id, context=trusted_context)
await run.wait()
```

For a bounded service-startup sweep:

```python
cursor = None
while True:
    batch = await harness.recover_pending_runs(
        context=trusted_context,
        cursor=cursor,
        limit=25,
        concurrency=4,
    )
    for item in batch.items:
        print(item.run_id, item.status, item.state, item.error_code)
    cursor = batch.next_cursor
    if cursor is None:
        break
```

The host enumerates authorized tenant/user scopes and starts a separate sweep for each;
there is intentionally no global cross-owner query. Cursors contain only a version,
owner digest, and last run ID, and fail with `RUN_RECOVERY_CURSOR_INVALID` when malformed
or reused for another owner. A page is limited to 100 runs and local claim concurrency
to 32. The default is 25/4. Results preserve keyset order and expose only state plus a
safe error code. `recovered` means the exact run was claimed/classified or its verified
pre-model worker was relaunched; inspect or await the returned run separately for final
business success. By default, candidates must be at least one configured lease duration
old, closing the queue-to-first-claim race; callers may set 0–86,400 seconds explicitly.

`recover_run()` reauthorizes the exact owner and atomically claims the run plus its
tenant/session lane. It never silently replays provider or tool work:

- a pre-dispatch operation with no checkpoint becomes a known failed run;
- a settled `run_request_checkpoint` continues exactly once when the model operation is
  absent or only `planned`, after verifying its content-addressed bytes, exact owner and
  session, full frozen authority context, harness-spec digest, request digest, internal
  operation semantics, and any existing model intent;
- on the certified Agno 2.9 path, ordered provider operations and artifacts are checked
  before any continuation. A settled governed capability result also requires the
  native `tool-batch` checkpoint; `dispatching`/`unknown` later provider work blocks,
  and a settled later provider result is replayed;
- after an approval decision but before a tool-result checkpoint, recovery validates
  the exact pending-request history, settled provider-1 artifact, decision/grant,
  authority/spec, and provider ordinal before deterministic reconstruction. The
  capability and provider 2 each execute at most once under their operation evidence;
- a declared child additionally validates every durable parent/root/depth relation and
  non-escalating grant under the recovery claim, restores the exact child spec before
  safe pre-model continuation, and refuses to dispatch beneath a terminal ancestor;
- `dispatching` or `unknown` non-repeatable work becomes
  `waiting_for_reconciliation`;
- a known failed or cancelled operation preserves that outcome;
- a known successful operation with a committed result artifact is verified and
  completed without redispatch; missing configuration/reference becomes
  `RUN_RECOVERY_RESULT_UNAVAILABLE`, while missing/tampered committed bytes are typed
  artifact corruption.

An unexpired worker claim cannot be stolen: bulk recovery reports `lease_busy`, advances
the current sweep, and a later fresh sweep can retry after expiry. Expired or explicitly
released claims may be reclaimed under higher fences. Paused, input/approval waits,
reconciliation waits, created runs, and terminal runs are never selected. New work that
sorts before the current keyset cursor is discovered by the next fresh sweep. Scanner
cancellation propagates to pending claims but does not undo a recovery or worker launch
that already committed. The persisted checkpoint contains the message, full execution
context/admission envelope, and canonical lifecycle keyword arguments, so its
ArtifactStore must use the deployment's encryption, access-control, retention, and
deletion policy. The current safe point is before outer-model dispatch: there is no
serialization of an in-flight Agno/Python stack. The separate Agno 2.9 path adds one
validated post-tool checkpoint and deterministic pre-tool approval reconstruction; it
does not generalize to nested/parallel/raw/streaming/parser/output-model continuation.
See [Durable artifacts](artifacts.md) and
[Operations, effects, and recovery](operations-and-recovery.md#agno-29-tool-batch-checkpoint-restart).

Reconciliation waits have their own bounded worker and are deliberately excluded from
general startup recovery:

```python
batch = await harness.reconcile_pending_operations(
    provider_observer,
    observer_digest="sha256:<observer-code-and-policy-digest>",
    context=trusted_context,
    cursor=cursor,
)
```

The host observer performs read-only external verification and returns exact
revision/digest-bound evidence, or `None` to defer. The ledger accepts only scoped,
physically readable reconciliation artifacts and CAS-settles `succeeded`, `failed`, or
`effect_absent`; it never retries the original effect. A prior settlement whose run
continuation was interrupted is reported as `continued` and completed/failed without
calling the observer again. See
[Evidence-bound reconciliation worker](operations-and-recovery.md#evidence-bound-reconciliation-worker).

## Concurrency, fairness, and shutdown ownership

Lifecycle workers combine bounded process-local tenant/session admission with
store-issued execution ownership:

```python
config = HarnessConfig(
    runtime_max_concurrency=16,
    runtime_max_waiting=1024,
    runtime_max_waiting_per_tenant=256,
    runtime_max_waiting_per_session=32,
    runtime_admission_timeout_seconds=30,
    runtime_lease_seconds=30,
    runtime_lease_renew_interval_seconds=10,
)
```

No `session_id` means the run receives its own lane. A same-session waiter takes no
execution slot; ready sessions enter capacity in tenant round-robin order, so a hot
tenant has bounded bypass while another tenant is ready. Nested global, tenant, and
session ceilings prevent a noisy scope from reserving every waiter; every level also
has, by default, a finite wait. Overflow and expiry become retryable
`RUNTIME_ADMISSION_OVERLOADED` terminal evidence rather than an unbounded coroutine or
an invisible drop. `harness.runtime_admission_stats()` returns content-free current,
peak, rejection, timeout, cancellation, tenant, and lane counters for metrics.

The fairness guarantee above is one process. The authoritative schema-v12 store then
atomically leases the run and exact tenant/session lane, so separate harness processes
sharing a store cannot concurrently own the same session. A heartbeat renews the exact
token and fences; lease loss cancels the worker and conservatively classifies in-flight
non-repeatable work. Distributed weighted tenant fairness and binding every nested
capability safe point to the execution fence remain release gates.

Shutdown is explicit:

```python
await harness.aclose(policy="drain")   # reject starts, wait, then close owned resources
await harness.aclose(policy="detach")  # reject starts, return; same-loop supervisor drains
await harness.aclose(policy="cancel")  # reject starts, cancel and settle, then close
```

`runtime_close_policy` selects the default (`drain`).
`runtime_close_timeout_seconds` optionally bounds how long a drain/cancel caller waits.
If that bound expires, `HARNESS_CLOSE_TIMEOUT` is raised while the shielded shutdown
supervisor remains responsible for the workers and resources. A later explicit
`cancel` may escalate an earlier drain/detach. Caller-owned runtime stores are never
closed by the harness; lazy in-memory stores are.

Resource ownership follows the same embedding rule across the default tool surface:

- host-local command executors, default-tool wrappers, configured MCP toolkits, and
  browser backends created by `AgentHarness` are harness-owned;
- an injected `db=`, `runtime_store=`, `RuntimeBackend` command/workspace/browser
  implementation, caller `tools=`, pack tool, or context-provider tool remains
  caller-owned;
- closing a host-local executor is idempotent, rejects new commands, closes parent-side
  log handles, and terminates/reaps every tracked background command. On POSIX, each
  background command has its own process group so descendants do not survive harness
  shutdown;
- standalone `LocalCommandExecutor` users should use its context manager or call
  `close()` explicitly.
- CLI async commands close their owned harness on the same event loop that created its
  lifecycle resources. Isolated scheduled harnesses drain after settlement and select
  explicit detach/cancel behavior when their supervisor is cancelled.

These are ownership guarantees, not garbage-collection conventions. The standard test,
compatibility, PostgreSQL, and publish lanes fail on `ResourceWarning` and
`pytest.PytestUnraisableExceptionWarning`.

`close()` is for synchronous code. Calling it from a running event loop raises
`HARNESS_ASYNC_CLOSE_REQUIRED`, and synchronous detach is rejected because a temporary
event loop cannot own background work. Async detach is process-local—it survives the
calling coroutine, not event-loop or process death. Durable recovery still requires T6.

## What is not yet certified

- lifecycle process restart beyond the implemented pre-model request, known-result,
  evidence-reconciliation, and certified Agno 2.9 governed tool/approval boundaries;
- complete capability/tool coverage through the universal `OperationGateway` (the
  lifecycle model call, registered capabilities, and currently constructed first-party
  tools are integrated; plugin/pack `CapabilitySpec` registrations join that path;
  configured MCP discovery/call joins it; explicit-profile `run/arun` enters the same
  lifecycle, while only named-legacy calls remain outside it; raw
  plugin/pack/context/caller-supplied MCP tools fail before lifecycle run creation);
- universal operation-dispatch binding to the active run-lease fence;
- distributed/weighted tenant fairness beyond process-local round-robin admission and
  exact service-wide session serialization;
- production PostgreSQL automatic election/record ownership, durable multi-AZ etcd
  quorum/backup certification, watchdog/STONITH and arbitrary-client/paused-host
  fencing, network-partition/latency/certificate-rotation control (the owned local
  three-voter TLS 1.2 mTLS/RBAC/member-loss/recovery regression passes),
  unplanned-loss RPO, production endpoint-discovery/rejoin automation and large-data rotations,
  production-scale noisy-neighbor/slow-
  exporter, encrypted off-host/artifact/key/PITR recovery, corruption response, and
  published RTO certification (the first-party adapter, real-service transaction
  tests, isolated load gate, typed connection-loss rollback, bounded exact-container
  single-primary stop/start, exact-manifest native restore, and owned planned
  lag/catch-up/fenced-promotion/read-write-pool, remote-apply false-ack/abrupt-loss/
  zero-observed-loss, local double-rewind/round-trip-role rehearsals, and optional
  application-level exact-server/fence/TTL/commit-revalidation containment under two
  writable timelines and the first-party etcd adapter's live revision/revocation/
  expiry/loss behavior are implemented);
- process-restart continuation for raw, nested, parallel, streaming, parser/output-
  model, directly injected opaque-model, or unsupported extension approval/tool stacks
  (the factory-backed outer operation plus one governed factory-backed Agno 2.9
  sequence and exact decision retry are implemented);
- effect-capable fork and post-dispatch pause/resume;
- native Agno/provider event ingress beyond emitted harness events, direct-compatibility
  projection, a versioned schema registry, and first-party outbox-to-OTEL/client
  exporters;
- removal guidance for named-legacy raw `spawn_subagent`, persisted database-clock child
  deadlines, provider-preflight token/cost reservation, certified provider receipts, a
  first-party governed cross-child artifact-reader capability, and distributed
  owner-scoped orphan-sweep scheduling.
  Model-visible and remote callers can now use host-owned `DeclaredChildTemplate`
  ingress without granting those callers model/budget/capability/learning choices. The
  implemented active-worker deadline and reported-usage settlement
  checks are operational controls, not restart-safe/pre-spend hard ceilings. Async
  REPL/TUI live display remains
  process-local even though its text
  segments are artifact-backed and replayable; raw provider event objects/tool UI
  state are not replayed, and raw child runs are still compatibility-only;
- scheduler JSON apply/cutover/rollback, untrusted multi-tenant schedule administration,
  scheduler-history retention, OTEL/operator alerts, and production scheduler
  partition/failover/soak certification. Atomic schema-v12 SQLite/PostgreSQL claims,
  leases/fences, lifecycle reattachment, retries, misfires, overlap policy, and stale-
  worker rejection are implemented;
- transparent artifact spill beyond the implemented host/plugin/pack registered-
  capability and lifecycle first-party-tool/configured-MCP slice to governed context-
  provider/raw-MCP adapters, direct compatibility tools, and outer model results. Manual plus opt-in
  automatic/reactive context replacement archives the complete scoped trajectory,
  supports lexical search/selective rehydration, retains spill/failure invariants, and
  accepts a bounded reviewed typed continuation with token-efficient priority indexing.
  Cooperative same-host file fencing is optional; multi-host/non-cooperating writers,
  live-provider proof, automatic continuation extraction/merge/fidelity, and drift
  certification remain open.

Do not label the current preview as durable execution merely because its lifecycle
record survives. See [ADR-0001](adr-0001-recovery-ownership.md) for the crash evidence
that makes this distinction necessary.

## Error reference

| Code | Meaning |
|---|---|
| `RUN_START_IDEMPOTENCY_CONFLICT` | Same scoped start key, different canonical request. |
| `RUN_REVISION_CONFLICT` | State changed before compare-and-set commit. Retry from the authoritative snapshot. |
| `RUN_TRANSITION_INVALID` | Command/worker transition is not valid from the current state. |
| `RUN_TERMINAL_IMMUTABLE` | An operation tried to reopen a terminal run. |
| `RUN_RESPONSE_REQUEST_MISMATCH` | Response does not match the exact pending request. |
| `APPROVAL_DECISION_REQUIRED` | Approval continuation lacks the exact settled state and decision digest. |
| `APPROVAL_RUN_NOT_WAITING` | A decision arrived after the run left its exact approval wait. |
| `APPROVAL_ALREADY_SETTLED` | A retry conflicts with the settled approval; exact replay returns the existing decision. |
| `RUN_STEERING_CLOSED` | Steering arrived after its certified safe point. |
| `RUN_CONTROL_OWNER_UNAVAILABLE` | This harness does not own the live pre-dispatch worker required to apply pause, resume, or steering. |
| `RUN_PAUSE_SAFE_POINT_UNAVAILABLE` | Current embedded worker cannot pause safely at this point. |
| `RUN_FORK_CHECKPOINT_REQUIRED` | No certified checkpoint exists for an effect-capable fork. |
| `RUN_EVENT_CURSOR_INVALID` | Cursor is malformed, unsupported, or belongs to another run. |
| `RUN_EVENT_CURSOR_EXPIRED` | Requested history was pruned; use the authorized resume cursor. |
| `RUNTIME_STORE_OVERLOADED` | Bounded store capacity is exhausted; retry with backoff. |
| `POSTGRES_WRITER_AUTHORITY_DENIED` | The external grant or exact writable-server check failed, changed before commit, or the server deadline expired; re-read authority before retrying. |
| `RUNTIME_STORE_CONNECTION_LOST` | An acquired connection failed; outcome may be ambiguous. Re-read/reconcile before retrying. |
| `RUNTIME_RETENTION_EXPORT_PENDING` | Undelivered outbox evidence blocks pruning. |
| `RUN_NOT_FOUND` | Run is absent or not visible to the exact owner. |
| `RUN_CANCELLED`, `RUN_FAILED`, `RUN_EXPIRED` | Typed terminal result from `wait()`. |
| `RUN_FAILED_UNKNOWN_EFFECTS` | Execution stopped with an external outcome that cannot be safely inferred or retried. |
| `OPERATION_ALREADY_DISPATCHING` | Another worker owns or may own the operation; implicit stealing is forbidden. |
| `OPERATION_RECONCILIATION_REQUIRED` | Operation outcome is ambiguous and needs external/operator evidence. |
| `OPERATION_RESULT_UNAVAILABLE` | The operation succeeded, but this process has no durable result loader. |
| `OPERATION_RESULT_SLOT_MISMATCH` | A result tried to fulfill an identity other than the one persisted before dispatch. |
| `OPERATION_RESULT_SLOT_ALREADY_FULFILLED` | The pre-provisioned result identity is already bound to a different artifact. |
| `ARTIFACT_CORRUPT` | A committed result artifact failed size/checksum/protection/format verification. |
| `ARTIFACT_SCOPE_MISMATCH` | Staged artifact ownership differs from the authoritative run. |
| `RUNTIME_LEASE_UNAVAILABLE` | The run or exact session lane has an unexpired owner. |
| `RUNTIME_LEASE_LOST` | The worker no longer owns the exact claim token and fences. |
| `RUNTIME_LEASE_CLAIM_RELEASED` | An explicitly released claim cannot be reopened. |
| `RUNTIME_LEASE_TERMINAL_RUN` | Execution ownership was requested for a terminal run. |
| `RUNTIME_STORE_LEASES_REQUIRED` | A custom RuntimeStore does not implement store-issued execution leases. |
| `RUNTIME_SUPERVISOR_FAILED` | A lease/deadline supervisor failed; the worker was cancelled and the run failed closed unless reconciliation was required. |
| `CHILD_RESOURCE_BUDGET_EXCEEDED` | Reported child token or cost usage exceeded its declared grant. |
| `CHILD_OUTPUT_SCHEMA_INVALID` | A declared child result schema is invalid, unsupported, or outside its bounds. |
| `CHILD_OUTPUT_SCHEMA_MISMATCH` | Settled normalized child content did not satisfy its declaration-bound schema. |
| `CHILD_SYNTHESIS_PAYLOAD_TOO_LARGE` | The bounded child-result evidence snapshot exceeds the host's total synthesis limit. |
| `CHILD_RECOVERY_LINEAGE_INVALID` | Persisted child ancestry or authority could not be certified; no restart dispatch occurred. |
| `CHILD_RECOVERY_ANCESTOR_TERMINAL` | Recovery cancelled the child without dispatch because an ancestor was terminal. |
| `RAW_SUBAGENT_LIFECYCLE_UNSUPPORTED` | An explicit profile attempted raw text-returning subagent orchestration; use declared children. |
| `TEAM_LIFECYCLE_UNSUPPORTED` | An explicit profile attempted a raw Agno Team preset; use declared children. |
| `SKILL_LIFECYCLE_DISPATCH_UNSUPPORTED` | A fork or command-tool skill attempted lifecycle admission without a declared child/capability contract. |
| `RUN_RECOVERY_CURSOR_INVALID` | A bulk-recovery cursor is malformed or belongs to another exact owner. |
| `RUN_RECOVERY_CHECKPOINT_UNAVAILABLE` | Recovery found no settled readable request checkpoint and refused to replay. |
| `RUN_RECOVERY_CHECKPOINT_INVALID` | Checkpoint schema or internal operation evidence is invalid. |
| `RUN_RECOVERY_CHECKPOINT_SCOPE_MISMATCH` | Checkpoint authority/artifact scope differs from the durable run owner. |
| `RUN_RECOVERY_REQUEST_MISMATCH` | Checkpoint content, artifact, and frozen request digest disagree. |
| `RUN_RECOVERY_SPEC_MISMATCH` | The current harness specification differs from the checkpointed runtime. |
| `RUN_RECOVERY_MODEL_INTENT_MISMATCH` | A pre-existing planned model intent is not the exact certified request. |
| `RUN_RECOVERY_RESULT_UNAVAILABLE` | External success is known, but no durable result artifact can complete the run. |
| `HARNESS_ASYNC_CLOSE_REQUIRED` | Synchronous close was called from an event loop. |
| `HARNESS_SYNC_DETACH_UNSUPPORTED` | Sync detach cannot preserve an async worker owner. |
| `HARNESS_CLOSE_TIMEOUT` | Caller wait expired; shutdown supervision continues. |
| `EXTENSION_TOOL_LIFECYCLE_UNSUPPORTED` | Lifecycle construction found raw extension or mutable dynamic-toolkit ingress; publish a `CapabilitySpec`. |
