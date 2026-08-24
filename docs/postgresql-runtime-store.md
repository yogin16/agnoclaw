# PostgreSQL RuntimeStore operations

Status: 0.12 development preview; real-service transaction/retention contracts,
one-standby remote-apply abrupt-loss recovery, round-trip rewind/rejoin, and optional
application-level writer-authority containment with a first-party etcd v3 adapter
pass; production controller/watchdog, partition/quorum, multi-fault, and RPO/RTO
certification remain open

Last live-service verification: 2026-08-18; the CI-equivalent combined command passes
60/60 against disposable PostgreSQL 17: 57 PostgreSQL-backed runtime, learning,
operation-race, scheduler, inspection, and service-migration cases plus three deliberate
SQLite operation-race repetitions. Coverage includes atomic output segments, depth-16
declared-child lineage/cancellation/recovery parity, forced backend loss during an
in-flight transaction, every-state/both-order effect races, and recovery planner gates
over 10,000 terminal runs and operations. It also covers read-only pooled inspection,
safe settlement measurements, the complete service-migration lifecycle, forced
process-death resume, three-database least privilege, and a 5,000-row memory bound. A
separate whole-database container restart
probe passes with the original pool and a fresh pool. A separate owned two-node gate
passes lag/catch-up, explicit old-primary fencing, promotion, and multi-host pool
reattachment without claiming that PostgreSQL supplies a split-brain control plane.
A synchronous companion repeatedly prevents false acknowledgement while its required
standby is unreachable and preserves the exact acknowledged runtime manifest through
an abrupt primary loss and explicit promotion. A third gate rewinds that killed former
writer, rejoins it as the exact synchronous standby, rotates the writer role back, and
rewinds/rejoins the other former writer.

`PostgresRuntimeStore` is the first-party service implementation of the same
`RuntimeStore` contract as SQLite. Install only when needed:

```bash
pip install 'agnoclaw[postgres]'
```

```python
from agnoclaw import (
    AgentHarness,
    LocalArtifactStore,
    PostgresRuntimeStore,
)
store = PostgresRuntimeStore(
    "postgresql://agnoclaw:secret@db.example/agnoclaw",
    min_pool_size=2,
    max_pool_size=10,
    max_waiting=100,
    pool_timeout_seconds=5,
)
artifacts = LocalArtifactStore("/var/lib/agnoclaw/artifacts")
harness = AgentHarness(
    model=model,
    runtime_store=store,
    artifact_store=artifacts,
)
```

The caller owns an injected store and must close it after all harnesses/workers that
share it have drained.

For operator inspection, create a separate least-privilege pool and request read-only
mode explicitly:

```python
inspection_store = PostgresRuntimeStore(
    inspection_dsn,
    read_only=True,
    min_pool_size=1,
    max_pool_size=2,
)
```

Read-only construction skips migrations, requires the database to already be at the
exact current schema, rejects `writer_authority`, and sets
`default_transaction_read_only=on` on every pooled connection. Store mutation entry
points additionally fail with typed `RUNTIME_STORE_READ_ONLY`; this application check
does not replace a PostgreSQL role that lacks write privileges. The first-party
`agnoclaw inspect run` command uses this mode and accepts only a credential environment
variable name, never a DSN value on the command line. See
[Observability and safe run inspection](observability.md).

The same store now backs durable scheduling:

```python
from agnoclaw import RuntimeSchedulerBackend, SchedulerJob

scheduler = RuntimeSchedulerBackend(store)
scheduler.upsert_job(
    SchedulerJob(
        name="daily-brief",
        schedule="0 8 * * 1-5",
        timezone="Asia/Dubai",
        prompt="Produce the bounded daily brief",
        max_retries=2,
        concurrency_key="daily-brief",
    )
)
```

Workers share this adapter and the same `AgentHarness` RuntimeStore. PostgreSQL locks
the job/run row and takes a transaction-scoped advisory lock per concurrency group, so
independent pools cannot claim the same occurrence or violate group serialization.
Database time controls due/lease decisions. Full lifecycle composition and the failure
contract are in [Durable scheduling](durable-scheduling.md).

For an HA deployment, inject a `PostgresWriterAuthorityProvider` into
`writer_authority=`. The first-party `EtcdPostgresWriterAuthority` consumes one exact,
dedicated, lease-backed etcd v3 key through the JSON gRPC gateway:

```python
import httpx
import os
import ssl

from agnoclaw.runtime import EtcdGatewayCredentials, EtcdPostgresWriterAuthority

tls = ssl.create_default_context(cafile="/run/secrets/etcd-ca.pem")
tls.load_cert_chain(
    certfile="/run/secrets/agnoclaw-etcd.pem",
    keyfile="/run/secrets/agnoclaw-etcd-key.pem",
)
etcd_http = httpx.Client(
    verify=tls,
    trust_env=False,
    limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
)
authority = EtcdPostgresWriterAuthority(
    endpoint="https://etcd.internal.example:2379",
    key="/agnoclaw/postgres/prod-a/writer",
    authority_id="prod-a-immutable-cluster-uid",
    # Pin this out-of-band from the intended cluster. Do not discover it from the
    # same untrusted request path at process startup.
    cluster_id="14841639068965178418",
    http_client=etcd_http,
    ttl_uncertainty_seconds=1,
    gateway_credentials=EtcdGatewayCredentials(
        endpoint="https://etcd.internal.example:2379",
        username="agnoclaw-authority-reader",
        password=os.environ["AGNOCLAW_ETCD_READER_PASSWORD"],
    ),
)

store = PostgresRuntimeStore(
    dsn,
    writer_authority=authority,
    writer_authority_check_timeout_seconds=2,
    writer_authority_safety_margin_seconds=1,
    writer_authority_max_transaction_seconds=30,
)
```

The controller—not agnoclaw—must create, exclusively own, renew, revoke, and transfer
the key. Its UTF-8 value has exactly this bounded schema and no extra fields:

```json
{"schema":"agnoclaw.postgres-writer-authority.v1","authority_id":"prod-a-immutable-cluster-uid","server_id":"postgres-a"}
```

Attach the key to a live, dedicated lease containing no other key. `server_id` must
equal a non-empty, deployment-unique PostgreSQL `cluster_name`. The adapter performs a
linearizable exact-key `Range`, checks the expected etcd `cluster_id`, live lease TTL,
and exact attached-key set, then repeats the linearizable `Range`. Both observations
must be identical. The key's etcd `mod_revision` is the fence token, so a delete,
recreate, transfer, or rewrite advances the generation without trusting a value- or
clock-supplied counter. The reported TTL subtracts request elapsed time and
`ttl_uncertainty_seconds`; agnoclaw compares no cross-system wall clocks.

The adapter never watches, caches, or returns last-known-good authority and never
creates, renews, or transfers a lease. The endpoint must be an HTTPS origin without
userinfo, path, query, or fragment. Plain HTTP is accepted only for explicit loopback
tests. Supply mTLS, CA validation, connection limits, and proxy policy through an
injected `httpx.Client`; use an `ssl.SSLContext` because tuple-style `httpx` client
certificates are deprecated. The caller owns and closes the client after the store
drains.

The JSON gRPC gateway cannot use a client certificate Common Name as the RBAC user.
Use a client certificate with no Common Name for transport authentication and
`EtcdGatewayCredentials` for `/v3/auth/authenticate`. Credentials are pinned to the
adapter's exact origin, exchanged inside the same total authority deadline, never sent
on redirects, and cached behind a lock. The adapter performs exactly one
reauthentication-and-retry after HTTP 401; all authentication responses are bounded and
all public failures are content-free. `clear_token()` supports explicit server-side
revocation, but endpoint cutover requires a new endpoint-bound credentials object and
authority adapter. Do not put credentials in the URL, client-global headers, logs, or
configuration committed to source control.

etcd's default `simple` token is member-local. Behind a load balancer or across direct
member failover, configure JWT tokens with the same protected verification/signing
material on every member, or otherwise guarantee token affinity and reauthenticate on
cutover; do not assume a simple token minted by one member is valid on another. Give
the reader role only exact-key read access; the dedicated lease inspection required by
the adapter is read-only. A custom `PostgresWriterAuthorityProvider` remains supported,
but it must provide the same fresh linearizable holder, monotonic fence, and
conservative relative-TTL contract.

With this option enabled, every store access enters a transaction, verifies authority
and exact non-recovery server identity at entry, sets PostgreSQL 17
`transaction_timeout` strictly inside the remaining lease, and freshly revalidates the
same generation/server before commit. Authority outage, late/invalid grant, short TTL,
stale or conflicting generation, standby selection, or server mismatch raises retryable
`POSTGRES_WRITER_AUTHORITY_DENIED` with a content-free reason. The provider call itself
must obey its timeout. Choose the external lease TTL so it exceeds control-plane retry
and uncertainty, agnoclaw's check timeout, transaction maximum, safety margin, and the
physical watchdog margin.

This is application-level containment for agnoclaw clients, not STONITH. It cannot stop
arbitrary SQL clients, a paused process/VM, or a host whose controller failed before it
could close PostgreSQL. PostgreSQL explicitly requires a mechanism to tell an old
primary it is no longer primary; mature controllers add watchdog reset because normal
process shutdown can fail. Production must use an HA controller plus required
watchdog/STONITH and test authority loss, host pause, and stale endpoint routing.
The design follows etcd's documented default linearizable KV reads, monotonic revision
clock, lease-backed key expiry, JSON gateway, and TLS/mTLS model: see the official
[API guarantees](https://etcd.io/docs/v3.6/learning/api_guarantees/),
[API overview](https://etcd.io/docs/v3.6/learning/api/),
[JSON gateway](https://etcd.io/docs/v3.6/dev-guide/api_grpc_gateway/), and
[transport security guide](https://etcd.io/docs/v3.6/op-guide/security/). The
[configuration reference](https://etcd.io/docs/v3.6/op-guide/configuration/) defines
client/peer certificate authentication, the TLS 1.2 default minimum, and `simple`/JWT
token modes.

As of 2026-08-13, upstream's latest release is
[etcd 3.7.1](https://github.com/etcd-io/etcd/releases/tag/v3.7.1), while this repository's
immutable live gate remains 3.6.14 until the 3.7 image digest and upgrade behavior pass
the same source and installed-artifact matrix. Do not downgrade below 3.6.11 when RBAC
protects partially trusted clients: the etcd security team documented a transaction
authorization bypass fixed in that patch line. The pinned 3.6.14 gate includes the fix;
see the [May 2026 advisory](https://etcd.io/blog/2026/may-patch-release/). This explicit
latest-versus-certified split prevents a mutable tag from silently changing release
evidence.

## Implemented database contract

- A bounded Psycopg pool starts explicitly and must pass its connection preflight.
  Exhausted/overfull pools fail with retryable `RUNTIME_STORE_OVERLOADED`; the queue is
  never intentionally unbounded. The configured connect timeout is also passed to every
  libpq connection attempt. A connection lost after acquisition is instead surfaced as
  content-free, non-retryable `RUNTIME_STORE_CONNECTION_LOST`: a write may have reached
  the server, so the caller must re-read authoritative state/reconcile before deciding
  whether to retry. Psycopg documents the pool queue, `min_size`,
  `max_size`, startup `wait()`, and statistics in its
  [official pool guide](https://www.psycopg.org/psycopg3/docs/advanced/pool.html).
- Connections use autocommit outside a unit of work, preventing idle read
  transactions. Mutations use explicit transaction contexts, which Psycopg commits on
  success and rolls back on exception as documented in
  [transaction management](https://www.psycopg.org/psycopg3/docs/basic/transactions.html).
- A provider-output segment's scoped artifact reference, content-free runtime event,
  monotonic sequence, and outbox row commit in one transaction; fault rollback cannot
  leave an authorized reference to merely staged bytes.
- Transaction isolation is PostgreSQL `READ COMMITTED`, strengthened where required by
  row-level `FOR UPDATE`, revision compare-and-set, unique constraints, and transaction
  advisory locks for migrations/start-idempotency arbitration.
- Creation and every transition atomically commit the snapshot, monotonic event,
  terminal projection when applicable, idempotency evidence, and outbox row.
- Declared-child creation atomically binds parent/root/depth lineage, unique delegation,
  child and parent events, and start idempotency. Child terminal settlement appends the
  parent event in the same transaction; parent completion enforces join policy; parent
  cancellation/failure/expiry locks the tree in ancestor-first order and requests
  cancellation for every eligible descendant.
- Outbox workers claim ordered rows with `FOR UPDATE SKIP LOCKED`; PostgreSQL describes
  `SKIP LOCKED` for queue-like consumers in its
  [SELECT reference](https://www.postgresql.org/docs/current/sql-select.html).
- Lease expiry and acknowledgement use the database clock. An acknowledgement needs
  the exact unexpired token; an expired or stolen lease cannot settle another worker's
  claim.
- Tenant/user/session and ready-outbox indexes are present. Ordered cursor reads—not
  notifications or replica observations—remain authoritative.
- Startup recovery uses an exact `(tenant_id, user_id)` filter,
  `runtime_runs_executable_owner_idx`, immutable run-ID keyset pagination, and a
  caller-supplied bound. Operation dispatch discovery uses
  `runtime_operations_dispatch_queue_idx`. Both partial indexes contain only the
  executable states they serve; terminal history does not expand either index. The API
  never exposes a global ownerless scan or includes intentional waits and pauses.
- Reconciliation discovery separately selects exact-owner reconciliation waits using
  `runtime_runs_reconciliation_owner_idx`,
  `runtime_operations_run_reconcile_idx`, database-clock age, and operation-ID
  keysets. Evidence validation, operation CAS/fence advance, artifact references,
  `operation.reconciled`, and outbox emission share one transaction; a settled wait can
  be continued after coordinator loss without another observer call.
- Migrations take a database advisory lock and are restart-idempotent. The current
  development schema is version 12. Version 4 added operation intents, exact mutation
  idempotency, revisions, dispatch fences, safe settlements, and operation-linked run
  events. Version 5 adds atomic run/session execution leases, exact tokens, expiry,
  heartbeat renewal, release, and monotonically increasing reclaim fences. Version 6
  adds scoped artifact metadata and operation-result references committed atomically
  with settlement and outbox evidence after external bytes are staged. Version 7 adds
  approval request, decision, and authorization-grant ledgers; run/state/expiry
  indexes; unique request-decision, request-grant, and grant-nonce constraints; and
  transactional approval lifecycle events/outbox rows. Version 8 adds exact-token
  dead-letter quarantine, safe reason codes, ready-queue isolation, bounded listing,
  and exact-CAS requeue. Version 9 adds a store-authoritative recovery-age timestamp and
  owner/state/age/run-ID discovery index; caller event clocks do not control eligibility.
  Version 10 adds owner-bound inspection/replay evidence, globally unique replay
  mutation IDs, and append-only service history independent of run/outbox retention.
  Version 11 adds explicit child relations, declaration digests, indexed parent lookup,
  unique parent/delegation identity, join evidence, and cancellation propagation.
  Version 12 adds durable scheduler job/run ledgers, deterministic occurrence and
  attempt identities, lease/fence state, retry timing, concurrency groups, and bounded
  history indexes.

Approval waiting locks the run row before inserting the immutable request. Settlement
uses the same run-then-approval row order, revision CAS, owner checks, exact request
digest/nonce, and database transaction so concurrent lifecycle cancellation cannot
deadlock with or be bypassed by a late approval. Leaving the wait cancels a pending
request in the same transaction as the run transition.

The CI service job and local real-service gate cover concurrent start idempotency,
run and operation row-lock/CAS contention, transaction rollback after an injected
fault, terminal/result persistence, ordered outbox leases, cursor retention,
idempotent transition replay after pruning, exact operation settlement/replay, and
atomic artifact-result references, approvals/grants, trajectory append, outbox deferral,
dead-letter owner isolation/audit/requeue, v9→v10 and v10→v11 preservation, concurrent
mutation convergence, replay rollback, exact-owner recovery/reconciliation pagination,
atomic evidence settlement, and declared-child lineage/join/cancellation/spec-1.1
persistence. `EXPLAIN` contracts first prove every recovery partial index is available;
an unforced `EXPLAIN (ANALYZE, BUFFERS)` fixture with 10,000 terminal runs and
operations then rejects sequential scans of either ledger. The shared operation matrix
injects rollback at every mutation checkpoint, reopens every state, covers every effect
class at all external boundaries, and uses finite database barriers to prove both
cancellation-versus-commit orders without sleeps. The lane also terminates the exact
PostgreSQL backend during a transition,
proves the uncommitted snapshot was rolled back with no partial event, reconnects the
bounded pool, renews the original lease/fence, and commits the retry. The real-service
suite skips unless
`AGNOCLAW_TEST_POSTGRES_URL` points to an isolated test database.

Operation rows and their mutation evidence are retained separately from visible run
events. Pruning a terminal run's event history therefore does not erase the evidence
needed to prove an operation mutation was already applied. See
[Operations, effects, and recovery](operations-and-recovery.md).

Execution ownership claims the run row and its exact tenant/session lane in one
transaction. Sorted advisory locks serialize session claims across different run rows.
PostgreSQL's transaction-stable database clock owns expiry; stale tokens cannot renew
or release a reclaimed lease. The embedded coordinator heartbeats ownership and
cancels itself on loss.

PostgreSQL stores artifact metadata and authorization, not bodies. Configure a shared
ArtifactStore suitable for every worker replica. A local filesystem store is valid only
when that filesystem is durably shared with the required consistency and recovery
properties. Back up the ledger, artifact objects, and matching key generations as one
recovery set. See [Durable artifacts](artifacts.md).

## Retention and cursor expiry

`prune_run_events()` is intentionally strict:

```python
decision = store.prune_run_events(
    run_id,
    through_sequence=10_000,
    owner=owner,
)
```

- only terminal runs may be pruned;
- every affected outbox row must already be delivered;
- the terminal event is retained;
- the durable pruning watermark advances transactionally;
- start and transition idempotency retain their original event evidence separately;
- a cursor older than the watermark raises `RUN_EVENT_CURSOR_EXPIRED` with an opaque
  `resume_cursor` for the earliest retained boundary.

Retention is not authorization. The same exact owner check runs before the watermark is
disclosed or data is removed.

## Pool and overload operations

Export `store.pool_stats` to metrics. At minimum alert on connection errors/loss,
request errors, queued-request wait time, and sustained pool saturation. Do not respond
to saturation by making the pool or admission queue unlimited; raise capacity, reduce
transaction duration, or shed/retry work with jitter.

`max_pool_size` is per process. Budget the sum across replicas and migrations below the
database's connection ceiling, leaving capacity for administration and recovery.

Lifecycle execution has a separate process-local gate: `runtime_max_concurrency`,
global/tenant/session waiting ceilings, and `runtime_admission_timeout_seconds`. Ready
sessions rotate across tenants; `harness.runtime_admission_stats()` exposes only counts
and peaks.
`RUNTIME_ADMISSION_OVERLOADED` therefore means the lifecycle gate rejected/expired a
wait, while `RUNTIME_STORE_OVERLOADED` means the PostgreSQL pool rejected/expired a
connection request. Neither queue is allowed to grow without a configured bound.

## Recovery-index performance gate

The safe local SQLite benchmark creates only temporary databases, measures 100 warm
samples of 100 calls at 1,000 and 10,000 terminal run/operation rows, and fails if any
p95 recovery path exceeds a 2.0 growth ratio:

```bash
uv run python scripts/benchmark_recovery_index.py
```

The final 2026-08-11 run measured 0.018703→0.018474 ms for recoverable operations,
0.023635→0.023232 ms for recoverable runs, and 0.051301→0.047823 ms for reconciliation
(p95 ratios 0.988, 0.983, and 0.932). It followed two additional passes; the largest
ratio across all three was 1.185. Use the executable command for regression comparison;
exact sub-millisecond timings vary by host. The live PostgreSQL suite separately proves
plan shape over 10,000 terminal rows. Neither gate replaces the open production-scale
latency, concurrency, and noisy-neighbor tests below.

## Bounded PostgreSQL load and isolation gate

Run the loopback-only service probe against an isolated database whose name contains
`test`:

```bash
uv run python scripts/benchmark_postgres_runtime.py \
  --dsn postgresql://postgres:secret@127.0.0.1:5432/agnoclaw_test
```

The probe creates 10,000 synthetic terminal-history rows under a cryptographically
random prefix, creates one recoverable run for a probe owner and a noisy owner, then:

- records p50/p95/p99 for `get_run()` and exact-owner recovery before and during three
  concurrent noisy-owner workers;
- fails on any owner leak, p99 above 25 ms, or p95 slowdown above 4x;
- holds both connections in a two-connection pool, permits at most two queued requests,
  and requires all four contenders to receive typed retryable overload errors;
- runs the deterministic process admission fairness oracle and requires the cool tenant
  after at most one ready hot-tenant turn; and
- removes every row under the exact random prefix, including on a failed measurement.

Five consecutive disposable-PostgreSQL-17 passes on 2026-08-11 completed all 400
probe calls while 1,299–1,324 noisy-owner queries completed. Worst observed noisy p99
was 3.967 ms and worst p95 slowdown was 1.794x; every 2+2 saturation probe returned four
`RUNTIME_STORE_OVERLOADED` errors with a 0.1-second retry hint, and all 10,002 synthetic
rows were removed per pass. Exact timings are host-specific; the thresholds and
invariants are the regression contract.

This establishes bounded local/CI behavior, exact-owner isolation, and process-local
fairness. It does **not** establish distributed weighted fairness, production memory or
connection budgets, primary failover, network partitions, slow-exporter isolation, or
production RPO/RTO.

## Disposable single-primary outage probe

For a stronger local drill, the probe validates one exact running Docker container and
its published DSN port, then stops it deliberately. A read must fail within the finite
pool/connect window with either typed capacity exhaustion or conservative
connection-loss semantics. The probe starts the same container, then requires the
already-open pool to reconnect, acknowledged state and the unexpired lease fence to
remain unchanged, a post-outage transition and contiguous events to commit, a fresh
pool to observe them, and application connections to remain at or below two:

```bash
uv run python scripts/postgres_restart_probe.py \
  --dsn 'postgresql://postgres:secret@127.0.0.1:55438/agnoclaw_test' \
  --container agnoclaw-postgres-test
```

The script refuses non-loopback hosts, non-simple/database names without `test`, a DSN
without an explicit user, ambiguous container names, mismatched published ports, and
unbounded timeouts. Its finalizer first heals the exact container, closes the old pool,
and retries exact marker cleanup without replacing the primary failure. Six
consecutive disposable PostgreSQL 17 drills failed the stopped-primary read in at most
1.003 seconds, reconnected in at most seven probes, completed in at most 3.752 seconds,
observed no more than two application connections, and removed one marker each.

This is destructive to the named container's availability and belongs only in isolated
QA. Passing it proves bounded single-primary stop/start and pool recovery; it does
**not** certify replica promotion/lag, a network partition or split-brain fence,
production resource budgets, or production RPO/RTO.

## Owned two-node fenced-promotion probe

The stronger topology gate creates a disposable PostgreSQL 17 primary and streaming
hot standby from `pg_basebackup --write-recovery-conf`. It requires explicit destructive
authority because its UUID-named data volumes are removed after the drill:

```bash
uv run python scripts/postgres_failover_probe.py \
  --allow-topology-create \
  --image postgres:17-alpine \
  --timeout 90 \
  --outage-timeout 1
```

Safety and truth conditions are executable:

- the normal path accepts only the exact `postgres:17-alpine` tag, retries its refresh
  within the overall timeout, resolves it to an immutable content digest, and records
  the running server version. An explicitly supplied
  `postgres@sha256:<64-lowercase-hex>` digest is instead inspected locally and must
  match exactly, enabling repeatable offline certification without trusting a mutable
  cached tag;
- the two database ports bind only to IPv4 loopback; replication authentication is
  limited to the private UUID-named Docker subnet and a random test-only password;
- the hot standby must report recovery/read-only mode and reject even a temporary-table
  write with SQLSTATE `25006`;
- replay is paused, a run plus its two execution leases and an evaluated learning
  candidate are committed, and positive WAL lag plus both marker absences must be
  observed before replay resumes;
- the standby must replay the last acknowledged WAL LSN, exact run/lease fence, and the
  learning candidate/evaluation/event history;
- the old primary is stopped and rechecked both before and after promotion; promotion
  is prohibited while it is running or while acknowledged WAL is behind;
- before promotion, both existing bounded pools must fail inside the configured
  interval because `target_session_attrs=read-write` rejects the remaining read-only
  standby;
- after `pg_promote`, the same runtime/learning pools and fresh pools must select the
  promoted writer, retain acknowledged state, commit run and learning transitions with
  contiguous events, and each stay within the two-connection limit; and
- cleanup attempts the standby, base-backup helper, old primary, both data volumes, and
  network in dependency order. It never restarts the old primary after promotion.

Five consecutive hardened local runs on 2026-08-12 passed. Deliberately paused lag was
7,984 bytes in each run; the no-writer interval failed closed in 1.000–1.003 seconds;
promotion took 0.117–0.124 seconds; total create/copy/lag/fence/promote/verify/cleanup
took 4.363–4.916 seconds; and one or two application connections were observed under
the declared cap. All six owned resources were removed after every run. These numbers
are local regression evidence, not portable SLOs.

The extended runtime-plus-learning gate passed again on 2026-08-18 against PostgreSQL
17.11. It measured 19,928 bytes of deliberate lag; runtime and learning no-writer
failures were typed/retryable in 1.005 and 1.004 seconds; promotion took 0.133 seconds;
both existing and fresh pools recovered; the learning candidate remained qualified and
then transitioned to quarantined at revision 2 with event sequences 1–3; one connection
per pool was observed; and all six resources were removed in 10.546 seconds. This is
still a local planned-cutover regression, not production failover certification.

This test uses asynchronous replication but waits for the exact acknowledged LSN before
a planned cutover, so no acknowledged application row is lost in the tested path. It
does **not** prove the RPO of an unplanned primary loss. PostgreSQL hot standbys are
read-only and eventually consistent; libpq's `target_session_attrs=read-write` selects
an acceptable writer, but neither feature fences a still-running old primary. Production
must supply a deployment-specific failover controller and reliable external fencing.
See PostgreSQL's official
[hot-standby contract](https://www.postgresql.org/docs/17/hot-standby.html),
[`pg_basebackup -R`](https://www.postgresql.org/docs/17/app-pgbasebackup.html),
[`pg_promote` and replay controls](https://www.postgresql.org/docs/17/functions-admin.html),
and [multi-host target selection](https://www.postgresql.org/docs/17/libpq-connect.html).

## Synchronous acknowledgement and abrupt-loss probe

The companion gate uses the same owned resources but changes the primary contract to
`synchronous_commit=remote_apply` and
`synchronous_standby_names=FIRST 1 (standby_<probe-id>)`:

```bash
uv run python scripts/postgres_synchronous_failover_probe.py \
  --allow-topology-create \
  --image postgres:17-alpine \
  --timeout 90 \
  --outage-timeout 1 \
  --blocked-observation 0.5
```

It verifies two separate durability facts. First, it disconnects the standby container
from only the UUID-named replication network while leaving its loopback client endpoint
running, then terminates only that exact named WAL-sender so host TCP timeout does not
control the fault window. The RuntimeStore connection itself must report
`remote_apply`, and a real `PostgresRuntimeStore.create_run()` must remain
unacknowledged for the full observation window. Reconnection must restore
`pg_stat_replication` to `streaming/sync`; only after remote replay may the application
commit return, and the new row must already be readable on the standby. This proves
false-ack prevention and also demonstrates the availability tradeoff: without the
required standby, writes wait.

Second, with replication healthy, the probe acknowledges a complete created→lease→
queued→release event/fence manifest, verifies the exact manifest on the standby, and
sends `SIGKILL` to the exact owned old-primary container. It reuses the same catch-up
and old-primary fence gates, requires the writer-selecting pool to fail closed before
promotion, promotes explicitly, then compares recovered state and complete serialized
events through the existing pool and a fresh pool while enforcing the two-connection
cap.

Five completed hardened PostgreSQL 17.10 drills passed on 2026-08-12 with the final
assertions. The partition withheld acknowledgement for 0.500–0.504 seconds; synchronous
rejoin took 0.231–0.294 seconds and acknowledgement returned only after rejoin at
0.735–0.794 seconds. Primary `SIGKILL` took 0.214–0.355 seconds; no-writer failure took
1.001–1.005 seconds; promotion took 0.116–0.122 seconds; one or two RuntimeStore
connections were observed under the cap; zero acknowledged runtime state/events were
observed lost; and all six resources were removed after every completed drill. The
topology-plus-drill window was 4.869–10.878 seconds. Registry refresh/provenance time is
reported separately and is not a failover SLO. A Docker Hub outage also proved the tag
path fails before topology creation when it cannot refresh provenance; an explicitly
verified immutable digest completed the same drill without weakening image identity.

PostgreSQL documents that synchronous commits wait for the configured streaming
standby, while per-transaction `local`/`off` can opt out. A deployment that requires
this RPO must therefore prevent policy bypass, budget the latency/availability tradeoff,
and monitor `state`, `sync_state`, and replay LSN. See the official
[replication settings](https://www.postgresql.org/docs/17/runtime-config-replication.html),
[high-availability comparison](https://www.postgresql.org/docs/17/high-availability.html),
and [`pg_stat_replication` fields](https://www.postgresql.org/docs/17/monitoring-stats.html).

Taken alone, this one-standby/single-fault drill does not prove simultaneous primary+
standby crash, storage/media failure, production automatic election/external fencing,
true network split brain, old-primary rewind/rejoin, repeated role rotation, or
production latency/availability SLOs. The next companion closes the local two-node
rewind/rotation regression only; the production gates remain release blocking.

## Old-primary rejoin and round-trip role rotation

The role-rotation companion exercises the recovery path that a one-way promotion omits:

```bash
uv run python scripts/postgres_role_rotation_probe.py \
  --allow-topology-create \
  --image postgres:17-alpine \
  --timeout 90 \
  --outage-timeout 1
```

The probe creates the same exact UUID-named PostgreSQL 17 pair with data checksums and
`full_page_writes=on`, then requires `remote_apply` on the actual RuntimeStore
connection. Before each cutover it forces `CHECKPOINT` on the writer and waits for the
exact flush LSN to replay on the standby. It then performs this complete sequence:

1. acknowledge created→queued runtime state and a lease fence;
2. send `SIGKILL` only to the exact owned primary, prove a bounded no-writer interval,
   and promote the synchronous standby;
3. run `pg_rewind` only against the stopped, exact owned old-primary volume, permit
   PostgreSQL's documented crash-recovery preparation for that abrupt target, rejoin it
   read-only, and make it the exact required synchronous standby;
4. acknowledge running state and a newer lease fence on the second writer;
5. cleanly stop that writer, prove another no-writer interval, promote the original
   node, and rewind the clean target with `--no-ensure-shutdown`;
6. rejoin the second former writer read-only and synchronous, acknowledge paused state
   and a third fence, then compare the exact state and serialized event manifest on
   both nodes through the surviving pool and a fresh pool.

The rewind helper deliberately does not use `pg_rewind -R`. That option can copy an
authenticated connection password into `postgresql.auto.conf`. Instead the disposable
gate supplies its random test-only password through a mode-0600 temporary `.pgpass`,
removes every copied historical `primary_conninfo`, writes exactly one password-free
connection string, and refuses the rejoin if a password remains in recovery
configuration. A failed `pg_rewind` leaves the target untrusted: never restart or retry
that data directory; remove it and provision a fresh base backup. Production secret
delivery must use the deployment's secret manager—the helper's short-lived container
environment is only part of this owned local topology.

Five completed hardened PostgreSQL 17.10 runs passed on 2026-08-12. The two no-writer
checks failed closed in 1.001–1.005 seconds; promotions took 0.115–0.125 seconds; the
abrupt-target rewind took 0.534–0.603 seconds; and the clean-target rewind took
0.201–0.240 seconds. Existing pools recovered within one to six observations, one or
two application connections stayed under the declared cap, all three exact writer
roles were observed, the final state was `paused` at revision/fence 3 with seven
contiguous events, and acknowledged state loss remained zero. Every run removed all
eight owned resources. The topology-plus-drill window was 8.982–21.527 seconds; it is
local regression evidence, not a portable RTO.

PostgreSQL documents `pg_rewind` as the fast path for bringing a diverged former
primary back as a standby. The target must be stopped, and rewind safety requires data
checksums or `wal_log_hints`, with `full_page_writes` enabled. PostgreSQL also warns
that a failed rewind target likely needs a new backup. See the official
[`pg_rewind` contract](https://www.postgresql.org/docs/17/app-pgrewind.html),
[failover guidance](https://www.postgresql.org/docs/17/warm-standby-failover.html), and
[WAL requirements](https://www.postgresql.org/docs/17/runtime-config-wal.html).

This closes old-primary rewind/rejoin and repeated role rotation only for the exact
owned two-node/single-fault regression. It does not certify automatic election,
external fencing, true partitions or split brain, witness/quorum behavior, simultaneous
faults, production endpoint discovery, large-data/archive-dependent rewind, PITR, or
production synchronous-latency, availability, RPO, and RTO policy.

## Dual-writer application-authority probe

The adversarial companion intentionally performs an unsafe promotion while the old
primary stays writable, producing two real PostgreSQL timelines:

```bash
uv run python scripts/postgres_split_brain_authority_probe.py \
  --allow-topology-create \
  --image postgres:17-alpine \
  --etcd-image quay.io/coreos/etcd@sha256:dfd3941bf6ced5fdb700f9b2d98b22b7bca7ceee13aec16224f93ff30d9a59c4 \
  --timeout 90 \
  --outage-timeout 1
```

It first proves the danger by committing a different RuntimeStore row through an
unguarded one-connection pool on each writer and asserting those rows exist only on
their respective divergent timelines. It then starts official etcd 3.6.14 from the
immutable digest above and uses the first-party adapter. The guard must reject the
stale writer, commit only on the named writer, deny both writers after the etcd process
is stopped, recover after the exact endpoint restarts, abort a real `pg_sleep(1)`
transaction via a 500 ms PostgreSQL `transaction_timeout`, and roll back a transaction
when a lease/revision transfer occurs immediately before commit.

The first hardened end-to-end PostgreSQL 17.10 + etcd 3.6.14 run on 2026-08-12 passes:
stale-writer denial takes 0.004 seconds; control-plane-loss denial takes 0.002–0.003
seconds; the server-enforced transaction abort takes 0.516 seconds; commit
revalidation returns `authority_changed` and rolls back the injected row; each guarded
pool holds one connection; and all seven owned resources are removed. Complete time is
6.151 seconds. The standalone live-etcd gate also proves revision advancement, explicit
revocation, natural unrenewed-lease expiry, process loss, and exact cleanup. These are
local regression measurements, not production SLOs.

The secure companion exercises the same adapter against three voting etcd members:

```bash
uv run python scripts/etcd_secure_quorum_probe.py \
  --allow-topology-create \
  --image quay.io/coreos/etcd@sha256:dfd3941bf6ced5fdb700f9b2d98b22b7bca7ceee13aec16224f93ff30d9a59c4 \
  --timeout 90 --lease-ttl 15
```

It generates unique member and peer certificates, never mounts the CA private key,
uses separate host-only reader/controller keys, requires client and peer certificate
verification with TLS 1.2 minimum, enables auth and least-privilege exact-key roles,
and validates exact 403/code-7 denials. It proves continued authority with one voter
stopped, fail closure after majority loss, reauthentication through another member,
and a newer fenced grant after quorum recovers. The first run on 2026-08-13 passes in
7.328 seconds; majority loss returns `etcd_timeout` in 1.003 seconds, fence 3 advances
to 4, and all four Docker resources plus temporary certificates are removed.

Together these prove agnoclaw's first-party etcd authority adapter under a real
two-writer fault and an owned local mTLS/RBAC/quorum regression. They do **not** certify
the external controller that owns/transfers the record, a durable multi-AZ quorum,
network-partition/latency or certificate/key-rotation chaos, automatic election,
arbitrary-client or physical fencing, host pause, multiple simultaneous faults,
backup/restore, or production RPO/RTO. PostgreSQL's official
[failover guidance](https://www.postgresql.org/docs/17/warm-standby-failover.html)
requires STONITH to prevent dual primaries. See also Patroni's
[watchdog contract](https://patroni.readthedocs.io/en/master/watchdog.html) and the
[Kubernetes Lease model](https://kubernetes.io/docs/concepts/architecture/leases/).

## Backup and restore

PostgreSQL remains the backup authority; agnoclaw does not wrap native backup semantics
in a weaker proprietary format. The repository now supplies an automated, deliberately
destructive local/CI restore rehearsal:

```bash
uv run python scripts/postgres_backup_restore_probe.py \
  --source-dsn postgresql://postgres:secret@127.0.0.1:5432/agnoclaw_test \
  --target-dsn postgresql://postgres:secret@127.0.0.1:5432/agnoclaw_restore_test \
  --container agnoclaw-postgres-test \
  --allow-target-reset
```

The probe refuses non-loopback DSNs, databases without `test`, a target without
`restore`, differing source/target endpoints, ambiguous container names, and omission
of the reset flag. It confirms that the named running container publishes the DSN
port, drops only the exact target, creates one source marker, and uses `pg_dump -Fc`
plus `pg_restore --exit-on-error`. Its content-minimized manifest covers every
`runtime_*` table row, column, index, constraint, and runtime sequence. After restore it
checks the queued snapshot, exact ordered/gap-free events, planned operation, and exact
start-idempotency replay; replay must not mutate the manifest.

Cleanup attempts the exact dump, target database, and source marker independently, so
a cleanup problem cannot replace the primary dump/restore failure. Successful cleanup
removes marker rows but intentionally does not rewind native source sequences. Five
consecutive local rehearsals after the service/load lane restored 18 runtime tables,
117 rows, and two sequences exactly; the worst observed dump+restore+verify window was
0.352 seconds. The manifest preserves relative logical column order while deliberately
ignoring physical ordinal gaps that native logical restore compacts. Those numbers
are diagnostic evidence from one disposable local PostgreSQL 17 container, not a
production RTO.

For an actual deployment, use native PostgreSQL/platform tooling and retain this order:

1. Take a consistent `pg_dump` or platform snapshot that includes every `runtime_*`
   table and schema-migration row.
2. Retain database encryption keys and externally referenced artifacts under the same
   recovery policy.
3. Restore into an isolated database.
4. Start `PostgresRuntimeStore` so forward migrations run under the advisory lock.
5. Verify schema version, event sequence monotonicity, terminal projections, pending
   outbox rows, and idempotency records before opening admission.
6. Rehearse reattachment and effect reconciliation with workers still disabled.
7. Only then enable workers and exporters.

Use `RuntimeOutboxWorker` for the generic export loop. PostgreSQL leases batches with
`FOR UPDATE SKIP LOCKED`; acknowledgement and `defer_outbox()` both require the exact
unexpired token. Export remains at least once: deduplicate downstream by `event_id` and
reconstruct run order with `sequence`. See [Durable event export](event-export.md).

Never restore the ledger without its artifact/key generation or run old and restored
writers against the same logical namespace. T14 owns old-writer fencing and verified
cutover tooling.

## Production gate still open

The following evidence is required before the `service` profile is stable:

- database-partition chaos under in-flight transactions (single-backend loss rollback/
  reconnect and whole-database restart now pass);
- deployment-specific automatic failover/external-fence behavior, network-partition
  split-brain injection, simultaneous/multiple failure, production synchronous-
  replication RPO/availability policy, endpoint discovery, production rejoin
  automation, multiple/large-data role rotations, and archive-dependent rewind (the
  owned planned asynchronous, one-standby/single-abrupt-loss remote-apply, and local
  two-rewind round-trip paths now pass; a real two-writer local fault also passes the
  application-authority seam with real single-node etcd, and an owned three-member
  mTLS/RBAC/member-loss gate passes, but controller election, durable multi-AZ quorum,
  network-partition/latency/certificate-rotation chaos, and physical fencing remain
  open);
- production-scale noisy-neighbor and slow-exporter isolation (the isolated 10,000-row
  noisy-read regression gate passes);
- migration on production-scale data with a rollback rehearsal;
- encrypted off-host retention, artifact/key-generation restore, PITR/replica
  promotion, and corruption response (the isolated exact-manifest restore rehearsal
  now passes locally and in CI);
- measured production p50/p95/p99 transaction/admission latency and bounded memory/
  queue depth (local/CI p50/p95/p99 and hard pool/admission bounds pass);
- cross-process weighted tenant fairness (process-local round-robin fairness passes);
- published RPO/RTO targets and successful timed recovery drills;
- binding every operation/capability safe point to the active run/session fence (the
  store-issued lease and reclaim primitive itself is implemented).

Passing a happy-path connection test is not this certification.
