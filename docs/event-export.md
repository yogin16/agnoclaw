# Durable event export

Status: preview first-party delivery boundary with a content-free OpenTelemetry bridge

`RuntimeOutboxWorker` exports committed `RuntimeEvent` batches from either first-party
`RuntimeStore`. It is the durable path for telemetry, audit, evaluation, and client
fan-out. An immediate `EventSink` remains a compatibility observer and is not a delivery
receipt.

## Minimal worker

```python
import asyncio

from agnoclaw import (
    RuntimeOutboxConfig,
    RuntimeOutboxWorker,
    SQLiteRuntimeStore,
)


class EventWarehouse:
    async def export(self, events):
        # Write the complete batch, idempotently, before returning.
        await warehouse.put_events(
            [event.to_dict() for event in events],
            idempotency_keys=[event.event_id for event in events],
        )


store = SQLiteRuntimeStore("runtime.db")
worker = RuntimeOutboxWorker(
    store=store,
    exporter=EventWarehouse(),
    config=RuntimeOutboxConfig(owner="telemetry-worker-1", max_attempts=20),
)
stop = asyncio.Event()
await worker.run(stop=stop)
```

Use a stable, deployment-unique owner for operations evidence. Set `stop` during
graceful shutdown; task cancellation also releases the live batch for retry. The worker
does not own or close the injected store or exporter.

## Delivery contract

- Delivery is **at least once**. A downstream write can succeed immediately before the
  process loses its lease or crashes. The exporter must deduplicate by `event_id`.
- Events inside one batch retain ascending outbox order. Multiple workers may finish
  different batches out of order; reconstruct per-run order with `(run_id, sequence)`.
- A batch is acknowledged only after `export(events)` returns. Failure or timeout
  defers the entire batch with bounded exponential delay, so partial remote success is
  safely redelivered.
- `delivery_timeout_seconds` must be below `lease_seconds`. A lease-loss acknowledgement
  fails rather than pretending delivery was recorded.
- Exporter exception text is not persisted or placed in `RuntimeOutboxBatchResult`.
  Reports expose stable `RUNTIME_OUTBOX_EXPORT_FAILED` or
  `RUNTIME_OUTBOX_EXPORT_TIMEOUT` codes. At `max_attempts`, the worker isolates the
  oldest event and a further failure returns `RUNTIME_OUTBOX_EXPORT_DEAD_LETTERED`;
  healthy events from the batch are immediately released.
- An empty `run_once()` result is normal. It means no event was currently eligible,
  including when failed work is waiting for its retry time.

`run_once()` is useful for an existing worker framework or deterministic tests. `run()`
adds only a bounded idle poll and stop event; process supervision, deployment leases,
and health reporting remain host responsibilities.

## Security and data handling

An exporter is privileged infrastructure. `trajectory.*` payloads are content-minimized,
but the outbox also contains authoritative lifecycle, operation, approval, and artifact
metadata. Apply destination authorization, transport encryption, tenant partitioning,
retention, deletion, and regional controls to the complete `RuntimeEvent`, not only its
trajectory subset. Never turn arbitrary exporter exception text into event attributes.

The worker does not reinterpret, enrich, or redact committed truth. If a field is unsafe
to export, prevent or protect it before persistence; exporter-only redaction is too late
for ledger safety.

For operational telemetry, use `RuntimeTelemetryBatchExporter` rather than sending the
raw event envelope to an OpenTelemetry backend. It deliberately projects only registered
event/state enums, bounded numeric measurements, and domain-separated HMAC identifiers
before calling `OpenTelemetryRuntimeSink`. Custom event names collapse to
`runtime.unknown`; prompts, arguments, targets, metadata, outputs, approval/error text,
and artifact bodies never enter schema `1.0`. Configuration and exact signal contracts
are in [Observability and safe run inspection](observability.md).

Dead-letter inspection and replay are privileged host operations. Use
`RuntimeDeadLetterAdmin`; do not expose either the admin object or its store methods as
model tools. The `ExecutionContext` must come from authenticated claims, a trusted host,
or an internal parent—not request path/body fields, client metadata, model text, or tool
arguments.

```python
from agnoclaw import (
    DEAD_LETTER_AUDIT_SCOPE,
    DEAD_LETTER_INSPECT_SCOPE,
    DEAD_LETTER_REQUEUE_SCOPE,
    ExecutionContext,
    RunOwner,
    RuntimeDeadLetterAdmin,
)

admin = RuntimeDeadLetterAdmin(store)
operator = ExecutionContext.create(
    tenant_id="tenant-42",
    user_id="ops-user-7",
    session_id="incident-8842",
    workspace_id="production-ops",
    roles=("runtime-operator",),
    scopes=(
        DEAD_LETTER_INSPECT_SCOPE,
        DEAD_LETTER_REQUEUE_SCOPE,
        DEAD_LETTER_AUDIT_SCOPE,
    ),
)
target = RunOwner(tenant_id="tenant-42", user_id="customer-9")

page = await admin.inspect(
    context=operator,
    owner=target,
    reason_code="incident_8842_review",
    limit=25,
)
dead_letter = page.items[0]
decision = await admin.requeue(
    dead_letter,
    context=operator,
    owner=target,
    reason_code="destination_recovered",
    mutation_id="incident-8842-requeue-outbox-319",
)
history = await admin.audit_history(
    context=operator,
    owner=target,
    limit=25,
)
```

Investigate the destination/configuration first. Requeue can redeliver an event whose
remote side effect succeeded before the final failed receipt, so `event_id` dedupe is
still mandatory. Keep `mutation_id` stable across timeout/cancellation retries and
never put secrets or free-form incident notes in it or `reason_code`.

The administration contract is intentionally narrow:

- authority fails closed unless the operator has an authoritative identity, a user ID,
  the exact operation scope, and the same tenant as the target owner;
- there is no global or cross-tenant owner enumeration; a host enumerates only owners
  it already knows and is authorized to administer;
- cursors are opaque and bound to the exact owner and view;
- every inspection page, including an empty page, appends a content-minimized audit
  record with target owner, operator/authority digests, safe reason, requested cursor/
  limit, result count, and returned bounds;
- replay binds exact owner, outbox ID, quarantine timestamp, delay, authority, operator,
  reason, and mutation ID. Identical retries return the prior decision; semantic reuse
  fails without changing the outbox;
- the replay update and audit append share one transaction. Once a database call starts,
  cancellation waits for its authoritative outcome before propagating; retry with the
  same mutation ID to learn the committed decision;
- audit-history reads require their own scope and do not recursively audit themselves.

Schema v10 history is append-only through the service API and deliberately excludes the
event payload and raw operator identity. It retains the exact target owner so operators
can prove which tenant/user lane was touched. This is not a tamper-evident external
archive against a privileged database administrator; export/anchor audit history if
your compliance model requires independent custody.

## Current boundary

The preview proves SQLite/PostgreSQL leasing, exact lease-token acknowledgement,
deferral and quarantine, one-at-a-time poison isolation, bounded retries, exact-CAS
requeue, timeout/cancellation release, and duplicate event IDs across retry. It does not
yet provide:

- per-tenant partitions, weighted fairness, or destination-specific cursors;
- lease renewal for exports longer than the configured deadline;
- live OpenTelemetry agent/model/tool spans, trace correlation, or built-in webhook,
  Kafka, and remote `HarnessRun` adapters;
- multi-destination independent acknowledgements;
- a packaged operator UI, independent audit anchoring/export, or owner-discovery control
  plane;
- outbox lag/retry/dead-letter self-metrics, support bundles, or production
  soak/backpressure certification. The current OpenTelemetry bridge emits safe
  event/token/cost counters, not exporter-health SLOs.

Those remain T4b/T5b/T10b/T13 release work. Alert on quarantine and keep retained event
bodies protected; quarantine prevents a poison event from stalling the queue but does
not decide whether it is safe to discard or replay.

## Store primitives

`lease_outbox()` increments `attempts` and returns an expiring token.
`acknowledge_outbox()` settles only a matching unexpired token. `defer_outbox()` likewise
requires the live token, clears ownership, and moves `available_at` by at most 24 hours.
`dead_letter_outbox()` also requires the live token and accepts only a safe reason code.
`inspect_dead_letters()` and `requeue_dead_letter()` are adapter primitives for
`RuntimeDeadLetterAdmin`, not raw end-user APIs. Both require an exact owner and digested
operator evidence; replay additionally requires an idempotent mutation ID and exact
quarantine timestamp. Retention refuses to prune an event while its outbox record is
pending, but a quarantined record retains its own event envelope after the ordinary
run-event copy is pruned. Dead-letter audit history has no run/outbox foreign key, so
its minimized proof survives ordinary event/outbox retention.

See [Run lifecycle](runtime-lifecycle.md), [Observability](observability.md),
[Harness architecture](architecture.md), and [Security](security.md) for the source
event, authority, projection, and retention contracts.
