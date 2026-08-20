# 0.12 migration preflight

The 0.12 preflight inventories legacy Agno SQLite learning data and agnoclaw JSON
schedules before any persisted-data cutover. It is deterministic, bounded, content-safe,
and strictly read-only.

The `check` command itself does **not** back up, lock, copy, rewrite, delete, or verify a
target, and every report intentionally retains `apply_allowed: false`. A separate local
SQLite/JSON `plan`/`apply`/`verify`/`cutover`/`rollback` workflow is now implemented;
operators must pass a clear preflight into that separately confirmed state machine. See
the [local migration runbook](migration-apply-0.12.md).

## Smallest check

```bash
agnoclaw migrate 0.12 check \
  --learning-db ~/.agnoclaw/sessions.db \
  --schedules ~/.agnoclaw/schedules.json
```

Use canonical JSON for automation:

```bash
agnoclaw migrate 0.12 check \
  --learning-db ~/.agnoclaw/sessions.db \
  --schedules ~/.agnoclaw/schedules.json \
  --json > migration-preflight.json
```

Exit codes are:

| Code | Meaning |
|---:|---|
| `0` | The selected sources have no preflight blockers; the check itself remains read-only. |
| `1` | A bounded mapping/configuration input failed before a report could be built. |
| `2` | Click rejected command syntax/options. |
| `3` | The report is valid and contains one or more migration blockers. |

Do not publish a report without review: it contains resolved local source paths, counts,
scope names, and file/logical digests. It never includes learning content, schedule
prompts, outputs, or raw parser/database exceptions.

## Resolve institutional learning scope

Agno 2.x institutional rows do not carry agnoclaw's required tenant authority. Every
institutional `(namespace, learning_type)` must have an explicit map or quarantine
decision. Put decisions in a bounded JSON file:

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
      "target_tenant_id": "acme",
      "target_namespace": "code-review"
    }
  ]
}
```

Then rerun:

```bash
agnoclaw migrate 0.12 check \
  --learning-db ~/.agnoclaw/sessions.db \
  --scope-map-file migration-scope-map.json \
  --json
```

Omit `learning_type` to make a namespace-wide decision. Exact
`namespace + learning_type` entries take precedence over the namespace-wide entry.
Duplicate source keys fail before inspection.

Personal `user_profile`/`user_memory` rows require a user owner. `session_context` rows
require a session owner. Legacy memory-table rows require at least a user, agent, or
team owner. Missing owners are blockers; the preflight never invents one.

## Resolve schedule semantics

The legacy JSON scheduler did not require timezone, misfire behavior, or a durable
old-writer fence. Supply explicit plan inputs when the jobs do not carry them:

```bash
agnoclaw migrate 0.12 check \
  --schedules ~/.agnoclaw/schedules.json \
  --timezone Asia/Dubai \
  --misfire-policy skip \
  --old-writer-fence-plan stop-launchd-and-record-fence:v1 \
  --json
```

Supported preflight misfire decisions are:

- `skip`: do not replay occurrences missed before cutover;
- `run_once`: create at most one post-cutover catch-up occurrence.

This input names the reviewed fence plan; it does not stop a process. The separate apply
command requires `--writers-stopped` and writes source sidecar fences immediately before
backup/import. Older and custom writers may not honor them, so process shutdown remains
an operator responsibility.

Duplicate job names, invalid schedules, run history referencing absent jobs, and
`running` legacy attempts block planning. They require rename/merge/quarantine or
settlement decisions; the checker never selects one automatically.

## Python API

```python
from agnoclaw import (
    LegacyLearningScopeMapping,
    LegacyScopeAction,
    inspect_migration_012,
)

report = inspect_migration_012(
    learning_sqlite_path="~/.agnoclaw/sessions.db",
    schedule_json_path="~/.agnoclaw/schedules.json",
    scope_mappings=(
        LegacyLearningScopeMapping(
            source_namespace="global",
            learning_type="learned_knowledge",
            action=LegacyScopeAction.QUARANTINE,
        ),
    ),
    schedule_default_timezone="Asia/Dubai",
    schedule_default_misfire_policy="skip",
    old_writer_fence_plan="stop-old-service:v1",
)

print(report.preflight_clear, report.blocker_count, report.report_digest)
```

`MigrationPreflightReport.to_dict()` is schema `1.0` and contains:

- exact source-file size/checksum evidence, including SQLite WAL/SHM companions;
- per-table schema, row-count, logical-row, learning-type, and namespace evidence;
- ownership-gap and logical-identity collision counts;
- normalized schedule counts/digest and semantic issue counts;
- the supplied scope-map decisions;
- stable finding codes, safe messages, resolutions, and counts;
- deterministic planned action classes and the report digest.

The table logical digest is a source fingerprint over canonical row values. The apply
workflow separately records transformation-aware import digests and independently
verifies exact target identities.

## Supported sources and bounds

The certified local source shapes are the Agno 2.6.4/2.9.0 SQLite
`agno_learnings`/`agno_memories` schemas (plus the historical
`agnoclaw_memories` table name) and `JsonSchedulerBackend`'s jobs/runs JSON shape.
Custom safe SQLite table names can be added with repeated `--learning-table` options or
`learning_table_names=`.

Default bounds are:

- 100,000 learning rows per table;
- 512 MiB across the SQLite database and live companions;
- 16 MiB for schedule JSON;
- 1 MiB for a CLI scope-map file.

Callers can raise the source/row bounds explicitly within hard limits. Large service
stores still require a reviewed service-scale scanner; raising a bound is not itself a
performance or memory certification.

## Concurrency and failure behavior

SQLite is opened with `mode=ro`, `query_only`, and a read transaction. Source file
signatures are checked before and after the scan. A source change during inspection is
a blocker. Live WAL/SHM sidecars are fingerprinted and block planning until the old
writer is frozen, checkpointed, and backed up.

Unreadable, corrupt, malformed, oversized, or unsupported sources produce stable safe
findings. Raw filesystem, SQLite, JSON, and mapping parser messages are not copied into
the report. Missing selected sources are warnings so operators can correct an optional
path without a crash.

## Current boundary

The preflight and mutation workflow currently cover local SQLite learning and JSON
schedules. PostgreSQL RuntimeStore native dump/restore has an independent loopback
exact-manifest rehearsal; that does not certify a data migration. PostgreSQL legacy
learning, Agno schedule tables, artifacts/keys, online dual-write/reverse replication,
service-scale mutation throughput, tombstone propagation, and production RPO/RTO remain T14b
gates. A clear preflight means “the source decisions are unambiguous enough to plan,”
not “writers are stopped” or “production cutover is certified.”

Focused preflight verification lives in `tests/test_migration.py`; mutation and rollback
contracts live in `tests/test_migration_apply.py`.
