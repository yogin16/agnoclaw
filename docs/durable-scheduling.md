# Durable scheduling

Status: implemented 0.12 preview for trusted host-owned jobs on SQLite and PostgreSQL

Last reviewed: 2026-08-13

Durable scheduling turns a time-based request into an ordinary agnoclaw lifecycle run.
It does not introduce a second agent engine: recurrence selects work, the
`RuntimeSchedulerBackend` claims one attempt, and `AgentHarness.start()` provides the
same identity, operation settlement, artifacts, recovery, policy, and learning boundary
used by other long-running work.

Use the JSON scheduler only for compatibility and local demos. It persists definitions
and history but has no cross-process transaction, lease, fence, duplicate-fire defense,
or crash recovery. New unattended work should use the schema-v12 RuntimeStore path.

## Quick start with SQLite

Install the CLI and the scheduler extra if you use five-field cron expressions:

```bash
pip install "agnoclaw[cli,scheduler]"
```

Create one job in the same RuntimeStore the worker will use:

```bash
agnoclaw schedule add daily-brief \
  --runtime-db ~/.agnoclaw/runtime.db \
  --schedule "0 8 * * 1-5" \
  --timezone Asia/Dubai \
  --prompt "Produce the bounded daily operations brief" \
  --isolated \
  --max-retries 2 \
  --retry-delay 30 \
  --retry-backoff 2 \
  --retry-max-delay 3600 \
  --retry-jitter 10 \
  --misfire-policy fire_once \
  --misfire-grace 300 \
  --concurrency-key daily-brief \
  --overlap-policy queue
```

Run the single-host worker:

```bash
agnoclaw schedule worker \
  --runtime-db ~/.agnoclaw/runtime.db \
  --artifacts ~/.agnoclaw/artifacts \
  --model claude-sonnet-4-6 \
  --provider anthropic \
  --permission-mode default
```

For a trusted single-owner worker that should use personal/session learning on jobs
whose definitions grant consent, add:

```bash
agnoclaw schedule worker \
  --runtime-db ~/.agnoclaw/runtime.db \
  --learning-profile personal-session \
  --tenant-id acme \
  --user-id alice \
  --session scheduled-operations
```

Inspect or manually fire work through the same database:

```bash
agnoclaw schedule show daily-brief --runtime-db ~/.agnoclaw/runtime.db
agnoclaw schedule runs daily-brief --runtime-db ~/.agnoclaw/runtime.db
agnoclaw schedule trigger daily-brief --runtime-db ~/.agnoclaw/runtime.db
agnoclaw schedule disable daily-brief --runtime-db ~/.agnoclaw/runtime.db
```

The worker command owns and closes its SQLite store and harness. Management commands
open the store only for their invocation. Ctrl+C first waits for scheduler-owned tasks
to quiesce and then detaches supervised lifecycle work; it does not invent a
cancellation or failed result.

## Embed a worker

For host composition, inject the same store into the harness and scheduler adapter:

```python
import asyncio

from agnoclaw import (
    AgentHarness,
    HarnessConfig,
    LocalArtifactStore,
    RuntimeSchedulerBackend,
    SchedulerJob,
    SQLiteRuntimeStore,
)
from agnoclaw.heartbeat import HeartbeatDaemon


async def main() -> None:
    store = SQLiteRuntimeStore("./runtime.db")
    harness = AgentHarness(
        "anthropic:claude-sonnet-4-6",
        config=HarnessConfig.durable(),
        runtime_store=store,
        artifact_store=LocalArtifactStore("./artifacts"),
        permission_mode="default",
    )
    scheduler = RuntimeSchedulerBackend(store)
    scheduler.upsert_job(
        SchedulerJob(
            name="hourly-check",
            schedule="1h",
            prompt="Check the queue and write a concise status report",
            isolated=True,
            max_retries=2,
            retry_delay_seconds=30,
            concurrency_key="operations-check",
        )
    )
    daemon = HeartbeatDaemon(
        harness,
        scheduler_backend=scheduler,
        heartbeat_enabled=False,
        scheduler_poll_interval_seconds=1,
        scheduler_claim_limit=10,
    )
    try:
        await daemon.run_forever()
    finally:
        await harness.aclose(policy="detach")
        store.close()


asyncio.run(main())
```

For multi-worker service composition, replace `SQLiteRuntimeStore` with
`PostgresRuntimeStore` and give every worker the same service store. PostgreSQL uses
row locks plus a transaction-scoped advisory lock for each concurrency group. The
first-party CLI intentionally starts only the simpler SQLite worker; a service host is
responsible for PostgreSQL credentials, external writer authority, lifecycle identity,
deployment supervision, artifacts, and shutdown ordering.

## Job contract

`SchedulerJob` stores a revisioned definition. Updating a job creates a new revision;
an already-created attempt retains its immutable definition snapshot.

| Field | Contract |
|---|---|
| `schedule` | Positive interval such as `45s`, `30m`, or `2h30m`; or a five-field cron expression with `agnoclaw[scheduler]`. |
| `timezone` | IANA timezone used for cron calculation; persisted occurrence timestamps are UTC. |
| `misfire_policy` | `fire_once` coalesces old backlog, `catch_up` advances one nominal occurrence at a time, and `skip` records terminal skipped history beyond the grace window. |
| `misfire_grace_seconds` | Lateness allowed before the selected misfire behavior applies. |
| `jitter_seconds` | Stable hash-derived delay for one occurrence; it does not change across workers or restarts. |
| `max_retries` | Additional attempts allowed only after a known retryable terminal failure. |
| retry fields | Bounded exponential backoff and deterministic jitter; each retry is a new attempt for the same occurrence. |
| `concurrency_key` | Jobs with the same key share one serialized group; the job name is the default key. |
| `overlap_policy` | `queue` permits at most one pending/retry backlog per group; `skip` records a terminal overlap skip. |
| `isolated` | Uses a deterministic fresh session named from the occurrence, while retaining the same governed harness and stores. |
| metadata | JSON-only and limited to 65,536 UTF-8 bytes. Treat it and the prompt as trusted host configuration. |

Cron preserves the configured wall-clock hour through offset changes. A nonexistent
spring-forward wall time is skipped; an ambiguous fall-back wall time fires once at its
first valid instant. These semantics are regression-tested with IANA timezone data.

Durable jobs use the worker's immutable model/provider. Per-job model overrides are
rejected because silently ignoring them would make the persisted definition untruthful.
Run a separate worker/store partition when a deployment needs a different model policy.

## Identity, leases, and delivery truth

One nominal occurrence has deterministic identity from `(job name, job revision,
scheduled_at)`. One retry attempt has deterministic identity from `(occurrence,
attempt)`. Reclaiming an expired or deliberately released attempt keeps both identities
and increments its fence; it does not spend the retry budget.

The lifecycle admission key is stable for that exact attempt. If a worker loses its
scheduler lease after `start()`, the next worker reattaches to the recorded
`runtime_run_id` or the same idempotent lifecycle admission instead of starting a new
logical run.

```text
due job
  -> atomic occurrence + fenced scheduler claim
  -> lifecycle start with deterministic idempotency key
  -> bind runtime_run_id
  -> renew scheduler claim while waiting
  -> terminal scheduler settlement
```

SQLite uses `BEGIN IMMEDIATE` to serialize claims. PostgreSQL locks the job/run row and
the concurrency-group advisory key in one transaction. Both use the database clock for
due tests and lease expiry. A stale worker cannot renew, bind, finish, or release after a
new fence owns the attempt.

This is at-least-once worker delivery with deduplicated logical lifecycle admission,
not a claim of exactly-once external side effects. Effects still require agnoclaw's
intent-before-dispatch operation ledger and provider reconciliation.

## Failure and recovery matrix

| Observation | Scheduler action |
|---|---|
| Known retryable failure before or during lifecycle execution | Settle `failed`; create one bounded `retry_wait` attempt when budget remains. |
| Known non-retryable failure | Settle `dead_lettered`; do not retry. |
| Scheduler supervision is cancelled before lifecycle admission | Release to `pending`; reclaim the same attempt. |
| Supervision is cancelled after lifecycle admission | Release to `detached`; retain and reattach the same `runtime_run_id`. |
| RuntimeStore connection is lost or completion acknowledgement is ambiguous | Release/detach the same attempt; never fabricate a retry. |
| Lifecycle reports `RUN_WAIT_INCOMPLETE` | Attempt fenced lifecycle recovery; if recovery ownership is unavailable, poll rather than duplicate. |
| Lifecycle requires reconciliation | Detach immediately so the lifecycle reconciliation owner can resolve it. |
| Scheduler claim expires | A later worker increments the scheduler fence and reclaims the same attempt. |
| Misfire/overlap policy says skip | Persist terminal `skipped` history without model execution. |

History output is a diagnostic preview capped at 4,096 characters. Truncation records
`output_preview_truncated` and the original character count. The lifecycle terminal
result and artifact/output surfaces remain authoritative for full content. Stored
errors are stable safe codes, not raw provider exceptions.

## Learning and self-improvement

Scheduled execution does not bypass `LearningProfile` or Agno's LearningMachine. The
same trusted owner/context and store configuration still determine which learning
stores exist and which writes are allowed.

Learning writes are off for a job unless the host explicitly sets consent:

```bash
agnoclaw schedule add preference-review \
  --runtime-db ~/.agnoclaw/runtime.db \
  --schedule 24h \
  --prompt "Review the owner's recent confirmed preferences" \
  --learning-consent
```

`--learning-consent` is only consent propagation; it does not grant tenant/user scope,
enable a store, bypass update budgets, or authorize institutional promotion. The CLI's
`personal-session` preset requires explicit trusted `--tenant-id`, `--user-id`, and
`--session`; JSON scheduling cannot enable it. Embedded service hosts should inject
their own `LearningPolicy` plus trusted context. Personal
and session writes still require that scoped context and configured policy.
Institutional observations still become governed candidates, pass held-in/held-out/
transfer and safety gates, and require promotion authority. Automatic promotion remains
off until the remaining observer, deletion, migration, and measured-benefit release
gates pass. See [Learning and self-improvement](learning.md) and
[Governed learning candidates](learning-candidates.md).

## Security and tenancy boundary

The current scheduler is a trusted host control plane. It does not expose untrusted
multi-tenant schedule CRUD. Prompts, skill names, metadata, timing, and learning consent
must come from authenticated host policy, not model output or arbitrary request bodies.

For a service deployment:

- partition workers/stores or wrap job administration with exact tenant ownership;
- encrypt and restrict the RuntimeStore because prompts and diagnostic previews are
  persisted;
- supply durable scoped artifacts and normal lifecycle authorization;
- use PostgreSQL writer authority/fencing where the deployment can have multiple
  writable database timelines;
- supervise worker health and alert on `dead_lettered`, long-lived `detached`, lease
  churn, and reconciliation waits;
- back up the scheduler tables with the runtime ledger and verify restore before
  enabling workers.

Deleting a job stops future occurrence creation but intentionally retains run history.
Retention/deletion automation for scheduler history remains an open release gate.

## Compatibility and migration

Commands with no store option still use `~/.agnoclaw/schedules.json`; `--store` selects
another JSON compatibility file. `--runtime-db` selects the schema-v12 SQLite ledger.
Never run the JSON loop and durable worker as co-owners of the same logical job.

The local read-only 0.12 preflight inventories JSON definitions/history and requires
timezone, misfire, and old-writer-fence decisions. The implemented T14b service scanner
verifies exact Agno source schedule identities and
in-flight state. Schedule-map schema 1.1 and the transformation compiler turn explicit
behavior into deterministic schema-v12 jobs and inactive history. The service lifecycle
now persists those rows with provenance and bounded checkpoints, independently verifies
source/target/unowned-write evidence, records cutover authority, and rolls back only its
exact writes. Production scheduler migration still needs process-kill, rogue-writer,
least-privilege/TLS, large-data, partition/failover, and soak certification. See [the
service migration runbook](migration-service-0.12.md).

## Verification and current limits

The implementation has deterministic SQLite contention and stale-fence tests, a
64-worker single-occurrence race, reopen/retry/revision/overlap/misfire tests, daemon
crash-boundary tests, SQLite/PostgreSQL parity, and a real disposable PostgreSQL 17
multi-connection claim/reclaim/retry exercise.

It does not yet certify:

- a public multi-tenant schedule administration API or operator UI;
- legacy JSON apply/cutover/rollback or old-writer fencing automation;
- production PostgreSQL partition/failover/soak with scheduler workload;
- OTEL scheduler metrics/traces and service-wide alerts;
- scheduler-history retention/deletion and encrypted backup/PITR recovery;
- arbitrary post-dispatch provider/tool-stack continuation;
- outcome-quality proof for autonomously scheduled self-improvement.

These limits block a final 0.12 service claim; they do not weaken the implemented
atomic occurrence, lease, fence, identity, retry, and recovery contracts.
