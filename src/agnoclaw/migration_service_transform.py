"""Deterministic, content-free transformation preview for service migration."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .learning import LearningProfile, LearningScope
from .migration import LegacyScopeAction, ScheduleMisfirePolicy
from .migration_apply import Migration012Error
from .migration_service import (
    PostgresMigration012Plan,
    PostgresMigrationTableEvidence,
    PostgresScheduleMap,
    PostgresScheduleMigrationRule,
    _canonical_json,
    _digest,
    _require_digest,
    _require_text,
)
from .migration_service_scan import scan_postgres_migration_012
from .runtime.context import ExecutionContext
from .runtime.scheduler import (
    SchedulerConfigurationError,
    SchedulerJob,
    next_schedule_time,
    scheduler_job_digest,
)

POSTGRES_MIGRATION_012_TRANSFORM_SCHEMA_VERSION = "1.0"
_CATEGORIES = ("learning_personal", "learning_quarantine", "schedule_job", "schedule_history")
_SOURCE_TABLES = ("agno_learnings", "agno_schedules", "agno_schedule_runs")
_PERSONAL_TYPES = {"user_profile", "user_memory", "session_context"}


class _StreamDigest:
    """Deterministic bounded-memory digest for an ordered sequence of JSON values."""

    def __init__(self) -> None:
        self._hasher = hashlib.sha256()
        self._count = 0

    def update(self, value: Any) -> None:
        payload = _canonical_json(value).encode("utf-8")
        self._hasher.update(len(payload).to_bytes(8, "big"))
        self._hasher.update(payload)
        self._count += 1

    @property
    def count(self) -> int:
        return self._count

    @property
    def digest(self) -> str:
        return "sha256:" + self._hasher.hexdigest()


@dataclass(frozen=True, slots=True)
class PostgresMigration012TransformReport:
    """Content-free summary of exact source-to-target transformations."""

    migration_id: str
    plan_digest: str
    generated_at: str
    counts: tuple[tuple[str, int], ...]
    category_digests: tuple[tuple[str, str], ...]
    source_table_digests: tuple[tuple[str, str], ...]
    target_identity_digest: str
    transform_digest: str = ""
    schema_version: str = POSTGRES_MIGRATION_012_TRANSFORM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != POSTGRES_MIGRATION_012_TRANSFORM_SCHEMA_VERSION:
            raise ValueError("unsupported PostgreSQL transformation report schema")
        for name in ("migration_id", "generated_at"):
            object.__setattr__(self, name, _require_text(getattr(self, name), name))
        object.__setattr__(self, "plan_digest", _require_digest(self.plan_digest, "plan_digest"))
        expected = tuple(sorted(_CATEGORIES))
        if tuple(name for name, _ in self.counts) != expected:
            raise ValueError("transformation counts must cover every canonical category")
        if tuple(name for name, _ in self.category_digests) != expected:
            raise ValueError("transformation digests must cover every canonical category")
        if any(isinstance(count, bool) or count < 0 for _, count in self.counts):
            raise ValueError("transformation counts cannot be negative")
        if tuple(name for name, _ in self.source_table_digests) != _SOURCE_TABLES:
            raise ValueError("source table digests must cover every canonical source table")
        for _, digest in (*self.category_digests, *self.source_table_digests):
            _require_digest(digest, "transformation_evidence_digest")
        _require_digest(self.target_identity_digest, "target_identity_digest")
        if self.transform_digest:
            _require_digest(self.transform_digest, "transform_digest")
            if self.transform_digest != self.computed_transform_digest:
                raise ValueError("transform_digest does not match transformation evidence")

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "migration_id": self.migration_id,
            "plan_digest": self.plan_digest,
            "generated_at": self.generated_at,
            "counts": dict(self.counts),
            "category_digests": dict(self.category_digests),
            "source_table_digests": dict(self.source_table_digests),
            "target_identity_digest": self.target_identity_digest,
        }
        if include_digest:
            payload["transform_digest"] = self.transform_digest
        return payload

    @property
    def computed_transform_digest(self) -> str:
        payload = self.to_dict(include_digest=False)
        payload.pop("generated_at")
        return _digest(payload)


@dataclass(frozen=True, slots=True)
class _TransformedRow:
    category: str
    source_table: str
    source_digest: str
    target_role: str
    target_table: str
    target_identity: str
    target_digest: str
    values: dict[str, Any] = field(repr=False, compare=False)

    def evidence(self) -> dict[str, str]:
        return {
            "category": self.category,
            "source_table": self.source_table,
            "source_digest": self.source_digest,
            "target_role": self.target_role,
            "target_table": self.target_table,
            "target_identity_digest": _digest(self.target_identity),
            "target_digest": self.target_digest,
        }


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not (-float("inf") < value < float("inf")):
            raise ValueError("non-finite JSON numbers are unsupported")
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON objects require string keys")
        return {key: _json_value(item) for key, item in value.items()}
    raise ValueError("source values must be JSON-compatible")


def _json_object(value: Any, *, wrapper: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return _json_value(value)
    if value is None:
        return {}
    return {wrapper: _json_value(value)}


def _epoch(value: Any, *, field_name: str, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer epoch")
    return value


def _mapping_for(plan: PostgresMigration012Plan, namespace: Any, learning_type: str):
    normalized = None if namespace is None else str(namespace)
    index = {item.key: item for item in plan.scope_mappings}
    return index.get((normalized, learning_type)) or index.get((normalized, None))


def _personal_scope(
    plan: PostgresMigration012Plan,
    row: dict[str, Any],
    learning_type: str,
) -> LearningScope:
    context = ExecutionContext.create(
        tenant_id=plan.target_tenant_id,
        org_id=plan.target_org_id,
        user_id=(str(row["user_id"]) if row.get("user_id") is not None else None),
        session_id=(str(row["session_id"]) if row.get("session_id") is not None else None),
        workspace_id=None,
    )
    if learning_type == "user_profile":
        policy = LearningProfile.personal(
            user_profile="always",
            user_memory=None,
            tenant_required=True,
            consent_required=False,
        )
    elif learning_type == "user_memory":
        policy = LearningProfile.personal(
            user_profile=None,
            user_memory="always",
            tenant_required=True,
            consent_required=False,
        )
    else:
        policy = LearningProfile.session(tenant_required=True)
    return LearningScope.resolve(
        policy,
        context,
        agent_id=plan.target_agent_id,
        consented=True,
    )


def _target_row(
    *,
    category: str,
    source_table: str,
    source_digest: str,
    target_role: str,
    target_table: str,
    target_identity: str,
    values: dict[str, Any],
) -> _TransformedRow:
    return _TransformedRow(
        category=category,
        source_table=source_table,
        source_digest=source_digest,
        target_role=target_role,
        target_table=target_table,
        target_identity=target_identity,
        target_digest=_digest({"target_table": target_table, "values": values}),
        values=values,
    )


def _transform_learning_row(
    plan: PostgresMigration012Plan,
    row: dict[str, Any],
    *,
    source_digest: str,
) -> _TransformedRow:
    learning_type = str(row.get("learning_type") or "")
    source_table = "agno_learnings"
    if not learning_type:
        raise Migration012Error(
            "MIGRATION_POSTGRES_LEARNING_ROW_INVALID",
            "A source learning row lacks a bounded learning type.",
            source_table=source_table,
            source_digest=source_digest,
        )
    if learning_type in _PERSONAL_TYPES:
        try:
            scope = _personal_scope(plan, row, learning_type)
            if learning_type == "user_profile":
                target_identity = f"user_profile_{scope.storage_user_id}"
            elif learning_type == "user_memory":
                target_identity = f"memories_{scope.storage_user_id}"
            else:
                target_identity = f"session_context_{scope.storage_session_id}"
            metadata = _json_object(row.get("metadata"), wrapper="legacy_metadata")
            metadata["agnoclaw_migration"] = {
                "schema_version": POSTGRES_MIGRATION_012_TRANSFORM_SCHEMA_VERSION,
                "migration_id": plan.migration_id,
                "source_digest": source_digest,
            }
            values = {
                "learning_id": target_identity,
                "learning_type": learning_type,
                "namespace": None,
                "user_id": scope.storage_user_id,
                "agent_id": plan.target_agent_id,
                "team_id": None,
                "workflow_id": None,
                "session_id": (
                    scope.storage_session_id if learning_type == "session_context" else None
                ),
                "entity_id": None,
                "entity_type": None,
                "content": _json_object(row.get("content"), wrapper="legacy_value"),
                "metadata": metadata,
                "created_at": _epoch(row.get("created_at"), field_name="created_at"),
                "updated_at": _epoch(row.get("updated_at"), field_name="updated_at", optional=True),
            }
        except Migration012Error:
            raise
        except Exception as exc:
            raise Migration012Error(
                "MIGRATION_POSTGRES_LEARNING_ROW_INVALID",
                "A personal learning row cannot be transformed under reviewed authority.",
                source_table=source_table,
                source_digest=source_digest,
                error_type=type(exc).__name__,
            ) from None
        return _target_row(
            category="learning_personal",
            source_table=source_table,
            source_digest=source_digest,
            target_role="target_learning",
            target_table="agno_learnings",
            target_identity=target_identity,
            values=values,
        )

    mapping = _mapping_for(plan, row.get("namespace"), learning_type)
    if mapping is None:
        raise Migration012Error(
            "MIGRATION_POSTGRES_LEARNING_SCOPE_UNRESOLVED",
            "An institutional learning row lacks a reviewed map-or-quarantine decision.",
            source_table=source_table,
            source_digest=source_digest,
        )
    values = {
        "source_digest": source_digest,
        "migration_id": plan.migration_id,
        "source_table": source_table,
        "learning_type": learning_type,
        "action": mapping.action.value,
        "target_tenant_id": (
            mapping.target_tenant_id if mapping.action is LegacyScopeAction.MAP else None
        ),
        "target_namespace": (
            mapping.target_namespace if mapping.action is LegacyScopeAction.MAP else None
        ),
        "record_json": _canonical_json(_json_value(row)),
    }
    return _target_row(
        category="learning_quarantine",
        source_table=source_table,
        source_digest=source_digest,
        target_role="target_learning",
        target_table="agnoclaw_migration_012_learning_quarantine",
        target_identity=source_digest,
        values=values,
    )


def _schedule_target_name(rule: PostgresScheduleMigrationRule) -> str:
    digest = _digest(
        {
            "tenant_id": rule.tenant_id,
            "user_id": rule.user_id,
            "session_id": rule.session_id,
            "agent_id": rule.agent_id,
            "worker_profile": rule.worker_profile,
            "source_schedule_id": rule.source_schedule_id,
        }
    )
    return "service_schedule_" + digest.removeprefix("sha256:")[:40]


def _transform_schedule_row(
    plan: PostgresMigration012Plan,
    rule: PostgresScheduleMigrationRule,
    *,
    source_digest: str,
) -> _TransformedRow:
    metadata = {
        "learning_consent": rule.learning_consent,
        "agnoclaw_migration": {
            "schema_version": POSTGRES_MIGRATION_012_TRANSFORM_SCHEMA_VERSION,
            "migration_id": plan.migration_id,
            "source_digest": source_digest,
            "source_schedule_id": rule.source_schedule_id,
        },
        "service_authority": {
            "tenant_id": rule.tenant_id,
            "user_id": rule.user_id,
            "session_id": rule.session_id,
            "agent_id": rule.agent_id,
            "worker_profile": rule.worker_profile,
        },
    }
    misfire_policy = "skip" if rule.misfire_policy is ScheduleMisfirePolicy.SKIP else "fire_once"
    try:
        job = SchedulerJob(
            name=_schedule_target_name(rule),
            schedule=rule.schedule,
            prompt=rule.prompt,
            isolated=rule.isolated,
            enabled=rule.enabled,
            timezone=rule.timezone,
            max_retries=rule.max_retries,
            retry_delay_seconds=rule.retry_delay_seconds,
            retry_backoff_multiplier=rule.retry_backoff_multiplier,
            retry_max_delay_seconds=rule.retry_max_delay_seconds,
            retry_jitter_seconds=rule.retry_jitter_seconds,
            jitter_seconds=rule.jitter_seconds,
            misfire_policy=misfire_policy,
            misfire_grace_seconds=rule.misfire_grace_seconds,
            concurrency_key=rule.concurrency_key,
            overlap_policy=rule.overlap_policy,
            metadata=metadata,
            revision=1,
            created_at=plan.planned_at,
            updated_at=plan.planned_at,
        )
        if job.enabled:
            job = replace(job, next_run_at=next_schedule_time(job, after=plan.planned_at))
    except SchedulerConfigurationError as exc:
        raise Migration012Error(
            "MIGRATION_SCHEDULE_MAP_INVALID",
            "A reviewed schedule cannot be evaluated; correct it or install the scheduler extra.",
            source_table="agno_schedules",
            source_digest=source_digest,
            install_extra="scheduler",
            error_type=type(exc).__name__,
        ) from None
    except Exception as exc:
        raise Migration012Error(
            "MIGRATION_POSTGRES_SCHEDULE_TRANSFORM_INVALID",
            "A reviewed schedule rule cannot be compiled into a durable job.",
            source_table="agno_schedules",
            source_digest=source_digest,
            error_type=type(exc).__name__,
        ) from None
    values = {
        "job_name": job.name,
        "revision": job.revision,
        "enabled": job.enabled,
        "next_run_at": job.next_run_at,
        "job_digest": scheduler_job_digest(job),
        "job_json": _canonical_json(job.to_dict()),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
    return _target_row(
        category="schedule_job",
        source_table="agno_schedules",
        source_digest=source_digest,
        target_role="target_runtime",
        target_table="runtime_scheduler_jobs",
        target_identity=job.name,
        values=values,
    )


def _transform_schedule_history_row(
    plan: PostgresMigration012Plan,
    row: dict[str, Any],
    rule: PostgresScheduleMigrationRule,
    *,
    source_digest: str,
) -> _TransformedRow:
    values = {
        "source_digest": source_digest,
        "migration_id": plan.migration_id,
        "source_run_id": str(row.get("id") or source_digest),
        "source_schedule_id": rule.source_schedule_id,
        "target_job_name": _schedule_target_name(rule),
        "status": str(row.get("status") or "unknown"),
        "record_json": _canonical_json(_json_value(row)),
    }
    return _target_row(
        category="schedule_history",
        source_table="agno_schedule_runs",
        source_digest=source_digest,
        target_role="target_runtime",
        target_table="agnoclaw_migration_012_schedule_history",
        target_identity=source_digest,
        values=values,
    )


def _register_identity(connection: sqlite3.Connection, transformed: _TransformedRow) -> None:
    try:
        connection.execute(
            "INSERT INTO identities(target_role, target_table, target_identity, "
            "source_digest, target_digest) VALUES (?, ?, ?, ?, ?)",
            (
                transformed.target_role,
                transformed.target_table,
                transformed.target_identity,
                transformed.source_digest,
                transformed.target_digest,
            ),
        )
    except sqlite3.IntegrityError:
        raise Migration012Error(
            "MIGRATION_POSTGRES_TARGET_COLLISION",
            "Multiple source rows resolve to one target migration identity.",
            target_role=transformed.target_role,
            target_table=transformed.target_table,
            target_identity_digest=_digest(transformed.target_identity),
        ) from None


def _expected_source_evidence(
    plan: PostgresMigration012Plan,
    table: str,
) -> PostgresMigrationTableEvidence:
    matches = [
        item for item in plan.table_evidence if item.role == "source" and item.table_name == table
    ]
    if len(matches) != 1:
        raise Migration012Error(
            "MIGRATION_POSTGRES_PLAN_EVIDENCE_INVALID",
            "The reviewed plan lacks exact source-table evidence.",
            source_table=table,
        )
    return matches[0]


def _stream_source_table(
    connection: Any,
    sql: Any,
    *,
    schema: str,
    table: str,
    batch_size: int,
    max_row_bytes: int,
    transform: Callable[[dict[str, Any], str], _TransformedRow],
    consume: Callable[[_TransformedRow], None],
) -> tuple[int, str]:
    qualified = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))
    query = sql.SQL(
        "SELECT to_jsonb(source_row)::text FROM {} AS source_row "
        'ORDER BY (to_jsonb(source_row)::text) COLLATE "C"'
    ).format(qualified)
    cursor = connection.cursor(name=f"agnoclaw_migration_transform_{table}")
    cursor.itersize = batch_size
    count = 0
    logical_hasher = hashlib.sha256()
    try:
        cursor.execute(query)
        while rows := cursor.fetchmany(batch_size):
            for value in rows:
                raw = str(value[0]).encode("utf-8")
                if len(raw) > max_row_bytes:
                    raise Migration012Error(
                        "MIGRATION_POSTGRES_ROW_LIMIT_EXCEEDED",
                        "A PostgreSQL row exceeds the explicit transformation byte limit.",
                        source_table=table,
                        max_row_bytes=max_row_bytes,
                    )
                row_digest_bytes = hashlib.sha256(raw).digest()
                logical_hasher.update(len(row_digest_bytes).to_bytes(4, "big"))
                logical_hasher.update(row_digest_bytes)
                source_digest = "sha256:" + row_digest_bytes.hex()
                try:
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict):
                        raise ValueError("source row must be a JSON object")
                    transformed = transform(parsed, source_digest)
                except Migration012Error:
                    raise
                except Exception as exc:
                    raise Migration012Error(
                        "MIGRATION_POSTGRES_SOURCE_ROW_INVALID",
                        "A source row cannot be transformed safely.",
                        source_table=table,
                        source_digest=source_digest,
                        error_type=type(exc).__name__,
                    ) from None
                consume(transformed)
                count += 1
    finally:
        cursor.close()
    return count, "sha256:" + logical_hasher.hexdigest()


def _source_relation_exists(connection: Any, *, schema: str, table: str) -> bool:
    row = connection.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = %s
              AND relation.relname = %s
              AND relation.relkind IN ('r', 'p')
        )
        """,
        (schema, table),
    ).fetchone()
    return bool(row and row[0])


def _compile_postgres_migration_012_source_snapshot(
    *,
    connection: Any,
    sql: Any,
    plan: PostgresMigration012Plan,
    schedule_map: PostgresScheduleMap,
    max_rows_per_table: int,
    batch_size: int,
    max_row_bytes: int,
    row_consumer: Callable[[_TransformedRow], None] | None = None,
) -> PostgresMigration012TransformReport:
    """Compile one already-open repeatable-read source snapshot."""
    category_evidence = {name: _StreamDigest() for name in _CATEGORIES}
    source_table_digests: list[tuple[str, str]] = []
    rules = {item.source_schedule_id: item for item in schedule_map.rules}
    with tempfile.TemporaryDirectory(prefix="agnoclaw-migration-012-transform-") as temp:
        registry_path = Path(temp) / "identity-registry.sqlite3"
        registry = sqlite3.connect(registry_path)
        os.chmod(registry_path, 0o600)
        try:
            registry.execute(
                "CREATE TABLE identities("
                "target_role TEXT NOT NULL, target_table TEXT NOT NULL, "
                "target_identity TEXT NOT NULL, source_digest TEXT NOT NULL, "
                "target_digest TEXT NOT NULL, "
                "PRIMARY KEY(target_role, target_table, target_identity))"
            )

            def consume(transformed: _TransformedRow) -> None:
                _register_identity(registry, transformed)
                category_evidence[transformed.category].update(transformed.evidence())
                if row_consumer is not None:
                    row_consumer(transformed)

            for table, transformer in (
                (
                    "agno_learnings",
                    lambda row, digest: _transform_learning_row(plan, row, source_digest=digest),
                ),
                (
                    "agno_schedules",
                    lambda row, digest: _transform_schedule_row(
                        plan,
                        rules[str(row.get("id") or "")],
                        source_digest=digest,
                    ),
                ),
                (
                    "agno_schedule_runs",
                    lambda row, digest: _transform_schedule_history_row(
                        plan,
                        row,
                        rules[str(row.get("schedule_id") or "")],
                        source_digest=digest,
                    ),
                ),
            ):
                expected = _expected_source_evidence(plan, table)
                exists = _source_relation_exists(
                    connection,
                    schema=plan.source.schema,
                    table=table,
                )
                if exists != expected.exists:
                    raise Migration012Error(
                        "MIGRATION_POSTGRES_SOURCE_DRIFT",
                        "A PostgreSQL source table existence changed after plan review.",
                        source_table=table,
                    )
                if not expected.exists:
                    source_table_digests.append((table, _StreamDigest().digest))
                    continue
                if expected.row_count > max_rows_per_table:
                    raise Migration012Error(
                        "MIGRATION_POSTGRES_ROW_LIMIT_EXCEEDED",
                        "A reviewed source table exceeds the transformation row limit.",
                        source_table=table,
                        max_rows_per_table=max_rows_per_table,
                    )
                count, logical_digest = _stream_source_table(
                    connection,
                    sql,
                    schema=plan.source.schema,
                    table=table,
                    batch_size=batch_size,
                    max_row_bytes=max_row_bytes,
                    transform=transformer,
                    consume=consume,
                )
                if count != expected.row_count or logical_digest != expected.logical_digest:
                    raise Migration012Error(
                        "MIGRATION_POSTGRES_SOURCE_DRIFT",
                        "A PostgreSQL source table changed during transformation.",
                        source_table=table,
                    )
                source_table_digests.append((table, logical_digest))
            registry.commit()
            identities = _StreamDigest()
            for row in registry.execute(
                "SELECT target_role, target_table, target_identity, source_digest, "
                "target_digest FROM identities "
                "ORDER BY target_role, target_table, target_identity"
            ):
                identities.update(
                    {
                        "target_role": row[0],
                        "target_table": row[1],
                        "target_identity_digest": _digest(row[2]),
                        "source_digest": row[3],
                        "target_digest": row[4],
                    }
                )
        finally:
            registry.close()
    report = PostgresMigration012TransformReport(
        migration_id=plan.migration_id,
        plan_digest=plan.plan_digest,
        generated_at=datetime.now(UTC).isoformat(),
        counts=tuple(
            sorted((name, evidence.count) for name, evidence in category_evidence.items())
        ),
        category_digests=tuple(
            sorted((name, evidence.digest) for name, evidence in category_evidence.items())
        ),
        source_table_digests=tuple(source_table_digests),
        target_identity_digest=identities.digest,
    )
    return replace(report, transform_digest=report.computed_transform_digest)


def _verify_exact_plan_scan(
    plan: PostgresMigration012Plan,
    schedule_map: PostgresScheduleMap,
    *,
    environment: dict[str, str] | None,
    statement_timeout_ms: int,
    lock_timeout_ms: int,
    max_rows_per_table: int,
    batch_size: int,
    max_row_bytes: int,
) -> None:
    scan = scan_postgres_migration_012(
        source=plan.source,
        target_learning=plan.target_learning,
        target_runtime=plan.target_runtime,
        schedule_map=schedule_map,
        scope_mappings=plan.scope_mappings,
        environment=environment,
        statement_timeout_ms=statement_timeout_ms,
        lock_timeout_ms=lock_timeout_ms,
        max_rows_per_table=max_rows_per_table,
        batch_size=batch_size,
        max_row_bytes=max_row_bytes,
    )
    if not scan.ready:
        raise Migration012Error(
            "MIGRATION_POSTGRES_TRANSFORM_SCAN_BLOCKED",
            "A fresh PostgreSQL scan contains blockers; transformation is refused.",
            blocker_count=len(scan.findings),
        )
    if (
        scan.endpoint_evidence_digests != plan.endpoint_evidence_digests
        or scan.table_evidence != plan.table_evidence
    ):
        raise Migration012Error(
            "MIGRATION_POSTGRES_PLAN_EVIDENCE_DRIFT",
            "PostgreSQL source or target evidence changed after plan review.",
        )


def preview_postgres_migration_012_transforms(
    *,
    plan: PostgresMigration012Plan,
    schedule_map: PostgresScheduleMap,
    environment: dict[str, str] | None = None,
    statement_timeout_ms: int = 60_000,
    lock_timeout_ms: int = 2_000,
    max_rows_per_table: int = 10_000_000,
    batch_size: int = 1_000,
    max_row_bytes: int = 16 * 1024 * 1024,
) -> PostgresMigration012TransformReport:
    """Compile every migration row without target writes or content in the report."""
    for name, value, lower, upper in (
        ("statement_timeout_ms", statement_timeout_ms, 1, 3_600_000),
        ("lock_timeout_ms", lock_timeout_ms, 1, 60_000),
        ("max_rows_per_table", max_rows_per_table, 1, 1_000_000_000),
        ("batch_size", batch_size, 1, 10_000),
        ("max_row_bytes", max_row_bytes, 1_024, 256 * 1024 * 1024),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or not lower <= value <= upper:
            raise ValueError(f"{name} must be between {lower} and {upper}")
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
    _verify_exact_plan_scan(
        plan,
        schedule_map,
        environment=environment,
        statement_timeout_ms=statement_timeout_ms,
        lock_timeout_ms=lock_timeout_ms,
        max_rows_per_table=max_rows_per_table,
        batch_size=batch_size,
        max_row_bytes=max_row_bytes,
    )

    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise Migration012Error(
            "MIGRATION_POSTGRES_DRIVER_UNAVAILABLE",
            "PostgreSQL transformation preview requires the agnoclaw postgres extra.",
            install_extra="postgres",
        ) from exc

    connection: Any = None
    try:
        connection = psycopg.connect(
            plan.source.resolve(environment),
            autocommit=True,
            connect_timeout=10,
            application_name="agnoclaw-migration-012-transform-preview",
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
        return _compile_postgres_migration_012_source_snapshot(
            connection=connection,
            sql=sql,
            plan=plan,
            schedule_map=schedule_map,
            max_rows_per_table=max_rows_per_table,
            batch_size=batch_size,
            max_row_bytes=max_row_bytes,
        )
    except Migration012Error:
        raise
    except Exception as exc:
        raise Migration012Error(
            "MIGRATION_POSTGRES_TRANSFORM_FAILED",
            "PostgreSQL transformation preview failed; driver details were redacted.",
            error_type=type(exc).__name__,
        ) from None
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass


__all__ = [
    "POSTGRES_MIGRATION_012_TRANSFORM_SCHEMA_VERSION",
    "PostgresMigration012TransformReport",
    "preview_postgres_migration_012_transforms",
]
