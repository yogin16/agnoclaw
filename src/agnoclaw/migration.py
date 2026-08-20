"""Read-only 0.12 migration preflight for legacy learning and schedules."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIGRATION_PREFLIGHT_SCHEMA_VERSION = "1.0"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INTERVAL_RE = re.compile(r"^(?:[1-9][0-9]*(?:\.[0-9]+)?[smhd])+$")
_DEFAULT_LEARNING_TABLES = (
    "agno_learnings",
    "agno_memories",
    "agnoclaw_memories",
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
    payload = _canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def _stat_signature(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, getattr(stat, "st_ino", 0)


def _sql_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"$float": repr(value)}
    if isinstance(value, bytes):
        return {
            "$bytes_digest": f"sha256:{hashlib.sha256(value).hexdigest()}",
            "$bytes_size": len(value),
        }
    return {"$type": f"{type(value).__module__}.{type(value).__qualname__}"}


def _require_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


class MigrationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class LegacyScopeAction(StrEnum):
    MAP = "map"
    QUARANTINE = "quarantine"


class ScheduleMisfirePolicy(StrEnum):
    SKIP = "skip"
    RUN_ONCE = "run_once"


@dataclass(frozen=True, slots=True)
class LegacyLearningScopeMapping:
    """Explicit map-or-quarantine decision for one legacy namespace/type."""

    source_namespace: str | None
    action: LegacyScopeAction
    learning_type: str | None = None
    target_tenant_id: str | None = None
    target_namespace: str | None = None

    def __post_init__(self) -> None:
        if self.source_namespace is not None:
            _require_text(self.source_namespace, field_name="source_namespace")
        if self.learning_type is not None:
            _require_text(self.learning_type, field_name="learning_type")
        object.__setattr__(self, "action", LegacyScopeAction(self.action))
        if self.action is LegacyScopeAction.MAP:
            if self.target_tenant_id is None or self.target_namespace is None:
                raise ValueError("map decisions require target tenant and namespace")
            _require_text(self.target_tenant_id, field_name="target_tenant_id")
            _require_text(self.target_namespace, field_name="target_namespace")
        elif self.target_tenant_id is not None or self.target_namespace is not None:
            raise ValueError("quarantine decisions cannot declare a target scope")

    @property
    def key(self) -> tuple[str | None, str | None]:
        return self.source_namespace, self.learning_type

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_namespace": self.source_namespace,
            "learning_type": self.learning_type,
            "action": self.action.value,
            "target_tenant_id": self.target_tenant_id,
            "target_namespace": self.target_namespace,
        }


@dataclass(frozen=True, slots=True)
class MigrationFinding:
    code: str
    severity: MigrationSeverity
    source: str
    safe_message: str
    resolution: str
    count: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("code", "source", "safe_message", "resolution"):
            _require_text(getattr(self, field_name), field_name=field_name)
        object.__setattr__(self, "severity", MigrationSeverity(self.severity))
        if self.count is not None and self.count < 0:
            raise ValueError("finding count cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "source": self.source,
            "safe_message": self.safe_message,
            "resolution": self.resolution,
            "count": self.count,
        }


@dataclass(frozen=True, slots=True)
class MigrationFileEvidence:
    role: str
    path: str
    size_bytes: int
    checksum: str

    def __post_init__(self) -> None:
        _require_text(self.role, field_name="role")
        _require_text(self.path, field_name="path")
        if self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.checksum):
            raise ValueError("checksum must be a canonical sha256 digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "checksum": self.checksum,
        }


@dataclass(frozen=True, slots=True)
class LearningTableInventory:
    table_name: str
    shape: str
    row_count: int
    scanned_rows: int
    schema_digest: str
    logical_digest: str | None
    learning_type_counts: tuple[tuple[str, int], ...] = ()
    namespace_counts: tuple[tuple[str | None, int], ...] = ()
    ownership_gap_count: int = 0
    collision_count: int = 0

    def __post_init__(self) -> None:
        _require_text(self.table_name, field_name="table_name")
        _require_text(self.shape, field_name="shape")
        for value in (
            self.row_count,
            self.scanned_rows,
            self.ownership_gap_count,
            self.collision_count,
        ):
            if value < 0:
                raise ValueError("inventory counts cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_name": self.table_name,
            "shape": self.shape,
            "row_count": self.row_count,
            "scanned_rows": self.scanned_rows,
            "schema_digest": self.schema_digest,
            "logical_digest": self.logical_digest,
            "learning_type_counts": dict(self.learning_type_counts),
            "namespace_counts": [
                {"namespace": namespace, "count": count}
                for namespace, count in self.namespace_counts
            ],
            "ownership_gap_count": self.ownership_gap_count,
            "collision_count": self.collision_count,
        }


@dataclass(frozen=True, slots=True)
class ScheduleInventory:
    format_version: str
    job_count: int
    run_count: int
    logical_digest: str
    duplicate_job_count: int
    missing_timezone_count: int
    missing_misfire_count: int
    invalid_schedule_count: int
    unknown_job_run_count: int
    in_flight_run_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "job_count": self.job_count,
            "run_count": self.run_count,
            "logical_digest": self.logical_digest,
            "duplicate_job_count": self.duplicate_job_count,
            "missing_timezone_count": self.missing_timezone_count,
            "missing_misfire_count": self.missing_misfire_count,
            "invalid_schedule_count": self.invalid_schedule_count,
            "unknown_job_run_count": self.unknown_job_run_count,
            "in_flight_run_count": self.in_flight_run_count,
        }


@dataclass(frozen=True, slots=True)
class MigrationPreflightReport:
    schema_version: str
    files: tuple[MigrationFileEvidence, ...]
    learning_tables: tuple[LearningTableInventory, ...]
    schedule: ScheduleInventory | None
    scope_mappings: tuple[LegacyLearningScopeMapping, ...]
    findings: tuple[MigrationFinding, ...]
    planned_actions: tuple[str, ...]
    read_only: bool = True
    apply_allowed: bool = False

    @property
    def blocker_count(self) -> int:
        return sum(item.severity is MigrationSeverity.BLOCKER for item in self.findings)

    @property
    def preflight_clear(self) -> bool:
        return self.blocker_count == 0

    @property
    def report_digest(self) -> str:
        return _digest(self._payload())

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "files": [item.to_dict() for item in self.files],
            "learning_tables": [item.to_dict() for item in self.learning_tables],
            "schedule": self.schedule.to_dict() if self.schedule is not None else None,
            "scope_mappings": [item.to_dict() for item in self.scope_mappings],
            "findings": [item.to_dict() for item in self.findings],
            "planned_actions": list(self.planned_actions),
            "read_only": self.read_only,
            "apply_allowed": self.apply_allowed,
            "preflight_clear": self.preflight_clear,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "report_digest": self.report_digest}


@dataclass(slots=True)
class _PreflightBuilder:
    files: list[MigrationFileEvidence] = field(default_factory=list)
    learning_tables: list[LearningTableInventory] = field(default_factory=list)
    schedule: ScheduleInventory | None = None
    findings: list[MigrationFinding] = field(default_factory=list)

    def finding(
        self,
        code: str,
        severity: MigrationSeverity,
        source: str,
        safe_message: str,
        resolution: str,
        *,
        count: int | None = None,
    ) -> None:
        self.findings.append(
            MigrationFinding(
                code=code,
                severity=severity,
                source=source,
                safe_message=safe_message,
                resolution=resolution,
                count=count,
            )
        )


def _file_evidence(path: Path, *, role: str) -> MigrationFileEvidence:
    return MigrationFileEvidence(
        role=role,
        path=str(path),
        size_bytes=path.stat().st_size,
        checksum=_file_digest(path),
    )


def _sqlite_companions(path: Path) -> tuple[Path, ...]:
    return tuple(
        candidate
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists() and candidate.is_file()
    )


def _quoted_identifier(value: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"invalid SQLite table identifier: {value!r}")
    return f'"{value}"'


def _row_digest(columns: tuple[str, ...], row: sqlite3.Row) -> bytes:
    normalized = {column: _sql_value(row[column]) for column in columns}
    return hashlib.sha256(_canonical_json(normalized).encode("utf-8")).digest()


def _aggregate_row_digest(row_digests: list[bytes]) -> str:
    hasher = hashlib.sha256()
    for row_digest in sorted(row_digests):
        hasher.update(len(row_digest).to_bytes(4, "big"))
        hasher.update(row_digest)
    return f"sha256:{hasher.hexdigest()}"


def _count_pairs(values: Iterable[str | None]) -> tuple[tuple[str | None, int], ...]:
    counts: dict[str | None, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return tuple(sorted(counts.items(), key=lambda item: (item[0] is not None, item[0] or "")))


def _mapping_for(
    mappings: dict[tuple[str | None, str | None], LegacyLearningScopeMapping],
    namespace: str | None,
    learning_type: str,
) -> LegacyLearningScopeMapping | None:
    return mappings.get((namespace, learning_type)) or mappings.get((namespace, None))


def _inspect_learning_table(
    connection: sqlite3.Connection,
    table_name: str,
    *,
    max_rows: int,
) -> tuple[LearningTableInventory, list[tuple[str | None, str]]]:
    quoted = _quoted_identifier(table_name)
    schema_rows = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
    columns = tuple(str(row[1]) for row in schema_rows)
    schema = [
        {
            "name": str(row[1]),
            "type": str(row[2]),
            "not_null": bool(row[3]),
            "primary_key": int(row[5]),
        }
        for row in schema_rows
    ]
    row_count = int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
    if {"learning_id", "learning_type", "content"}.issubset(columns):
        shape = "agno_unified_learning"
    elif {"memory_id", "memory"}.issubset(columns):
        shape = "agno_legacy_memory"
    else:
        return (
            LearningTableInventory(
                table_name=table_name,
                shape="unsupported",
                row_count=row_count,
                scanned_rows=0,
                schema_digest=_digest(schema),
                logical_digest=None,
            ),
            [],
        )

    if row_count > max_rows:
        return (
            LearningTableInventory(
                table_name=table_name,
                shape=shape,
                row_count=row_count,
                scanned_rows=0,
                schema_digest=_digest(schema),
                logical_digest=None,
            ),
            [],
        )

    connection.row_factory = sqlite3.Row
    rows = connection.execute(f"SELECT * FROM {quoted}").fetchall()
    row_digests = [_row_digest(columns, row) for row in rows]
    unresolved_scopes: list[tuple[str | None, str]] = []
    learning_types: list[str] = []
    namespaces: list[str | None] = []
    ownership_gap_count = 0
    collision_count = 0

    if shape == "agno_unified_learning":
        scope_counts: dict[tuple[Any, ...], int] = {}
        identity_columns = tuple(
            column
            for column in (
                "learning_type",
                "namespace",
                "user_id",
                "agent_id",
                "team_id",
                "workflow_id",
                "session_id",
                "entity_id",
                "entity_type",
            )
            if column in columns
        )
        for row in rows:
            learning_type = str(row["learning_type"] or "<null>")
            namespace = row["namespace"] if "namespace" in columns else None
            namespace = str(namespace) if namespace is not None else None
            learning_types.append(learning_type)
            namespaces.append(namespace)
            if learning_type not in {"user_profile", "user_memory", "session_context"}:
                unresolved_scopes.append((namespace, learning_type))
            if learning_type in {"user_profile", "user_memory"}:
                owner_missing = "user_id" not in columns or row["user_id"] is None
            elif learning_type == "session_context":
                owner_missing = "session_id" not in columns or row["session_id"] is None
            else:
                owner_missing = False  # institutional scope is resolved by an explicit map
            if owner_missing:
                ownership_gap_count += 1
            key = tuple(_sql_value(row[column]) for column in identity_columns)
            scope_counts[key] = scope_counts.get(key, 0) + 1
        collision_count = sum(count - 1 for count in scope_counts.values() if count > 1)
    else:
        ids: dict[Any, int] = {}
        for row in rows:
            memory_id = _sql_value(row["memory_id"])
            ids[memory_id] = ids.get(memory_id, 0) + 1
            if not any(
                column in columns and row[column] is not None
                for column in ("user_id", "agent_id", "team_id")
            ):
                ownership_gap_count += 1
        collision_count = sum(count - 1 for count in ids.values() if count > 1)

    return (
        LearningTableInventory(
            table_name=table_name,
            shape=shape,
            row_count=row_count,
            scanned_rows=len(rows),
            schema_digest=_digest(schema),
            logical_digest=_aggregate_row_digest(row_digests),
            learning_type_counts=tuple(
                (str(key), value) for key, value in _count_pairs(learning_types)
            ),
            namespace_counts=_count_pairs(namespaces),
            ownership_gap_count=ownership_gap_count,
            collision_count=collision_count,
        ),
        unresolved_scopes,
    )


def _inspect_learning_sqlite(
    builder: _PreflightBuilder,
    path: Path,
    *,
    table_names: tuple[str, ...],
    mappings: dict[tuple[str | None, str | None], LegacyLearningScopeMapping],
    max_rows: int,
    max_bytes: int,
) -> None:
    source = "learning_sqlite"
    if not path.exists() or not path.is_file():
        builder.finding(
            "MIGRATION_LEARNING_SOURCE_MISSING",
            MigrationSeverity.WARNING,
            source,
            "The configured legacy learning SQLite file does not exist.",
            "Confirm the path or omit this source when there is no legacy learning data.",
        )
        return

    try:
        companions = _sqlite_companions(path)
        before = {candidate: _stat_signature(candidate) for candidate in companions}
        for candidate in companions:
            if candidate == path:
                role = "learning_sqlite"
            elif str(candidate).endswith("-wal"):
                role = "learning_sqlite_wal"
            else:
                role = "learning_sqlite_shm"
            builder.files.append(_file_evidence(candidate, role=role))
    except OSError:
        builder.finding(
            "MIGRATION_LEARNING_SOURCE_INVALID",
            MigrationSeverity.BLOCKER,
            source,
            "The legacy learning database could not be read safely.",
            "Verify the path and permissions; raw filesystem errors are redacted.",
        )
        return
    total_bytes = sum(signature[0] for signature in before.values())
    if total_bytes > max_bytes:
        builder.finding(
            "MIGRATION_LEARNING_SOURCE_TOO_LARGE",
            MigrationSeverity.BLOCKER,
            source,
            "The learning database exceeds the bounded inspection size limit.",
            "Raise max_learning_bytes explicitly or use a reviewed service-scale scanner.",
            count=total_bytes,
        )
        return
    if len(companions) > 1:
        builder.finding(
            "MIGRATION_LEARNING_LIVE_WAL",
            MigrationSeverity.BLOCKER,
            source,
            "The learning database has live SQLite sidecar files.",
            "Freeze the old writer, checkpoint it, and take a verified backup "
            "before planning copy.",
        )

    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            available = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            selected = [name for name in table_names if name in available]
            if not selected:
                builder.finding(
                    "MIGRATION_LEARNING_TABLES_NOT_FOUND",
                    MigrationSeverity.WARNING,
                    source,
                    "No configured legacy Agno learning tables were found.",
                    "Confirm custom table names or omit this source if it contains "
                    "no learning data.",
                )
            for table_name in selected:
                inventory, unresolved_scopes = _inspect_learning_table(
                    connection,
                    table_name,
                    max_rows=max_rows,
                )
                builder.learning_tables.append(inventory)
                table_source = f"learning_sqlite:{table_name}"
                if inventory.shape == "unsupported":
                    builder.finding(
                        "MIGRATION_LEARNING_SCHEMA_UNSUPPORTED",
                        MigrationSeverity.BLOCKER,
                        table_source,
                        "The configured table does not match a certified Agno learning schema.",
                        "Provide the correct table or add a reviewed schema adapter.",
                    )
                    continue
                if inventory.row_count > max_rows:
                    builder.finding(
                        "MIGRATION_LEARNING_SCAN_LIMIT_EXCEEDED",
                        MigrationSeverity.BLOCKER,
                        table_source,
                        "The learning table exceeds the bounded preflight scan limit.",
                        "Raise max_learning_rows explicitly or use a reviewed "
                        "service-scale scanner.",
                        count=inventory.row_count,
                    )
                    continue
                if inventory.ownership_gap_count:
                    builder.finding(
                        "MIGRATION_LEARNING_OWNER_MISSING",
                        MigrationSeverity.BLOCKER,
                        table_source,
                        "Legacy personal/session learning rows lack their required "
                        "user or session owner.",
                        "Map the rows to an explicit scope or quarantine them before copy.",
                        count=inventory.ownership_gap_count,
                    )
                if inventory.collision_count:
                    builder.finding(
                        "MIGRATION_LEARNING_SCOPE_COLLISION",
                        MigrationSeverity.BLOCKER,
                        table_source,
                        "Multiple legacy rows resolve to the same logical learning identity.",
                        "Choose a deterministic merge, supersession, or quarantine decision.",
                        count=inventory.collision_count,
                    )
                unresolved = {
                    item
                    for item in unresolved_scopes
                    if _mapping_for(mappings, item[0], item[1]) is None
                }
                if unresolved:
                    builder.finding(
                        "MIGRATION_LEARNING_SCOPE_UNRESOLVED",
                        MigrationSeverity.BLOCKER,
                        table_source,
                        "Legacy institutional namespaces lack an explicit "
                        "map-or-quarantine decision.",
                        "Provide LegacyLearningScopeMapping for each reported namespace/type.",
                        count=len(unresolved),
                    )
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError):
        builder.finding(
            "MIGRATION_LEARNING_SOURCE_INVALID",
            MigrationSeverity.BLOCKER,
            source,
            "The legacy learning database could not be inspected safely.",
            "Verify the path, permissions, integrity, and SQLite format; raw errors are redacted.",
        )

    try:
        after_candidates = _sqlite_companions(path)
        after = {candidate: _stat_signature(candidate) for candidate in after_candidates}
    except OSError:
        after = {}
    if before != after:
        builder.finding(
            "MIGRATION_SOURCE_CHANGED_DURING_SCAN",
            MigrationSeverity.BLOCKER,
            source,
            "The legacy learning source changed during preflight.",
            "Freeze the old writer and rerun against a verified snapshot.",
        )


def _job_setting(job: dict[str, Any], name: str) -> Any:
    if name in job:
        return job[name]
    metadata = job.get("metadata")
    return metadata.get(name) if isinstance(metadata, dict) else None


def _valid_schedule(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    value = value.strip()
    return bool(_INTERVAL_RE.fullmatch(value) or len(value.split()) == 5)


def _inspect_schedule_json(
    builder: _PreflightBuilder,
    path: Path,
    *,
    default_timezone: str | None,
    default_misfire_policy: ScheduleMisfirePolicy | None,
    old_writer_fence_plan: str | None,
    max_bytes: int,
) -> None:
    source = "schedule_json"
    if not path.exists() or not path.is_file():
        builder.finding(
            "MIGRATION_SCHEDULE_SOURCE_MISSING",
            MigrationSeverity.WARNING,
            source,
            "The configured legacy schedule JSON file does not exist.",
            "Confirm the path or omit this source when there are no legacy schedules.",
        )
        return
    try:
        before = _stat_signature(path)
        builder.files.append(_file_evidence(path, role=source))
    except OSError:
        builder.finding(
            "MIGRATION_SCHEDULE_SOURCE_INVALID",
            MigrationSeverity.BLOCKER,
            source,
            "The legacy schedule source could not be read safely.",
            "Verify the path and permissions; raw filesystem errors are redacted.",
        )
        return
    if before[0] > max_bytes:
        builder.finding(
            "MIGRATION_SCHEDULE_SOURCE_TOO_LARGE",
            MigrationSeverity.BLOCKER,
            source,
            "The schedule file exceeds the bounded JSON inspection limit.",
            "Raise max_schedule_bytes explicitly or inspect a verified bounded export.",
            count=before[0],
        )
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        builder.finding(
            "MIGRATION_SCHEDULE_SOURCE_INVALID",
            MigrationSeverity.BLOCKER,
            source,
            "The legacy schedule source is not valid bounded UTF-8 JSON.",
            "Repair or quarantine the source; raw parser errors are redacted.",
        )
        return
    if not isinstance(payload, dict):
        builder.finding(
            "MIGRATION_SCHEDULE_SHAPE_INVALID",
            MigrationSeverity.BLOCKER,
            source,
            "The schedule root must be an object containing jobs and runs arrays.",
            "Export the source in the documented legacy scheduler shape.",
        )
        return
    jobs = payload.get("jobs", [])
    runs = payload.get("runs", [])
    if not isinstance(jobs, list) or not isinstance(runs, list):
        builder.finding(
            "MIGRATION_SCHEDULE_SHAPE_INVALID",
            MigrationSeverity.BLOCKER,
            source,
            "The schedule jobs and runs values must be arrays.",
            "Repair or quarantine malformed entries before migration.",
        )
        return
    if any(not isinstance(item, dict) for item in jobs + runs):
        builder.finding(
            "MIGRATION_SCHEDULE_ENTRY_INVALID",
            MigrationSeverity.BLOCKER,
            source,
            "Every schedule job and run entry must be an object.",
            "Repair or quarantine malformed entries before migration.",
        )
        return

    typed_jobs = [item for item in jobs if isinstance(item, dict)]
    typed_runs = [item for item in runs if isinstance(item, dict)]
    names = [item.get("name") for item in typed_jobs]
    valid_names = [str(name) for name in names if isinstance(name, str) and name.strip()]
    duplicate_job_count = len(valid_names) - len(set(valid_names))
    missing_timezone_count = 0
    missing_misfire_count = 0
    invalid_schedule_count = 0
    partition_required_count = 0
    for job in typed_jobs:
        if not isinstance(job.get("name"), str) or not str(job["name"]).strip():
            invalid_schedule_count += 1
        if not isinstance(job.get("prompt"), str) or not str(job["prompt"]).strip():
            invalid_schedule_count += 1
        if not _valid_schedule(job.get("schedule")):
            invalid_schedule_count += 1
        timezone = _job_setting(job, "timezone") or default_timezone
        if timezone is None:
            missing_timezone_count += 1
        else:
            try:
                ZoneInfo(str(timezone))
            except (TypeError, ValueError, ZoneInfoNotFoundError):
                invalid_schedule_count += 1
        misfire = _job_setting(job, "misfire_policy") or default_misfire_policy
        if misfire is None:
            missing_misfire_count += 1
        else:
            try:
                ScheduleMisfirePolicy(misfire)
            except ValueError:
                invalid_schedule_count += 1
        if job.get("model_id") is not None or job.get("provider") is not None:
            partition_required_count += 1

    known_jobs = set(valid_names)
    unknown_job_run_count = sum(
        1 for run in typed_runs if str(run.get("job_name") or "") not in known_jobs
    )
    in_flight_run_count = sum(1 for run in typed_runs if run.get("status") == "running")
    builder.schedule = ScheduleInventory(
        format_version=str(payload.get("format_version") or "legacy-unversioned"),
        job_count=len(typed_jobs),
        run_count=len(typed_runs),
        logical_digest=_digest(payload),
        duplicate_job_count=duplicate_job_count,
        missing_timezone_count=missing_timezone_count,
        missing_misfire_count=missing_misfire_count,
        invalid_schedule_count=invalid_schedule_count,
        unknown_job_run_count=unknown_job_run_count,
        in_flight_run_count=in_flight_run_count,
    )
    checks = (
        (
            duplicate_job_count,
            "MIGRATION_SCHEDULE_JOB_COLLISION",
            "Duplicate legacy job names would collapse to one target identity.",
            "Rename, merge, or quarantine duplicate jobs explicitly.",
        ),
        (
            missing_timezone_count,
            "MIGRATION_SCHEDULE_TIMEZONE_UNRESOLVED",
            "Legacy jobs do not have an explicit source timezone.",
            "Provide per-job timezone metadata or an explicit default timezone.",
        ),
        (
            missing_misfire_count,
            "MIGRATION_SCHEDULE_MISFIRE_UNRESOLVED",
            "Legacy jobs do not have an explicit misfire policy.",
            "Provide per-job policy or choose skip/run_once as the migration default.",
        ),
        (
            invalid_schedule_count,
            "MIGRATION_SCHEDULE_INVALID",
            "One or more schedule records have invalid required fields or semantics.",
            "Repair or quarantine invalid records before planning import.",
        ),
        (
            unknown_job_run_count,
            "MIGRATION_SCHEDULE_RUN_ORPHANED",
            "Legacy run history references jobs absent from the source inventory.",
            "Map the missing job or quarantine its run history.",
        ),
        (
            in_flight_run_count,
            "MIGRATION_SCHEDULE_RUN_IN_FLIGHT",
            "Legacy run history contains in-flight attempts.",
            "Settle or quarantine them after fencing the old scheduler.",
        ),
        (
            partition_required_count,
            "MIGRATION_SCHEDULE_MODEL_PARTITION_REQUIRED",
            "Legacy jobs contain per-job model or provider overrides that durable workers reject.",
            "Partition those jobs by immutable worker configuration and remove the "
            "per-job overrides before migration.",
        ),
    )
    for count, code, message, resolution in checks:
        if count:
            builder.finding(
                code,
                MigrationSeverity.BLOCKER,
                source,
                message,
                resolution,
                count=count,
            )
    if typed_jobs and old_writer_fence_plan is None:
        builder.finding(
            "MIGRATION_SCHEDULE_FENCE_UNPLANNED",
            MigrationSeverity.BLOCKER,
            source,
            "No explicit old-scheduler writer-fence plan was supplied.",
            "Name the reviewed fence/cutover mechanism before planning import.",
        )
    try:
        changed = _stat_signature(path) != before
    except OSError:
        changed = True
    if changed:
        builder.finding(
            "MIGRATION_SOURCE_CHANGED_DURING_SCAN",
            MigrationSeverity.BLOCKER,
            source,
            "The legacy schedule source changed during preflight.",
            "Freeze the old writer and rerun against a verified snapshot.",
        )


def inspect_migration_012(
    *,
    learning_sqlite_path: str | Path | None = None,
    schedule_json_path: str | Path | None = None,
    learning_table_names: Iterable[str] = _DEFAULT_LEARNING_TABLES,
    scope_mappings: Iterable[LegacyLearningScopeMapping] = (),
    schedule_default_timezone: str | None = None,
    schedule_default_misfire_policy: str | ScheduleMisfirePolicy | None = None,
    old_writer_fence_plan: str | None = None,
    max_learning_rows: int = 100_000,
    max_learning_bytes: int = 512 * 1024 * 1024,
    max_schedule_bytes: int = 16 * 1024 * 1024,
) -> MigrationPreflightReport:
    """Inspect legacy sources without mutating, locking, copying, or deleting them."""
    if not 1 <= max_learning_rows <= 10_000_000:
        raise ValueError("max_learning_rows must be between 1 and 10,000,000")
    if not 1 <= max_learning_bytes <= 16 * 1024 * 1024 * 1024:
        raise ValueError("max_learning_bytes must be between 1 byte and 16 GiB")
    if not 1 <= max_schedule_bytes <= 1024 * 1024 * 1024:
        raise ValueError("max_schedule_bytes must be between 1 and 1 GiB")
    table_names = tuple(learning_table_names)
    if not table_names or any(not _IDENTIFIER_RE.fullmatch(name) for name in table_names):
        raise ValueError("learning_table_names must contain safe SQLite identifiers")
    if len(set(table_names)) != len(table_names):
        raise ValueError("learning_table_names cannot contain duplicates")

    mappings = tuple(scope_mappings)
    if any(not isinstance(item, LegacyLearningScopeMapping) for item in mappings):
        raise TypeError("scope_mappings must contain LegacyLearningScopeMapping values")
    mapping_index = {item.key: item for item in mappings}
    if len(mapping_index) != len(mappings):
        raise ValueError("scope_mappings cannot contain duplicate source keys")
    if schedule_default_timezone is not None:
        _require_text(schedule_default_timezone, field_name="schedule_default_timezone")
        try:
            ZoneInfo(schedule_default_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("schedule_default_timezone must be an IANA timezone") from exc
    misfire = (
        ScheduleMisfirePolicy(schedule_default_misfire_policy)
        if schedule_default_misfire_policy is not None
        else None
    )
    if old_writer_fence_plan is not None:
        _require_text(old_writer_fence_plan, field_name="old_writer_fence_plan")

    builder = _PreflightBuilder()
    if learning_sqlite_path is not None:
        _inspect_learning_sqlite(
            builder,
            Path(learning_sqlite_path).expanduser().resolve(),
            table_names=table_names,
            mappings=mapping_index,
            max_rows=max_learning_rows,
            max_bytes=max_learning_bytes,
        )
    if schedule_json_path is not None:
        _inspect_schedule_json(
            builder,
            Path(schedule_json_path).expanduser().resolve(),
            default_timezone=schedule_default_timezone,
            default_misfire_policy=misfire,
            old_writer_fence_plan=old_writer_fence_plan,
            max_bytes=max_schedule_bytes,
        )
    if learning_sqlite_path is None and schedule_json_path is None:
        builder.finding(
            "MIGRATION_NO_LEGACY_SOURCES",
            MigrationSeverity.INFO,
            "preflight",
            "No legacy learning or schedule source was selected.",
            "No persisted-data migration is required for this check.",
        )

    planned_actions = (
        "freeze old writers and create a verified backup before apply",
        "apply only explicit learning map-or-quarantine decisions",
        "import with stable identifiers and idempotent mutations",
        "verify source/target counts, logical digests, and read behavior",
        "retain rollback until the future contraction boundary is acknowledged",
    )
    return MigrationPreflightReport(
        schema_version=MIGRATION_PREFLIGHT_SCHEMA_VERSION,
        files=tuple(builder.files),
        learning_tables=tuple(builder.learning_tables),
        schedule=builder.schedule,
        scope_mappings=tuple(sorted(mappings, key=lambda item: _canonical_json(item.to_dict()))),
        findings=tuple(
            sorted(
                builder.findings,
                key=lambda item: (item.severity.value, item.code, item.source),
            )
        ),
        planned_actions=planned_actions,
    )


__all__ = [
    "LegacyLearningScopeMapping",
    "LegacyScopeAction",
    "LearningTableInventory",
    "MIGRATION_PREFLIGHT_SCHEMA_VERSION",
    "MigrationFileEvidence",
    "MigrationFinding",
    "MigrationPreflightReport",
    "MigrationSeverity",
    "ScheduleInventory",
    "ScheduleMisfirePolicy",
    "inspect_migration_012",
]
