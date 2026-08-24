"""Read-only PostgreSQL inventory for the 0.12 service migration."""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .migration import (
    LegacyLearningScopeMapping,
    MigrationFinding,
    MigrationSeverity,
)
from .migration_apply import Migration012Error
from .migration_service import (
    PostgresMigration012Plan,
    PostgresMigrationBackupReceipt,
    PostgresMigrationDatabaseRef,
    PostgresMigrationTableEvidence,
    PostgresScheduleMap,
    _create_postgres_migration_012_plan_unchecked,
)

POSTGRES_MIGRATION_012_SCAN_SCHEMA_VERSION = "1.0"
_SOURCE_TABLES = ("agno_learnings", "agno_schedules", "agno_schedule_runs")
_TARGET_LEARNING_TABLES = (
    "agno_learnings",
    "agnoclaw_migration_012_learning_quarantine",
    "agnoclaw_migration_012_provenance",
    "agnoclaw_migration_012_control",
)
_TARGET_RUNTIME_TABLES = (
    "runtime_scheduler_jobs",
    "runtime_scheduler_runs",
    "agnoclaw_migration_012_schedule_history",
    "agnoclaw_migration_012_provenance",
    "agnoclaw_migration_012_control",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _require_digest(value: str, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{field_name} must be a canonical sha256 digest")
    return value


@dataclass(frozen=True, slots=True)
class PostgresMigration012ScanReport:
    """Content-free evidence and blockers from one read-only database snapshot set."""

    scanned_at: str
    database_reference_digests: tuple[tuple[str, str], ...]
    endpoint_evidence_digests: tuple[tuple[str, str], ...]
    table_evidence: tuple[PostgresMigrationTableEvidence, ...]
    findings: tuple[MigrationFinding, ...]
    source_schedule_id_set_digest: str
    scope_mapping_digest: str
    schema_version: str = POSTGRES_MIGRATION_012_SCAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != POSTGRES_MIGRATION_012_SCAN_SCHEMA_VERSION:
            raise ValueError("unsupported PostgreSQL migration scan schema")
        if not isinstance(self.scanned_at, str) or not self.scanned_at.strip():
            raise ValueError("scanned_at must be a non-empty timestamp")
        for field_name, evidence in (
            ("database_reference", self.database_reference_digests),
            ("endpoint_evidence", self.endpoint_evidence_digests),
        ):
            roles: list[str] = []
            for role, digest in evidence:
                if not isinstance(role, str) or not role:
                    raise ValueError(f"{field_name} role must be non-empty")
                roles.append(role)
                _require_digest(digest, f"{field_name}_digest")
            if len(set(roles)) != len(roles):
                raise ValueError(f"{field_name} roles must be unique")
        table_keys = [(item.role, item.table_name) for item in self.table_evidence]
        if len(set(table_keys)) != len(table_keys):
            raise ValueError("table evidence identities must be unique")
        _require_digest(
            self.source_schedule_id_set_digest,
            "source_schedule_id_set_digest",
        )
        _require_digest(self.scope_mapping_digest, "scope_mapping_digest")

    @property
    def ready(self) -> bool:
        return not any(item.severity is MigrationSeverity.BLOCKER for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scanned_at": self.scanned_at,
            "ready": self.ready,
            "database_reference_digests": [
                {"role": role, "digest": digest} for role, digest in self.database_reference_digests
            ],
            "endpoint_evidence_digests": [
                {"role": role, "digest": digest} for role, digest in self.endpoint_evidence_digests
            ],
            "table_evidence": [item.to_dict() for item in self.table_evidence],
            "findings": [item.to_dict() for item in self.findings],
            "source_schedule_id_set_digest": self.source_schedule_id_set_digest,
            "scope_mapping_digest": self.scope_mapping_digest,
        }


@dataclass(frozen=True, slots=True)
class _ScannedTable:
    evidence: PostgresMigrationTableEvidence
    columns: frozenset[str]


def _finding(
    code: str,
    source: str,
    safe_message: str,
    resolution: str,
    *,
    count: int | None = None,
) -> MigrationFinding:
    return MigrationFinding(
        code=code,
        severity=MigrationSeverity.BLOCKER,
        source=source,
        safe_message=safe_message,
        resolution=resolution,
        count=count,
    )


def _load_driver() -> tuple[Any, Any]:
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise Migration012Error(
            "MIGRATION_POSTGRES_DRIVER_UNAVAILABLE",
            "PostgreSQL migration scanning requires the agnoclaw postgres extra.",
            install_extra="postgres",
        ) from exc
    return psycopg, sql


def _fetchone(cursor: Any, *, operation: str) -> tuple[Any, ...]:
    row = cursor.fetchone()
    if row is None:
        raise Migration012Error(
            "MIGRATION_POSTGRES_SCAN_INCOMPLETE",
            "A PostgreSQL migration scan query returned no control evidence.",
            operation=operation,
        )
    return tuple(row)


def _close_scan_handles(connection: Any, cursor: Any) -> None:
    if cursor is not None:
        with suppress(Exception):
            cursor.close()
    if connection is not None:
        with suppress(Exception):
            connection.rollback()
        with suppress(Exception):
            connection.close()


def _relation_metadata(
    cursor: Any,
    *,
    schema: str,
    table: str,
) -> tuple[int, dict[str, Any]] | None:
    cursor.execute(
        """
        SELECT c.oid, c.relkind, c.relrowsecurity, c.relforcerowsecurity
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')
        """,
        (schema, table),
    )
    relation = cursor.fetchone()
    if relation is None:
        return None
    relation_oid = int(relation[0])
    cursor.execute(
        """
        SELECT a.attnum, a.attname,
               pg_catalog.format_type(a.atttypid, a.atttypmod),
               a.attnotnull,
               COALESCE(pg_catalog.pg_get_expr(d.adbin, d.adrelid), ''),
               a.attidentity,
               a.attgenerated
        FROM pg_catalog.pg_attribute AS a
        LEFT JOIN pg_catalog.pg_attrdef AS d
          ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (relation_oid,),
    )
    columns = [tuple(row) for row in cursor.fetchall()]
    cursor.execute(
        """
        SELECT conname, contype, pg_catalog.pg_get_constraintdef(oid, true)
        FROM pg_catalog.pg_constraint
        WHERE conrelid = %s
        ORDER BY conname
        """,
        (relation_oid,),
    )
    constraints = [tuple(row) for row in cursor.fetchall()]
    cursor.execute(
        """
        SELECT pg_catalog.pg_get_indexdef(indexrelid)
        FROM pg_catalog.pg_index
        WHERE indrelid = %s
        ORDER BY indexrelid
        """,
        (relation_oid,),
    )
    indexes = [str(row[0]) for row in cursor.fetchall()]
    return relation_oid, {
        "relation_kind": str(relation[1]),
        "row_security": bool(relation[2]),
        "force_row_security": bool(relation[3]),
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
    }


def _stream_table_digest(
    connection: Any,
    sql: Any,
    *,
    schema: str,
    table: str,
    batch_size: int,
    max_row_bytes: int,
) -> tuple[int, str]:
    qualified = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))
    query = sql.SQL(
        "SELECT to_jsonb(scan_row)::text FROM {} AS scan_row "
        'ORDER BY (to_jsonb(scan_row)::text) COLLATE "C"'
    ).format(qualified)
    cursor = connection.cursor(name=f"agnoclaw_migration_scan_{table}")
    cursor.itersize = batch_size
    count = 0
    hasher = hashlib.sha256()
    try:
        cursor.execute(query)
        while rows := cursor.fetchmany(batch_size):
            for row in rows:
                payload = str(row[0]).encode("utf-8")
                if len(payload) > max_row_bytes:
                    raise Migration012Error(
                        "MIGRATION_POSTGRES_ROW_LIMIT_EXCEEDED",
                        "A PostgreSQL row exceeds the explicit migration scan byte limit.",
                        table_name=table,
                        max_row_bytes=max_row_bytes,
                    )
                row_digest = hashlib.sha256(payload).digest()
                hasher.update(len(row_digest).to_bytes(4, "big"))
                hasher.update(row_digest)
                count += 1
    finally:
        cursor.close()
    return count, "sha256:" + hasher.hexdigest()


def _scan_table(
    connection: Any,
    cursor: Any,
    sql: Any,
    *,
    reference: PostgresMigrationDatabaseRef,
    table: str,
    max_rows: int,
    batch_size: int,
    max_row_bytes: int,
) -> _ScannedTable:
    metadata = _relation_metadata(cursor, schema=reference.schema, table=table)
    if metadata is None:
        return _ScannedTable(
            evidence=PostgresMigrationTableEvidence(
                role=reference.role,
                table_name=table,
                row_count=0,
                schema_digest=_digest(
                    {"exists": False, "schema": reference.schema, "table": table}
                ),
                logical_digest=None,
                exists=False,
            ),
            columns=frozenset(),
        )
    _, schema_metadata = metadata
    columns = frozenset(str(item[1]) for item in schema_metadata["columns"])
    qualified = sql.SQL("{}.{}").format(
        sql.Identifier(reference.schema),
        sql.Identifier(table),
    )
    cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(qualified))
    row_count = int(_fetchone(cursor, operation="table_count")[0])
    logical_digest: str | None = None
    if row_count <= max_rows:
        scanned_count, logical_digest = _stream_table_digest(
            connection,
            sql,
            schema=reference.schema,
            table=table,
            batch_size=batch_size,
            max_row_bytes=max_row_bytes,
        )
        if scanned_count != row_count:
            raise Migration012Error(
                "MIGRATION_POSTGRES_SNAPSHOT_DRIFT",
                "A PostgreSQL table count changed inside a repeatable-read snapshot.",
                role=reference.role,
                table_name=table,
            )
    return _ScannedTable(
        evidence=PostgresMigrationTableEvidence(
            role=reference.role,
            table_name=table,
            row_count=row_count,
            schema_digest=_digest(schema_metadata),
            logical_digest=logical_digest,
        ),
        columns=columns,
    )


def _stream_text_set_digest(
    connection: Any,
    sql: Any,
    *,
    schema: str,
    table: str,
    column: str,
    batch_size: int,
) -> tuple[int, str]:
    qualified = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))
    identifier = sql.Identifier(column)
    query = sql.SQL('SELECT {}::text FROM {} ORDER BY ({}::text) COLLATE "C"').format(
        identifier, qualified, identifier
    )
    cursor = connection.cursor(name=f"agnoclaw_migration_ids_{table}")
    cursor.itersize = batch_size
    count = 0
    hasher = hashlib.sha256()
    hasher.update(b"[")
    try:
        cursor.execute(query)
        while rows := cursor.fetchmany(batch_size):
            for row in rows:
                if count:
                    hasher.update(b",")
                hasher.update(
                    json.dumps(
                        str(row[0]),
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                count += 1
    finally:
        cursor.close()
    hasher.update(b"]")
    return count, "sha256:" + hasher.hexdigest()


def _mapping_exists(
    mappings: dict[tuple[str | None, str | None], LegacyLearningScopeMapping],
    namespace: str | None,
    learning_type: str,
) -> bool:
    return (namespace, learning_type) in mappings or (namespace, None) in mappings


def _inspect_source_semantics(
    connection: Any,
    cursor: Any,
    sql: Any,
    *,
    source: PostgresMigrationDatabaseRef,
    scanned: dict[str, _ScannedTable],
    schedule_map: PostgresScheduleMap,
    mappings: tuple[LegacyLearningScopeMapping, ...],
    max_rows: int,
    batch_size: int,
) -> tuple[list[MigrationFinding], str]:
    findings: list[MigrationFinding] = []
    mapping_index = {item.key: item for item in mappings}
    learning = scanned["agno_learnings"]
    learning_required = {
        "learning_id",
        "learning_type",
        "namespace",
        "user_id",
        "agent_id",
        "team_id",
        "workflow_id",
        "session_id",
        "entity_id",
        "entity_type",
        "content",
        "metadata",
        "created_at",
        "updated_at",
    }
    if learning.evidence.exists and not learning_required.issubset(learning.columns):
        findings.append(
            _finding(
                "MIGRATION_POSTGRES_LEARNING_SCHEMA_UNSUPPORTED",
                "source:agno_learnings",
                "The source learning table does not match the certified Agno schema.",
                "Use Agno's supported unified learning table or add a reviewed adapter.",
            )
        )
    elif learning.evidence.exists and learning.evidence.row_count <= max_rows:
        qualified = sql.SQL("{}.{}").format(
            sql.Identifier(source.schema), sql.Identifier("agno_learnings")
        )
        cursor.execute(
            sql.SQL(
                "SELECT count(*) FROM {} WHERE "
                "(learning_type IN ('user_profile', 'user_memory') AND user_id IS NULL) "
                "OR (learning_type = 'session_context' AND session_id IS NULL)"
            ).format(qualified)
        )
        owner_gap_count = int(_fetchone(cursor, operation="learning_owner_gaps")[0])
        if owner_gap_count:
            findings.append(
                _finding(
                    "MIGRATION_POSTGRES_LEARNING_OWNER_MISSING",
                    "source:agno_learnings",
                    "Personal or session learning rows lack their required owner.",
                    "Repair or explicitly quarantine those records before apply.",
                    count=owner_gap_count,
                )
            )
        cursor.execute(
            sql.SQL(
                "SELECT count(*) FROM ("
                "SELECT learning_type, "
                "CASE WHEN learning_type = 'session_context' THEN session_id ELSE user_id END "
                "AS target_owner FROM {} "
                "WHERE learning_type IN ('user_profile', 'user_memory', 'session_context') "
                "GROUP BY learning_type, target_owner HAVING count(*) > 1"
                ") AS collisions"
            ).format(qualified)
        )
        collision_count = int(_fetchone(cursor, operation="learning_target_collisions")[0])
        if collision_count:
            findings.append(
                _finding(
                    "MIGRATION_POSTGRES_LEARNING_TARGET_COLLISION",
                    "source:agno_learnings",
                    "Multiple personal learning rows resolve to one target identity.",
                    "Merge or quarantine duplicate owner/type rows before planning.",
                    count=collision_count,
                )
            )
        cursor.execute(
            sql.SQL(
                "SELECT namespace::text, learning_type::text FROM {} "
                "WHERE learning_type NOT IN ('user_profile', 'user_memory', 'session_context') "
                "GROUP BY namespace, learning_type"
            ).format(qualified)
        )
        unresolved = 0
        for namespace, learning_type in cursor.fetchall():
            normalized_namespace = None if namespace is None else str(namespace)
            if not _mapping_exists(
                mapping_index,
                normalized_namespace,
                str(learning_type),
            ):
                unresolved += 1
        if unresolved:
            findings.append(
                _finding(
                    "MIGRATION_POSTGRES_LEARNING_SCOPE_UNRESOLVED",
                    "source:agno_learnings",
                    "Institutional learning scopes lack explicit map-or-quarantine decisions.",
                    "Provide a reviewed scope mapping for every namespace and learning type.",
                    count=unresolved,
                )
            )

    schedules = scanned["agno_schedules"]
    schedule_id_set_digest = _digest([])
    schedule_required = {"id", "locked_by", "locked_at"}
    if schedules.evidence.exists and not schedule_required.issubset(schedules.columns):
        findings.append(
            _finding(
                "MIGRATION_POSTGRES_SCHEDULE_SCHEMA_UNSUPPORTED",
                "source:agno_schedules",
                "The source schedule table does not match the certified Agno schema.",
                "Upgrade Agno or add a reviewed schedule adapter before migration.",
            )
        )
    elif schedules.evidence.exists and schedules.evidence.row_count <= max_rows:
        schedule_count, schedule_id_set_digest = _stream_text_set_digest(
            connection,
            sql,
            schema=source.schema,
            table="agno_schedules",
            column="id",
            batch_size=batch_size,
        )
        if schedule_count != schedules.evidence.row_count:
            findings.append(
                _finding(
                    "MIGRATION_POSTGRES_SCHEDULE_ID_DUPLICATE",
                    "source:agno_schedules",
                    "The source schedule identity count is inconsistent.",
                    "Repair schedule identities before migration.",
                )
            )
        qualified = sql.SQL("{}.{}").format(
            sql.Identifier(source.schema), sql.Identifier("agno_schedules")
        )
        cursor.execute(
            sql.SQL(
                "SELECT count(*) FROM {} WHERE locked_by IS NOT NULL OR locked_at IS NOT NULL"
            ).format(qualified)
        )
        locked_count = int(_fetchone(cursor, operation="schedule_locks")[0])
        if locked_count:
            findings.append(
                _finding(
                    "MIGRATION_POSTGRES_SCHEDULE_IN_FLIGHT",
                    "source:agno_schedules",
                    "Source schedules are locked or in flight.",
                    "Stop scheduler writers and wait for or resolve every in-flight run.",
                    count=locked_count,
                )
            )
    if schedule_id_set_digest != schedule_map.source_id_set_digest:
        findings.append(
            _finding(
                "MIGRATION_POSTGRES_SCHEDULE_MAP_MISMATCH",
                "source:agno_schedules",
                "The sensitive schedule map does not exactly cover source schedule identities.",
                "Add missing mappings, remove orphan mappings, and regenerate the plan.",
            )
        )

    schedule_runs = scanned["agno_schedule_runs"]
    run_required = {"id", "schedule_id", "status", "completed_at"}
    if schedule_runs.evidence.exists and not run_required.issubset(schedule_runs.columns):
        findings.append(
            _finding(
                "MIGRATION_POSTGRES_SCHEDULE_RUN_SCHEMA_UNSUPPORTED",
                "source:agno_schedule_runs",
                "The source schedule-run table does not match the certified Agno schema.",
                "Upgrade Agno or add a reviewed schedule-history adapter.",
            )
        )
    elif schedule_runs.evidence.exists:
        qualified = sql.SQL("{}.{}").format(
            sql.Identifier(source.schema), sql.Identifier("agno_schedule_runs")
        )
        cursor.execute(
            sql.SQL(
                "SELECT count(*) FROM {} WHERE completed_at IS NULL "
                "OR lower(status) NOT IN ('success', 'failed', 'timeout')"
            ).format(qualified)
        )
        active_count = int(_fetchone(cursor, operation="active_schedule_runs")[0])
        if active_count:
            findings.append(
                _finding(
                    "MIGRATION_POSTGRES_SCHEDULE_RUN_IN_FLIGHT",
                    "source:agno_schedule_runs",
                    "Source schedule-run history contains non-terminal executions.",
                    "Wait for completion or record an explicit incident decision.",
                    count=active_count,
                )
            )
        if schedules.evidence.exists and "id" in schedules.columns:
            schedule_table = sql.SQL("{}.{}").format(
                sql.Identifier(source.schema),
                sql.Identifier("agno_schedules"),
            )
            cursor.execute(
                sql.SQL(
                    "SELECT count(*) FROM {} AS source_run "
                    "LEFT JOIN {} AS source_schedule "
                    "ON source_schedule.id = source_run.schedule_id "
                    "WHERE source_schedule.id IS NULL"
                ).format(qualified, schedule_table)
            )
            orphaned_count = int(_fetchone(cursor, operation="orphaned_schedule_runs")[0])
        else:
            orphaned_count = schedule_runs.evidence.row_count
        if orphaned_count:
            findings.append(
                _finding(
                    "MIGRATION_POSTGRES_SCHEDULE_RUN_ORPHANED",
                    "source:agno_schedule_runs",
                    "Source schedule history references a missing schedule identity.",
                    "Restore or explicitly quarantine orphaned history before planning.",
                    count=orphaned_count,
                )
            )
    return findings, schedule_id_set_digest


def _scan_connection_group(
    psycopg: Any,
    sql: Any,
    *,
    dsn: str,
    requests: tuple[tuple[PostgresMigrationDatabaseRef, tuple[str, ...]], ...],
    statement_timeout_ms: int,
    lock_timeout_ms: int,
    max_rows: int,
    batch_size: int,
    max_row_bytes: int,
) -> tuple[list[tuple[str, str]], dict[tuple[str, str], _ScannedTable], Any]:
    connection: Any = None
    cursor: Any = None
    try:
        connection = psycopg.connect(
            dsn,
            autocommit=True,
            connect_timeout=10,
            application_name="agnoclaw-migration-012-scan",
        )
        cursor = connection.cursor()
        cursor.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cursor.execute(
            "SELECT pg_catalog.set_config('statement_timeout', %s, true)",
            (f"{statement_timeout_ms}ms",),
        )
        cursor.execute(
            "SELECT pg_catalog.set_config('lock_timeout', %s, true)",
            (f"{lock_timeout_ms}ms",),
        )
        cursor.execute("SHOW transaction_read_only")
        if str(_fetchone(cursor, operation="transaction_read_only")[0]).lower() != "on":
            raise Migration012Error(
                "MIGRATION_POSTGRES_READ_ONLY_NOT_ENFORCED",
                "The PostgreSQL scanner could not enforce a read-only transaction.",
            )
        cursor.execute("SHOW transaction_isolation")
        if str(_fetchone(cursor, operation="transaction_isolation")[0]).lower() != (
            "repeatable read"
        ):
            raise Migration012Error(
                "MIGRATION_POSTGRES_SNAPSHOT_NOT_ENFORCED",
                "The PostgreSQL scanner could not enforce repeatable-read isolation.",
            )
        cursor.execute(
            """
            SELECT current_database(), oid, current_setting('server_version_num')
            FROM pg_catalog.pg_database
            WHERE datname = current_database()
            """
        )
        database_name, database_oid, server_version = _fetchone(
            cursor, operation="endpoint_identity"
        )
        endpoints: list[tuple[str, str]] = []
        scanned: dict[tuple[str, str], _ScannedTable] = {}
        for reference, tables in requests:
            endpoints.append(
                (
                    reference.role,
                    _digest(
                        {
                            "database_name": str(database_name),
                            "database_oid": int(database_oid),
                            "server_version_num": str(server_version),
                            "schema": reference.schema,
                            "role": reference.role,
                        }
                    ),
                )
            )
            for table in tables:
                scanned[(reference.role, table)] = _scan_table(
                    connection,
                    cursor,
                    sql,
                    reference=reference,
                    table=table,
                    max_rows=max_rows,
                    batch_size=batch_size,
                    max_row_bytes=max_row_bytes,
                )
        return endpoints, scanned, (connection, cursor)
    except Migration012Error:
        _close_scan_handles(connection, cursor)
        raise
    except Exception as exc:
        _close_scan_handles(connection, cursor)
        raise Migration012Error(
            "MIGRATION_POSTGRES_SCAN_FAILED",
            "A PostgreSQL endpoint could not be scanned safely; driver details were redacted.",
            endpoint_roles=sorted(reference.role for reference, _ in requests),
            error_type=type(exc).__name__,
        ) from None


def scan_postgres_migration_012(
    *,
    source: PostgresMigrationDatabaseRef,
    target_learning: PostgresMigrationDatabaseRef,
    target_runtime: PostgresMigrationDatabaseRef,
    schedule_map: PostgresScheduleMap,
    scope_mappings: tuple[LegacyLearningScopeMapping, ...] = (),
    environment: dict[str, str] | None = None,
    statement_timeout_ms: int = 60_000,
    lock_timeout_ms: int = 2_000,
    max_rows_per_table: int = 10_000_000,
    batch_size: int = 1_000,
    max_row_bytes: int = 16 * 1024 * 1024,
) -> PostgresMigration012ScanReport:
    """Scan source and targets without writes, secrets, or content in the report."""
    for name, value, lower, upper in (
        ("statement_timeout_ms", statement_timeout_ms, 1, 3_600_000),
        ("lock_timeout_ms", lock_timeout_ms, 1, 60_000),
        ("max_rows_per_table", max_rows_per_table, 1, 1_000_000_000),
        ("batch_size", batch_size, 1, 10_000),
        ("max_row_bytes", max_row_bytes, 1_024, 256 * 1024 * 1024),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or not lower <= value <= upper:
            raise ValueError(f"{name} must be between {lower} and {upper}")
    if len({item.key for item in scope_mappings}) != len(scope_mappings):
        raise ValueError("scope_mappings cannot contain duplicate source keys")
    references = (
        (source, _SOURCE_TABLES),
        (target_learning, _TARGET_LEARNING_TABLES),
        (target_runtime, _TARGET_RUNTIME_TABLES),
    )
    expected_roles = ("source", "target_learning", "target_runtime")
    if tuple(item[0].role for item in references) != expected_roles:
        raise ValueError("PostgreSQL migration database references have invalid roles")
    psycopg, sql = _load_driver()
    grouped: dict[str, list[tuple[PostgresMigrationDatabaseRef, tuple[str, ...]]]] = {}
    for reference, tables in references:
        grouped.setdefault(reference.resolve(environment), []).append((reference, tables))

    endpoints: list[tuple[str, str]] = []
    scanned_tables: dict[tuple[str, str], _ScannedTable] = {}
    open_groups: list[tuple[Any, Any]] = []
    try:
        for dsn, requests in grouped.items():
            group_endpoints, group_tables, handles = _scan_connection_group(
                psycopg,
                sql,
                dsn=dsn,
                requests=tuple(requests),
                statement_timeout_ms=statement_timeout_ms,
                lock_timeout_ms=lock_timeout_ms,
                max_rows=max_rows_per_table,
                batch_size=batch_size,
                max_row_bytes=max_row_bytes,
            )
            endpoints.extend(group_endpoints)
            scanned_tables.update(group_tables)
            open_groups.append(handles)

        source_scanned = {table: scanned_tables[(source.role, table)] for table in _SOURCE_TABLES}
        source_connection, source_cursor = next(
            handles
            for requests, handles in zip(grouped.values(), open_groups, strict=True)
            if any(reference.role == "source" for reference, _ in requests)
        )
        findings, schedule_id_set_digest = _inspect_source_semantics(
            source_connection,
            source_cursor,
            sql,
            source=source,
            scanned=source_scanned,
            schedule_map=schedule_map,
            mappings=scope_mappings,
            max_rows=max_rows_per_table,
            batch_size=batch_size,
        )
        for scanned in scanned_tables.values():
            if scanned.evidence.exists and scanned.evidence.logical_digest is None:
                findings.append(
                    _finding(
                        "MIGRATION_POSTGRES_SCAN_LIMIT_EXCEEDED",
                        f"{scanned.evidence.role}:{scanned.evidence.table_name}",
                        "A PostgreSQL table exceeds the explicit migration scan row limit.",
                        "Raise the reviewed limit or reduce/archive the source before planning.",
                        count=scanned.evidence.row_count,
                    )
                )
        return PostgresMigration012ScanReport(
            scanned_at=datetime.now(UTC).isoformat(),
            database_reference_digests=tuple(
                sorted(
                    (reference.role, _digest(reference.to_dict())) for reference, _ in references
                )
            ),
            endpoint_evidence_digests=tuple(sorted(endpoints)),
            table_evidence=tuple(
                sorted(
                    (item.evidence for item in scanned_tables.values()),
                    key=lambda item: (item.role, item.table_name),
                )
            ),
            findings=tuple(sorted(findings, key=lambda item: (item.code, item.source))),
            source_schedule_id_set_digest=schedule_id_set_digest,
            scope_mapping_digest=_digest(
                sorted(
                    (item.to_dict() for item in scope_mappings),
                    key=_canonical_json,
                )
            ),
        )
    finally:
        for connection, cursor in open_groups:
            _close_scan_handles(connection, cursor)


def create_postgres_migration_012_plan_from_scan(
    *,
    scan: PostgresMigration012ScanReport,
    source: PostgresMigrationDatabaseRef,
    target_learning: PostgresMigrationDatabaseRef,
    target_runtime: PostgresMigrationDatabaseRef,
    target_tenant_id: str,
    target_agent_id: str,
    schedule_map: PostgresScheduleMap,
    backup_receipt: PostgresMigrationBackupReceipt,
    writer_fence_plan: str,
    target_org_id: str | None = None,
    scope_mappings: tuple[LegacyLearningScopeMapping, ...] = (),
) -> PostgresMigration012Plan:
    """Create a plan only when it is bound to a ready, exact scanner result."""
    if not scan.ready:
        raise Migration012Error(
            "MIGRATION_POSTGRES_SCAN_BLOCKED",
            "The PostgreSQL migration scan contains blockers and cannot authorize a plan.",
            blocker_count=sum(item.severity is MigrationSeverity.BLOCKER for item in scan.findings),
        )
    references = (source, target_learning, target_runtime)
    expected_reference_digests = tuple(
        sorted((reference.role, _digest(reference.to_dict())) for reference in references)
    )
    if scan.database_reference_digests != expected_reference_digests:
        raise Migration012Error(
            "MIGRATION_POSTGRES_SCAN_REFERENCE_MISMATCH",
            "The PostgreSQL scan was produced for different database references.",
        )
    expected_mapping_digest = _digest(
        sorted((item.to_dict() for item in scope_mappings), key=_canonical_json)
    )
    if scan.scope_mapping_digest != expected_mapping_digest:
        raise Migration012Error(
            "MIGRATION_POSTGRES_SCAN_SCOPE_MISMATCH",
            "The PostgreSQL scan was produced with different learning scope decisions.",
        )
    if scan.source_schedule_id_set_digest != schedule_map.source_id_set_digest:
        raise Migration012Error(
            "MIGRATION_POSTGRES_SCAN_SCHEDULE_MISMATCH",
            "The PostgreSQL scan does not match the supplied schedule map identities.",
        )
    return _create_postgres_migration_012_plan_unchecked(
        source=source,
        target_learning=target_learning,
        target_runtime=target_runtime,
        target_tenant_id=target_tenant_id,
        target_agent_id=target_agent_id,
        schedule_map=schedule_map,
        backup_receipt=backup_receipt,
        writer_fence_plan=writer_fence_plan,
        endpoint_evidence_digests=scan.endpoint_evidence_digests,
        table_evidence=scan.table_evidence,
        target_org_id=target_org_id,
        scope_mappings=scope_mappings,
    )


__all__ = [
    "POSTGRES_MIGRATION_012_SCAN_SCHEMA_VERSION",
    "PostgresMigration012ScanReport",
    "create_postgres_migration_012_plan_from_scan",
    "scan_postgres_migration_012",
]
