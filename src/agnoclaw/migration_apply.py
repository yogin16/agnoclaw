"""Crash-resumable 0.12 local persisted-data migration."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import shutil
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from .learning import LearningProfile, LearningScope
from .migration import (
    LegacyLearningScopeMapping,
    ScheduleMisfirePolicy,
    inspect_migration_012,
)
from .runtime.context import ExecutionContext
from .runtime.errors import HarnessError
from .runtime.scheduler import (
    RuntimeSchedulerBackend,
    SchedulerJob,
    next_schedule_time,
    scheduler_fence_path,
    scheduler_job_digest,
)
from .runtime.store import SQLiteRuntimeStore

MIGRATION_012_PLAN_SCHEMA_VERSION = "1.0"
MIGRATION_012_MANIFEST_SCHEMA_VERSION = "1.0"
MIGRATION_012_ROLLBACK_BOUNDARY = "before-explicit-schema-contraction-v1"
_CONTROL_FILE_LIMIT = 4 * 1024 * 1024


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _json_safe(value: Any) -> Any:
    """Return a deterministic JSON representation without unsafe repr leakage."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"$float": repr(value)}
    if isinstance(value, bytes):
        return {
            "$bytes_base64": base64.b64encode(value).decode("ascii"),
            "$bytes_size": len(value),
        }
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return {"$type": f"{type(value).__module__}.{type(value).__qualname__}"}


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _require_text(value: str | None, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write((_canonical_json(payload) + "\n").encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _bounded_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > _CONTROL_FILE_LIMIT:
        raise Migration012Error(
            "MIGRATION_CONTROL_FILE_INVALID",
            "The migration control file is absent or exceeds its bounded size.",
            path=str(path),
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Migration012Error(
            "MIGRATION_CONTROL_FILE_INVALID",
            "The migration control file is not valid UTF-8 JSON.",
            path=str(path),
        ) from exc
    if not isinstance(payload, dict):
        raise Migration012Error(
            "MIGRATION_CONTROL_FILE_INVALID",
            "The migration control file root must be an object.",
            path=str(path),
        )
    return payload


class Migration012Phase(StrEnum):
    BACKED_UP = "backed_up"
    APPLIED = "applied"
    VERIFIED = "verified"
    CUTOVER = "cutover"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class Migration012Error(HarnessError):
    """Safe, structured failure from the 0.12 migration workflow."""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(
            code=code,
            category="migration",
            message=message,
            retryable=False,
            details=details,
        )


@dataclass(frozen=True, slots=True)
class Migration012Plan:
    """Content-free, checksum-bound migration intent."""

    migration_id: str
    preflight_digest: str
    planned_at: str
    learning_source: str | None
    schedule_source: str | None
    target_learning_db: str | None
    target_runtime_db: str | None
    target_tenant_id: str | None
    target_org_id: str | None
    target_agent_id: str | None
    learning_tables: tuple[str, ...]
    scope_mappings: tuple[LegacyLearningScopeMapping, ...]
    schedule_timezone: str | None
    schedule_misfire_policy: ScheduleMisfirePolicy | None
    old_writer_fence_plan: str | None
    source_evidence: tuple[tuple[str, str, int], ...]
    rollback_boundary: str = MIGRATION_012_ROLLBACK_BOUNDARY
    schema_version: str = MIGRATION_012_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MIGRATION_012_PLAN_SCHEMA_VERSION:
            raise ValueError("unsupported migration plan schema version")
        _require_text(self.migration_id, "migration_id")
        _require_text(self.preflight_digest, "preflight_digest")
        if self.learning_source is not None:
            _require_text(self.target_learning_db, "target_learning_db")
            _require_text(self.target_tenant_id, "target_tenant_id")
            _require_text(self.target_agent_id, "target_agent_id")
        if self.schedule_source is not None:
            _require_text(self.target_runtime_db, "target_runtime_db")
            _require_text(self.old_writer_fence_plan, "old_writer_fence_plan")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "migration_id": self.migration_id,
            "preflight_digest": self.preflight_digest,
            "planned_at": self.planned_at,
            "sources": {
                "learning_sqlite": self.learning_source,
                "schedule_json": self.schedule_source,
            },
            "targets": {
                "learning_sqlite": self.target_learning_db,
                "runtime_sqlite": self.target_runtime_db,
            },
            "target_authority": {
                "tenant_id": self.target_tenant_id,
                "org_id": self.target_org_id,
                "agent_id": self.target_agent_id,
            },
            "learning_tables": list(self.learning_tables),
            "scope_mappings": [item.to_dict() for item in self.scope_mappings],
            "schedule_defaults": {
                "timezone": self.schedule_timezone,
                "misfire_policy": (
                    self.schedule_misfire_policy.value
                    if self.schedule_misfire_policy is not None
                    else None
                ),
            },
            "old_writer_fence_plan": self.old_writer_fence_plan,
            "source_evidence": [
                {"path": path, "checksum": checksum, "size_bytes": size}
                for path, checksum, size in self.source_evidence
            ],
            "rollback_boundary": self.rollback_boundary,
        }

    @property
    def plan_digest(self) -> str:
        return _digest(self._payload())

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "plan_digest": self.plan_digest}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Self:
        try:
            sources = payload["sources"]
            targets = payload["targets"]
            authority = payload["target_authority"]
            defaults = payload["schedule_defaults"]
            plan = cls(
                schema_version=str(payload["schema_version"]),
                migration_id=str(payload["migration_id"]),
                preflight_digest=str(payload["preflight_digest"]),
                planned_at=str(payload["planned_at"]),
                learning_source=sources.get("learning_sqlite"),
                schedule_source=sources.get("schedule_json"),
                target_learning_db=targets.get("learning_sqlite"),
                target_runtime_db=targets.get("runtime_sqlite"),
                target_tenant_id=authority.get("tenant_id"),
                target_org_id=authority.get("org_id"),
                target_agent_id=authority.get("agent_id"),
                learning_tables=tuple(str(item) for item in payload["learning_tables"]),
                scope_mappings=tuple(
                    LegacyLearningScopeMapping(**item) for item in payload["scope_mappings"]
                ),
                schedule_timezone=defaults.get("timezone"),
                schedule_misfire_policy=(
                    ScheduleMisfirePolicy(defaults["misfire_policy"])
                    if defaults.get("misfire_policy") is not None
                    else None
                ),
                old_writer_fence_plan=payload.get("old_writer_fence_plan"),
                source_evidence=tuple(
                    (str(item["path"]), str(item["checksum"]), int(item["size_bytes"]))
                    for item in payload["source_evidence"]
                ),
                rollback_boundary=str(payload["rollback_boundary"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise Migration012Error(
                "MIGRATION_PLAN_INVALID",
                "The migration plan does not match the certified schema.",
            ) from exc
        if payload.get("plan_digest") != plan.plan_digest:
            raise Migration012Error(
                "MIGRATION_PLAN_DIGEST_MISMATCH",
                "The migration plan digest does not match its content.",
            )
        return plan


def create_migration_012_plan(
    *,
    learning_sqlite_path: str | Path | None = None,
    schedule_json_path: str | Path | None = None,
    target_learning_db: str | Path | None = None,
    target_runtime_db: str | Path | None = None,
    target_tenant_id: str | None = None,
    target_org_id: str | None = None,
    target_agent_id: str | None = None,
    learning_table_names: tuple[str, ...] = (
        "agno_learnings",
        "agno_memories",
        "agnoclaw_memories",
    ),
    scope_mappings: tuple[LegacyLearningScopeMapping, ...] = (),
    schedule_default_timezone: str | None = None,
    schedule_default_misfire_policy: str | ScheduleMisfirePolicy | None = None,
    old_writer_fence_plan: str | None = None,
) -> Migration012Plan:
    """Build a deterministic-authority plan without copying or mutating data."""
    if learning_sqlite_path is None and schedule_json_path is None:
        raise ValueError("at least one legacy source is required")
    learning_source = _resolved(learning_sqlite_path) if learning_sqlite_path else None
    schedule_source = _resolved(schedule_json_path) if schedule_json_path else None
    learning_target = _resolved(target_learning_db) if target_learning_db else None
    runtime_target = _resolved(target_runtime_db) if target_runtime_db else None
    if learning_source is not None and learning_target is None:
        raise ValueError("target_learning_db is required for learning migration")
    if schedule_source is not None and runtime_target is None:
        raise ValueError("target_runtime_db is required for schedule migration")
    sources = {item for item in (learning_source, schedule_source) if item is not None}
    targets = {item for item in (learning_target, runtime_target) if item is not None}
    if sources & targets:
        raise ValueError("migration source and target paths must be distinct")
    report = inspect_migration_012(
        learning_sqlite_path=learning_source,
        schedule_json_path=schedule_source,
        learning_table_names=learning_table_names,
        scope_mappings=scope_mappings,
        schedule_default_timezone=schedule_default_timezone,
        schedule_default_misfire_policy=schedule_default_misfire_policy,
        old_writer_fence_plan=old_writer_fence_plan,
    )
    if not report.preflight_clear:
        raise Migration012Error(
            "MIGRATION_PREFLIGHT_BLOCKED",
            "The migration plan cannot be created while preflight blockers remain.",
            blocker_codes=sorted(
                finding.code for finding in report.findings if finding.severity.value == "blocker"
            ),
        )
    evidence = tuple(sorted((item.path, item.checksum, item.size_bytes) for item in report.files))
    identity = {
        "preflight_digest": report.report_digest,
        "targets": sorted(str(item) for item in targets),
        "tenant_id": target_tenant_id,
        "org_id": target_org_id,
        "agent_id": target_agent_id,
    }
    migration_id = "mig012_" + _digest(identity).split(":", 1)[1][:24]
    misfire = (
        ScheduleMisfirePolicy(schedule_default_misfire_policy)
        if schedule_default_misfire_policy is not None
        else None
    )
    plan = Migration012Plan(
        migration_id=migration_id,
        preflight_digest=report.report_digest,
        planned_at=_now(),
        learning_source=str(learning_source) if learning_source else None,
        schedule_source=str(schedule_source) if schedule_source else None,
        target_learning_db=str(learning_target) if learning_target else None,
        target_runtime_db=str(runtime_target) if runtime_target else None,
        target_tenant_id=target_tenant_id,
        target_org_id=target_org_id,
        target_agent_id=target_agent_id,
        learning_tables=learning_table_names,
        scope_mappings=tuple(scope_mappings),
        schedule_timezone=schedule_default_timezone,
        schedule_misfire_policy=misfire,
        old_writer_fence_plan=old_writer_fence_plan,
        source_evidence=evidence,
    )
    direct_rows, _ = _source_learning_rows(plan)
    direct_ids = [str(row["learning_id"]) for row in direct_rows]
    collision_count = len(direct_ids) - len(set(direct_ids))
    if collision_count:
        raise Migration012Error(
            "MIGRATION_TARGET_IDENTITY_COLLISION",
            "Legacy personal records collapse onto the same target learning identity.",
            collision_count=collision_count,
        )
    return plan


def write_migration_012_plan(path: str | Path, plan: Migration012Plan) -> Path:
    """Atomically write one validated local 0.12 migration plan."""
    target = _resolved(path)
    _atomic_json(target, plan.to_dict())
    return target


def read_migration_012_plan(path: str | Path) -> Migration012Plan:
    """Read and validate one bounded local 0.12 migration plan."""
    return Migration012Plan.from_dict(_bounded_json(_resolved(path)))


def _json_object(value: Any, *, wrapper: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {wrapper: value}
    if isinstance(value, dict):
        return _json_safe(value)
    return {wrapper: _json_safe(value)}


def _mapping_for(plan: Migration012Plan, namespace: Any, learning_type: str):
    normalized = str(namespace) if namespace is not None else None
    index = {item.key: item for item in plan.scope_mappings}
    return index.get((normalized, learning_type)) or index.get((normalized, None))


def _personal_scope(
    plan: Migration012Plan, row: dict[str, Any], learning_type: str
) -> LearningScope:
    context = ExecutionContext.create(
        tenant_id=plan.target_tenant_id,
        org_id=plan.target_org_id,
        user_id=str(row["user_id"]) if row.get("user_id") is not None else None,
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
        agent_id=str(plan.target_agent_id),
        consented=True,
    )


def _source_learning_rows(
    plan: Migration012Plan,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if plan.learning_source is None:
        return [], []
    direct: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    source_path = Path(plan.learning_source)
    connection = sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        available = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        for table in plan.learning_tables:
            if table not in available:
                continue
            columns = tuple(
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            )
            rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
            unified = {"learning_id", "learning_type", "content"}.issubset(columns)
            for source_row in rows:
                row = {column: _json_safe(source_row[column]) for column in columns}
                source_digest = _digest({"table": table, "row": row})
                learning_type = str(source_row["learning_type"]) if unified else "legacy_memory"
                if unified and learning_type in {
                    "user_profile",
                    "user_memory",
                    "session_context",
                }:
                    scope = _personal_scope(plan, row, learning_type)
                    if learning_type == "user_profile":
                        learning_id = f"user_profile_{scope.storage_user_id}"
                    elif learning_type == "user_memory":
                        learning_id = f"memories_{scope.storage_user_id}"
                    else:
                        learning_id = f"session_context_{scope.storage_session_id}"
                    metadata = _json_object(row.get("metadata"), wrapper="legacy_metadata")
                    metadata["agnoclaw_migration"] = {
                        "schema_version": "1.0",
                        "migration_id": plan.migration_id,
                        "source_digest": source_digest,
                    }
                    direct.append(
                        {
                            "learning_id": learning_id,
                            "learning_type": learning_type,
                            "namespace": None,
                            "user_id": scope.storage_user_id,
                            "agent_id": plan.target_agent_id,
                            "team_id": None,
                            "workflow_id": None,
                            "session_id": (
                                scope.storage_session_id
                                if learning_type == "session_context"
                                else None
                            ),
                            "entity_id": None,
                            "entity_type": None,
                            "content": _json_object(row.get("content"), wrapper="legacy_value"),
                            "metadata": metadata,
                            "created_at": int(row.get("created_at") or 0),
                            "updated_at": int(row.get("updated_at") or 0),
                        }
                    )
                    continue
                mapping = (
                    _mapping_for(plan, row.get("namespace"), learning_type) if unified else None
                )
                quarantine.append(
                    {
                        "source_digest": source_digest,
                        "migration_id": plan.migration_id,
                        "source_table": table,
                        "learning_type": learning_type,
                        "action": mapping.action.value if mapping else "quarantine",
                        "target_tenant_id": (mapping.target_tenant_id if mapping else None),
                        "target_namespace": (mapping.target_namespace if mapping else None),
                        "record_json": _canonical_json(row),
                    }
                )
    finally:
        connection.close()
    return direct, quarantine


def _create_learning_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS agno_learnings (
            learning_id VARCHAR PRIMARY KEY NOT NULL,
            learning_type VARCHAR NOT NULL,
            namespace VARCHAR,
            user_id VARCHAR,
            agent_id VARCHAR,
            team_id VARCHAR,
            workflow_id VARCHAR,
            session_id VARCHAR,
            entity_id VARCHAR,
            entity_type VARCHAR,
            content JSON NOT NULL,
            metadata JSON,
            created_at BIGINT NOT NULL,
            updated_at BIGINT
        );
        CREATE TABLE IF NOT EXISTS agnoclaw_migration_012_learning_quarantine (
            source_digest TEXT PRIMARY KEY,
            migration_id TEXT NOT NULL,
            source_table TEXT NOT NULL,
            learning_type TEXT NOT NULL,
            action TEXT NOT NULL,
            target_tenant_id TEXT,
            target_namespace TEXT,
            record_json TEXT NOT NULL
        );
        """
    )


def _stored_learning_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "content": _canonical_json(row["content"]),
        "metadata": _canonical_json(row["metadata"]),
    }


def _apply_learning(plan: Migration012Plan) -> dict[str, Any]:
    direct, quarantine = _source_learning_rows(plan)
    target = _resolved(str(plan.target_learning_db))
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        _create_learning_tables(connection)
        connection.execute("BEGIN IMMEDIATE")
        for row in direct:
            expected = _stored_learning_row(row)
            existing = connection.execute(
                "SELECT * FROM agno_learnings WHERE learning_id = ?",
                (row["learning_id"],),
            ).fetchone()
            if existing is not None:
                if {key: existing[key] for key in expected} != expected:
                    raise Migration012Error(
                        "MIGRATION_TARGET_CONFLICT",
                        "A target learning identity already contains different data.",
                        learning_id=row["learning_id"],
                    )
                continue
            names = ",".join(expected)
            placeholders = ",".join("?" for _ in expected)
            connection.execute(
                f"INSERT INTO agno_learnings({names}) VALUES ({placeholders})",
                tuple(expected.values()),
            )
        for row in quarantine:
            existing = connection.execute(
                "SELECT * FROM agnoclaw_migration_012_learning_quarantine WHERE source_digest = ?",
                (row["source_digest"],),
            ).fetchone()
            if existing is not None:
                if {key: existing[key] for key in row} != row:
                    raise Migration012Error(
                        "MIGRATION_TARGET_CONFLICT",
                        "A target quarantine identity already contains different data.",
                        source_digest=row["source_digest"],
                    )
                continue
            names = ",".join(row)
            placeholders = ",".join("?" for _ in row)
            connection.execute(
                "INSERT INTO agnoclaw_migration_012_learning_quarantine"
                f"({names}) VALUES ({placeholders})",
                tuple(row.values()),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "personal_rows": len(direct),
        "quarantined_rows": len(quarantine),
        "logical_digest": _digest({"direct": direct, "quarantine": quarantine}),
    }


def _job_setting(job: dict[str, Any], name: str) -> Any:
    if name in job:
        return job[name]
    metadata = job.get("metadata")
    return metadata.get(name) if isinstance(metadata, dict) else None


def _source_schedule(
    plan: Migration012Plan,
) -> tuple[list[SchedulerJob], list[dict[str, Any]]]:
    if plan.schedule_source is None:
        return [], []
    payload = json.loads(Path(plan.schedule_source).read_text(encoding="utf-8"))
    desired: list[SchedulerJob] = []
    for raw in payload.get("jobs", []):
        metadata = dict(raw.get("metadata") or {})
        metadata["agnoclaw_migration"] = {
            "schema_version": "1.0",
            "migration_id": plan.migration_id,
            "source_digest": _digest(raw),
        }
        legacy_misfire = _job_setting(raw, "misfire_policy") or plan.schedule_misfire_policy
        misfire = ScheduleMisfirePolicy(str(legacy_misfire))
        job = SchedulerJob(
            name=str(raw["name"]),
            schedule=str(raw["schedule"]),
            prompt=str(raw["prompt"]),
            skill=raw.get("skill"),
            isolated=bool(raw.get("isolated", False)),
            enabled=bool(raw.get("enabled", True)),
            timezone=str(_job_setting(raw, "timezone") or plan.schedule_timezone),
            max_retries=int(raw.get("max_retries", 0)),
            retry_delay_seconds=int(raw.get("retry_delay_seconds", 30)),
            retry_backoff_multiplier=float(raw.get("retry_backoff_multiplier", 2.0)),
            retry_max_delay_seconds=int(raw.get("retry_max_delay_seconds", 3600)),
            retry_jitter_seconds=int(raw.get("retry_jitter_seconds", 0)),
            jitter_seconds=int(raw.get("jitter_seconds", 0)),
            misfire_policy="skip" if misfire is ScheduleMisfirePolicy.SKIP else "fire_once",
            misfire_grace_seconds=int(raw.get("misfire_grace_seconds", 300)),
            concurrency_key=raw.get("concurrency_key"),
            overlap_policy=str(raw.get("overlap_policy") or "queue"),
            metadata=metadata,
            revision=1,
            created_at=plan.planned_at,
            updated_at=plan.planned_at,
        )
        next_run_at = next_schedule_time(job, after=plan.planned_at) if job.enabled else None
        legacy_next_run = raw.get("next_run_at")
        if job.enabled and misfire is ScheduleMisfirePolicy.RUN_ONCE and legacy_next_run:
            try:
                due_at = datetime.fromisoformat(str(legacy_next_run))
                if due_at.tzinfo is not None and due_at.astimezone(UTC) <= datetime.fromisoformat(
                    plan.planned_at
                ):
                    next_run_at = due_at.astimezone(UTC).isoformat()
            except ValueError:
                pass
        desired.append(replace(job, next_run_at=next_run_at))
    history: list[dict[str, Any]] = []
    for raw in payload.get("runs", []):
        history.append(
            {
                "source_digest": _digest(raw),
                "migration_id": plan.migration_id,
                "source_run_id": str(raw.get("run_id") or _digest(raw)),
                "job_name": str(raw.get("job_name")),
                "status": str(raw.get("status") or "unknown"),
                "record_json": _canonical_json(raw),
            }
        )
    return desired, history


def _apply_schedule(plan: Migration012Plan) -> dict[str, Any]:
    jobs, history = _source_schedule(plan)
    store = SQLiteRuntimeStore(str(plan.target_runtime_db))
    backend = RuntimeSchedulerBackend(store)
    try:
        for job in jobs:
            existing = backend.get_job(job.name)
            if existing is not None:
                if scheduler_job_digest(existing) != scheduler_job_digest(job):
                    raise Migration012Error(
                        "MIGRATION_TARGET_CONFLICT",
                        "A target scheduler identity already contains different behavior.",
                        job_name=job.name,
                    )
                continue
            backend.upsert_job(job)
        with store._transaction() as connection:  # noqa: SLF001
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agnoclaw_migration_012_schedule_history (
                    source_digest TEXT PRIMARY KEY,
                    migration_id TEXT NOT NULL,
                    source_run_id TEXT NOT NULL,
                    job_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL
                )
                """
            )
            for row in history:
                existing = connection.execute(
                    "SELECT * FROM agnoclaw_migration_012_schedule_history WHERE source_digest = ?",
                    (row["source_digest"],),
                ).fetchone()
                if existing is not None:
                    if {key: existing[key] for key in row} != row:
                        raise Migration012Error(
                            "MIGRATION_TARGET_CONFLICT",
                            "A target schedule-history identity contains different data.",
                            source_digest=row["source_digest"],
                        )
                    continue
                names = ",".join(row)
                placeholders = ",".join("?" for _ in row)
                connection.execute(
                    "INSERT INTO agnoclaw_migration_012_schedule_history"
                    f"({names}) VALUES ({placeholders})",
                    tuple(row.values()),
                )
    finally:
        store.close()
    return {
        "jobs": len(jobs),
        "archived_runs": len(history),
        "logical_digest": _digest({"jobs": [job.to_dict() for job in jobs], "history": history}),
    }


def _verify_source_evidence(plan: Migration012Plan) -> None:
    for path_text, expected, size in plan.source_evidence:
        path = Path(path_text)
        if not path.is_file() or path.stat().st_size != size or _file_digest(path) != expected:
            raise Migration012Error(
                "MIGRATION_SOURCE_DRIFT",
                "A source changed after planning; create a new plan from a frozen source.",
                source_path=str(path),
            )


def _write_fences(plan: Migration012Plan) -> list[str]:
    fences: list[str] = []
    for source_text, kind in (
        (plan.learning_source, "learning_sqlite"),
        (plan.schedule_source, "schedule_json"),
    ):
        if source_text is None:
            continue
        source = Path(source_text)
        marker = scheduler_fence_path(source)
        payload = {
            "schema_version": "1.0",
            "migration_id": plan.migration_id,
            "source_path": str(source),
            "source_checksum": _file_digest(source),
            "kind": kind,
            "fence_plan": plan.old_writer_fence_plan,
        }
        if marker.exists():
            if _bounded_json(marker) != payload:
                raise Migration012Error(
                    "MIGRATION_FENCE_CONFLICT",
                    "A different migration fence already exists for the source.",
                    fence_path=str(marker),
                )
        else:
            _atomic_json(marker, payload)
        fences.append(str(marker))
    return fences


def _sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    source_connection = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    _fsync_path(target)


def _fsync_path(path: Path) -> None:
    """Durably settle a local file and its directory entry."""
    if path.is_file():
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _backup_files(plan: Migration012Plan, state_dir: Path) -> list[dict[str, Any]]:
    backups: list[dict[str, Any]] = []
    if plan.learning_source is not None:
        source = Path(plan.learning_source)
        backup = state_dir / "source-learning.sqlite"
        _sqlite_backup(source, backup)
        backups.append(
            {
                "role": "source_learning",
                "path": str(source),
                "backup": str(backup),
                "existed": True,
                "backup_checksum": _file_digest(backup),
            }
        )
    if plan.schedule_source is not None:
        source = Path(plan.schedule_source)
        backup = state_dir / "source-schedule.json"
        temporary = backup.with_name(f".{backup.name}.tmp-{os.getpid()}")
        shutil.copy2(source, temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, backup)
        _fsync_path(backup)
        backups.append(
            {
                "role": "source_schedule",
                "path": str(source),
                "backup": str(backup),
                "existed": True,
                "backup_checksum": _file_digest(backup),
            }
        )
    seen: set[Path] = set()
    for role, target_text in (
        ("target_learning", plan.target_learning_db),
        ("target_runtime", plan.target_runtime_db),
    ):
        if target_text is None:
            continue
        target = Path(target_text)
        if target in seen:
            continue
        seen.add(target)
        existed = target.is_file()
        backup = state_dir / f"{role}.sqlite"
        checksum = None
        if existed:
            _sqlite_backup(target, backup)
            checksum = _file_digest(backup)
        backups.append(
            {
                "role": role,
                "path": str(target),
                "backup": str(backup),
                "existed": existed,
                "backup_checksum": checksum,
            }
        )
    return backups


def _target_digests(plan: Migration012Plan) -> dict[str, str]:
    paths = {
        Path(item) for item in (plan.target_learning_db, plan.target_runtime_db) if item is not None
    }
    evidence: dict[str, str] = {}
    for path in sorted(paths):
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            if candidate.is_file():
                evidence[str(candidate)] = _file_digest(candidate)
    return evidence


def _manifest_path(state_dir: str | Path) -> Path:
    return _resolved(state_dir) / "manifest.json"


def _load_manifest(state_dir: str | Path) -> dict[str, Any]:
    payload = _bounded_json(_manifest_path(state_dir))
    if payload.get("schema_version") != MIGRATION_012_MANIFEST_SCHEMA_VERSION:
        raise Migration012Error(
            "MIGRATION_MANIFEST_INVALID",
            "The migration manifest schema version is unsupported.",
        )
    digest = payload.pop("manifest_digest", None)
    if digest != _digest(payload):
        raise Migration012Error(
            "MIGRATION_MANIFEST_DIGEST_MISMATCH",
            "The migration manifest digest does not match its content.",
        )
    payload["manifest_digest"] = digest
    return payload


def _save_manifest(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    value = {key: item for key, item in payload.items() if key != "manifest_digest"}
    value["updated_at"] = _now()
    value["manifest_digest"] = _digest(value)
    _atomic_json(path, value)
    return value


def apply_migration_012(
    plan: Migration012Plan,
    *,
    state_dir: str | Path,
    confirm_plan_digest: str,
    writers_stopped: bool,
) -> dict[str, Any]:
    """Fence writers, back up every store, and idempotently import local data."""
    if confirm_plan_digest != plan.plan_digest:
        raise Migration012Error(
            "MIGRATION_CONFIRMATION_MISMATCH",
            "Apply requires the exact plan digest printed by the plan command.",
        )
    if not writers_stopped:
        raise Migration012Error(
            "MIGRATION_WRITERS_NOT_CONFIRMED_STOPPED",
            "Stop legacy and target writers before apply and confirm that action.",
        )
    directory = _resolved(state_dir)
    protected = {
        _resolved(item)
        for item in (
            plan.learning_source,
            plan.schedule_source,
            plan.target_learning_db,
            plan.target_runtime_db,
        )
        if item is not None
    }
    if directory in protected:
        raise ValueError("state_dir must be distinct from source and target paths")
    manifest_path = _manifest_path(directory)
    if manifest_path.exists():
        manifest = _load_manifest(directory)
        if manifest.get("plan_digest") != plan.plan_digest:
            raise Migration012Error(
                "MIGRATION_STATE_CONFLICT",
                "The state directory belongs to a different migration plan.",
            )
        if manifest.get("phase") in {
            Migration012Phase.APPLIED.value,
            Migration012Phase.VERIFIED.value,
            Migration012Phase.CUTOVER.value,
        }:
            return manifest
        if manifest.get("phase") == Migration012Phase.ROLLED_BACK.value:
            raise Migration012Error(
                "MIGRATION_ALREADY_ROLLED_BACK",
                "A rolled-back state directory cannot be reused for apply.",
            )
    else:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        _verify_source_evidence(plan)
        fences = _write_fences(plan)
        backups = _backup_files(plan, directory)
        manifest = _save_manifest(
            manifest_path,
            {
                "schema_version": MIGRATION_012_MANIFEST_SCHEMA_VERSION,
                "migration_id": plan.migration_id,
                "plan_digest": plan.plan_digest,
                "plan": plan.to_dict(),
                "phase": Migration012Phase.BACKED_UP.value,
                "created_at": _now(),
                "rollback_boundary": plan.rollback_boundary,
                "rollback_available": True,
                "fences": fences,
                "backups": backups,
                "imports": {},
                "target_post_digests": {},
            },
        )
    _verify_source_evidence(plan)
    imports: dict[str, Any] = {}
    if plan.learning_source is not None:
        imports["learning"] = _apply_learning(plan)
    if plan.schedule_source is not None:
        imports["schedule"] = _apply_schedule(plan)
    manifest["imports"] = imports
    manifest["target_post_digests"] = _target_digests(plan)
    manifest["phase"] = Migration012Phase.APPLIED.value
    return _save_manifest(manifest_path, manifest)


def verify_migration_012(*, state_dir: str | Path) -> dict[str, Any]:
    """Recompute transformations and compare every imported target identity."""
    manifest = _load_manifest(state_dir)
    if manifest["phase"] == Migration012Phase.ROLLED_BACK.value:
        raise Migration012Error(
            "MIGRATION_ALREADY_ROLLED_BACK",
            "A rolled-back migration has no active target to verify.",
        )
    plan = Migration012Plan.from_dict(manifest["plan"])
    _verify_source_evidence(plan)
    observed: dict[str, Any] = {}
    if plan.learning_source is not None:
        direct, quarantine = _source_learning_rows(plan)
        connection = sqlite3.connect(str(plan.target_learning_db))
        connection.row_factory = sqlite3.Row
        try:
            for row in direct:
                expected = _stored_learning_row(row)
                target = connection.execute(
                    "SELECT * FROM agno_learnings WHERE learning_id = ?",
                    (row["learning_id"],),
                ).fetchone()
                if target is None or {key: target[key] for key in expected} != expected:
                    raise Migration012Error(
                        "MIGRATION_VERIFICATION_FAILED",
                        "An imported learning identity failed exact verification.",
                        learning_id=row["learning_id"],
                    )
            for row in quarantine:
                target = connection.execute(
                    "SELECT * FROM agnoclaw_migration_012_learning_quarantine "
                    "WHERE source_digest = ?",
                    (row["source_digest"],),
                ).fetchone()
                if target is None or {key: target[key] for key in row} != row:
                    raise Migration012Error(
                        "MIGRATION_VERIFICATION_FAILED",
                        "An imported quarantine identity failed exact verification.",
                        source_digest=row["source_digest"],
                    )
        finally:
            connection.close()
        observed["learning"] = {
            "personal_rows": len(direct),
            "quarantined_rows": len(quarantine),
            "logical_digest": _digest({"direct": direct, "quarantine": quarantine}),
        }
    if plan.schedule_source is not None:
        jobs, history = _source_schedule(plan)
        store = SQLiteRuntimeStore(str(plan.target_runtime_db))
        backend = RuntimeSchedulerBackend(store)
        try:
            for job in jobs:
                target = backend.get_job(job.name)
                if target is None or scheduler_job_digest(target) != scheduler_job_digest(job):
                    raise Migration012Error(
                        "MIGRATION_VERIFICATION_FAILED",
                        "An imported scheduler job failed behavior verification.",
                        job_name=job.name,
                    )
            with store._lock:  # noqa: SLF001
                for row in history:
                    target = store._connection.execute(  # noqa: SLF001
                        "SELECT * FROM agnoclaw_migration_012_schedule_history "
                        "WHERE source_digest = ?",
                        (row["source_digest"],),
                    ).fetchone()
                    if target is None or {key: target[key] for key in row} != row:
                        raise Migration012Error(
                            "MIGRATION_VERIFICATION_FAILED",
                            "An archived scheduler run failed exact verification.",
                            source_digest=row["source_digest"],
                        )
        finally:
            store.close()
        observed["schedule"] = {
            "jobs": len(jobs),
            "archived_runs": len(history),
            "logical_digest": _digest(
                {"jobs": [job.to_dict() for job in jobs], "history": history}
            ),
        }
    if observed != manifest["imports"]:
        raise Migration012Error(
            "MIGRATION_VERIFICATION_FAILED",
            "Target counts or logical digests differ from the apply manifest.",
        )
    manifest["verification"] = {"verified_at": _now(), "imports": observed}
    if manifest["phase"] != Migration012Phase.CUTOVER.value:
        manifest["phase"] = Migration012Phase.VERIFIED.value
    return _save_manifest(_manifest_path(state_dir), manifest)


def cutover_migration_012(*, state_dir: str | Path, confirm_migration_id: str) -> dict[str, Any]:
    """Record explicit cutover only after independent verification."""
    manifest = _load_manifest(state_dir)
    if manifest["migration_id"] != confirm_migration_id:
        raise Migration012Error(
            "MIGRATION_CONFIRMATION_MISMATCH",
            "Cutover requires the exact migration identifier.",
        )
    if manifest["phase"] not in {
        Migration012Phase.VERIFIED.value,
        Migration012Phase.CUTOVER.value,
    }:
        raise Migration012Error(
            "MIGRATION_NOT_VERIFIED",
            "Verify the imported data before cutover.",
        )
    marker = _resolved(state_dir) / "cutover.json"
    if not marker.exists():
        _atomic_json(
            marker,
            {
                "schema_version": "1.0",
                "migration_id": manifest["migration_id"],
                "plan_digest": manifest["plan_digest"],
                "rollback_boundary": manifest["rollback_boundary"],
            },
        )
    manifest["phase"] = Migration012Phase.CUTOVER.value
    manifest["cutover_marker"] = str(marker)
    return _save_manifest(_manifest_path(state_dir), manifest)


def _target_evidence(target: Path) -> dict[str, str]:
    return {
        str(candidate): _file_digest(candidate)
        for candidate in (target, Path(f"{target}-wal"), Path(f"{target}-shm"))
        if candidate.is_file()
    }


def _rollback_target_is_safe(record: dict[str, Any], post_digests: dict[str, str]) -> bool:
    target = Path(record["path"])
    current = _target_evidence(target)
    post = {
        key: value
        for key, value in post_digests.items()
        if key == str(target) or key in {f"{target}-wal", f"{target}-shm"}
    }
    restored = {str(target): str(record["backup_checksum"])} if record["existed"] else {}
    return current == post or current == restored


def rollback_migration_012(
    *,
    state_dir: str | Path,
    confirm_migration_id: str,
    writers_stopped: bool,
) -> dict[str, Any]:
    """Restore target preimages and release legacy writer fences."""
    manifest = _load_manifest(state_dir)
    if manifest["migration_id"] != confirm_migration_id:
        raise Migration012Error(
            "MIGRATION_CONFIRMATION_MISMATCH",
            "Rollback requires the exact migration identifier.",
        )
    if not writers_stopped:
        raise Migration012Error(
            "MIGRATION_WRITERS_NOT_CONFIRMED_STOPPED",
            "Stop every target writer before rollback and confirm that action.",
        )
    if manifest["phase"] == Migration012Phase.ROLLED_BACK.value:
        return manifest
    if not manifest.get("rollback_available"):
        raise Migration012Error(
            "MIGRATION_ROLLBACK_UNAVAILABLE",
            "Rollback is no longer available for this migration state.",
        )
    plan = Migration012Plan.from_dict(manifest["plan"])
    target_records = [
        record for record in manifest["backups"] if str(record["role"]).startswith("target_")
    ]
    if any(
        not _rollback_target_is_safe(record, manifest["target_post_digests"])
        for record in target_records
    ):
        raise Migration012Error(
            "MIGRATION_TARGET_DRIFT",
            "A target changed after apply; rollback would discard newer data.",
        )
    for record in target_records:
        if record["existed"]:
            backup = Path(record["backup"])
            if not backup.is_file() or _file_digest(backup) != record["backup_checksum"]:
                raise Migration012Error(
                    "MIGRATION_BACKUP_CORRUPT",
                    "A rollback backup no longer matches its verified checksum.",
                    backup_path=str(backup),
                )
    _verify_source_evidence(plan)
    if manifest["phase"] != Migration012Phase.ROLLING_BACK.value:
        manifest["phase"] = Migration012Phase.ROLLING_BACK.value
        manifest["rollback_started_at"] = _now()
        manifest = _save_manifest(_manifest_path(state_dir), manifest)
    restored: set[str] = set()
    for record in target_records:
        target = Path(record["path"])
        if str(target) in restored:
            continue
        restored.add(str(target))
        for suffix in ("-wal", "-shm"):
            Path(f"{target}{suffix}").unlink(missing_ok=True)
        if record["existed"]:
            backup = Path(record["backup"])
            temporary = target.with_name(f".{target.name}.rollback-{os.getpid()}")
            shutil.copy2(backup, temporary)
            os.replace(temporary, target)
            _fsync_path(target)
        else:
            target.unlink(missing_ok=True)
            _fsync_path(target)
    for marker_text in manifest["fences"]:
        Path(marker_text).unlink(missing_ok=True)
    if manifest.get("cutover_marker"):
        Path(manifest["cutover_marker"]).unlink(missing_ok=True)
    manifest["phase"] = Migration012Phase.ROLLED_BACK.value
    manifest["rollback_available"] = False
    manifest["rolled_back_at"] = _now()
    return _save_manifest(_manifest_path(state_dir), manifest)


__all__ = [
    "MIGRATION_012_MANIFEST_SCHEMA_VERSION",
    "MIGRATION_012_PLAN_SCHEMA_VERSION",
    "MIGRATION_012_ROLLBACK_BOUNDARY",
    "Migration012Error",
    "Migration012Phase",
    "Migration012Plan",
    "apply_migration_012",
    "create_migration_012_plan",
    "cutover_migration_012",
    "read_migration_012_plan",
    "rollback_migration_012",
    "verify_migration_012",
    "write_migration_012_plan",
]
