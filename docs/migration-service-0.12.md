# Operate the PostgreSQL/service 0.12 migration

Status: scan/plan/preview and provenance-owned apply/verify/cutover/rollback implemented;
production certification is in progress

Last updated: 2026-08-14

This runbook defines the service-scale counterpart to the implemented
[local SQLite/JSON workflow](migration-apply-0.12.md). It moves supported Agno
PostgreSQL learning and schedule records into explicit 0.12 PostgreSQL targets without
putting a DSN, secret, row value, prompt, or learning content in a plan, log, event, or
CLI error.

Do not use the local migration command against PostgreSQL. Do not improvise this
workflow with `INSERT … SELECT`: Agno learning identity, institutional quarantine,
schedule execution semantics, target provenance, and rollback ownership all require
explicit transformations.

## Implemented development lifecycle

The Python API and non-interactive CLI now provide the complete migration lifecycle:

- `PostgresMigrationDatabaseRef` keeps only an uppercase credential environment-variable
  name and a validated schema identifier in control artifacts;
- `scan_postgres_migration_012()` groups identical resolved endpoints, opens one
  `REPEATABLE READ, READ ONLY` snapshot per endpoint group, enforces statement and lock
  timeouts, and uses identifier-safe PostgreSQL composition;
- table rows are streamed in bounded batches into deterministic SHA-256 evidence; row
  values, learning content, prompts, DSNs, and raw driver errors never enter the report;
- the scanner inventories all three Agno source tables and both target surfaces, checks
  the certified Agno learning/history shapes, personal ownership and post-rekey
  collisions, institutional map coverage, exact schedule-map identity coverage,
  schedule locks, and non-terminal schedule runs;
- `create_postgres_migration_012_plan_from_scan()` is the only public plan factory. It
  refuses blocker-bearing scans and binds the plan to the exact database references,
  scope decisions, schedule map, endpoints, schemas, table evidence, backup receipt,
  and writer-fence intent;
- plan files are immutable typed data, digest-tamper-evident, atomically replaced,
  directory-fsynced, mode 0600, and protected from symbolic-link destinations;
- schedule-map schema 1.1 digest-binds the interval/cron, prompt, authority, worker
  profile, enabled/isolation/learning-consent state, timezone, misfire grace, overlap,
  complete retry/backoff/jitter policy, and concurrency group; no executable behavior
  is inferred from a mutable endpoint payload;
- `preview_postgres_migration_012_transforms()` repeats the exact-plan scan, then opens a
  second repeatable-read/read-only source snapshot, recomputes each source-table digest,
  rekeys personal learning through `LearningScope`, quarantines institutional learning,
  compiles durable jobs, archives terminal history, and detects identity collisions with
  a mode-0600 disk-backed registry rather than an unbounded in-memory set;
- `apply_postgres_migration_012()` rebinds source and targets to the planned database
  identities, acquires non-blocking target-schema advisory locks, compiles the same
  repeatable-read source snapshot twice so all evidence is proven before writes, and
  commits bounded target batches with monotonic control revisions and unique provenance;
- every target identity is classified as `inserted` or `preexisting_identical`; another
  migration cannot claim the same role/table/identity, and resume re-verifies committed
  target values before proceeding;
- `verify_postgres_migration_012()` uses new connections to recompile source evidence,
  compare every transformed target value and provenance row, and recompute the baseline
  target tables after excluding migration-inserted identities, detecting unrelated
  target inserts, updates, and deletes;
- `cutover_postgres_migration_012()` repeats independent verification and records a
  digest-bound external deployment receipt. It never changes credentials, endpoints,
  routing, or starts writers;
- `rollback_postgres_migration_012()` requires stopped writers, preserves identical
  preexisting rows, deletes only exact inserted rows, refuses owned or unowned drift,
  reverses runtime before learning, and resumes through durable `rolling_back` state;
- `agnoclaw migrate 0.12 service check|plan|preview|apply|verify|cutover|rollback`
  exposes the lifecycle with bounded flags, schema-v1 JSON, semantic exit codes,
  stdout/stderr separation, no DSN-valued option, no prompt, exact confirmations,
  dry-run for each mutating command, and actionable next commands.

All 27 current service contracts pass against disposable PostgreSQL 17. The crash matrix
uses one database with separate source, learning-target, and runtime-target schemas and
one-row read/write batches. A second production-shaped matrix streams 5,000 learning rows
plus schedule/history records across three independent databases through three isolated
least-privilege roles, proves cross-database and source-write denial, and caps Python
preview memory below 64 MiB. Together they prove the read-only boundary before apply,
schema-lock contention refusal, deterministic
preview, repeat apply, source/endpoint/owned/unowned drift rejection, dry-run, independent
verify, receipt-only cutover, rollback-window enforcement, forced process death and
durable resume after target-control and row-batch commits during both apply and rollback,
failed-rollback resume, reverse-order rollback, content/DSN redaction, and zero
executable target rows after rollback. CI runs the live lane and the exact-wheel
lifecycle-help smoke with the `cli,postgres,scheduler` extras.

This is an **implemented development workflow**, not production certification. Do not
use it on production data until the complete checkpoint-kill, deployment-fence/
rogue-writer, credential-rotation/TLS, production-volume, native-backup, and measured
RPO/RTO gates below pass. Independent databases, baseline least privilege, and a bounded
5,000-row rehearsal now pass but are not substitutes for those full gates.

### Run the complete CLI lifecycle

Install both optional surfaces. Supply actual DSNs only through the named environment
variables or the deployment secret injector; command arguments contain reference names,
not credentials.

```bash
pip install "agnoclaw[cli,postgres,scheduler]"

agnoclaw migrate 0.12 service check \
  --source-credential-env AGNO_SOURCE_DSN \
  --source-schema agno \
  --target-learning-credential-env AGNO_TARGET_DSN \
  --target-learning-schema agno \
  --target-runtime-credential-env AGNO_TARGET_DSN \
  --target-runtime-schema agnoclaw_runtime \
  --schedule-map-file private/schedule-map.json \
  --scope-map-file private/scope-map.json \
  --json

agnoclaw migrate 0.12 service plan \
  --source-credential-env AGNO_SOURCE_DSN \
  --source-schema agno \
  --target-learning-credential-env AGNO_TARGET_DSN \
  --target-learning-schema agno \
  --target-runtime-credential-env AGNO_TARGET_DSN \
  --target-runtime-schema agnoclaw_runtime \
  --schedule-map-file private/schedule-map.json \
  --scope-map-file private/scope-map.json \
  --target-tenant-id tenant-a \
  --target-agent-id reviewer \
  --backup-receipt-id backup-object-v7 \
  --backup-receipt-digest "$BACKUP_RECEIPT_DIGEST" \
  --restore-test-id restore-drill-42 \
  --writer-fence-plan deployment-stop:v3 \
  --output migration-plan.json \
  --json

agnoclaw migrate 0.12 service preview \
  --plan migration-plan.json \
  --schedule-map-file private/schedule-map.json \
  --batch-size 1000 \
  --json

agnoclaw migrate 0.12 service apply \
  --plan migration-plan.json \
  --schedule-map-file private/schedule-map.json \
  --confirm-plan-digest "$PLAN_DIGEST" \
  --confirm-transform-digest "$TRANSFORM_DIGEST" \
  --confirm-backup-receipt-digest "$BACKUP_RECEIPT_DIGEST" \
  --confirm-writer-fence-plan deployment-stop:v3 \
  --writers-stopped --json

agnoclaw migrate 0.12 service verify \
  --plan migration-plan.json \
  --schedule-map-file private/schedule-map.json \
  --confirm-plan-digest "$PLAN_DIGEST" \
  --confirm-transform-digest "$TRANSFORM_DIGEST" \
  --confirm-writer-fence-plan deployment-stop:v3 \
  --writers-stopped --json

agnoclaw migrate 0.12 service cutover \
  --plan migration-plan.json \
  --schedule-map-file private/schedule-map.json \
  --confirm-plan-digest "$PLAN_DIGEST" \
  --confirm-transform-digest "$TRANSFORM_DIGEST" \
  --confirm-writer-fence-plan deployment-stop:v3 \
  --writers-stopped \
  --cutover-receipt-id deployment-change-42 \
  --cutover-receipt-digest "$CUTOVER_RECEIPT_DIGEST" --json

agnoclaw migrate 0.12 service rollback \
  --plan migration-plan.json \
  --schedule-map-file private/schedule-map.json \
  --confirm-plan-digest "$PLAN_DIGEST" \
  --confirm-transform-digest "$TRANSFORM_DIGEST" \
  --confirm-writer-fence-plan deployment-stop:v3 \
  --writers-stopped \
  --confirm-no-post-cutover-target-writes --json
```

`plan` performs a new scan rather than trusting an earlier terminal report. It refuses
an existing output unless `--overwrite` is explicit. Overwrite never prompts, and a
symbolic-link destination fails closed. A successful plan contains
`apply_available: false` until exact transformation preview succeeds.
`preview` accepts the exact plan plus private schedule map, refuses changed source or
target evidence, and returns a transform digest, target-identity-set digest,
per-category counts/digests, and source-table digests. It writes neither source nor
target databases and returns the exact apply command with `apply_available: true`.

All mutation commands require the plan and transform digests, exact writer-fence token,
and `--writers-stopped`; apply additionally requires the backup receipt digest. Each
supports `--dry-run`. Cutover records external authority but performs no deployment
change. Post-cutover rollback additionally requires
`--confirm-no-post-cutover-target-writes`; exact target drift still fails closed.

The automation contract is:

| Exit | Meaning | Output |
|---:|---|---|
| `0` | Requested read, dry-run, or lifecycle operation completed | Schema-v1 data/receipt on stdout |
| `2` | Invalid/missing CLI option | Click usage diagnostic on stderr |
| `3` | Semantic blocker, confirmation/state conflict, or existing output | Blocker report on stdout when scanning succeeded; otherwise structured error on stderr |
| `4` | Integrity, digest, drift, or verification failure | Structured non-transient error on stderr |
| `75` | Retryable PostgreSQL scan/transaction failure | Structured error with `transient: true` on stderr |
| `78` | Credential reference, driver, schedule-map, or scope-map configuration error | Structured non-transient error on stderr |

The JSON envelope includes `schema_version`, `command`, `status`, `ok`, `result`, and
next-action fields. Structured errors add a stable code, safe details, remediation,
`transient`, and the semantic exit code. Raw driver text and resolved credentials are
never emitted. Evidence or endpoint drift, missing provenance, unowned target writes,
and unsafe rollback exit `4`; retryable scan/transaction/lock failures exit `75`.

### Scan and create a reviewable plan

```python
from agnoclaw import (
    LegacyLearningScopeMapping,
    LegacyScopeAction,
    PostgresMigrationBackupReceipt,
    PostgresMigrationDatabaseRef,
    create_postgres_migration_012_plan_from_scan,
    load_postgres_schedule_map,
    preview_postgres_migration_012_transforms,
    scan_postgres_migration_012,
    write_postgres_migration_012_plan,
)

source = PostgresMigrationDatabaseRef("source", "AGNO_SOURCE_DSN", "agno")
target_learning = PostgresMigrationDatabaseRef(
    "target_learning", "AGNO_TARGET_DSN", "agno"
)
target_runtime = PostgresMigrationDatabaseRef(
    "target_runtime", "AGNO_TARGET_DSN", "agnoclaw_runtime"
)
scope_mappings = (
    LegacyLearningScopeMapping(
        source_namespace="global",
        learning_type="learned_knowledge",
        action=LegacyScopeAction.QUARANTINE,
    ),
)
schedule_map = load_postgres_schedule_map("private/schedule-map.json")
scan = scan_postgres_migration_012(
    source=source,
    target_learning=target_learning,
    target_runtime=target_runtime,
    schedule_map=schedule_map,
    scope_mappings=scope_mappings,
)
if not scan.ready:
    raise RuntimeError([finding.code for finding in scan.findings])

plan = create_postgres_migration_012_plan_from_scan(
    scan=scan,
    source=source,
    target_learning=target_learning,
    target_runtime=target_runtime,
    target_tenant_id="tenant-a",
    target_agent_id="reviewer",
    schedule_map=schedule_map,
    scope_mappings=scope_mappings,
    backup_receipt=PostgresMigrationBackupReceipt(
        receipt_id="backup-object-v7",
        receipt_digest="sha256:" + "5" * 64,
        restore_test_id="restore-drill-42",
    ),
    writer_fence_plan="deployment-stop:v3",
)
write_postgres_migration_012_plan("migration-plan.json", plan)
preview = preview_postgres_migration_012_transforms(
    plan=plan,
    schedule_map=schedule_map,
)
print(preview.to_dict())
```

Keep the schedule map outside ordinary logs and artifacts. A ready scan means only that
the read-only preconditions are satisfied; it is not an apply or cutover receipt.

### Apply and verify from Python

Use the exact reviewed objects and confirmations. These calls do not validate an
external backup service or stop deployment writers for you; they require and persist
the reviewed evidence.

```python
from agnoclaw import (
    apply_postgres_migration_012,
    cutover_postgres_migration_012,
    rollback_postgres_migration_012,
    verify_postgres_migration_012,
)

confirmations = {
    "confirm_plan_digest": plan.plan_digest,
    "confirm_transform_digest": preview.transform_digest,
    "confirm_writer_fence_plan": plan.writer_fence_plan,
    "writers_stopped": True,
}
applied = apply_postgres_migration_012(
    plan=plan,
    schedule_map=schedule_map,
    confirm_backup_receipt_digest=plan.backup_receipt.receipt_digest,
    **confirmations,
)
verified = verify_postgres_migration_012(
    plan=plan,
    schedule_map=schedule_map,
    **confirmations,
)
cutover = cutover_postgres_migration_012(
    plan=plan,
    schedule_map=schedule_map,
    cutover_receipt_id="deployment-change-42",
    cutover_receipt_digest="sha256:" + "8" * 64,
    **confirmations,
)

# Emergency restore-style rollback is allowed only before any post-cutover target write.
rolled_back = rollback_postgres_migration_012(
    plan=plan,
    schedule_map=schedule_map,
    confirm_no_post_cutover_target_writes=True,
    **confirmations,
)
```

## Non-negotiable guarantees

1. **Credentials are references.** A plan stores environment-variable or broker
   reference names, never resolved DSNs. Resolution happens only inside the command
   process. Errors expose the reference name and stable code, not driver text.
2. **Planning and preview are read-only.** Source and target evidence is collected in bounded
   `REPEATABLE READ, READ ONLY` transactions with statement and lock timeouts. The
   report contains schema/count/logical digests and server identity digests, never row
   values. Preview repeats the exact-plan scan, then recomputes transformation evidence
   from a fresh read-only snapshot before returning any digest.
3. **Backup is independent.** Apply requires a reviewed native-backup receipt bound to
   an immutable object/version and SHA-256 digest. Creating a provenance table is not a
   database backup.
4. **All writers are stopped.** The operator assertion covers old Agno, current
   agnoclaw, direct SQL clients, schedulers, workers, and maintenance jobs for every
   source and target. A migration fence and advisory lock add defense in depth but
   cannot stop an old arbitrary client.
5. **No inferred schedule behavior.** Each source Agno schedule has an explicit,
   digest-bound mapping to interval/cron, prompt, tenant/user/session/agent authority,
   immutable worker configuration, enabled/isolation/learning-consent state, timezone,
   misfire/grace, overlap, complete retry/backoff/jitter, and concurrency policy. An
   endpoint or payload shape is evidence—not permission to guess execution behavior.
6. **Personal learning is re-keyed.** User Profile, User Memory, and Session Context
   identities are derived from reviewed target authority through `LearningScope`.
   Missing owners and post-rekey collisions block before target writes.
7. **Institutional learning is quarantined.** Learned Knowledge, Entity Memory,
   Decision Log, legacy memory shapes, and unknown types enter an inactive,
   owner-scoped quarantine with source provenance. A map decision records intended
   scope; it never promotes the row into model-visible institutional memory.
8. **Apply is idempotent and crash-resumable.** Every target row is staged with source
   digest, transformed digest, migration ID, and an inserted-versus-preexisting flag.
   A conflicting identity or changed target fails closed.
9. **Verification is independent.** Verify re-reads source and target through fresh
   connections and recomputes exact transformed identities, counts, logical digests,
   scheduler behavior digests, provenance, and fence state.
10. **Rollback owns only its writes.** It deletes rows proven to have been inserted by
    this migration, preserves logical-identical preexisting rows, restores reviewed
    target preimages, and refuses target drift. There is no force flag.
11. **Cutover does not edit deployment configuration.** It records the verified
    decision and receipt. The deployment controller changes credentials/endpoints and
    starts writers in a separately reviewed rollout.
12. **The rollback window is explicit.** Restore-style rollback ends at the first
    legitimate post-cutover target write unless a separately certified reverse
    migration captures it.

## Database roles

The workflow names four logical roles. They may resolve to fewer physical databases,
but schema and advisory-lock identity remain explicit:

| Role | Default source/target tables | Purpose |
|---|---|---|
| source learning | `ai.agno_learnings` | Agno personal and institutional learning |
| source schedules | `ai.agno_schedules`, `ai.agno_schedule_runs` | Definitions and historical executions |
| target learning | `ai.agno_learnings` plus agnoclaw quarantine, provenance, and control tables | Re-keyed personal records and inactive institutional evidence |
| target runtime | `runtime_scheduler_jobs`, `runtime_scheduler_runs`, inactive history, provenance, and control tables | Schema-v12 executable schedules and non-executable legacy history |

Table/schema names are identifiers validated against a strict grammar. They are never
interpolated as raw SQL. Non-default Agno schemas must be passed explicitly; the
process search path is not migration authority.

## Control artifacts

### Scope map

Use the same bounded `scope_mappings` structure as the local migration. Every
institutional `(namespace, learning_type)` pair needs an exact or reviewed wildcard
map/quarantine decision. Personal records do not accept a namespace map as a substitute
for their required user/session owner.

### Schedule map

The schedule map is sensitive operational configuration and should be mode 0600. Its
digest, item count, and source-ID-set digest enter the plan; prompts do not.

```json
{
  "schema_version": "1.1",
  "schedules": [
    {
      "source_schedule_id": "daily-review",
      "schedule": "0 9 * * *",
      "prompt": "Review the daily incident queue",
      "tenant_id": "tenant-a",
      "user_id": "scheduler",
      "session_id": "schedule-daily-review",
      "agent_id": "reviewer",
      "worker_profile": "service-reviewer-v3",
      "enabled": true,
      "isolated": true,
      "learning_consent": false,
      "timezone": "Asia/Dubai",
      "misfire_policy": "skip",
      "misfire_grace_seconds": 300,
      "overlap_policy": "skip",
      "max_retries": 3,
      "retry_delay_seconds": 30,
      "retry_backoff_multiplier": 2.0,
      "retry_max_delay_seconds": 3600,
      "retry_jitter_seconds": 0,
      "jitter_seconds": 0,
      "concurrency_key": "tenant-a:daily-review"
    }
  ]
}
```

Schema 1.1 is intentionally strict: older maps that omit executable behavior must be
reviewed and regenerated rather than silently defaulted. The source ID set must match
exactly: no missing executable schedule and no orphan map entry. Completed source runs
are archived as non-executable history. A locked/in-flight source run blocks planning
until it settles or receives an explicit incident decision. Every rule must match the
plan's trusted target tenant and agent; use separate reviewed plans when those
authorities differ. Cron evaluation requires the `scheduler` extra; positive interval
schedules remain core-only.

### Backup receipt

The receipt is produced by the deployment backup system after a restore rehearsal. At
minimum it binds:

- source database identity and immutable snapshot/object version;
- target database preimage snapshot/object version when targets are not empty;
- creation timestamp and retention/expiry;
- encrypted location identifier;
- SHA-256 or provider-native immutable checksum;
- restore command/runbook version and the successful rehearsal evidence ID.

The plan stores only the receipt ID and digest. Apply verifies the exact reviewed
receipt again; it does not claim to validate an opaque provider backup merely because a
string was supplied.

## Transaction and crash model

The source snapshot is never held open across an operator review. Planning records its
logical evidence; apply opens a new repeatable-read/read-only snapshot, proves its full
transform once without writes, then replays the same snapshot into bounded target
batches. Target work uses short transactions, a non-blocking schema-wide advisory lock,
unique provenance, and a migration-control table with monotonic phase and revision. The
advisory lock serializes this migration tool; arbitrary clients do not honor it, so the
confirmed deployment writer fence remains necessary.

For each target:

1. rebind the DSN to the planned database identity, acquire the schema advisory lock,
   and verify the operator supplied the reviewed writer-fence token;
2. create or verify control and provenance schema;
3. prove the complete transformation and identity set before target writes;
4. classify each target identity as `inserted` or `preexisting_identical`;
5. insert only absent rows and atomically advance that target's checkpoint;
6. commit, then continue with the next target;
7. on restart, verify completed checkpoints before resuming.

Cross-database atomicity is deliberately not claimed. The durable control record and
per-target checkpoints make partial progress observable and reversible without a
distributed transaction coordinator.

## Verification and rollback

Verify must use new connections after apply. It checks:

- exact endpoint/schema/table identity against the reviewed plan;
- unchanged source logical evidence;
- complete target identity and transformed-value equality;
- source-to-target count reconciliation by personal/quarantine/schedule/history class;
- source-ID-set and schedule behavior digests;
- provenance uniqueness and inserted/preexisting classification;
- confirmed fence evidence and absence of owned or unowned post-plan writes.

Rollback first repeats those checks with writers stopped. It reverses targets in the
opposite order, checkpointing every step. Rows marked `inserted` are deleted only when
their current digest still matches; identical preexisting rows are untouched. Any
preimage is restored only after its current value matches the apply record. The
deployment controller—not this command—owns releasing or changing external writer
fences after all targets and control records verify.

## Required release evidence

This workflow is not production-certified until all of the following pass:

- unit/property tests for plan redaction, identifier validation, transformation,
  collisions, idempotency, drift, and resume;
- disposable PostgreSQL 17 source/target end-to-end apply/verify/cutover/rollback;
- process kill at every committed checkpoint and both sides of target-row insertion
  (representative target-control and row-batch deaths now pass for apply and rollback);
- two independent target databases and same-database/different-schema layouts
  (both layouts now pass);
- roles with least privilege, rotated credentials, TLS, and non-default schemas
  (three isolated least-privilege roles now pass; rotation/TLS remain);
- native dump/restore and large-data bounded-memory rehearsal (5,000 rows under a
  64 MiB Python preview cap now pass; native restore and production volume remain);
- concurrent rogue-writer and advisory-lock/fence attacks;
- schedule timezone/DST/misfire/retry/overlap and in-flight-run cases;
- institutional quarantine and personal deletion/retention verification;
- installed-wheel CLI JSON, exit-code, no-secret, and clean-room operator drills;
- production-like backup retention plus measured RPO/RTO and rollback-window decision.

Until those gates pass, the service workflow remains an implementation contract and
must not be represented as production-ready.
