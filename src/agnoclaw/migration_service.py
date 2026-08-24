"""Content-free control plane for the 0.12 PostgreSQL/service migration."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self, cast
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .migration import LegacyLearningScopeMapping, ScheduleMisfirePolicy
from .migration_apply import Migration012Error

POSTGRES_MIGRATION_012_PLAN_SCHEMA_VERSION = "1.0"
POSTGRES_MIGRATION_012_SCHEDULE_MAP_SCHEMA_VERSION = "1.1"
POSTGRES_MIGRATION_012_ROLLBACK_BOUNDARY = "before-first-legitimate-post-cutover-target-write-v1"
_CONTROL_FILE_LIMIT = 4 * 1024 * 1024
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_CONTROL_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


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


def _require_text(value: str | None, field_name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} exceeds its bounded length")
    return normalized


def _require_digest(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical sha256 digest")
    return value


def _require_control_token(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _CONTROL_TOKEN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be an opaque non-secret control token")
    return value


def _bounded_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > _CONTROL_FILE_LIMIT:
        raise Migration012Error(
            "MIGRATION_CONTROL_FILE_INVALID",
            "The migration control file is absent or exceeds its bounded size.",
            control_file_role="service_migration",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Migration012Error(
            "MIGRATION_CONTROL_FILE_INVALID",
            "The migration control file is not valid bounded UTF-8 JSON.",
            control_file_role="service_migration",
        ) from exc
    if not isinstance(payload, dict):
        raise Migration012Error(
            "MIGRATION_CONTROL_FILE_INVALID",
            "The migration control file root must be an object.",
            control_file_role="service_migration",
        )
    return payload


def _atomic_control_file(path: Path, payload: dict[str, Any]) -> Path:
    path = path.expanduser()
    if path.is_symlink():
        raise Migration012Error(
            "MIGRATION_CONTROL_PATH_UNSAFE",
            "The migration control-file destination cannot be a symbolic link.",
            control_file_role="service_migration",
        )
    path = path.parent.resolve(strict=False) / path.name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid4().hex}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write((_canonical_json(payload) + "\n").encode())
            output.flush()
            os.fsync(output.fileno())
        if path.is_symlink():
            raise Migration012Error(
                "MIGRATION_CONTROL_PATH_UNSAFE",
                "The migration control-file destination became a symbolic link.",
                control_file_role="service_migration",
            )
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


@dataclass(frozen=True, slots=True)
class PostgresMigrationDatabaseRef:
    """A serializable credential reference, never a resolved PostgreSQL DSN."""

    role: str
    credential_env: str
    schema: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _require_text(self.role, "role", maximum=64))
        if (
            not isinstance(self.credential_env, str)
            or _ENVIRONMENT_NAME.fullmatch(self.credential_env) is None
        ):
            raise ValueError("credential_env must be an uppercase environment variable name")
        if not isinstance(self.schema, str) or _IDENTIFIER.fullmatch(self.schema) is None:
            raise ValueError("schema must be a bounded PostgreSQL identifier")

    def resolve(self, environment: dict[str, str] | None = None) -> str:
        values = os.environ if environment is None else environment
        dsn = values.get(self.credential_env)
        if not isinstance(dsn, str) or not dsn.strip():
            raise Migration012Error(
                "MIGRATION_POSTGRES_CREDENTIAL_UNAVAILABLE",
                "A PostgreSQL credential reference could not be resolved.",
                role=self.role,
                credential_env=self.credential_env,
            )
        if "\x00" in dsn or "\n" in dsn or "\r" in dsn:
            raise Migration012Error(
                "MIGRATION_POSTGRES_CREDENTIAL_INVALID",
                "A PostgreSQL credential reference resolved to an invalid value.",
                role=self.role,
                credential_env=self.credential_env,
            )
        return dsn.strip()

    def to_dict(self) -> dict[str, str]:
        return {
            "role": self.role,
            "credential_env": self.credential_env,
            "schema": self.schema,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        return cls(
            role=cast(str, value.get("role")),
            credential_env=cast(str, value.get("credential_env")),
            schema=cast(str, value.get("schema")),
        )


@dataclass(frozen=True, slots=True)
class PostgresMigrationBackupReceipt:
    """Reviewed immutable backup evidence referenced by a content-free plan."""

    receipt_id: str
    receipt_digest: str
    restore_test_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "receipt_id", _require_control_token(self.receipt_id, "receipt_id")
        )
        object.__setattr__(
            self,
            "receipt_digest",
            _require_digest(self.receipt_digest, "receipt_digest"),
        )
        object.__setattr__(
            self,
            "restore_test_id",
            _require_control_token(self.restore_test_id, "restore_test_id"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "receipt_id": self.receipt_id,
            "receipt_digest": self.receipt_digest,
            "restore_test_id": self.restore_test_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        return cls(
            receipt_id=cast(str, value.get("receipt_id")),
            receipt_digest=cast(str, value.get("receipt_digest")),
            restore_test_id=cast(str, value.get("restore_test_id")),
        )


@dataclass(frozen=True, slots=True)
class PostgresScheduleMigrationRule:
    """Sensitive executable schedule mapping; only its digest enters the plan."""

    source_schedule_id: str
    schedule: str
    prompt: str
    tenant_id: str
    user_id: str
    session_id: str
    agent_id: str
    worker_profile: str
    enabled: bool
    isolated: bool
    learning_consent: bool
    timezone: str
    misfire_policy: ScheduleMisfirePolicy
    misfire_grace_seconds: int
    overlap_policy: str
    max_retries: int
    retry_delay_seconds: int
    retry_backoff_multiplier: float
    retry_max_delay_seconds: int
    retry_jitter_seconds: int
    jitter_seconds: int
    concurrency_key: str

    def __post_init__(self) -> None:
        for name in (
            "source_schedule_id",
            "schedule",
            "prompt",
            "tenant_id",
            "user_id",
            "session_id",
            "agent_id",
            "worker_profile",
            "timezone",
            "overlap_policy",
            "concurrency_key",
        ):
            limit = 32_768 if name == "prompt" else 512
            object.__setattr__(
                self,
                name,
                _require_text(getattr(self, name), name, maximum=limit),
            )
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must name an installed IANA timezone") from exc
        object.__setattr__(
            self,
            "misfire_policy",
            ScheduleMisfirePolicy(self.misfire_policy),
        )
        if self.overlap_policy not in {"skip", "queue"}:
            raise ValueError("overlap_policy must be skip or queue")
        for name in ("enabled", "isolated", "learning_consent"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        if not 0 <= self.max_retries <= 100:
            raise ValueError("max_retries must be between 0 and 100")
        if not 0 <= self.retry_delay_seconds <= 86_400:
            raise ValueError("retry_delay_seconds must be between 0 and 86400")
        if not 1.0 <= float(self.retry_backoff_multiplier) <= 10.0:
            raise ValueError("retry_backoff_multiplier must be between 1 and 10")
        for name, maximum in (
            ("retry_max_delay_seconds", 604_800),
            ("retry_jitter_seconds", 86_400),
            ("jitter_seconds", 86_400),
            ("misfire_grace_seconds", 604_800),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise ValueError(f"{name} must be between 0 and {maximum}")
        if self.retry_max_delay_seconds < self.retry_delay_seconds:
            raise ValueError("retry_max_delay_seconds cannot be below retry_delay_seconds")

    def control_dict(self) -> dict[str, Any]:
        return {
            "source_schedule_id": self.source_schedule_id,
            "schedule": self.schedule,
            "prompt": self.prompt,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "worker_profile": self.worker_profile,
            "enabled": self.enabled,
            "isolated": self.isolated,
            "learning_consent": self.learning_consent,
            "timezone": self.timezone,
            "misfire_policy": self.misfire_policy.value,
            "misfire_grace_seconds": self.misfire_grace_seconds,
            "overlap_policy": self.overlap_policy,
            "max_retries": self.max_retries,
            "retry_delay_seconds": self.retry_delay_seconds,
            "retry_backoff_multiplier": self.retry_backoff_multiplier,
            "retry_max_delay_seconds": self.retry_max_delay_seconds,
            "retry_jitter_seconds": self.retry_jitter_seconds,
            "jitter_seconds": self.jitter_seconds,
            "concurrency_key": self.concurrency_key,
        }


@dataclass(frozen=True, slots=True)
class PostgresScheduleMapSummary:
    """Immutable, content-free evidence for a sensitive schedule map."""

    count: int
    map_digest: str
    source_id_set_digest: str
    schema_version: str = POSTGRES_MIGRATION_012_SCHEDULE_MAP_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != POSTGRES_MIGRATION_012_SCHEDULE_MAP_SCHEMA_VERSION:
            raise ValueError("schedule_map_summary has an unsupported schema")
        if not isinstance(self.count, int) or isinstance(self.count, bool) or self.count < 0:
            raise ValueError("schedule_map_summary count must be a non-negative integer")
        object.__setattr__(self, "map_digest", _require_digest(self.map_digest, "map_digest"))
        object.__setattr__(
            self,
            "source_id_set_digest",
            _require_digest(self.source_id_set_digest, "source_id_set_digest"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "count": self.count,
            "map_digest": self.map_digest,
            "source_id_set_digest": self.source_id_set_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        expected_keys = {
            "schema_version",
            "count",
            "map_digest",
            "source_id_set_digest",
        }
        if set(value) != expected_keys:
            raise ValueError("schedule_map_summary has an invalid shape")
        return cls(
            schema_version=cast(str, value.get("schema_version")),
            count=cast(int, value.get("count")),
            map_digest=cast(str, value.get("map_digest")),
            source_id_set_digest=cast(str, value.get("source_id_set_digest")),
        )


@dataclass(frozen=True, slots=True)
class PostgresScheduleMap:
    rules: tuple[PostgresScheduleMigrationRule, ...]
    map_digest: str
    source_id_set_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.rules, tuple) or len(self.rules) > 100_000:
            raise ValueError("schedule map rules must be a bounded immutable tuple")
        if any(not isinstance(item, PostgresScheduleMigrationRule) for item in self.rules):
            raise TypeError("schedule map rules must contain PostgresScheduleMigrationRule values")
        identities = [item.source_schedule_id for item in self.rules]
        if len(set(identities)) != len(identities):
            raise ValueError("schedule map rules cannot contain duplicate source identities")
        if tuple(sorted(self.rules, key=lambda item: item.source_schedule_id)) != self.rules:
            raise ValueError("schedule map rules must use canonical source-identity order")
        if self.map_digest != _digest([item.control_dict() for item in self.rules]):
            raise ValueError("schedule map digest does not match its rules")
        if self.source_id_set_digest != _digest(sorted(identities)):
            raise ValueError("schedule source-identity digest does not match its rules")

    @property
    def count(self) -> int:
        return len(self.rules)

    @property
    def summary(self) -> PostgresScheduleMapSummary:
        return PostgresScheduleMapSummary(
            count=self.count,
            map_digest=self.map_digest,
            source_id_set_digest=self.source_id_set_digest,
        )

    def summary_dict(self) -> dict[str, Any]:
        return self.summary.to_dict()


def load_postgres_schedule_map(path: str | Path) -> PostgresScheduleMap:
    """Load a bounded sensitive map and return digest-only plan evidence."""
    payload = _bounded_json(Path(path).expanduser().resolve(strict=False))
    if payload.get("schema_version") != POSTGRES_MIGRATION_012_SCHEDULE_MAP_SCHEMA_VERSION:
        raise Migration012Error(
            "MIGRATION_SCHEDULE_MAP_SCHEMA_UNSUPPORTED",
            "The PostgreSQL schedule map schema version is unsupported.",
            supported_schema=POSTGRES_MIGRATION_012_SCHEDULE_MAP_SCHEMA_VERSION,
        )
    raw_rules = payload.get("schedules")
    if not isinstance(raw_rules, list) or len(raw_rules) > 100_000:
        raise Migration012Error(
            "MIGRATION_SCHEDULE_MAP_INVALID",
            "The PostgreSQL schedule map must contain a bounded schedules array.",
        )
    try:
        rules = tuple(PostgresScheduleMigrationRule(**item) for item in raw_rules)
    except (TypeError, ValueError) as exc:
        raise Migration012Error(
            "MIGRATION_SCHEDULE_MAP_INVALID",
            "A PostgreSQL schedule mapping entry is invalid.",
        ) from exc
    identities = [rule.source_schedule_id for rule in rules]
    if len(set(identities)) != len(identities):
        raise Migration012Error(
            "MIGRATION_SCHEDULE_MAP_DUPLICATE",
            "The PostgreSQL schedule map contains duplicate source identities.",
        )
    ordered = tuple(sorted(rules, key=lambda item: item.source_schedule_id))
    return PostgresScheduleMap(
        rules=ordered,
        map_digest=_digest([item.control_dict() for item in ordered]),
        source_id_set_digest=_digest(sorted(identities)),
    )


@dataclass(frozen=True, slots=True)
class PostgresMigrationTableEvidence:
    """Content-free source or target evidence produced by read-only preflight."""

    role: str
    table_name: str
    row_count: int
    schema_digest: str
    logical_digest: str | None
    exists: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _require_text(self.role, "role", maximum=64))
        if not isinstance(self.table_name, str) or _IDENTIFIER.fullmatch(self.table_name) is None:
            raise ValueError("table_name must be a bounded PostgreSQL identifier")
        if not isinstance(self.exists, bool):
            raise ValueError("exists must be a boolean")
        if self.row_count < 0:
            raise ValueError("row_count cannot be negative")
        if not self.exists and (self.row_count != 0 or self.logical_digest is not None):
            raise ValueError("absent tables cannot contain rows or a logical digest")
        object.__setattr__(
            self,
            "schema_digest",
            _require_digest(self.schema_digest, "schema_digest"),
        )
        if self.logical_digest is not None:
            object.__setattr__(
                self,
                "logical_digest",
                _require_digest(self.logical_digest, "logical_digest"),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "table_name": self.table_name,
            "row_count": self.row_count,
            "schema_digest": self.schema_digest,
            "logical_digest": self.logical_digest,
            "exists": self.exists,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        return cls(
            role=cast(str, value.get("role")),
            table_name=cast(str, value.get("table_name")),
            row_count=cast(int, value.get("row_count")),
            schema_digest=cast(str, value.get("schema_digest")),
            logical_digest=cast(str | None, value.get("logical_digest")),
            exists=cast(bool, value.get("exists")),
        )


@dataclass(frozen=True, slots=True)
class PostgresMigration012Plan:
    """Digest-bound, content-free intent for a deployment-coordinated migration."""

    migration_id: str
    planned_at: str
    source: PostgresMigrationDatabaseRef
    target_learning: PostgresMigrationDatabaseRef
    target_runtime: PostgresMigrationDatabaseRef
    target_tenant_id: str
    target_org_id: str | None
    target_agent_id: str
    scope_mappings: tuple[LegacyLearningScopeMapping, ...]
    schedule_map_summary: PostgresScheduleMapSummary
    backup_receipt: PostgresMigrationBackupReceipt
    writer_fence_plan: str
    endpoint_evidence_digests: tuple[tuple[str, str], ...]
    table_evidence: tuple[PostgresMigrationTableEvidence, ...]
    rollback_boundary: str = POSTGRES_MIGRATION_012_ROLLBACK_BOUNDARY
    schema_version: str = POSTGRES_MIGRATION_012_PLAN_SCHEMA_VERSION
    plan_digest: str = ""

    def __post_init__(self) -> None:
        for name in ("migration_id", "planned_at", "target_tenant_id", "target_agent_id"):
            object.__setattr__(self, name, _require_text(getattr(self, name), name))
        if self.target_org_id is not None:
            object.__setattr__(
                self,
                "target_org_id",
                _require_text(self.target_org_id, "target_org_id"),
            )
        object.__setattr__(
            self,
            "writer_fence_plan",
            _require_control_token(self.writer_fence_plan, "writer_fence_plan"),
        )
        if self.schema_version != POSTGRES_MIGRATION_012_PLAN_SCHEMA_VERSION:
            raise ValueError("unsupported PostgreSQL migration plan schema")
        if self.rollback_boundary != POSTGRES_MIGRATION_012_ROLLBACK_BOUNDARY:
            raise ValueError("unsupported PostgreSQL migration rollback boundary")
        if not self.migration_id.startswith("pgmig012_"):
            raise ValueError("migration_id must use the PostgreSQL 0.12 namespace")
        expected_roles = {
            "source": self.source,
            "target_learning": self.target_learning,
            "target_runtime": self.target_runtime,
        }
        for role, reference in expected_roles.items():
            if reference.role != role:
                raise ValueError(f"{role} database reference has the wrong role")
        if not isinstance(self.schedule_map_summary, PostgresScheduleMapSummary):
            raise ValueError("schedule_map_summary must be immutable summary evidence")
        endpoint_roles: list[str] = []
        for role, digest in self.endpoint_evidence_digests:
            endpoint_roles.append(_require_text(role, "endpoint_evidence_role", maximum=64))
            _require_digest(digest, "endpoint_evidence_digest")
        if len(set(endpoint_roles)) != len(endpoint_roles):
            raise ValueError("endpoint evidence roles must be unique")
        if not self.table_evidence:
            raise ValueError("table evidence is required")
        mapping_keys = [item.key for item in self.scope_mappings]
        if len(set(mapping_keys)) != len(mapping_keys):
            raise ValueError("scope_mappings cannot contain duplicate source keys")

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "migration_id": self.migration_id,
            "planned_at": self.planned_at,
            "source": self.source.to_dict(),
            "target_learning": self.target_learning.to_dict(),
            "target_runtime": self.target_runtime.to_dict(),
            "target_authority": {
                "tenant_id": self.target_tenant_id,
                "org_id": self.target_org_id,
                "agent_id": self.target_agent_id,
            },
            "scope_mappings": [item.to_dict() for item in self.scope_mappings],
            "schedule_map": self.schedule_map_summary.to_dict(),
            "backup_receipt": self.backup_receipt.to_dict(),
            "writer_fence_plan": self.writer_fence_plan,
            "endpoint_evidence_digests": [
                {"role": role, "digest": digest} for role, digest in self.endpoint_evidence_digests
            ],
            "table_evidence": [item.to_dict() for item in self.table_evidence],
            "rollback_boundary": self.rollback_boundary,
        }
        if include_digest:
            payload["plan_digest"] = self.plan_digest
        return payload

    @property
    def computed_plan_digest(self) -> str:
        return _digest(self.to_dict(include_digest=False))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        authority = value.get("target_authority")
        if not isinstance(authority, dict):
            raise ValueError("target_authority must be an object")
        mappings = value.get("scope_mappings")
        endpoint_evidence = value.get("endpoint_evidence_digests")
        table_evidence = value.get("table_evidence")
        if not isinstance(mappings, list):
            raise ValueError("scope_mappings must be an array")
        if not isinstance(endpoint_evidence, list):
            raise ValueError("endpoint_evidence_digests must be an array")
        if not isinstance(table_evidence, list):
            raise ValueError("table_evidence must be an array")
        return cls(
            schema_version=cast(str, value.get("schema_version")),
            migration_id=cast(str, value.get("migration_id")),
            planned_at=cast(str, value.get("planned_at")),
            source=PostgresMigrationDatabaseRef.from_dict(value.get("source", {})),
            target_learning=PostgresMigrationDatabaseRef.from_dict(
                value.get("target_learning", {})
            ),
            target_runtime=PostgresMigrationDatabaseRef.from_dict(value.get("target_runtime", {})),
            target_tenant_id=cast(str, authority.get("tenant_id")),
            target_org_id=cast(str | None, authority.get("org_id")),
            target_agent_id=cast(str, authority.get("agent_id")),
            scope_mappings=tuple(LegacyLearningScopeMapping(**item) for item in mappings),
            schedule_map_summary=PostgresScheduleMapSummary.from_dict(
                value.get("schedule_map", {})
            ),
            backup_receipt=PostgresMigrationBackupReceipt.from_dict(
                value.get("backup_receipt", {})
            ),
            writer_fence_plan=cast(str, value.get("writer_fence_plan")),
            endpoint_evidence_digests=tuple(
                (item["role"], item["digest"]) for item in endpoint_evidence
            ),
            table_evidence=tuple(
                PostgresMigrationTableEvidence.from_dict(item) for item in table_evidence
            ),
            rollback_boundary=cast(str, value.get("rollback_boundary")),
            plan_digest=cast(str, value.get("plan_digest", "")),
        )


def _create_postgres_migration_012_plan_unchecked(
    *,
    source: PostgresMigrationDatabaseRef,
    target_learning: PostgresMigrationDatabaseRef,
    target_runtime: PostgresMigrationDatabaseRef,
    target_tenant_id: str,
    target_agent_id: str,
    schedule_map: PostgresScheduleMap,
    backup_receipt: PostgresMigrationBackupReceipt,
    writer_fence_plan: str,
    endpoint_evidence_digests: tuple[tuple[str, str], ...],
    table_evidence: tuple[PostgresMigrationTableEvidence, ...],
    target_org_id: str | None = None,
    scope_mappings: tuple[LegacyLearningScopeMapping, ...] = (),
) -> PostgresMigration012Plan:
    """Create a plan only after a read-only scanner supplies digest evidence."""
    if not endpoint_evidence_digests:
        raise ValueError("endpoint evidence is required")
    if any(
        rule.tenant_id != target_tenant_id or rule.agent_id != target_agent_id
        for rule in schedule_map.rules
    ):
        raise ValueError(
            "schedule rules must match the plan's trusted target tenant and agent authority"
        )
    ordered_mappings = tuple(
        sorted(scope_mappings, key=lambda item: _canonical_json(item.to_dict()))
    )
    ordered_endpoints = tuple(sorted(endpoint_evidence_digests))
    ordered_tables = tuple(sorted(table_evidence, key=lambda item: (item.role, item.table_name)))
    plan = PostgresMigration012Plan(
        migration_id=f"pgmig012_{uuid4().hex}",
        planned_at=datetime.now(UTC).isoformat(),
        source=source,
        target_learning=target_learning,
        target_runtime=target_runtime,
        target_tenant_id=target_tenant_id,
        target_org_id=target_org_id,
        target_agent_id=target_agent_id,
        scope_mappings=ordered_mappings,
        schedule_map_summary=schedule_map.summary,
        backup_receipt=backup_receipt,
        writer_fence_plan=writer_fence_plan,
        endpoint_evidence_digests=ordered_endpoints,
        table_evidence=ordered_tables,
    )
    return replace(plan, plan_digest=plan.computed_plan_digest)


def write_postgres_migration_012_plan(
    path: str | Path,
    plan: PostgresMigration012Plan,
) -> Path:
    """Atomically write a digest-valid PostgreSQL 0.12 migration plan."""
    if plan.plan_digest != plan.computed_plan_digest:
        raise Migration012Error(
            "MIGRATION_PLAN_DIGEST_MISMATCH",
            "The PostgreSQL migration plan digest does not match its contents.",
        )
    return _atomic_control_file(Path(path), plan.to_dict())


def read_postgres_migration_012_plan(path: str | Path) -> PostgresMigration012Plan:
    """Read and digest-verify one bounded PostgreSQL 0.12 migration plan."""
    try:
        plan = PostgresMigration012Plan.from_dict(
            _bounded_json(Path(path).expanduser().resolve(strict=False))
        )
    except Migration012Error:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise Migration012Error(
            "MIGRATION_CONTROL_FILE_INVALID",
            "The PostgreSQL migration plan has an invalid shape.",
        ) from exc
    if plan.plan_digest != plan.computed_plan_digest:
        raise Migration012Error(
            "MIGRATION_PLAN_DIGEST_MISMATCH",
            "The PostgreSQL migration plan digest does not match its contents.",
        )
    return plan


__all__ = [
    "POSTGRES_MIGRATION_012_PLAN_SCHEMA_VERSION",
    "POSTGRES_MIGRATION_012_ROLLBACK_BOUNDARY",
    "POSTGRES_MIGRATION_012_SCHEDULE_MAP_SCHEMA_VERSION",
    "PostgresMigration012Plan",
    "PostgresMigrationBackupReceipt",
    "PostgresMigrationDatabaseRef",
    "PostgresMigrationTableEvidence",
    "PostgresScheduleMap",
    "PostgresScheduleMapSummary",
    "PostgresScheduleMigrationRule",
    "load_postgres_schedule_map",
    "read_postgres_migration_012_plan",
    "write_postgres_migration_012_plan",
]
