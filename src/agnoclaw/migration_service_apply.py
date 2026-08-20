"""Crash-resumable PostgreSQL/service migration target lifecycle."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .migration_apply import Migration012Error
from .migration_service import (
    PostgresMigration012Plan,
    PostgresMigrationDatabaseRef,
    PostgresScheduleMap,
    _digest,
    _require_control_token,
    _require_digest,
    _require_text,
)
from .migration_service_transform import (
    PostgresMigration012TransformReport,
    _compile_postgres_migration_012_source_snapshot,
    _StreamDigest,
    _TransformedRow,
    preview_postgres_migration_012_transforms,
)

POSTGRES_MIGRATION_012_LIFECYCLE_SCHEMA_VERSION = "1.0"
_TARGET_ROLES = ("target_learning", "target_runtime")
_ACTIVE_PHASES = {"applying", "applied", "verified", "cutover"}
_TIMESTAMP_COLUMNS = {"next_run_at", "created_at", "updated_at"}
_CheckpointObserver = Callable[[str, str, tuple[str, ...], int], None]


class PostgresMigration012Phase(StrEnum):
    APPLYING = "applying"
    APPLIED = "applied"
    VERIFIED = "verified"
    CUTOVER = "cutover"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True, slots=True)
class PostgresMigration012LifecycleReceipt:
    """Content-free receipt for one service-migration lifecycle operation."""

    operation: str
    migration_id: str
    plan_digest: str
    transform_digest: str
    generated_at: str
    role_phases: tuple[tuple[str, str], ...]
    counts: tuple[tuple[str, str, str, int], ...]
    rollback_available: bool
    receipt_digest: str = ""
    schema_version: str = POSTGRES_MIGRATION_012_LIFECYCLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != POSTGRES_MIGRATION_012_LIFECYCLE_SCHEMA_VERSION:
            raise ValueError("unsupported PostgreSQL migration lifecycle receipt schema")
        if self.operation not in {"apply", "verify", "cutover", "rollback"}:
            raise ValueError("unsupported PostgreSQL migration lifecycle operation")
        for name in ("migration_id", "generated_at"):
            object.__setattr__(self, name, _require_text(getattr(self, name), name))
        object.__setattr__(self, "plan_digest", _require_digest(self.plan_digest, "plan_digest"))
        object.__setattr__(
            self,
            "transform_digest",
            _require_digest(self.transform_digest, "transform_digest"),
        )
        if tuple(role for role, _ in self.role_phases) != _TARGET_ROLES:
            raise ValueError("lifecycle receipt must cover both target roles")
        valid_phases = {item.value for item in PostgresMigration012Phase}
        if any(phase not in valid_phases for _, phase in self.role_phases):
            raise ValueError("lifecycle receipt contains an invalid phase")
        if tuple(sorted(self.counts)) != self.counts:
            raise ValueError("lifecycle receipt counts must be canonical")
        for role, category, disposition, count in self.counts:
            if (
                role not in _TARGET_ROLES
                or not category
                or disposition
                not in {
                    "inserted",
                    "preexisting_identical",
                }
            ):
                raise ValueError("lifecycle receipt contains an invalid count identity")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("lifecycle receipt counts cannot be negative")
        if not isinstance(self.rollback_available, bool):
            raise ValueError("rollback_available must be a boolean")
        if self.receipt_digest:
            _require_digest(self.receipt_digest, "receipt_digest")
            if self.receipt_digest != self.computed_receipt_digest:
                raise ValueError("receipt_digest does not match lifecycle evidence")

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "operation": self.operation,
            "migration_id": self.migration_id,
            "plan_digest": self.plan_digest,
            "transform_digest": self.transform_digest,
            "generated_at": self.generated_at,
            "role_phases": dict(self.role_phases),
            "counts": [
                {
                    "target_role": role,
                    "category": category,
                    "disposition": disposition,
                    "count": count,
                }
                for role, category, disposition, count in self.counts
            ],
            "rollback_available": self.rollback_available,
        }
        if include_digest:
            payload["receipt_digest"] = self.receipt_digest
        return payload

    @property
    def computed_receipt_digest(self) -> str:
        payload = self.to_dict(include_digest=False)
        payload.pop("generated_at")
        return _digest(payload)


@dataclass(frozen=True, slots=True)
class _TableSpec:
    target_role: str
    table_name: str
    identity_column: str
    columns: tuple[str, ...]
    jsonb_columns: frozenset[str] = frozenset()


_TABLE_SPECS = {
    "agno_learnings": _TableSpec(
        target_role="target_learning",
        table_name="agno_learnings",
        identity_column="learning_id",
        columns=(
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
        ),
        jsonb_columns=frozenset({"content", "metadata"}),
    ),
    "agnoclaw_migration_012_learning_quarantine": _TableSpec(
        target_role="target_learning",
        table_name="agnoclaw_migration_012_learning_quarantine",
        identity_column="source_digest",
        columns=(
            "source_digest",
            "migration_id",
            "source_table",
            "learning_type",
            "action",
            "target_tenant_id",
            "target_namespace",
            "record_json",
        ),
    ),
    "runtime_scheduler_jobs": _TableSpec(
        target_role="target_runtime",
        table_name="runtime_scheduler_jobs",
        identity_column="job_name",
        columns=(
            "job_name",
            "revision",
            "enabled",
            "next_run_at",
            "job_digest",
            "job_json",
            "created_at",
            "updated_at",
        ),
    ),
    "agnoclaw_migration_012_schedule_history": _TableSpec(
        target_role="target_runtime",
        table_name="agnoclaw_migration_012_schedule_history",
        identity_column="source_digest",
        columns=(
            "source_digest",
            "migration_id",
            "source_run_id",
            "source_schedule_id",
            "target_job_name",
            "status",
            "record_json",
        ),
    ),
}


@dataclass(slots=True)
class _TargetHandle:
    connection: Any
    schema: str
    roles: tuple[str, ...]
    sql: Any


def _validate_bounds(
    *,
    statement_timeout_ms: int,
    lock_timeout_ms: int,
    max_rows_per_table: int,
    read_batch_size: int,
    write_batch_size: int,
    max_row_bytes: int,
) -> None:
    for name, value, lower, upper in (
        ("statement_timeout_ms", statement_timeout_ms, 1, 3_600_000),
        ("lock_timeout_ms", lock_timeout_ms, 1, 60_000),
        ("max_rows_per_table", max_rows_per_table, 1, 1_000_000_000),
        ("read_batch_size", read_batch_size, 1, 10_000),
        ("write_batch_size", write_batch_size, 1, 10_000),
        ("max_row_bytes", max_row_bytes, 1_024, 256 * 1024 * 1024),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ValueError(f"{name} must be between {lower} and {upper}")


def _validate_plan_and_confirmations(
    *,
    plan: PostgresMigration012Plan,
    schedule_map: PostgresScheduleMap,
    confirm_plan_digest: str,
    confirm_transform_digest: str,
    confirm_writer_fence_plan: str,
    writers_stopped: bool,
    confirm_backup_receipt_digest: str | None = None,
) -> None:
    if plan.plan_digest != plan.computed_plan_digest:
        raise Migration012Error(
            "MIGRATION_PLAN_DIGEST_MISMATCH",
            "The PostgreSQL migration plan digest does not match its contents.",
        )
    if schedule_map.summary != plan.schedule_map_summary:
        raise Migration012Error(
            "MIGRATION_POSTGRES_SCHEDULE_MAP_MISMATCH",
            "The sensitive schedule map does not match the reviewed plan.",
        )
    if confirm_plan_digest != plan.plan_digest:
        raise Migration012Error(
            "MIGRATION_CONFIRMATION_MISMATCH",
            "The operation requires the exact reviewed PostgreSQL plan digest.",
            confirmation="plan_digest",
        )
    _require_digest(confirm_transform_digest, "confirm_transform_digest")
    if confirm_writer_fence_plan != plan.writer_fence_plan:
        raise Migration012Error(
            "MIGRATION_WRITER_FENCE_CONFIRMATION_MISMATCH",
            "The operation requires the exact reviewed writer-fence plan token.",
        )
    if not writers_stopped:
        raise Migration012Error(
            "MIGRATION_WRITERS_NOT_CONFIRMED_STOPPED",
            "Stop every source and target writer before continuing.",
        )
    if (
        confirm_backup_receipt_digest is not None
        and confirm_backup_receipt_digest != plan.backup_receipt.receipt_digest
    ):
        raise Migration012Error(
            "MIGRATION_BACKUP_CONFIRMATION_MISMATCH",
            "Apply requires the exact reviewed restore-tested backup receipt digest.",
        )


def _qualified(sql: Any, schema: str, table: str) -> Any:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def _table_exists(connection: Any, *, schema: str, table: str) -> bool:
    row = connection.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = %s AND relation.relname = %s
              AND relation.relkind IN ('r', 'p')
        )
        """,
        (schema, table),
    ).fetchone()
    if isinstance(row, dict):
        return bool(next(iter(row.values())))
    return bool(row and row[0])


def _connect_target_handles(
    plan: PostgresMigration012Plan,
    *,
    environment: dict[str, str] | None,
    statement_timeout_ms: int,
    lock_timeout_ms: int,
) -> tuple[list[_TargetHandle], dict[str, _TargetHandle]]:
    try:
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise Migration012Error(
            "MIGRATION_POSTGRES_DRIVER_UNAVAILABLE",
            "PostgreSQL service migration requires the agnoclaw postgres extra.",
            install_extra="postgres",
        ) from exc
    grouped: dict[tuple[str, str], list[PostgresMigrationDatabaseRef]] = {}
    for reference in (plan.target_learning, plan.target_runtime):
        grouped.setdefault((reference.resolve(environment), reference.schema), []).append(reference)
    handles: list[_TargetHandle] = []
    role_handles: dict[str, _TargetHandle] = {}
    try:
        for (dsn, schema), references in sorted(grouped.items(), key=lambda item: item[0][1]):
            connection = psycopg.connect(
                dsn,
                autocommit=True,
                connect_timeout=10,
                application_name="agnoclaw-migration-012-target-lifecycle",
                row_factory=dict_row,
            )
            roles = tuple(sorted(reference.role for reference in references))
            endpoint = connection.execute(
                """
                SELECT current_database() AS database_name, oid AS database_oid,
                       current_setting('server_version_num') AS server_version_num
                FROM pg_catalog.pg_database WHERE datname = current_database()
                """
            ).fetchone()
            if endpoint is None:
                connection.close()
                raise Migration012Error(
                    "MIGRATION_POSTGRES_TARGET_ENDPOINT_DRIFT",
                    "A target endpoint identity could not be verified.",
                    target_roles=list(roles),
                )
            planned_endpoints = dict(plan.endpoint_evidence_digests)
            for reference in references:
                observed = _digest(
                    {
                        "database_name": str(endpoint["database_name"]),
                        "database_oid": int(endpoint["database_oid"]),
                        "server_version_num": str(endpoint["server_version_num"]),
                        "schema": reference.schema,
                        "role": reference.role,
                    }
                )
                if observed != planned_endpoints.get(reference.role):
                    connection.close()
                    raise Migration012Error(
                        "MIGRATION_POSTGRES_TARGET_ENDPOINT_DRIFT",
                        "A target endpoint identity differs from the reviewed plan.",
                        target_role=reference.role,
                    )
            lock_key = f"agnoclaw:migration:0.12:{schema}"
            locked = connection.execute(
                "SELECT pg_catalog.pg_try_advisory_lock(pg_catalog.hashtextextended(%s, 0)) "
                "AS locked",
                (lock_key,),
            ).fetchone()
            if not locked or not bool(locked["locked"]):
                connection.close()
                raise Migration012Error(
                    "MIGRATION_POSTGRES_LOCK_UNAVAILABLE",
                    "Another migration owns the target-schema advisory lock.",
                    target_roles=list(roles),
                )
            connection.execute(
                "SELECT pg_catalog.set_config('statement_timeout', %s, false)",
                (f"{statement_timeout_ms}ms",),
            )
            connection.execute(
                "SELECT pg_catalog.set_config('lock_timeout', %s, false)",
                (f"{lock_timeout_ms}ms",),
            )
            connection.autocommit = False
            handle = _TargetHandle(connection=connection, schema=schema, roles=roles, sql=sql)
            handles.append(handle)
            for role in roles:
                role_handles[role] = handle
        return handles, role_handles
    except Migration012Error:
        _close_target_handles(handles)
        raise
    except Exception as exc:
        _close_target_handles(handles)
        raise Migration012Error(
            "MIGRATION_POSTGRES_TARGET_CONNECTION_FAILED",
            "A PostgreSQL migration target could not be locked; driver details were redacted.",
            error_type=type(exc).__name__,
        ) from None


def _close_target_handles(handles: list[_TargetHandle]) -> None:
    for handle in reversed(handles):
        try:
            handle.connection.rollback()
        except Exception:
            pass
        try:
            handle.connection.close()
        except Exception:
            pass


def _open_source_snapshot(
    plan: PostgresMigration012Plan,
    *,
    environment: dict[str, str] | None,
    statement_timeout_ms: int,
    lock_timeout_ms: int,
) -> tuple[Any, Any]:
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise Migration012Error(
            "MIGRATION_POSTGRES_DRIVER_UNAVAILABLE",
            "PostgreSQL service migration requires the agnoclaw postgres extra.",
            install_extra="postgres",
        ) from exc
    connection: Any = None
    try:
        connection = psycopg.connect(
            plan.source.resolve(environment),
            autocommit=True,
            connect_timeout=10,
            application_name="agnoclaw-migration-012-source-lifecycle",
        )
        connection.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        connection.execute(
            "SELECT pg_catalog.set_config('statement_timeout', %s, true)",
            (f"{statement_timeout_ms}ms",),
        )
        connection.execute(
            "SELECT pg_catalog.set_config('lock_timeout', %s, true)",
            (f"{lock_timeout_ms}ms",),
        )
        readonly = connection.execute("SHOW transaction_read_only").fetchone()
        isolation = connection.execute("SHOW transaction_isolation").fetchone()
        if not readonly or str(readonly[0]).lower() != "on":
            raise Migration012Error(
                "MIGRATION_POSTGRES_READ_ONLY_NOT_ENFORCED",
                "The migration source snapshot is not read-only.",
            )
        if not isolation or str(isolation[0]).lower() != "repeatable read":
            raise Migration012Error(
                "MIGRATION_POSTGRES_SNAPSHOT_NOT_ENFORCED",
                "The migration source snapshot is not repeatable-read.",
            )
        endpoint = connection.execute(
            """
            SELECT current_database(), oid, current_setting('server_version_num')
            FROM pg_catalog.pg_database WHERE datname = current_database()
            """
        ).fetchone()
        if endpoint is None:
            raise Migration012Error(
                "MIGRATION_POSTGRES_SOURCE_ENDPOINT_DRIFT",
                "The source endpoint identity could not be verified.",
            )
        observed = _digest(
            {
                "database_name": str(endpoint[0]),
                "database_oid": int(endpoint[1]),
                "server_version_num": str(endpoint[2]),
                "schema": plan.source.schema,
                "role": plan.source.role,
            }
        )
        expected = dict(plan.endpoint_evidence_digests).get("source")
        if observed != expected:
            raise Migration012Error(
                "MIGRATION_POSTGRES_SOURCE_ENDPOINT_DRIFT",
                "The source endpoint identity differs from the reviewed plan.",
            )
        return connection, sql
    except Migration012Error:
        if connection is not None:
            connection.close()
        raise
    except Exception as exc:
        if connection is not None:
            connection.close()
        raise Migration012Error(
            "MIGRATION_POSTGRES_SOURCE_CONNECTION_FAILED",
            "The PostgreSQL migration source could not be read; driver details were redacted.",
            error_type=type(exc).__name__,
        ) from None


def _close_source(connection: Any) -> None:
    if connection is None:
        return
    try:
        connection.rollback()
    except Exception:
        pass
    try:
        connection.close()
    except Exception:
        pass


def _create_control_tables(handle: _TargetHandle) -> None:
    control = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_control")
    provenance = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_provenance")
    handle.connection.execute(
        handle.sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {} (
                migration_id TEXT NOT NULL,
                target_role TEXT NOT NULL CHECK (
                    target_role IN ('target_learning', 'target_runtime')
                ),
                schema_version TEXT NOT NULL,
                plan_digest TEXT NOT NULL,
                transform_digest TEXT NOT NULL,
                phase TEXT NOT NULL CHECK (
                    phase IN ('applying', 'applied', 'verified', 'cutover',
                              'rolling_back', 'rolled_back')
                ),
                revision BIGINT NOT NULL CHECK (revision >= 1),
                expected_rows BIGINT NOT NULL CHECK (expected_rows >= 0),
                applied_rows BIGINT NOT NULL DEFAULT 0 CHECK (applied_rows >= 0),
                inserted_rows BIGINT NOT NULL DEFAULT 0 CHECK (inserted_rows >= 0),
                preexisting_rows BIGINT NOT NULL DEFAULT 0 CHECK (preexisting_rows >= 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                applied_at TIMESTAMPTZ,
                verified_at TIMESTAMPTZ,
                cutover_at TIMESTAMPTZ,
                rolled_back_at TIMESTAMPTZ,
                cutover_receipt_id TEXT,
                cutover_receipt_digest TEXT,
                PRIMARY KEY (migration_id, target_role)
            )
            """
        ).format(control)
    )
    handle.connection.execute(
        handle.sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {} (
                migration_id TEXT NOT NULL,
                target_role TEXT NOT NULL,
                category TEXT NOT NULL,
                target_table TEXT NOT NULL,
                target_identity TEXT NOT NULL,
                target_identity_digest TEXT NOT NULL,
                source_table TEXT NOT NULL,
                source_digest TEXT NOT NULL,
                target_digest TEXT NOT NULL,
                disposition TEXT NOT NULL CHECK (
                    disposition IN ('inserted', 'preexisting_identical')
                ),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                rolled_back_at TIMESTAMPTZ,
                PRIMARY KEY (migration_id, target_role, target_table, target_identity_digest),
                UNIQUE (target_role, target_table, target_identity_digest),
                FOREIGN KEY (migration_id, target_role)
                    REFERENCES {} (migration_id, target_role) ON DELETE RESTRICT
            )
            """
        ).format(provenance, control)
    )


def _create_role_tables(handle: _TargetHandle, role: str) -> None:
    if role == "target_learning":
        learning = _qualified(handle.sql, handle.schema, "agno_learnings")
        quarantine = _qualified(
            handle.sql, handle.schema, "agnoclaw_migration_012_learning_quarantine"
        )
        handle.connection.execute(
            handle.sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    learning_id TEXT PRIMARY KEY NOT NULL,
                    learning_type TEXT NOT NULL,
                    namespace TEXT,
                    user_id TEXT,
                    agent_id TEXT,
                    team_id TEXT,
                    workflow_id TEXT,
                    session_id TEXT,
                    entity_id TEXT,
                    entity_type TEXT,
                    content JSONB NOT NULL,
                    metadata JSONB,
                    created_at BIGINT NOT NULL,
                    updated_at BIGINT
                )
                """
            ).format(learning)
        )
        handle.connection.execute(
            handle.sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    source_digest TEXT PRIMARY KEY,
                    migration_id TEXT NOT NULL,
                    source_table TEXT NOT NULL,
                    learning_type TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_tenant_id TEXT,
                    target_namespace TEXT,
                    record_json TEXT NOT NULL
                )
                """
            ).format(quarantine)
        )
    elif role == "target_runtime":
        jobs = _qualified(handle.sql, handle.schema, "runtime_scheduler_jobs")
        history = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_schedule_history")
        handle.connection.execute(
            handle.sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    job_name TEXT PRIMARY KEY,
                    revision BIGINT NOT NULL CHECK (revision >= 1),
                    enabled BOOLEAN NOT NULL,
                    next_run_at TIMESTAMPTZ,
                    job_digest TEXT NOT NULL,
                    job_json TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            ).format(jobs)
        )
        handle.connection.execute(
            handle.sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    source_digest TEXT PRIMARY KEY,
                    migration_id TEXT NOT NULL,
                    source_run_id TEXT NOT NULL,
                    source_schedule_id TEXT NOT NULL,
                    target_job_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL
                )
                """
            ).format(history)
        )
    else:
        raise ValueError("unsupported target role")


def _required_columns(handle: _TargetHandle, table: str) -> frozenset[str]:
    rows = handle.connection.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (handle.schema, table),
    ).fetchall()
    return frozenset(str(row["column_name"]) for row in rows)


def _verify_target_table_shapes(handle: _TargetHandle) -> None:
    common = {
        "agnoclaw_migration_012_control": {
            "migration_id",
            "target_role",
            "schema_version",
            "plan_digest",
            "transform_digest",
            "phase",
            "revision",
            "expected_rows",
            "applied_rows",
            "inserted_rows",
            "preexisting_rows",
            "created_at",
            "updated_at",
            "applied_at",
            "verified_at",
            "cutover_at",
            "rolled_back_at",
            "cutover_receipt_id",
            "cutover_receipt_digest",
        },
        "agnoclaw_migration_012_provenance": {
            "migration_id",
            "target_role",
            "category",
            "target_table",
            "target_identity",
            "target_identity_digest",
            "source_table",
            "source_digest",
            "target_digest",
            "disposition",
            "created_at",
            "rolled_back_at",
        },
    }
    expected = dict(common)
    for spec in _TABLE_SPECS.values():
        if spec.target_role in handle.roles:
            expected[spec.table_name] = set(spec.columns)
    for table, required in expected.items():
        observed = _required_columns(handle, table)
        if not set(required).issubset(observed):
            raise Migration012Error(
                "MIGRATION_POSTGRES_TARGET_SCHEMA_INVALID",
                "A target table lacks required migration columns.",
                target_roles=list(handle.roles),
                table_name=table,
            )


def _control_row(handle: _TargetHandle, migration_id: str, role: str) -> dict[str, Any] | None:
    if not _table_exists(
        handle.connection,
        schema=handle.schema,
        table="agnoclaw_migration_012_control",
    ):
        handle.connection.rollback()
        return None
    control = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_control")
    row = handle.connection.execute(
        handle.sql.SQL("SELECT * FROM {} WHERE migration_id = %s AND target_role = %s").format(
            control
        ),
        (migration_id, role),
    ).fetchone()
    handle.connection.rollback()
    return dict(row) if row is not None else None


def _validate_control_row(
    row: dict[str, Any],
    *,
    plan: PostgresMigration012Plan,
    role: str,
    transform_digest: str,
) -> None:
    expected = {
        "migration_id": plan.migration_id,
        "target_role": role,
        "schema_version": POSTGRES_MIGRATION_012_LIFECYCLE_SCHEMA_VERSION,
        "plan_digest": plan.plan_digest,
        "transform_digest": transform_digest,
    }
    if any(str(row.get(name)) != value for name, value in expected.items()):
        raise Migration012Error(
            "MIGRATION_POSTGRES_CONTROL_CONFLICT",
            "A target control record belongs to different reviewed migration evidence.",
            target_role=role,
        )
    if str(row.get("phase")) not in {item.value for item in PostgresMigration012Phase}:
        raise Migration012Error(
            "MIGRATION_POSTGRES_CONTROL_CONFLICT",
            "A target control record has an unsupported lifecycle phase.",
            target_role=role,
        )


def _expected_role_rows(report: PostgresMigration012TransformReport, role: str) -> int:
    counts = dict(report.counts)
    if role == "target_learning":
        return counts["learning_personal"] + counts["learning_quarantine"]
    return counts["schedule_job"] + counts["schedule_history"]


def _initialize_target_controls(
    *,
    handles: list[_TargetHandle],
    plan: PostgresMigration012Plan,
    report: PostgresMigration012TransformReport,
    checkpoint_observer: _CheckpointObserver | None = None,
) -> None:
    for checkpoint, handle in enumerate(handles, start=1):
        _create_control_tables(handle)
        for role in handle.roles:
            _create_role_tables(handle, role)
        _verify_target_table_shapes(handle)
        control = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_control")
        for role in handle.roles:
            handle.connection.execute(
                handle.sql.SQL(
                    """
                    INSERT INTO {}(
                        migration_id, target_role, schema_version, plan_digest,
                        transform_digest, phase, revision, expected_rows
                    ) VALUES (%s, %s, %s, %s, %s, 'applying', 1, %s)
                    ON CONFLICT (migration_id, target_role) DO NOTHING
                    """
                ).format(control),
                (
                    plan.migration_id,
                    role,
                    POSTGRES_MIGRATION_012_LIFECYCLE_SCHEMA_VERSION,
                    plan.plan_digest,
                    report.transform_digest,
                    _expected_role_rows(report, role),
                ),
            )
            row = handle.connection.execute(
                handle.sql.SQL(
                    "SELECT * FROM {} WHERE migration_id = %s AND target_role = %s FOR UPDATE"
                ).format(control),
                (plan.migration_id, role),
            ).fetchone()
            if row is None:
                raise Migration012Error(
                    "MIGRATION_POSTGRES_CONTROL_CONFLICT",
                    "A target control record could not be initialized.",
                    target_role=role,
                )
            _validate_control_row(
                dict(row),
                plan=plan,
                role=role,
                transform_digest=report.transform_digest,
            )
            if int(row["expected_rows"]) != _expected_role_rows(report, role):
                raise Migration012Error(
                    "MIGRATION_POSTGRES_CONTROL_CONFLICT",
                    "A target control record has a different expected row count.",
                    target_role=role,
                )
            if str(row["phase"]) in {
                PostgresMigration012Phase.ROLLING_BACK.value,
                PostgresMigration012Phase.ROLLED_BACK.value,
            }:
                raise Migration012Error(
                    "MIGRATION_POSTGRES_PHASE_CONFLICT",
                    "A rolling-back or rolled-back migration cannot be applied.",
                    target_role=role,
                    phase=str(row["phase"]),
                )
        if checkpoint_observer is not None:
            checkpoint_observer("apply", "before_initialize_commit", handle.roles, checkpoint)
        handle.connection.commit()
        if checkpoint_observer is not None:
            checkpoint_observer("apply", "after_initialize_commit", handle.roles, checkpoint)


def _canonical_timestamp(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        return value
    if parsed.tzinfo is None:
        raise ValueError("migration timestamps must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _normalize_values(values: dict[str, Any]) -> dict[str, Any]:
    return {
        name: (_canonical_timestamp(value) if name in _TIMESTAMP_COLUMNS else value)
        for name, value in values.items()
    }


def _fetch_target_values(
    handle: _TargetHandle,
    spec: _TableSpec,
    identity: str,
    *,
    for_update: bool,
) -> dict[str, Any] | None:
    table = _qualified(handle.sql, handle.schema, spec.table_name)
    columns = handle.sql.SQL(", ").join(handle.sql.Identifier(name) for name in spec.columns)
    suffix = handle.sql.SQL(" FOR UPDATE") if for_update else handle.sql.SQL("")
    row = handle.connection.execute(
        handle.sql.SQL("SELECT {} FROM {} WHERE {} = %s{}").format(
            columns,
            table,
            handle.sql.Identifier(spec.identity_column),
            suffix,
        ),
        (identity,),
    ).fetchone()
    return dict(row) if row is not None else None


def _insert_target_row(
    handle: _TargetHandle,
    spec: _TableSpec,
    values: dict[str, Any],
) -> bool:
    from psycopg.types.json import Jsonb

    table = _qualified(handle.sql, handle.schema, spec.table_name)
    columns = handle.sql.SQL(", ").join(handle.sql.Identifier(name) for name in spec.columns)
    placeholders = handle.sql.SQL(", ").join(handle.sql.Placeholder() for _ in spec.columns)
    parameters = tuple(
        Jsonb(values[name])
        if name in spec.jsonb_columns and values[name] is not None
        else values[name]
        for name in spec.columns
    )
    row = handle.connection.execute(
        handle.sql.SQL(
            "INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO NOTHING RETURNING {}"
        ).format(
            table,
            columns,
            placeholders,
            handle.sql.Identifier(spec.identity_column),
            handle.sql.Identifier(spec.identity_column),
        ),
        parameters,
    ).fetchone()
    return row is not None


def _provenance_for_identity(
    handle: _TargetHandle,
    *,
    role: str,
    transformed: _TransformedRow,
    for_update: bool,
) -> dict[str, Any] | None:
    provenance = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_provenance")
    suffix = handle.sql.SQL(" FOR UPDATE") if for_update else handle.sql.SQL("")
    row = handle.connection.execute(
        handle.sql.SQL(
            "SELECT * FROM {} WHERE target_role = %s AND target_table = %s "
            "AND target_identity_digest = %s{}"
        ).format(provenance, suffix),
        (role, transformed.target_table, _digest(transformed.target_identity)),
    ).fetchone()
    return dict(row) if row is not None else None


def _validate_provenance(
    row: dict[str, Any],
    *,
    plan: PostgresMigration012Plan,
    transformed: _TransformedRow,
) -> None:
    expected = {
        "migration_id": plan.migration_id,
        "target_role": transformed.target_role,
        "category": transformed.category,
        "target_table": transformed.target_table,
        "target_identity": transformed.target_identity,
        "target_identity_digest": _digest(transformed.target_identity),
        "source_table": transformed.source_table,
        "source_digest": transformed.source_digest,
        "target_digest": transformed.target_digest,
    }
    if any(str(row.get(name)) != value for name, value in expected.items()):
        raise Migration012Error(
            "MIGRATION_POSTGRES_PROVENANCE_CONFLICT",
            "A target identity is owned by different migration provenance.",
            target_role=transformed.target_role,
            target_table=transformed.target_table,
            target_identity_digest=_digest(transformed.target_identity),
        )


def _insert_provenance(
    handle: _TargetHandle,
    *,
    plan: PostgresMigration012Plan,
    transformed: _TransformedRow,
    disposition: str,
) -> None:
    provenance = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_provenance")
    handle.connection.execute(
        handle.sql.SQL(
            """
            INSERT INTO {}(
                migration_id, target_role, category, target_table, target_identity,
                target_identity_digest, source_table, source_digest, target_digest,
                disposition
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """
        ).format(provenance),
        (
            plan.migration_id,
            transformed.target_role,
            transformed.category,
            transformed.target_table,
            transformed.target_identity,
            _digest(transformed.target_identity),
            transformed.source_table,
            transformed.source_digest,
            transformed.target_digest,
            disposition,
        ),
    )
    observed = _provenance_for_identity(
        handle,
        role=transformed.target_role,
        transformed=transformed,
        for_update=True,
    )
    if observed is None:
        raise Migration012Error(
            "MIGRATION_POSTGRES_PROVENANCE_CONFLICT",
            "Target provenance could not be recorded.",
            target_role=transformed.target_role,
            target_table=transformed.target_table,
        )
    _validate_provenance(observed, plan=plan, transformed=transformed)
    if str(observed.get("disposition")) != disposition:
        raise Migration012Error(
            "MIGRATION_POSTGRES_PROVENANCE_CONFLICT",
            "Target provenance has a different insert classification.",
            target_role=transformed.target_role,
            target_table=transformed.target_table,
        )


def _target_values_match(observed: dict[str, Any], transformed: _TransformedRow) -> bool:
    spec = _TABLE_SPECS[transformed.target_table]
    expected = {name: transformed.values[name] for name in spec.columns}
    actual = {name: observed.get(name) for name in spec.columns}
    return _normalize_values(actual) == _normalize_values(expected)


def _control_phase(handle: _TargetHandle, plan: PostgresMigration012Plan, role: str) -> str:
    control = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_control")
    row = handle.connection.execute(
        handle.sql.SQL(
            "SELECT phase FROM {} WHERE migration_id = %s AND target_role = %s FOR UPDATE"
        ).format(control),
        (plan.migration_id, role),
    ).fetchone()
    if row is None:
        raise Migration012Error(
            "MIGRATION_POSTGRES_CONTROL_CONFLICT",
            "Target control state is missing.",
            target_role=role,
        )
    return str(row["phase"])


def _apply_transformed_row(
    *,
    handle: _TargetHandle,
    plan: PostgresMigration012Plan,
    transformed: _TransformedRow,
) -> None:
    spec = _TABLE_SPECS.get(transformed.target_table)
    if spec is None or spec.target_role != transformed.target_role:
        raise Migration012Error(
            "MIGRATION_POSTGRES_TARGET_SCHEMA_INVALID",
            "A transformed row targets an unsupported table.",
            target_role=transformed.target_role,
            target_table=transformed.target_table,
        )
    phase = _control_phase(handle, plan, transformed.target_role)
    provenance = _provenance_for_identity(
        handle,
        role=transformed.target_role,
        transformed=transformed,
        for_update=True,
    )
    observed = _fetch_target_values(
        handle,
        spec,
        transformed.target_identity,
        for_update=True,
    )
    if provenance is not None:
        _validate_provenance(provenance, plan=plan, transformed=transformed)
        if provenance.get("rolled_back_at") is not None:
            raise Migration012Error(
                "MIGRATION_POSTGRES_PHASE_CONFLICT",
                "A rolled-back target identity cannot be applied again.",
                target_role=transformed.target_role,
                target_table=transformed.target_table,
            )
        if observed is None or not _target_values_match(observed, transformed):
            raise Migration012Error(
                "MIGRATION_POSTGRES_TARGET_DRIFT",
                "A provenance-owned target row differs from reviewed transformation evidence.",
                target_role=transformed.target_role,
                target_table=transformed.target_table,
                target_identity_digest=_digest(transformed.target_identity),
            )
        return
    if phase != PostgresMigration012Phase.APPLYING.value:
        raise Migration012Error(
            "MIGRATION_POSTGRES_PROVENANCE_MISSING",
            "An applied target identity lacks migration provenance.",
            target_role=transformed.target_role,
            target_table=transformed.target_table,
        )
    inserted = False
    if observed is None:
        inserted = _insert_target_row(handle, spec, transformed.values)
        observed = _fetch_target_values(
            handle,
            spec,
            transformed.target_identity,
            for_update=True,
        )
    if observed is None or not _target_values_match(observed, transformed):
        raise Migration012Error(
            "MIGRATION_POSTGRES_TARGET_CONFLICT",
            "A target identity contains data different from the reviewed transformation.",
            target_role=transformed.target_role,
            target_table=transformed.target_table,
            target_identity_digest=_digest(transformed.target_identity),
        )
    _insert_provenance(
        handle,
        plan=plan,
        transformed=transformed,
        disposition="inserted" if inserted else "preexisting_identical",
    )


def _provenance_counts(
    handle: _TargetHandle,
    *,
    plan: PostgresMigration012Plan,
    role: str,
) -> tuple[tuple[str, str, str, int], ...]:
    provenance = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_provenance")
    rows = handle.connection.execute(
        handle.sql.SQL(
            "SELECT category, disposition, count(*) AS count FROM {} "
            "WHERE migration_id = %s AND target_role = %s "
            "GROUP BY category, disposition ORDER BY category, disposition"
        ).format(provenance),
        (plan.migration_id, role),
    ).fetchall()
    return tuple(
        (role, str(row["category"]), str(row["disposition"]), int(row["count"])) for row in rows
    )


def _provenance_total(handle: _TargetHandle, *, plan: PostgresMigration012Plan, role: str) -> int:
    return sum(count for _, _, _, count in _provenance_counts(handle, plan=plan, role=role))


def _checkpoint_control(handle: _TargetHandle, plan: PostgresMigration012Plan) -> None:
    control = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_control")
    for role in handle.roles:
        phase = _control_phase(handle, plan, role)
        if phase != PostgresMigration012Phase.APPLYING.value:
            continue
        counts = _provenance_counts(handle, plan=plan, role=role)
        inserted = sum(count for _, _, disposition, count in counts if disposition == "inserted")
        preexisting = sum(
            count for _, _, disposition, count in counts if disposition == "preexisting_identical"
        )
        handle.connection.execute(
            handle.sql.SQL(
                "UPDATE {} SET revision = revision + 1, applied_rows = %s, "
                "inserted_rows = %s, preexisting_rows = %s, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE migration_id = %s AND target_role = %s AND phase = 'applying'"
            ).format(control),
            (inserted + preexisting, inserted, preexisting, plan.migration_id, role),
        )


def _finalize_apply(
    *,
    handles: list[_TargetHandle],
    plan: PostgresMigration012Plan,
    report: PostgresMigration012TransformReport,
) -> None:
    for handle in handles:
        _checkpoint_control(handle, plan)
        control = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_control")
        for role in handle.roles:
            total = _provenance_total(handle, plan=plan, role=role)
            expected = _expected_role_rows(report, role)
            if total != expected:
                raise Migration012Error(
                    "MIGRATION_POSTGRES_APPLY_INCOMPLETE",
                    "Target provenance does not cover every transformed row.",
                    target_role=role,
                    expected_rows=expected,
                    observed_rows=total,
                )
            handle.connection.execute(
                handle.sql.SQL(
                    "UPDATE {} SET phase = 'applied', revision = revision + 1, "
                    "applied_rows = %s, applied_at = COALESCE(applied_at, CURRENT_TIMESTAMP), "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE migration_id = %s AND target_role = %s AND phase = 'applying'"
                ).format(control),
                (total, plan.migration_id, role),
            )
        handle.connection.commit()


class _BatchedConsumer:
    def __init__(
        self,
        *,
        handles: list[_TargetHandle],
        role_handles: dict[str, _TargetHandle],
        plan: PostgresMigration012Plan,
        write_batch_size: int,
        operation: Callable[[_TargetHandle, _TransformedRow], None],
        role_filter: str | None = None,
        operation_name: str = "apply",
        checkpoint_observer: _CheckpointObserver | None = None,
    ) -> None:
        self._handles = handles
        self._role_handles = role_handles
        self._plan = plan
        self._write_batch_size = write_batch_size
        self._operation = operation
        self._role_filter = role_filter
        self._pending = {id(handle): 0 for handle in handles}
        self._operation_name = operation_name
        self._checkpoint_observer = checkpoint_observer
        self._checkpoint = 0

    def _commit(self, handle: _TargetHandle) -> None:
        self._checkpoint += 1
        if self._checkpoint_observer is not None:
            self._checkpoint_observer(
                self._operation_name,
                "before_batch_commit",
                handle.roles,
                self._checkpoint,
            )
        handle.connection.commit()
        if self._checkpoint_observer is not None:
            self._checkpoint_observer(
                self._operation_name,
                "after_batch_commit",
                handle.roles,
                self._checkpoint,
            )

    def __call__(self, transformed: _TransformedRow) -> None:
        if self._role_filter is not None and transformed.target_role != self._role_filter:
            return
        handle = self._role_handles[transformed.target_role]
        self._operation(handle, transformed)
        key = id(handle)
        self._pending[key] += 1
        if self._pending[key] >= self._write_batch_size:
            _checkpoint_control(handle, self._plan)
            self._commit(handle)
            self._pending[key] = 0

    def flush(self) -> None:
        for handle in self._handles:
            if self._pending[id(handle)]:
                _checkpoint_control(handle, self._plan)
                self._commit(handle)
                self._pending[id(handle)] = 0


def _compile_snapshot(
    *,
    connection: Any,
    sql: Any,
    plan: PostgresMigration012Plan,
    schedule_map: PostgresScheduleMap,
    max_rows_per_table: int,
    read_batch_size: int,
    max_row_bytes: int,
    consumer: Callable[[_TransformedRow], None] | None = None,
) -> PostgresMigration012TransformReport:
    return _compile_postgres_migration_012_source_snapshot(
        connection=connection,
        sql=sql,
        plan=plan,
        schedule_map=schedule_map,
        max_rows_per_table=max_rows_per_table,
        batch_size=read_batch_size,
        max_row_bytes=max_row_bytes,
        row_consumer=consumer,
    )


def _require_transform(report: PostgresMigration012TransformReport, expected: str) -> None:
    if report.transform_digest != expected:
        raise Migration012Error(
            "MIGRATION_POSTGRES_TRANSFORM_CONFIRMATION_MISMATCH",
            "Current source transformations differ from the reviewed preview digest.",
        )


def _receipt(
    *,
    operation: str,
    handles: list[_TargetHandle],
    role_handles: dict[str, _TargetHandle],
    plan: PostgresMigration012Plan,
    transform_digest: str,
) -> PostgresMigration012LifecycleReceipt:
    phases: list[tuple[str, str]] = []
    counts: list[tuple[str, str, str, int]] = []
    for role in _TARGET_ROLES:
        handle = role_handles[role]
        control = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_control")
        row = handle.connection.execute(
            handle.sql.SQL(
                "SELECT phase FROM {} WHERE migration_id = %s AND target_role = %s"
            ).format(control),
            (plan.migration_id, role),
        ).fetchone()
        if row is None:
            raise Migration012Error(
                "MIGRATION_POSTGRES_CONTROL_CONFLICT",
                "A lifecycle receipt cannot be produced without both target controls.",
                target_role=role,
            )
        phases.append((role, str(row["phase"])))
        counts.extend(_provenance_counts(handle, plan=plan, role=role))
    for handle in handles:
        handle.connection.rollback()
    rollback_available = all(
        phase != PostgresMigration012Phase.ROLLED_BACK.value for _, phase in phases
    )
    value = PostgresMigration012LifecycleReceipt(
        operation=operation,
        migration_id=plan.migration_id,
        plan_digest=plan.plan_digest,
        transform_digest=transform_digest,
        generated_at=datetime.now(UTC).isoformat(),
        role_phases=tuple(phases),
        counts=tuple(sorted(counts)),
        rollback_available=rollback_available,
    )
    return replace(value, receipt_digest=value.computed_receipt_digest)


def apply_postgres_migration_012(
    *,
    plan: PostgresMigration012Plan,
    schedule_map: PostgresScheduleMap,
    confirm_plan_digest: str,
    confirm_transform_digest: str,
    confirm_backup_receipt_digest: str,
    confirm_writer_fence_plan: str,
    writers_stopped: bool,
    environment: dict[str, str] | None = None,
    statement_timeout_ms: int = 60_000,
    lock_timeout_ms: int = 2_000,
    max_rows_per_table: int = 10_000_000,
    read_batch_size: int = 1_000,
    write_batch_size: int = 1_000,
    max_row_bytes: int = 16 * 1024 * 1024,
    _checkpoint_observer: _CheckpointObserver | None = None,
) -> PostgresMigration012LifecycleReceipt:
    """Idempotently write reviewed transformations with durable provenance."""
    _validate_bounds(
        statement_timeout_ms=statement_timeout_ms,
        lock_timeout_ms=lock_timeout_ms,
        max_rows_per_table=max_rows_per_table,
        read_batch_size=read_batch_size,
        write_batch_size=write_batch_size,
        max_row_bytes=max_row_bytes,
    )
    _validate_plan_and_confirmations(
        plan=plan,
        schedule_map=schedule_map,
        confirm_plan_digest=confirm_plan_digest,
        confirm_transform_digest=confirm_transform_digest,
        confirm_writer_fence_plan=confirm_writer_fence_plan,
        writers_stopped=writers_stopped,
        confirm_backup_receipt_digest=confirm_backup_receipt_digest,
    )
    if _checkpoint_observer is not None and not callable(_checkpoint_observer):
        raise ValueError("_checkpoint_observer must be callable")
    handles: list[_TargetHandle] = []
    source_connection: Any = None
    try:
        handles, role_handles = _connect_target_handles(
            plan,
            environment=environment,
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        )
        existing = {
            role: _control_row(role_handles[role], plan.migration_id, role)
            for role in _TARGET_ROLES
        }
        if not any(existing.values()):
            preview = preview_postgres_migration_012_transforms(
                plan=plan,
                schedule_map=schedule_map,
                environment=environment,
                statement_timeout_ms=statement_timeout_ms,
                lock_timeout_ms=lock_timeout_ms,
                max_rows_per_table=max_rows_per_table,
                batch_size=read_batch_size,
                max_row_bytes=max_row_bytes,
            )
            _require_transform(preview, confirm_transform_digest)
        else:
            for role, row in existing.items():
                if row is not None:
                    _validate_control_row(
                        row,
                        plan=plan,
                        role=role,
                        transform_digest=confirm_transform_digest,
                    )
        source_connection, source_sql = _open_source_snapshot(
            plan,
            environment=environment,
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        )
        report = _compile_snapshot(
            connection=source_connection,
            sql=source_sql,
            plan=plan,
            schedule_map=schedule_map,
            max_rows_per_table=max_rows_per_table,
            read_batch_size=read_batch_size,
            max_row_bytes=max_row_bytes,
        )
        _require_transform(report, confirm_transform_digest)
        _initialize_target_controls(
            handles=handles,
            plan=plan,
            report=report,
            checkpoint_observer=_checkpoint_observer,
        )
        consumer = _BatchedConsumer(
            handles=handles,
            role_handles=role_handles,
            plan=plan,
            write_batch_size=write_batch_size,
            operation=lambda handle, row: _apply_transformed_row(
                handle=handle,
                plan=plan,
                transformed=row,
            ),
            operation_name="apply",
            checkpoint_observer=_checkpoint_observer,
        )
        applied_report = _compile_snapshot(
            connection=source_connection,
            sql=source_sql,
            plan=plan,
            schedule_map=schedule_map,
            max_rows_per_table=max_rows_per_table,
            read_batch_size=read_batch_size,
            max_row_bytes=max_row_bytes,
            consumer=consumer,
        )
        consumer.flush()
        _require_transform(applied_report, confirm_transform_digest)
        _verify_unowned_target_evidence(
            role_handles=role_handles,
            plan=plan,
            max_rows_per_table=max_rows_per_table,
            read_batch_size=read_batch_size,
            max_row_bytes=max_row_bytes,
        )
        _finalize_apply(handles=handles, plan=plan, report=applied_report)
        return _receipt(
            operation="apply",
            handles=handles,
            role_handles=role_handles,
            plan=plan,
            transform_digest=confirm_transform_digest,
        )
    except Migration012Error:
        raise
    except Exception as exc:
        raise Migration012Error(
            "MIGRATION_POSTGRES_APPLY_FAILED",
            "PostgreSQL migration apply failed; driver details were redacted.",
            error_type=type(exc).__name__,
        ) from None
    finally:
        _close_source(source_connection)
        _close_target_handles(handles)


def _verify_transformed_row(
    *,
    handle: _TargetHandle,
    plan: PostgresMigration012Plan,
    transformed: _TransformedRow,
) -> None:
    spec = _TABLE_SPECS[transformed.target_table]
    provenance = _provenance_for_identity(
        handle,
        role=transformed.target_role,
        transformed=transformed,
        for_update=False,
    )
    if provenance is None:
        raise Migration012Error(
            "MIGRATION_POSTGRES_PROVENANCE_MISSING",
            "A transformed target identity lacks migration provenance.",
            target_role=transformed.target_role,
            target_table=transformed.target_table,
        )
    _validate_provenance(provenance, plan=plan, transformed=transformed)
    if provenance.get("rolled_back_at") is not None:
        raise Migration012Error(
            "MIGRATION_POSTGRES_PHASE_CONFLICT",
            "Rolled-back provenance cannot be verified as active.",
            target_role=transformed.target_role,
        )
    observed = _fetch_target_values(
        handle,
        spec,
        transformed.target_identity,
        for_update=False,
    )
    if observed is None or not _target_values_match(observed, transformed):
        raise Migration012Error(
            "MIGRATION_POSTGRES_VERIFICATION_FAILED",
            "A target row differs from reviewed transformation evidence.",
            target_role=transformed.target_role,
            target_table=transformed.target_table,
            target_identity_digest=_digest(transformed.target_identity),
        )


def _provenance_identity_digest(
    *,
    role_handles: dict[str, _TargetHandle],
    plan: PostgresMigration012Plan,
) -> tuple[int, str]:
    evidence = _StreamDigest()
    for role in _TARGET_ROLES:
        handle = role_handles[role]
        provenance = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_provenance")
        rows = handle.connection.execute(
            handle.sql.SQL(
                "SELECT target_role, target_table, target_identity, source_digest, "
                "target_digest FROM {} WHERE migration_id = %s AND target_role = %s "
                "ORDER BY target_role, target_table, target_identity"
            ).format(provenance),
            (plan.migration_id, role),
        ).fetchall()
        for row in rows:
            evidence.update(
                {
                    "target_role": str(row["target_role"]),
                    "target_table": str(row["target_table"]),
                    "target_identity_digest": _digest(str(row["target_identity"])),
                    "source_digest": str(row["source_digest"]),
                    "target_digest": str(row["target_digest"]),
                }
            )
    return evidence.count, evidence.digest


def _baseline_table_evidence(
    plan: PostgresMigration012Plan,
    *,
    role: str,
    table: str,
) -> Any:
    matches = [
        item for item in plan.table_evidence if item.role == role and item.table_name == table
    ]
    if len(matches) != 1:
        raise Migration012Error(
            "MIGRATION_POSTGRES_PLAN_EVIDENCE_INVALID",
            "The reviewed plan lacks exact target-table evidence.",
            target_role=role,
            table_name=table,
        )
    return matches[0]


def _stream_unowned_target_table(
    handle: _TargetHandle,
    *,
    plan: PostgresMigration012Plan,
    role: str,
    table: str,
    max_rows_per_table: int,
    read_batch_size: int,
    max_row_bytes: int,
) -> tuple[int, str]:
    qualified = _qualified(handle.sql, handle.schema, table)
    provenance = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_provenance")
    spec = _TABLE_SPECS.get(table)
    where = handle.sql.SQL("")
    parameters: tuple[Any, ...] = ()
    if spec is not None:
        where = handle.sql.SQL(
            " WHERE NOT EXISTS (SELECT 1 FROM {} AS migration_provenance "
            "WHERE migration_provenance.migration_id = %s "
            "AND migration_provenance.target_role = %s "
            "AND migration_provenance.target_table = %s "
            "AND migration_provenance.disposition = 'inserted' "
            "AND migration_provenance.target_identity = (target_row.{})::text)"
        ).format(provenance, handle.sql.Identifier(spec.identity_column))
        parameters = (plan.migration_id, role, table)
    query = handle.sql.SQL(
        "SELECT to_jsonb(target_row)::text AS row_json FROM {} AS target_row{} "
        'ORDER BY (to_jsonb(target_row)::text) COLLATE "C"'
    ).format(qualified, where)
    cursor = handle.connection.cursor(name=f"agnoclaw_migration_unowned_{role}_{table}")
    cursor.itersize = read_batch_size
    count = 0
    hasher = hashlib.sha256()
    try:
        cursor.execute(query, parameters)
        while rows := cursor.fetchmany(read_batch_size):
            for row in rows:
                raw = str(row["row_json"]).encode("utf-8")
                if len(raw) > max_row_bytes:
                    raise Migration012Error(
                        "MIGRATION_POSTGRES_ROW_LIMIT_EXCEEDED",
                        "A target row exceeds the verification byte limit.",
                        target_role=role,
                        table_name=table,
                        max_row_bytes=max_row_bytes,
                    )
                row_digest = hashlib.sha256(raw).digest()
                hasher.update(len(row_digest).to_bytes(4, "big"))
                hasher.update(row_digest)
                count += 1
                if count > max_rows_per_table:
                    raise Migration012Error(
                        "MIGRATION_POSTGRES_ROW_LIMIT_EXCEEDED",
                        "A target table exceeds the verification row limit.",
                        target_role=role,
                        table_name=table,
                        max_rows_per_table=max_rows_per_table,
                    )
    finally:
        cursor.close()
    return count, "sha256:" + hasher.hexdigest()


def _verify_unowned_target_evidence(
    *,
    role_handles: dict[str, _TargetHandle],
    plan: PostgresMigration012Plan,
    max_rows_per_table: int,
    read_batch_size: int,
    max_row_bytes: int,
) -> None:
    excluded = {"agnoclaw_migration_012_control", "agnoclaw_migration_012_provenance"}
    empty_digest = "sha256:" + hashlib.sha256().hexdigest()
    for role in _TARGET_ROLES:
        handle = role_handles[role]
        tables = sorted(
            item.table_name
            for item in plan.table_evidence
            if item.role == role and item.table_name not in excluded
        )
        for table in tables:
            baseline = _baseline_table_evidence(plan, role=role, table=table)
            exists = _table_exists(
                handle.connection,
                schema=handle.schema,
                table=table,
            )
            touched = table in _TABLE_SPECS
            if not exists:
                if baseline.exists or touched:
                    raise Migration012Error(
                        "MIGRATION_POSTGRES_TARGET_DRIFT",
                        "A required target table is missing during verification.",
                        target_role=role,
                        table_name=table,
                    )
                continue
            if not baseline.exists and not touched:
                raise Migration012Error(
                    "MIGRATION_POSTGRES_TARGET_DRIFT",
                    "An unowned target table appeared after plan review.",
                    target_role=role,
                    table_name=table,
                )
            count, logical_digest = _stream_unowned_target_table(
                handle,
                plan=plan,
                role=role,
                table=table,
                max_rows_per_table=max_rows_per_table,
                read_batch_size=read_batch_size,
                max_row_bytes=max_row_bytes,
            )
            expected_count = baseline.row_count if baseline.exists else 0
            expected_digest = baseline.logical_digest if baseline.exists else empty_digest
            if count != expected_count or logical_digest != expected_digest:
                raise Migration012Error(
                    "MIGRATION_POSTGRES_UNOWNED_TARGET_DRIFT",
                    "Target data outside migration ownership changed after plan review.",
                    target_role=role,
                    table_name=table,
                    expected_rows=expected_count,
                    observed_rows=count,
                )


def _mark_verified(
    *,
    handles: list[_TargetHandle],
    role_handles: dict[str, _TargetHandle],
    plan: PostgresMigration012Plan,
    report: PostgresMigration012TransformReport,
    record_phase: bool,
) -> None:
    count, digest = _provenance_identity_digest(role_handles=role_handles, plan=plan)
    if count != sum(dict(report.counts).values()) or digest != report.target_identity_digest:
        raise Migration012Error(
            "MIGRATION_POSTGRES_VERIFICATION_FAILED",
            "Target provenance identity evidence is incomplete or changed.",
        )
    for handle in handles:
        control = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_control")
        for role in handle.roles:
            phase = _control_phase(handle, plan, role)
            if phase not in {
                PostgresMigration012Phase.APPLIED.value,
                PostgresMigration012Phase.VERIFIED.value,
                PostgresMigration012Phase.CUTOVER.value,
            }:
                raise Migration012Error(
                    "MIGRATION_POSTGRES_PHASE_CONFLICT",
                    "Only applied migration targets can be verified.",
                    target_role=role,
                    phase=phase,
                )
            if not record_phase:
                continue
            handle.connection.execute(
                handle.sql.SQL(
                    "UPDATE {} SET phase = 'verified', revision = revision + 1, "
                    "verified_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                    "WHERE migration_id = %s AND target_role = %s AND phase = 'applied'"
                ).format(control),
                (plan.migration_id, role),
            )
        handle.connection.commit()


def _verify_with_handles(
    *,
    handles: list[_TargetHandle],
    role_handles: dict[str, _TargetHandle],
    plan: PostgresMigration012Plan,
    schedule_map: PostgresScheduleMap,
    transform_digest: str,
    environment: dict[str, str] | None,
    statement_timeout_ms: int,
    lock_timeout_ms: int,
    max_rows_per_table: int,
    read_batch_size: int,
    max_row_bytes: int,
    record_phase: bool,
) -> PostgresMigration012TransformReport:
    for role in _TARGET_ROLES:
        row = _control_row(role_handles[role], plan.migration_id, role)
        if row is None:
            raise Migration012Error(
                "MIGRATION_POSTGRES_CONTROL_CONFLICT",
                "Verification requires both target control records.",
                target_role=role,
            )
        _validate_control_row(
            row,
            plan=plan,
            role=role,
            transform_digest=transform_digest,
        )
    source_connection: Any = None
    try:
        source_connection, source_sql = _open_source_snapshot(
            plan,
            environment=environment,
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        )
        report = _compile_snapshot(
            connection=source_connection,
            sql=source_sql,
            plan=plan,
            schedule_map=schedule_map,
            max_rows_per_table=max_rows_per_table,
            read_batch_size=read_batch_size,
            max_row_bytes=max_row_bytes,
            consumer=lambda row: _verify_transformed_row(
                handle=role_handles[row.target_role],
                plan=plan,
                transformed=row,
            ),
        )
        _require_transform(report, transform_digest)
        _verify_unowned_target_evidence(
            role_handles=role_handles,
            plan=plan,
            max_rows_per_table=max_rows_per_table,
            read_batch_size=read_batch_size,
            max_row_bytes=max_row_bytes,
        )
        _mark_verified(
            handles=handles,
            role_handles=role_handles,
            plan=plan,
            report=report,
            record_phase=record_phase,
        )
        return report
    finally:
        _close_source(source_connection)


def verify_postgres_migration_012(
    *,
    plan: PostgresMigration012Plan,
    schedule_map: PostgresScheduleMap,
    confirm_plan_digest: str,
    confirm_transform_digest: str,
    confirm_writer_fence_plan: str,
    writers_stopped: bool,
    environment: dict[str, str] | None = None,
    statement_timeout_ms: int = 60_000,
    lock_timeout_ms: int = 2_000,
    max_rows_per_table: int = 10_000_000,
    read_batch_size: int = 1_000,
    max_row_bytes: int = 16 * 1024 * 1024,
    dry_run: bool = False,
) -> PostgresMigration012LifecycleReceipt:
    """Independently re-read source, targets, controls, and provenance."""
    _validate_bounds(
        statement_timeout_ms=statement_timeout_ms,
        lock_timeout_ms=lock_timeout_ms,
        max_rows_per_table=max_rows_per_table,
        read_batch_size=read_batch_size,
        write_batch_size=1,
        max_row_bytes=max_row_bytes,
    )
    _validate_plan_and_confirmations(
        plan=plan,
        schedule_map=schedule_map,
        confirm_plan_digest=confirm_plan_digest,
        confirm_transform_digest=confirm_transform_digest,
        confirm_writer_fence_plan=confirm_writer_fence_plan,
        writers_stopped=writers_stopped,
    )
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    handles: list[_TargetHandle] = []
    try:
        handles, role_handles = _connect_target_handles(
            plan,
            environment=environment,
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        )
        _verify_with_handles(
            handles=handles,
            role_handles=role_handles,
            plan=plan,
            schedule_map=schedule_map,
            transform_digest=confirm_transform_digest,
            environment=environment,
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
            max_rows_per_table=max_rows_per_table,
            read_batch_size=read_batch_size,
            max_row_bytes=max_row_bytes,
            record_phase=not dry_run,
        )
        return _receipt(
            operation="verify",
            handles=handles,
            role_handles=role_handles,
            plan=plan,
            transform_digest=confirm_transform_digest,
        )
    except Migration012Error:
        raise
    except Exception as exc:
        raise Migration012Error(
            "MIGRATION_POSTGRES_VERIFY_FAILED",
            "PostgreSQL migration verification failed; driver details were redacted.",
            error_type=type(exc).__name__,
        ) from None
    finally:
        _close_target_handles(handles)


def cutover_postgres_migration_012(
    *,
    plan: PostgresMigration012Plan,
    schedule_map: PostgresScheduleMap,
    confirm_plan_digest: str,
    confirm_transform_digest: str,
    confirm_writer_fence_plan: str,
    writers_stopped: bool,
    cutover_receipt_id: str,
    cutover_receipt_digest: str,
    environment: dict[str, str] | None = None,
    statement_timeout_ms: int = 60_000,
    lock_timeout_ms: int = 2_000,
    max_rows_per_table: int = 10_000_000,
    read_batch_size: int = 1_000,
    max_row_bytes: int = 16 * 1024 * 1024,
    dry_run: bool = False,
) -> PostgresMigration012LifecycleReceipt:
    """Verify and record cutover authority without editing deployment configuration."""
    _validate_bounds(
        statement_timeout_ms=statement_timeout_ms,
        lock_timeout_ms=lock_timeout_ms,
        max_rows_per_table=max_rows_per_table,
        read_batch_size=read_batch_size,
        write_batch_size=1,
        max_row_bytes=max_row_bytes,
    )
    _validate_plan_and_confirmations(
        plan=plan,
        schedule_map=schedule_map,
        confirm_plan_digest=confirm_plan_digest,
        confirm_transform_digest=confirm_transform_digest,
        confirm_writer_fence_plan=confirm_writer_fence_plan,
        writers_stopped=writers_stopped,
    )
    cutover_receipt_id = _require_control_token(cutover_receipt_id, "cutover_receipt_id")
    cutover_receipt_digest = _require_digest(cutover_receipt_digest, "cutover_receipt_digest")
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    handles: list[_TargetHandle] = []
    try:
        handles, role_handles = _connect_target_handles(
            plan,
            environment=environment,
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        )
        _verify_with_handles(
            handles=handles,
            role_handles=role_handles,
            plan=plan,
            schedule_map=schedule_map,
            transform_digest=confirm_transform_digest,
            environment=environment,
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
            max_rows_per_table=max_rows_per_table,
            read_batch_size=read_batch_size,
            max_row_bytes=max_row_bytes,
            record_phase=not dry_run,
        )
        if dry_run:
            return _receipt(
                operation="cutover",
                handles=handles,
                role_handles=role_handles,
                plan=plan,
                transform_digest=confirm_transform_digest,
            )
        for handle in handles:
            control = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_control")
            for role in handle.roles:
                row = handle.connection.execute(
                    handle.sql.SQL(
                        "SELECT phase, cutover_receipt_id, cutover_receipt_digest FROM {} "
                        "WHERE migration_id = %s AND target_role = %s FOR UPDATE"
                    ).format(control),
                    (plan.migration_id, role),
                ).fetchone()
                if row is None or str(row["phase"]) not in {"verified", "cutover"}:
                    raise Migration012Error(
                        "MIGRATION_POSTGRES_PHASE_CONFLICT",
                        "Cutover requires independently verified target state.",
                        target_role=role,
                    )
                if str(row["phase"]) == "cutover" and (
                    str(row["cutover_receipt_id"]) != cutover_receipt_id
                    or str(row["cutover_receipt_digest"]) != cutover_receipt_digest
                ):
                    raise Migration012Error(
                        "MIGRATION_POSTGRES_CUTOVER_CONFLICT",
                        "Cutover was already recorded with different authority evidence.",
                        target_role=role,
                    )
                handle.connection.execute(
                    handle.sql.SQL(
                        "UPDATE {} SET phase = 'cutover', revision = revision + 1, "
                        "cutover_at = COALESCE(cutover_at, CURRENT_TIMESTAMP), "
                        "cutover_receipt_id = %s, cutover_receipt_digest = %s, "
                        "updated_at = CURRENT_TIMESTAMP "
                        "WHERE migration_id = %s AND target_role = %s AND phase = 'verified'"
                    ).format(control),
                    (
                        cutover_receipt_id,
                        cutover_receipt_digest,
                        plan.migration_id,
                        role,
                    ),
                )
            handle.connection.commit()
        return _receipt(
            operation="cutover",
            handles=handles,
            role_handles=role_handles,
            plan=plan,
            transform_digest=confirm_transform_digest,
        )
    except Migration012Error:
        raise
    except Exception as exc:
        raise Migration012Error(
            "MIGRATION_POSTGRES_CUTOVER_FAILED",
            "PostgreSQL migration cutover failed; driver details were redacted.",
            error_type=type(exc).__name__,
        ) from None
    finally:
        _close_target_handles(handles)


def _rollback_transformed_row(
    *,
    handle: _TargetHandle,
    plan: PostgresMigration012Plan,
    transformed: _TransformedRow,
) -> None:
    spec = _TABLE_SPECS[transformed.target_table]
    provenance = _provenance_for_identity(
        handle,
        role=transformed.target_role,
        transformed=transformed,
        for_update=True,
    )
    if provenance is None:
        raise Migration012Error(
            "MIGRATION_POSTGRES_PROVENANCE_MISSING",
            "Rollback refuses a target identity without migration provenance.",
            target_role=transformed.target_role,
            target_table=transformed.target_table,
        )
    _validate_provenance(provenance, plan=plan, transformed=transformed)
    observed = _fetch_target_values(
        handle,
        spec,
        transformed.target_identity,
        for_update=True,
    )
    disposition = str(provenance["disposition"])
    already_rolled_back = provenance.get("rolled_back_at") is not None
    if disposition == "inserted":
        if already_rolled_back:
            if observed is not None:
                raise Migration012Error(
                    "MIGRATION_POSTGRES_ROLLBACK_DRIFT",
                    "A previously removed migration row reappeared.",
                    target_role=transformed.target_role,
                    target_table=transformed.target_table,
                )
            return
        if observed is None or not _target_values_match(observed, transformed):
            raise Migration012Error(
                "MIGRATION_POSTGRES_ROLLBACK_DRIFT",
                "Rollback refuses a missing or changed migration-owned target row.",
                target_role=transformed.target_role,
                target_table=transformed.target_table,
                target_identity_digest=_digest(transformed.target_identity),
            )
        table = _qualified(handle.sql, handle.schema, spec.table_name)
        handle.connection.execute(
            handle.sql.SQL("DELETE FROM {} WHERE {} = %s").format(
                table, handle.sql.Identifier(spec.identity_column)
            ),
            (transformed.target_identity,),
        )
    else:
        if observed is None or not _target_values_match(observed, transformed):
            raise Migration012Error(
                "MIGRATION_POSTGRES_ROLLBACK_DRIFT",
                "Rollback refuses changed preexisting target data.",
                target_role=transformed.target_role,
                target_table=transformed.target_table,
                target_identity_digest=_digest(transformed.target_identity),
            )
        if already_rolled_back:
            return
    provenance_table = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_provenance")
    handle.connection.execute(
        handle.sql.SQL(
            "UPDATE {} SET rolled_back_at = CURRENT_TIMESTAMP "
            "WHERE migration_id = %s AND target_role = %s AND target_table = %s "
            "AND target_identity_digest = %s AND rolled_back_at IS NULL"
        ).format(provenance_table),
        (
            plan.migration_id,
            transformed.target_role,
            transformed.target_table,
            _digest(transformed.target_identity),
        ),
    )


def rollback_postgres_migration_012(
    *,
    plan: PostgresMigration012Plan,
    schedule_map: PostgresScheduleMap,
    confirm_plan_digest: str,
    confirm_transform_digest: str,
    confirm_writer_fence_plan: str,
    writers_stopped: bool,
    confirm_no_post_cutover_target_writes: bool = False,
    environment: dict[str, str] | None = None,
    statement_timeout_ms: int = 60_000,
    lock_timeout_ms: int = 2_000,
    max_rows_per_table: int = 10_000_000,
    read_batch_size: int = 1_000,
    write_batch_size: int = 1_000,
    max_row_bytes: int = 16 * 1024 * 1024,
    dry_run: bool = False,
    _checkpoint_observer: _CheckpointObserver | None = None,
) -> PostgresMigration012LifecycleReceipt:
    """Remove only exact migration-owned rows and preserve identical preexisting rows."""
    _validate_bounds(
        statement_timeout_ms=statement_timeout_ms,
        lock_timeout_ms=lock_timeout_ms,
        max_rows_per_table=max_rows_per_table,
        read_batch_size=read_batch_size,
        write_batch_size=write_batch_size,
        max_row_bytes=max_row_bytes,
    )
    _validate_plan_and_confirmations(
        plan=plan,
        schedule_map=schedule_map,
        confirm_plan_digest=confirm_plan_digest,
        confirm_transform_digest=confirm_transform_digest,
        confirm_writer_fence_plan=confirm_writer_fence_plan,
        writers_stopped=writers_stopped,
    )
    if not isinstance(confirm_no_post_cutover_target_writes, bool):
        raise ValueError("confirm_no_post_cutover_target_writes must be a boolean")
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    if _checkpoint_observer is not None and not callable(_checkpoint_observer):
        raise ValueError("_checkpoint_observer must be callable")
    handles: list[_TargetHandle] = []
    source_connection: Any = None
    try:
        handles, role_handles = _connect_target_handles(
            plan,
            environment=environment,
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        )
        phases: dict[str, str] = {}
        controls: dict[str, dict[str, Any]] = {}
        for role in _TARGET_ROLES:
            row = _control_row(role_handles[role], plan.migration_id, role)
            if row is None:
                raise Migration012Error(
                    "MIGRATION_POSTGRES_CONTROL_CONFLICT",
                    "Rollback requires both target control records.",
                    target_role=role,
                )
            _validate_control_row(
                row,
                plan=plan,
                role=role,
                transform_digest=confirm_transform_digest,
            )
            phases[role] = str(row["phase"])
            controls[role] = row
        was_cutover = any(row.get("cutover_at") is not None for row in controls.values())
        if was_cutover and not confirm_no_post_cutover_target_writes:
            raise Migration012Error(
                "MIGRATION_POSTGRES_ROLLBACK_WINDOW_UNCONFIRMED",
                "Post-cutover rollback requires confirmation that no target writer has run.",
            )
        allowed = {"applied", "verified", "cutover", "rolling_back", "rolled_back"}
        if any(phase not in allowed for phase in phases.values()):
            raise Migration012Error(
                "MIGRATION_POSTGRES_PHASE_CONFLICT",
                "Rollback requires completely applied target state.",
                role_phases=phases,
            )
        if dry_run and any(phase not in _ACTIVE_PHASES for phase in phases.values()):
            raise Migration012Error(
                "MIGRATION_POSTGRES_PHASE_CONFLICT",
                "Rollback dry-run requires active, completely applied target state.",
                role_phases=phases,
            )
        source_connection, source_sql = _open_source_snapshot(
            plan,
            environment=environment,
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        )
        report = _compile_snapshot(
            connection=source_connection,
            sql=source_sql,
            plan=plan,
            schedule_map=schedule_map,
            max_rows_per_table=max_rows_per_table,
            read_batch_size=read_batch_size,
            max_row_bytes=max_row_bytes,
        )
        _require_transform(report, confirm_transform_digest)
        _verify_unowned_target_evidence(
            role_handles=role_handles,
            plan=plan,
            max_rows_per_table=max_rows_per_table,
            read_batch_size=read_batch_size,
            max_row_bytes=max_row_bytes,
        )
        if dry_run:
            observed = _compile_snapshot(
                connection=source_connection,
                sql=source_sql,
                plan=plan,
                schedule_map=schedule_map,
                max_rows_per_table=max_rows_per_table,
                read_batch_size=read_batch_size,
                max_row_bytes=max_row_bytes,
                consumer=lambda row: _verify_transformed_row(
                    handle=role_handles[row.target_role],
                    plan=plan,
                    transformed=row,
                ),
            )
            _require_transform(observed, confirm_transform_digest)
            return _receipt(
                operation="rollback",
                handles=handles,
                role_handles=role_handles,
                plan=plan,
                transform_digest=confirm_transform_digest,
            )
        for handle in handles:
            control = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_control")
            for role in handle.roles:
                handle.connection.execute(
                    handle.sql.SQL(
                        "UPDATE {} SET phase = 'rolling_back', revision = revision + 1, "
                        "updated_at = CURRENT_TIMESTAMP WHERE migration_id = %s "
                        "AND target_role = %s AND phase IN ('applied', 'verified', 'cutover')"
                    ).format(control),
                    (plan.migration_id, role),
                )
            handle.connection.commit()
        for role in reversed(_TARGET_ROLES):
            consumer = _BatchedConsumer(
                handles=handles,
                role_handles=role_handles,
                plan=plan,
                write_batch_size=write_batch_size,
                operation=lambda handle, row: _rollback_transformed_row(
                    handle=handle,
                    plan=plan,
                    transformed=row,
                ),
                role_filter=role,
                operation_name="rollback",
                checkpoint_observer=_checkpoint_observer,
            )
            observed = _compile_snapshot(
                connection=source_connection,
                sql=source_sql,
                plan=plan,
                schedule_map=schedule_map,
                max_rows_per_table=max_rows_per_table,
                read_batch_size=read_batch_size,
                max_row_bytes=max_row_bytes,
                consumer=consumer,
            )
            consumer.flush()
            _require_transform(observed, confirm_transform_digest)
            handle = role_handles[role]
            control = _qualified(handle.sql, handle.schema, "agnoclaw_migration_012_control")
            handle.connection.execute(
                handle.sql.SQL(
                    "UPDATE {} SET phase = 'rolled_back', revision = revision + 1, "
                    "rolled_back_at = COALESCE(rolled_back_at, CURRENT_TIMESTAMP), "
                    "updated_at = CURRENT_TIMESTAMP WHERE migration_id = %s "
                    "AND target_role = %s AND phase = 'rolling_back'"
                ).format(control),
                (plan.migration_id, role),
            )
            handle.connection.commit()
        return _receipt(
            operation="rollback",
            handles=handles,
            role_handles=role_handles,
            plan=plan,
            transform_digest=confirm_transform_digest,
        )
    except Migration012Error:
        raise
    except Exception as exc:
        raise Migration012Error(
            "MIGRATION_POSTGRES_ROLLBACK_FAILED",
            "PostgreSQL migration rollback failed; driver details were redacted.",
            error_type=type(exc).__name__,
        ) from None
    finally:
        _close_source(source_connection)
        _close_target_handles(handles)


__all__ = [
    "POSTGRES_MIGRATION_012_LIFECYCLE_SCHEMA_VERSION",
    "PostgresMigration012LifecycleReceipt",
    "PostgresMigration012Phase",
    "apply_postgres_migration_012",
    "cutover_postgres_migration_012",
    "rollback_postgres_migration_012",
    "verify_postgres_migration_012",
]
