# Operate the local 0.12 data migration

Status: implemented development-preview workflow for local SQLite learning and JSON
schedules

Last updated: 2026-08-13

This runbook moves supported legacy Agno SQLite learning rows and agnoclaw JSON
schedules into explicit 0.12 SQLite targets. The workflow is non-interactive,
digest-bound, crash-resumable, and reversible while the targets remain unchanged.

Do not use this local workflow for PostgreSQL, a live multi-process service, Agno
schedule tables, artifacts/keys, or a production cutover whose rollback must preserve
new post-cutover writes. Those remain separate release gates.

## What the workflow guarantees

- `plan` repeats the bounded read-only preflight and writes a mode-0600, content-free
  plan bound to source checksums, target paths, authority, mapping decisions, and a
  rollback boundary.
- `apply` requires the exact plan digest and an explicit assertion that every legacy
  and target writer is stopped. It writes source fence markers, takes SQLite-native and
  atomic JSON backups before target mutation, and records a digest-bound manifest.
- Repeating `apply` with the same plan/state directory resumes or returns the same
  migration. Existing target identities must be identical; conflicts fail closed.
- `verify` independently regenerates the transformations and compares exact identities,
  normalized values, behavior digests, counts, and logical digests.
- `cutover` requires verification and records an explicit marker. It does not edit
  application configuration or start/stop a process.
- `rollback` requires the exact migration ID and a fresh writers-stopped assertion. It
  verifies source, backup, target, WAL, and SHM evidence before restoration, records a
  resumable `rolling_back` phase, restores target preimages, and releases legacy
  fences. It never overwrites newer target data.

The source files are never rewritten. Apply keeps the old source fences in place after
cutover; only a successful rollback releases them. There is no schema-contraction or
finalize command in this checkpoint.

## Before the maintenance window

1. Upgrade a staging environment first and confirm the installed command exposes
   `check`, `plan`, `apply`, `verify`, `cutover`, and `rollback`:

   ```bash
   agnoclaw migrate 0.12 --help
   ```

2. Put the state directory on storage with enough capacity for complete source and
   target preimages. The plan is content-free but contains local paths and authority
   identifiers. The state directory contains raw learning, schedule prompts/history,
   and target backups; protect and encrypt it as production data.
3. Inventory every process that can write the legacy learning database, legacy JSON
   schedule, target learning database, or target runtime database. Include old package
   versions and direct Agno/SQLite clients; a sidecar marker cannot stop arbitrary code.
4. Resolve every institutional learning namespace with `map` or `quarantine`, and every
   schedule with explicit timezone/misfire semantics. Per-job model/provider overrides
   must be partitioned into immutable worker configurations before migration.
5. Choose distinct source, target, plan, and state paths. Do not place `state-dir` at a
   source or target file path.

## 1. Run the read-only check

Create a bounded mapping file such as `migration-scope-map.json`:

```json
{
  "scope_mappings": [
    {
      "source_namespace": "global",
      "learning_type": "learned_knowledge",
      "action": "quarantine",
      "target_tenant_id": null,
      "target_namespace": null
    },
    {
      "source_namespace": "code-review",
      "learning_type": "decision_log",
      "action": "map",
      "target_tenant_id": "tenant-a",
      "target_namespace": "code-review"
    }
  ]
}
```

Then run:

```bash
agnoclaw migrate 0.12 check \
  --learning-db ~/.agnoclaw/sessions.db \
  --schedules ~/.agnoclaw/schedules.json \
  --scope-map-file migration-scope-map.json \
  --timezone Asia/Dubai \
  --misfire-policy skip \
  --old-writer-fence-plan stop-all-legacy-writers:v1 \
  --json > migration-preflight.json
```

Exit `0` means the selected source decisions are clear enough to plan. It is not
permission to apply while writers are running. Exit `3` means semantic blockers remain.

## 2. Create and review the plan

Planning is read-only with respect to sources and targets:

```bash
agnoclaw migrate 0.12 plan \
  --learning-db ~/.agnoclaw/sessions.db \
  --schedules ~/.agnoclaw/schedules.json \
  --target-learning-db ~/.agnoclaw/0.12-learning.db \
  --target-runtime-db ~/.agnoclaw/0.12-runtime.db \
  --target-tenant-id tenant-a \
  --target-agent-id primary-agent \
  --scope-map-file migration-scope-map.json \
  --timezone Asia/Dubai \
  --misfire-policy skip \
  --old-writer-fence-plan stop-all-legacy-writers:v1 \
  --output migration-plan.json \
  --json > migration-plan-result.json
```

Review these fields in `migration-plan.json`:

- `migration_id`, `plan_digest`, and `preflight_digest`;
- every source and target path;
- target tenant/org/agent authority;
- source checksums/sizes and learning table selection;
- scope mappings, timezone, misfire policy, and old-writer plan;
- rollback boundary `before-explicit-schema-contraction-v1`.

Planning twice at different times produces a different `planned_at` and therefore a
different plan digest. Apply only the reviewed file and its exact printed digest. Any
plan edit makes the digest invalid.

## 3. Stop writers and apply

Stop and verify every source and target writer first. For SQLite, checkpoint and close
all connections; the preflight refuses live WAL/SHM sidecars. Then run the exact digest
from the reviewed plan:

```bash
agnoclaw migrate 0.12 apply \
  --plan migration-plan.json \
  --state-dir /secure/agnoclaw-migration-012 \
  --confirm-plan 'sha256:REVIEWED_PLAN_DIGEST' \
  --writers-stopped \
  --json > migration-apply-result.json
```

`--writers-stopped` is an assertion, not an automation primitive. Apply creates sibling
`*.agnoclaw-fence.json` markers for each source. Current agnoclaw JSON scheduler and
default SQLite learning/session construction fail closed on these markers, but older or
custom writers may ignore them and must remain stopped.

The state directory is mode 0700 and contains mode-0600 control/backup files. Preserve
the whole directory. Its `manifest.json` is the authoritative phase and evidence ledger.

If the process exits after the manifest reaches `backed_up`, rerun the identical apply
command. Do not create a new plan or reuse the state directory for another plan.

## 4. Verify before cutover

```bash
agnoclaw migrate 0.12 verify \
  --state-dir /secure/agnoclaw-migration-012 \
  --json > migration-verify-result.json
```

Verification must reach `phase: verified`. It compares transformed personal learning,
institutional quarantine records, scheduler behavior, and archived run-history rows.
Running verify again is safe. Verifying after a recorded cutover retains `phase:
cutover` rather than moving the state backward.

## 5. Understand the data transformation

Personal Agno `user_profile`, `user_memory`, and `session_context` rows are re-keyed
through agnoclaw's trusted target tenant/org/agent scope and imported into
`agno_learnings`. Missing user/session owners block planning. Conflicting target
identities fail instead of choosing a winner.

Institutional rows and historical `agno_memories`/`agnoclaw_memories` shapes are copied
into `agnoclaw_migration_012_learning_quarantine`. They are not activated for model
recall. A `map` decision records the reviewed target tenant/namespace for later governed
promotion; it does not bypass candidate evaluation or silently write shared memory.

Scheduler jobs move to the schema-v12 RuntimeStore. A `skip` decision schedules the
next occurrence after planning. A `run_once` decision preserves one overdue legacy
`next_run_at`, when present and timezone-aware, as a durable `fire_once` misfire; it
never manufactures multiple backfill occurrences. Legacy completed run records are
archived in `agnoclaw_migration_012_schedule_history`; they do not become executable
runtime attempts.

## 6. Record cutover

Use the exact `migration_id` from the verified result:

```bash
agnoclaw migrate 0.12 cutover \
  --state-dir /secure/agnoclaw-migration-012 \
  --confirm-migration 'mig012_REVIEWED_ID' \
  --json > migration-cutover-result.json
```

The command writes `cutover.json`; it does not edit configuration. Point the new
application at the target databases and start its writers only after the marker is
recorded and your deployment checks pass.

The certified rollback window ends when a target receives a legitimate new write.
That is intentional: restoring a preimage after new writes would lose data. Keep new
writers stopped until the go/no-go decision if immediate restore-style rollback is a
requirement.

## 7. Roll back safely

Stop every target writer again, then run:

```bash
agnoclaw migrate 0.12 rollback \
  --state-dir /secure/agnoclaw-migration-012 \
  --confirm-migration 'mig012_REVIEWED_ID' \
  --writers-stopped \
  --json > migration-rollback-result.json
```

Rollback refuses missing/corrupt backups, changed sources, changed target/WAL/SHM
evidence, or a mismatched migration ID. There is no force flag. If interrupted in
`rolling_back`, keep writers stopped and repeat the exact command; already restored
preimages are recognized and the remaining targets are restored. Success reaches
`rolled_back`, removes the cutover marker and source fences, and makes that state
directory ineligible for another apply.

## State and exit-code reference

| Phase | Meaning | Safe next action |
|---|---|---|
| `backed_up` | Fences and verified preimages exist; import may be incomplete | Repeat exact `apply` |
| `applied` | Import completed and target evidence was recorded | `verify` |
| `verified` | Independent target verification passed | `cutover` or `rollback` |
| `cutover` | Explicit cutover marker recorded; rollback retained while targets do not change | Start new writers or stop them and `rollback` |
| `rolling_back` | Restore began and may have been interrupted | Keep writers stopped; repeat exact `rollback` |
| `rolled_back` | Preimages restored and legacy fences released | Archive evidence; do not reuse state directory |

| Exit | Meaning |
|---:|---|
| `0` | Command completed or safely returned its idempotent existing state |
| `2` | CLI syntax/options were rejected |
| `3` | Preconditions, explicit confirmation, mapping, conflict, or state transition blocked the command |
| `4` | Plan/manifest/backup/source/target integrity or independent verification failed |

With `--json`, successful data is written to stdout and failures to stderr. The stable
wrapper is schema `1.0`: `command`, `ok`, `result`/`error`, and `next_command`.

## Current certification boundary

Focused contracts cover content-free/tamper-evident planning, source drift, binary
legacy values, exact CLI error/output behavior, direct/quarantined learning, durable
schedule import, overdue run-once semantics, post-rekey identity collisions, source
writer fences, target conflicts, WAL-aware target drift, interrupted apply resume,
post-cutover verification, apply/verify/cutover/rollback, and interrupted multi-target
rollback resume. The focused migration implementation lane is 10/10 and the combined
migration/preflight/documentation lane is 28/28 in the current primary environment;
the full repository gate is 1,788 passed and 47 service/credential-dependent skips.

This does not certify PostgreSQL/service migration, arbitrary legacy writers, target
dual-write, online zero-downtime migration, post-cutover reverse replication, artifacts
or encryption-key movement, huge-store throughput, or production RPO/RTO. Keep those
claims behind their own evidence gates.
