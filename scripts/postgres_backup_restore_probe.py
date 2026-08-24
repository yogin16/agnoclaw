#!/usr/bin/env python3
"""Dump and restore an isolated PostgreSQL RuntimeStore with exact integrity proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass
from urllib.parse import ParseResult, urlparse
from uuid import uuid4

from agnoclaw.runtime import PostgresRuntimeStore
from agnoclaw.runtime.lifecycle import LifecycleTransition, RunSnapshot, TransitionKind
from agnoclaw.runtime.operations import EffectClass, OperationIntent, OperationKind
from agnoclaw.runtime.store import RunOwner

_DATABASE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TABLE_RE = re.compile(r"^runtime_[a-z0-9_]+$")


@dataclass(frozen=True)
class DatabaseManifest:
    digest: str
    table_counts: dict[str, int]
    sequence_count: int
    section_digests: dict[str, str]

    @property
    def row_count(self) -> int:
        return sum(self.table_counts.values())


@dataclass(frozen=True)
class ProbeTarget:
    source: ParseResult
    target: ParseResult
    source_database: str
    target_database: str
    database_user: str
    port: int


@dataclass(frozen=True)
class CleanupResult:
    dump_removed: bool
    target_removed: bool
    source_rows_cleaned: int
    failures: tuple[str, ...]


def _database(parsed: ParseResult) -> str:
    return parsed.path.removeprefix("/")


def _validate_target(
    source_dsn: str,
    target_dsn: str,
    container: str,
    *,
    allow_target_reset: bool,
) -> ProbeTarget:
    source = urlparse(source_dsn)
    target = urlparse(target_dsn)
    for label, parsed in (("source", source), ("target", target)):
        database = _database(parsed)
        if parsed.scheme not in {"postgres", "postgresql"}:
            raise ValueError(f"--{label}-dsn must use postgres:// or postgresql://")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(f"probe refuses a non-loopback {label} database")
        if "test" not in database.lower():
            raise ValueError(f"{label} database name must contain 'test'")
        if not _DATABASE_RE.fullmatch(database):
            raise ValueError(f"{label} database requires a simple identifier")
        if not parsed.username:
            raise ValueError(f"--{label}-dsn requires an explicit database user")
    source_database = _database(source)
    target_database = _database(target)
    if source_database == target_database:
        raise ValueError("source and target databases must differ")
    if "restore" not in target_database.lower():
        raise ValueError("target database name must contain 'restore'")
    if (source.hostname, source.port or 5432, source.username) != (
        target.hostname,
        target.port or 5432,
        target.username,
    ):
        raise ValueError("source and target must use the same host, port, and database user")
    if not allow_target_reset:
        raise ValueError("--allow-target-reset is required because the target is dropped")
    if not _CONTAINER_RE.fullmatch(container):
        raise ValueError("--container must be one exact Docker container name")
    return ProbeTarget(
        source=source,
        target=target,
        source_database=source_database,
        target_database=target_database,
        database_user=str(source.username),
        port=source.port or 5432,
    )


def _docker(
    container: str,
    *arguments: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", "exec", container, *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        command = arguments[0] if arguments else "docker exec"
        raise RuntimeError(f"container command {command!r} failed") from exc


def _verify_container(container: str, *, port: int, timeout: float) -> None:
    try:
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        published = subprocess.run(
            ["docker", "port", container, "5432/tcp"],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("could not verify the exact PostgreSQL container") from exc
    if inspected.stdout.strip() != "true":
        raise RuntimeError("the exact PostgreSQL container is not running")
    published_ports = {
        line.rsplit(":", 1)[-1].strip()
        for line in published.stdout.splitlines()
        if ":" in line
    }
    if str(port) not in published_ports:
        raise RuntimeError("container port 5432 is not published on the DSN port")


def _database_manifest(store: PostgresRuntimeStore) -> DatabaseManifest:
    table_counts: dict[str, int] = {}
    row_digests: dict[str, str] = {}
    with store._connection() as conn:  # noqa: SLF001 - isolated backup oracle
        tables = [
            str(row["table_name"])
            for row in conn.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name LIKE 'runtime_%'
                ORDER BY table_name
                """
            ).fetchall()
        ]
        for table in tables:
            if not _TABLE_RE.fullmatch(table):
                raise RuntimeError("runtime schema contains an unsafe table identifier")
            digest = hashlib.sha256()
            count = 0
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT to_jsonb(item)::text AS row_json FROM {table} AS item "
                    "ORDER BY to_jsonb(item)::text"
                )
                for row in cursor:
                    digest.update(str(row["row_json"]).encode("utf-8"))
                    digest.update(b"\n")
                    count += 1
            table_counts[table] = count
            row_digests[table] = digest.hexdigest()

        # Preserve logical relative order while excluding physical ordinal gaps that
        # pg_dump legitimately compacts after a historical DROP COLUMN.
        columns = [
            dict(row)
            for row in conn.execute(
                """
                SELECT table_name, column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name LIKE 'runtime_%'
                ORDER BY table_name, ordinal_position
                """
            ).fetchall()
        ]
        indexes = [
            dict(row)
            for row in conn.execute(
                """
                SELECT tablename, indexname, indexdef FROM pg_indexes
                WHERE schemaname = 'public' AND tablename LIKE 'runtime_%'
                ORDER BY tablename, indexname
                """
            ).fetchall()
        ]
        constraints = [
            dict(row)
            for row in conn.execute(
                """
                SELECT relation.relname AS table_name, constraint_row.conname,
                       pg_get_constraintdef(constraint_row.oid, true) AS definition
                FROM pg_constraint AS constraint_row
                JOIN pg_class AS relation ON relation.oid = constraint_row.conrelid
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public' AND relation.relname LIKE 'runtime_%'
                ORDER BY relation.relname, constraint_row.conname
                """
            ).fetchall()
        ]
        sequences = [
            dict(row)
            for row in conn.execute(
                """
                SELECT sequencename, start_value, min_value, max_value, increment_by,
                       cycle, cache_size, last_value
                FROM pg_sequences
                WHERE schemaname = 'public' AND sequencename LIKE 'runtime_%'
                ORDER BY sequencename
                """
            ).fetchall()
        ]
    payload: dict[str, object] = {
        "tables": table_counts,
        "row_digests": row_digests,
        "columns": columns,
        "indexes": indexes,
        "constraints": constraints,
        "sequences": sequences,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    section_digests = {
        name: hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
                "utf-8"
            )
        ).hexdigest()
        for name, value in payload.items()
    }
    section_digests.update({f"rows:{table}": digest for table, digest in row_digests.items()})
    return DatabaseManifest(
        digest=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        table_counts=table_counts,
        sequence_count=len(sequences),
        section_digests=section_digests,
    )


def _manifest_differences(left: DatabaseManifest, right: DatabaseManifest) -> list[str]:
    return sorted(
        name
        for name in left.section_digests.keys() | right.section_digests.keys()
        if left.section_digests.get(name) != right.section_digests.get(name)
    )


def _marker(
    store: PostgresRuntimeStore,
    *,
    prefix: str,
) -> tuple[RunSnapshot, RunOwner, str, str, list[str]]:
    run_id = f"{prefix}:run"
    operation_id = f"{prefix}:operation"
    owner = RunOwner(tenant_id=f"{prefix}:tenant", user_id="backup-probe-user")
    snapshot = RunSnapshot(
        run_id=run_id,
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        session_id=f"{prefix}:session",
    )
    request_digest = "sha256:" + hashlib.sha256(prefix.encode("utf-8")).hexdigest()
    created = store.create_run(
        snapshot,
        idempotency_scope=f"{prefix}:scope",
        idempotency_key=f"{prefix}:start",
        request_digest=request_digest,
    )
    store.apply_transition(
        LifecycleTransition(
            run_id=run_id,
            kind=TransitionKind.QUEUE,
            transition_id=f"{prefix}:queue",
        ),
        expected_revision=created.snapshot.revision,
    )
    store.prepare_operation(
        OperationIntent(
            operation_id=operation_id,
            run_id=run_id,
            attempt_id=f"{prefix}:attempt",
            kind=OperationKind.CAPABILITY,
            target="agnoclaw.backup_probe",
            request_digest=request_digest,
            effect_class=EffectClass.READ_ONLY,
        )
    )
    event_ids = [event.event_id for event in store.list_events(run_id, owner=owner)]
    return snapshot, owner, operation_id, request_digest, event_ids


def _verify_restored_marker(
    store: PostgresRuntimeStore,
    *,
    snapshot: RunSnapshot,
    owner: RunOwner,
    operation_id: str,
    request_digest: str,
    event_ids: list[str],
    prefix: str,
) -> None:
    restored = store.get_run(snapshot.run_id, owner=owner)
    if restored.state.value != "queued":
        raise RuntimeError("restored marker run is not queued")
    restored_events = store.list_events(snapshot.run_id, owner=owner)
    if [event.event_id for event in restored_events] != event_ids:
        raise RuntimeError("restored marker events changed identity or order")
    if [event.sequence for event in restored_events] != list(
        range(1, len(restored_events) + 1)
    ):
        raise RuntimeError("restored marker event sequence is not contiguous")
    operation = store.get_operation(operation_id, owner=owner)
    if operation.state.value != "planned":
        raise RuntimeError("restored operation intent changed state")
    replay = store.create_run(
        snapshot,
        idempotency_scope=f"{prefix}:scope",
        idempotency_key=f"{prefix}:start",
        request_digest=request_digest,
    )
    if not replay.idempotent or replay.snapshot.run_id != snapshot.run_id:
        raise RuntimeError("restored start idempotency evidence did not replay")


def _cleanup_source(source_dsn: str, run_id: str) -> int:
    with PostgresRuntimeStore(source_dsn, min_pool_size=1, max_pool_size=1) as store:
        with store._transaction() as conn:  # noqa: SLF001 - exact probe-row cleanup
            rows = conn.execute(
                "DELETE FROM runtime_runs WHERE run_id = %s RETURNING run_id",
                (run_id,),
            ).fetchall()
    return len(rows)


def _cleanup_probe(
    *,
    container: str,
    target: ProbeTarget,
    dump_path: str,
    source_dsn: str,
    run_id: str | None,
    timeout: float,
) -> CleanupResult:
    """Attempt every exact cleanup without replacing the probe's primary failure."""
    failures: list[str] = []
    dump_removed = False
    target_removed = False
    source_rows_cleaned = 0

    try:
        _docker(container, "rm", "-f", dump_path, timeout=timeout)
        dump_removed = True
    except Exception as exc:  # noqa: BLE001 - cleanup must continue to exact targets
        failures.append(f"dump cleanup failed ({type(exc).__name__})")

    try:
        _docker(
            container,
            "dropdb",
            "--if-exists",
            "--force",
            "--username",
            target.database_user,
            target.target_database,
            timeout=timeout,
        )
        target_removed = True
    except Exception as exc:  # noqa: BLE001 - cleanup must continue to exact targets
        failures.append(f"target cleanup failed ({type(exc).__name__})")

    if run_id is not None:
        try:
            source_rows_cleaned = _cleanup_source(source_dsn, run_id)
        except Exception as exc:  # noqa: BLE001 - preserve primary probe failure
            failures.append(f"source marker cleanup failed ({type(exc).__name__})")

    return CleanupResult(
        dump_removed=dump_removed,
        target_removed=target_removed,
        source_rows_cleaned=source_rows_cleaned,
        failures=tuple(failures),
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Use pg_dump/pg_restore inside one exact local PostgreSQL container, "
            "then compare content-minimized data/schema/sequence manifests."
        )
    )
    parser.add_argument("--source-dsn", required=True)
    parser.add_argument("--target-dsn", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--allow-target-reset", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--max-local-rto-seconds", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    try:
        target = _validate_target(
            args.source_dsn,
            args.target_dsn,
            args.container,
            allow_target_reset=args.allow_target_reset,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not 1 <= args.timeout_seconds <= 600:
        raise SystemExit("--timeout-seconds must be between 1 and 600")
    if not 0 < args.max_local_rto_seconds <= 600:
        raise SystemExit("--max-local-rto-seconds must be between 0 and 600")
    _verify_container(args.container, port=target.port, timeout=args.timeout_seconds)

    prefix = f"agnoclaw_backup_probe_{uuid4().hex}"
    dump_path = f"/tmp/{prefix}.dump"
    snapshot: RunSnapshot | None = None
    output: dict[str, object] | None = None
    primary_failure: BaseException | None = None
    cleanup = CleanupResult(
        dump_removed=False,
        target_removed=False,
        source_rows_cleaned=0,
        failures=(),
    )
    started = time.monotonic()
    try:
        _docker(
            args.container,
            "dropdb",
            "--if-exists",
            "--force",
            "--username",
            target.database_user,
            target.target_database,
            timeout=args.timeout_seconds,
        )
        _docker(
            args.container,
            "createdb",
            "--username",
            target.database_user,
            "--owner",
            target.database_user,
            target.target_database,
            timeout=args.timeout_seconds,
        )
        with PostgresRuntimeStore(
            args.source_dsn,
            min_pool_size=1,
            max_pool_size=2,
            application_name="agnoclaw-backup-source-probe",
        ) as source_store:
            snapshot, owner, operation_id, request_digest, event_ids = _marker(
                source_store,
                prefix=prefix,
            )
            source_before = _database_manifest(source_store)

        dump_started = time.monotonic()
        _docker(
            args.container,
            "pg_dump",
            "--format=custom",
            "--file",
            dump_path,
            "--username",
            target.database_user,
            "--dbname",
            target.source_database,
            timeout=args.timeout_seconds,
        )
        dump_seconds = time.monotonic() - dump_started
        size_result = _docker(
            args.container,
            "stat",
            "-c",
            "%s",
            dump_path,
            timeout=args.timeout_seconds,
        )
        backup_size_bytes = int(size_result.stdout.strip())

        with PostgresRuntimeStore(
            args.source_dsn,
            min_pool_size=1,
            max_pool_size=1,
            application_name="agnoclaw-backup-source-stability-probe",
        ) as source_store:
            source_after = _database_manifest(source_store)
        if source_after != source_before:
            raise RuntimeError("source runtime ledger changed while the dump was captured")

        restore_started = time.monotonic()
        _docker(
            args.container,
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--username",
            target.database_user,
            "--dbname",
            target.target_database,
            dump_path,
            timeout=args.timeout_seconds,
        )
        restore_seconds = time.monotonic() - restore_started

        verify_started = time.monotonic()
        with PostgresRuntimeStore(
            args.target_dsn,
            min_pool_size=1,
            max_pool_size=2,
            application_name="agnoclaw-backup-restored-probe",
        ) as restored_store:
            restored_before_replay = _database_manifest(restored_store)
            if restored_before_replay != source_before:
                changed = ", ".join(
                    _manifest_differences(source_before, restored_before_replay)
                )
                raise RuntimeError(
                    "restored runtime data/schema/sequence manifest differs: " + changed
                )
            _verify_restored_marker(
                restored_store,
                snapshot=snapshot,
                owner=owner,
                operation_id=operation_id,
                request_digest=request_digest,
                event_ids=event_ids,
                prefix=prefix,
            )
            restored_after_replay = _database_manifest(restored_store)
            if restored_after_replay != source_before:
                raise RuntimeError("idempotent restored reads/replay mutated the ledger")
        verify_seconds = time.monotonic() - verify_started
        local_rto_seconds = dump_seconds + restore_seconds + verify_seconds
        failures = (
            [
                f"local dump+restore+verify {local_rto_seconds:.3f}s exceeds "
                f"{args.max_local_rto_seconds:.3f}s"
            ]
            if local_rto_seconds > args.max_local_rto_seconds
            else []
        )
        output = {
            "status": "failed" if failures else "passed",
            "scope": "isolated_single_container_backup_restore_probe",
            "production_certification": False,
            "source_database": target.source_database,
            "target_database": target.target_database,
            "manifest_digest": source_before.digest,
            "runtime_table_count": len(source_before.table_counts),
            "runtime_row_count": source_before.row_count,
            "runtime_sequence_count": source_before.sequence_count,
            "backup_size_bytes": backup_size_bytes,
            "dump_seconds": round(dump_seconds, 3),
            "restore_seconds": round(restore_seconds, 3),
            "verify_seconds": round(verify_seconds, 3),
            "local_rto_seconds": round(local_rto_seconds, 3),
            "max_local_rto_seconds": args.max_local_rto_seconds,
            "source_stable_during_dump": True,
            "restored_manifest_exact": True,
            "idempotent_replay_exact": True,
            "failures": failures,
            "open_production_gates": [
                "encrypted off-host backup retention",
                "artifact/key-generation restore",
                "point-in-time recovery and replica promotion",
                "production RPO/RTO and corruption response",
            ],
        }
    except BaseException as exc:
        primary_failure = exc
    finally:
        cleanup = _cleanup_probe(
            container=args.container,
            target=target,
            dump_path=dump_path,
            source_dsn=args.source_dsn,
            run_id=snapshot.run_id if snapshot is not None else None,
            timeout=args.timeout_seconds,
        )

    if primary_failure is not None:
        if cleanup.failures:
            primary_failure.add_note("; ".join(cleanup.failures))
        raise primary_failure.with_traceback(primary_failure.__traceback__)
    if cleanup.failures:
        raise RuntimeError("backup/restore probe cleanup failed: " + "; ".join(cleanup.failures))
    if output is None:
        raise AssertionError("backup/restore probe completed without a result")

    output["dump_removed"] = cleanup.dump_removed
    output["source_rows_cleaned"] = cleanup.source_rows_cleaned
    output["target_removed"] = cleanup.target_removed
    output["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 1 if output["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
