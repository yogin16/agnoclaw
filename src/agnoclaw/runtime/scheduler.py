"""Persistent scheduler contracts for embedded agnoclaw runtimes."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..migration_fence import assert_migration_store_writable, migration_writer_fence_path
from .errors import HarnessError

MAX_SCHEDULER_LEASE_SECONDS = 86_400
MAX_SCHEDULER_BATCH_SIZE = 100
MAX_SCHEDULER_OUTPUT_CHARS = 4_096
MAX_SCHEDULER_METADATA_BYTES = 65_536
_INTERVAL_PATTERN = re.compile(
    r"^(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$",
    re.IGNORECASE,
)


class SchedulerMisfirePolicy(StrEnum):
    """What to do when an occurrence is discovered after its nominal time."""

    FIRE_ONCE = "fire_once"
    CATCH_UP = "catch_up"
    SKIP = "skip"


class SchedulerOverlapPolicy(StrEnum):
    """What to do when a job's concurrency group is already occupied."""

    QUEUE = "queue"
    SKIP = "skip"


class SchedulerLeaseLostError(HarnessError):
    """The scheduler claim is stale, expired, or owned by another worker."""

    def __init__(self, *, run_id: str):
        super().__init__(
            code="SCHEDULER_LEASE_LOST",
            category="scheduler",
            message="The scheduler execution claim is no longer authoritative.",
            retryable=False,
            details={"schedule_run_id": run_id},
        )


class SchedulerConfigurationError(HarnessError):
    """A schedule cannot be evaluated safely."""

    def __init__(self, message: str, *, schedule: str, timezone: str):
        super().__init__(
            code="SCHEDULER_CONFIGURATION_INVALID",
            category="scheduler",
            message=message,
            retryable=False,
            details={"schedule": schedule, "timezone": timezone},
        )


def scheduler_store_path(root: str | Path | None = None) -> Path:
    """Return the default local scheduler store path."""
    if root is not None:
        return Path(root).expanduser().resolve()
    return Path.home().joinpath(".agnoclaw", "schedules.json").resolve()


def scheduler_fence_path(path: str | Path) -> Path:
    """Return the migration writer-fence marker for a JSON scheduler store."""
    return migration_writer_fence_path(path)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("scheduler timestamps must include a timezone")
    return parsed.astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _interval_seconds(schedule: str) -> int | None:
    match = _INTERVAL_PATTERN.fullmatch(schedule.strip())
    if match is None or not any(match.group(key) for key in ("hours", "minutes", "seconds")):
        return None
    seconds = (
        int(match.group("hours") or 0) * 3_600
        + int(match.group("minutes") or 0) * 60
        + int(match.group("seconds") or 0)
    )
    return seconds if seconds > 0 else None


def next_schedule_time(job: SchedulerJob, *, after: str | datetime) -> str:
    """Return the next nominal UTC occurrence strictly after ``after``.

    Intervals are core-only. Five-field cron expressions intentionally require the
    scheduler extra, and are evaluated using an aware ``zoneinfo`` clock so DST is not
    silently interpreted in the host's local timezone.
    """
    base = _parse_iso(after) if isinstance(after, str) else after.astimezone(UTC)
    interval = _interval_seconds(job.schedule)
    if interval is not None:
        return (base + timedelta(seconds=interval)).isoformat()
    if len(job.schedule.strip().split()) != 5:
        raise SchedulerConfigurationError(
            "Use a positive interval or a five-field cron expression.",
            schedule=job.schedule,
            timezone=job.timezone,
        )
    try:
        zone = ZoneInfo(job.timezone)
    except ZoneInfoNotFoundError as exc:
        raise SchedulerConfigurationError(
            "The schedule timezone is not available in the IANA timezone database.",
            schedule=job.schedule,
            timezone=job.timezone,
        ) from exc
    try:
        from croniter import croniter
    except ImportError as exc:
        raise SchedulerConfigurationError(
            "Cron schedules require the 'agnoclaw[scheduler]' extra.",
            schedule=job.schedule,
            timezone=job.timezone,
        ) from exc
    # Generate nominal wall-clock values without an attached offset. croniter's aware
    # datetime path can shift a fixed local hour across a DST boundary (for example,
    # 09:00 can become 08:00 after spring-forward). Resolve each nominal wall time
    # through zoneinfo instead. Nonexistent spring-forward times are skipped; an
    # ambiguous fall-back time fires once at its first valid instant.
    cursor = base.astimezone(zone).replace(tzinfo=None)
    while True:
        try:
            nominal = croniter(job.schedule, cursor).get_next(datetime).replace(tzinfo=None)
        except (KeyError, TypeError, ValueError) as exc:
            raise SchedulerConfigurationError(
                "The cron expression is invalid.",
                schedule=job.schedule,
                timezone=job.timezone,
            ) from exc
        local = nominal.replace(tzinfo=zone, fold=0)
        utc_candidate = local.astimezone(UTC)
        # A nonexistent wall time round-trips to a different nominal value. fold=0
        # deliberately selects the first instant of an ambiguous fall-back time.
        if utc_candidate.astimezone(zone).replace(tzinfo=None) == nominal and utc_candidate > base:
            return utc_candidate.isoformat()
        cursor = nominal


def scheduler_job_digest(job: SchedulerJob) -> str:
    """Digest only behavior-affecting job fields, excluding storage bookkeeping."""
    payload = job.to_dict()
    for key in ("revision", "next_run_at", "created_at", "updated_at"):
        payload.pop(key, None)
    return "sha256:" + hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def scheduler_occurrence_id(*, job_name: str, job_revision: int, scheduled_at: str) -> str:
    material = _canonical_json(
        {"job_name": job_name, "job_revision": job_revision, "scheduled_at": scheduled_at}
    )
    return "schedocc_" + hashlib.sha256(material.encode()).hexdigest()[:40]


def scheduler_run_id(*, occurrence_id: str, attempt: int) -> str:
    material = f"{occurrence_id}:{attempt}".encode()
    return "schedrun_" + hashlib.sha256(material).hexdigest()[:40]


def scheduler_idempotency_key(record: SchedulerRunRecord) -> str:
    """Stable lifecycle admission key reused whenever one attempt is reclaimed."""
    occurrence = record.occurrence_id or record.run_id
    return f"scheduler:{occurrence}:attempt:{record.attempt}"


def scheduler_jitter_seconds(job: SchedulerJob, *, occurrence_id: str) -> int:
    """Return stable, non-negative per-occurrence jitter."""
    if job.jitter_seconds <= 0:
        return 0
    value = int.from_bytes(hashlib.sha256(occurrence_id.encode()).digest()[:8], "big")
    return value % (job.jitter_seconds + 1)


def scheduler_retry_delay(job: SchedulerJob, *, failed_attempt: int) -> float:
    """Return bounded exponential retry backoff with deterministic jitter."""
    delay = min(
        float(job.retry_max_delay_seconds),
        float(job.retry_delay_seconds)
        * (float(job.retry_backoff_multiplier) ** max(0, failed_attempt - 1)),
    )
    if job.retry_jitter_seconds:
        material = f"{job.name}:{failed_attempt}".encode()
        value = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
        delay += value % (job.retry_jitter_seconds + 1)
    return delay


@dataclass(frozen=True)
class SchedulerJob:
    """A persisted scheduled harness job."""

    name: str
    schedule: str
    prompt: str
    skill: str | None = None
    isolated: bool = False
    model_id: str | None = None
    provider: str | None = None
    enabled: bool = True
    timezone: str = "UTC"
    max_retries: int = 0
    retry_delay_seconds: int = 30
    retry_backoff_multiplier: float = 2.0
    retry_max_delay_seconds: int = 3_600
    retry_jitter_seconds: int = 0
    jitter_seconds: int = 0
    misfire_policy: str = SchedulerMisfirePolicy.FIRE_ONCE.value
    misfire_grace_seconds: int = 300
    concurrency_key: str | None = None
    overlap_policy: str = SchedulerOverlapPolicy.QUEUE.value
    metadata: dict[str, Any] = field(default_factory=dict)
    revision: int = 0
    next_run_at: str | None = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        if not self.name.strip() or len(self.name) > 256:
            raise ValueError("scheduler job name must contain 1 to 256 characters")
        if not self.schedule.strip() or len(self.schedule) > 256:
            raise ValueError("scheduler expression must contain 1 to 256 characters")
        if not self.prompt.strip() or len(self.prompt) > 1_000_000:
            raise ValueError("scheduler prompt must contain 1 to 1000000 characters")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("scheduler timezone must be a valid IANA timezone") from exc
        if isinstance(self.max_retries, bool) or not 0 <= self.max_retries <= 100:
            raise ValueError("max_retries must be between 0 and 100")
        for name, value, maximum in (
            ("retry_delay_seconds", self.retry_delay_seconds, 86_400),
            ("retry_max_delay_seconds", self.retry_max_delay_seconds, 604_800),
            ("retry_jitter_seconds", self.retry_jitter_seconds, 86_400),
            ("jitter_seconds", self.jitter_seconds, 86_400),
            ("misfire_grace_seconds", self.misfire_grace_seconds, 604_800),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise ValueError(f"{name} must be between 0 and {maximum}")
        if not 1.0 <= float(self.retry_backoff_multiplier) <= 10.0:
            raise ValueError("retry_backoff_multiplier must be between 1 and 10")
        SchedulerMisfirePolicy(self.misfire_policy)
        SchedulerOverlapPolicy(self.overlap_policy)
        if self.concurrency_key is not None and (
            not self.concurrency_key.strip() or len(self.concurrency_key) > 256
        ):
            raise ValueError("concurrency_key must contain 1 to 256 characters")
        if self.revision < 0:
            raise ValueError("scheduler job revision must be non-negative")
        if self.next_run_at is not None:
            _parse_iso(self.next_run_at)
        try:
            metadata_json = _canonical_json(self.metadata)
        except (TypeError, ValueError) as exc:
            raise ValueError("scheduler metadata must be JSON serializable") from exc
        if len(metadata_json.encode("utf-8")) > MAX_SCHEDULER_METADATA_BYTES:
            raise ValueError(
                f"scheduler metadata cannot exceed {MAX_SCHEDULER_METADATA_BYTES} bytes"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SchedulerJob:
        return cls(
            name=str(data["name"]),
            schedule=str(data["schedule"]),
            prompt=str(data["prompt"]),
            skill=data.get("skill"),
            isolated=bool(data.get("isolated", False)),
            model_id=data.get("model_id"),
            provider=data.get("provider"),
            enabled=bool(data.get("enabled", True)),
            timezone=str(data.get("timezone") or "UTC"),
            max_retries=int(data.get("max_retries", 0)),
            retry_delay_seconds=int(data.get("retry_delay_seconds", 30)),
            retry_backoff_multiplier=float(data.get("retry_backoff_multiplier", 2.0)),
            retry_max_delay_seconds=int(data.get("retry_max_delay_seconds", 3_600)),
            retry_jitter_seconds=int(data.get("retry_jitter_seconds", 0)),
            jitter_seconds=int(data.get("jitter_seconds", 0)),
            misfire_policy=str(data.get("misfire_policy") or "fire_once"),
            misfire_grace_seconds=int(data.get("misfire_grace_seconds", 300)),
            concurrency_key=data.get("concurrency_key"),
            overlap_policy=str(data.get("overlap_policy") or "queue"),
            metadata=dict(data.get("metadata") or {}),
            revision=int(data.get("revision", 0)),
            next_run_at=data.get("next_run_at"),
            created_at=str(data.get("created_at") or _now_iso()),
            updated_at=str(data.get("updated_at") or _now_iso()),
        )


@dataclass(frozen=True)
class SchedulerRunRecord:
    """A persisted scheduled job execution record."""

    run_id: str
    job_name: str
    status: str
    started_at: str
    finished_at: str | None = None
    output: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    occurrence_id: str | None = None
    attempt: int = 1
    scheduled_at: str | None = None
    available_at: str | None = None
    runtime_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SchedulerRunRecord:
        return cls(
            run_id=str(data["run_id"]),
            job_name=str(data["job_name"]),
            status=str(data["status"]),
            started_at=str(data["started_at"]),
            finished_at=data.get("finished_at"),
            output=data.get("output"),
            error=data.get("error"),
            metadata=dict(data.get("metadata") or {}),
            occurrence_id=data.get("occurrence_id"),
            attempt=int(data.get("attempt", 1)),
            scheduled_at=data.get("scheduled_at"),
            available_at=data.get("available_at"),
            runtime_run_id=data.get("runtime_run_id"),
        )


@dataclass(frozen=True)
class SchedulerDueJob:
    """A job observed as due or recoverable using the store's authoritative clock."""

    job: SchedulerJob
    observed_at: str


@dataclass(frozen=True)
class SchedulerDueRun:
    """A pending, retrying, or expired-claim attempt ready to be claimed."""

    record: SchedulerRunRecord
    job: SchedulerJob
    observed_at: str


@dataclass(frozen=True)
class SchedulerRunClaim:
    """Fenced ownership of one exact scheduler attempt."""

    record: SchedulerRunRecord
    job: SchedulerJob
    worker_id: str
    claim_id: str
    lease_token: str
    fence_token: int
    acquired_at: str
    expires_at: str

    @property
    def run_id(self) -> str:
        return self.record.run_id


@runtime_checkable
class SchedulerCatalog(Protocol):
    """Shared schedule definition and history surface."""

    def upsert_job(self, job: SchedulerJob) -> SchedulerJob: ...

    def get_job(self, name: str) -> SchedulerJob | None: ...

    def list_jobs(self, *, enabled: bool | None = None) -> list[SchedulerJob]: ...

    def delete_job(self, name: str) -> bool: ...

    def set_job_enabled(self, name: str, enabled: bool) -> SchedulerJob | None: ...

    def list_runs(
        self,
        *,
        job_name: str | None = None,
        limit: int | None = None,
    ) -> list[SchedulerRunRecord]: ...


@runtime_checkable
class SchedulerBackend(SchedulerCatalog, Protocol):
    """Compatibility scheduler surface without leased worker ownership."""

    def record_run_start(
        self,
        job_name: str,
        *,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SchedulerRunRecord: ...

    def record_run_finish(
        self,
        run_id: str,
        *,
        status: str,
        output: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SchedulerRunRecord | None: ...


@runtime_checkable
class DurableSchedulerStore(Protocol):
    """Transactional extension implemented by runtime stores at schema v12."""

    def scheduler_now(self) -> str: ...

    def upsert_scheduler_job(self, job: SchedulerJob) -> SchedulerJob: ...

    def get_scheduler_job(self, name: str) -> SchedulerJob | None: ...

    def list_scheduler_jobs(self, *, enabled: bool | None = None) -> list[SchedulerJob]: ...

    def delete_scheduler_job(self, name: str) -> bool: ...

    def set_scheduler_job_enabled(
        self,
        name: str,
        enabled: bool,
        *,
        next_run_at: str | None,
    ) -> SchedulerJob | None: ...

    def list_due_scheduler_jobs(self, *, limit: int) -> list[SchedulerDueJob]: ...

    def list_ready_scheduler_runs(self, *, limit: int) -> list[SchedulerDueRun]: ...

    def claim_scheduler_job(
        self,
        name: str,
        *,
        expected_revision: int,
        expected_next_run_at: str | None,
        next_run_at: str | None,
        available_at: str | None,
        skip_reason: str | None,
        worker_id: str,
        claim_id: str,
        lease_seconds: int,
    ) -> SchedulerRunClaim | None: ...

    def claim_scheduler_run(
        self,
        run_id: str,
        *,
        worker_id: str,
        claim_id: str,
        lease_seconds: int,
    ) -> SchedulerRunClaim | None: ...

    def claim_scheduler_now(
        self,
        job_name: str,
        *,
        occurrence_id: str,
        run_id: str,
        scheduled_at: str,
        worker_id: str,
        claim_id: str,
        lease_seconds: int,
    ) -> SchedulerRunClaim | None: ...

    def renew_scheduler_claim(
        self, claim: SchedulerRunClaim, *, lease_seconds: int
    ) -> SchedulerRunClaim: ...

    def bind_scheduler_runtime_run(
        self, claim: SchedulerRunClaim, *, runtime_run_id: str
    ) -> SchedulerRunClaim: ...

    def finish_scheduler_claim(
        self,
        claim: SchedulerRunClaim,
        *,
        status: str,
        output: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
        retry_delay_seconds: float | None = None,
    ) -> SchedulerRunRecord: ...

    def release_scheduler_claim(self, claim: SchedulerRunClaim) -> bool: ...

    def list_scheduler_runs(
        self, *, job_name: str | None = None, limit: int | None = None
    ) -> list[SchedulerRunRecord]: ...


@runtime_checkable
class DurableSchedulerBackend(SchedulerCatalog, Protocol):
    """Leased/fenced scheduler surface used by service workers."""

    def claim_due_runs(
        self,
        *,
        worker_id: str,
        limit: int = 10,
        lease_seconds: int = 30,
    ) -> list[SchedulerRunClaim]: ...

    def claim_now(
        self,
        job_name: str,
        *,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> SchedulerRunClaim | None: ...

    def renew_claim(
        self, claim: SchedulerRunClaim, *, lease_seconds: int = 30
    ) -> SchedulerRunClaim: ...

    def bind_runtime_run(
        self, claim: SchedulerRunClaim, *, runtime_run_id: str
    ) -> SchedulerRunClaim: ...

    def finish_claim(
        self,
        claim: SchedulerRunClaim,
        *,
        status: str,
        output: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SchedulerRunRecord: ...

    def release_claim(self, claim: SchedulerRunClaim) -> bool: ...


def is_durable_scheduler_backend(value: object) -> bool:
    """Use structural detection without relying on runtime Protocol isinstance quirks."""
    return all(
        callable(getattr(value, name, None))
        for name in (
            "claim_due_runs",
            "claim_now",
            "renew_claim",
            "bind_runtime_run",
            "finish_claim",
            "release_claim",
        )
    )


class InMemorySchedulerBackend:
    """In-process scheduler backend useful for tests and embedded demos."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, SchedulerJob] = {}
        self._runs: dict[str, SchedulerRunRecord] = {}

    def upsert_job(self, job: SchedulerJob) -> SchedulerJob:
        now = _now_iso()
        existing = self._jobs.get(job.name)
        stored = replace(
            job,
            metadata=dict(job.metadata),
            revision=(existing.revision + 1 if existing else max(1, job.revision)),
            next_run_at=(
                job.next_run_at
                if job.next_run_at is not None
                else next_schedule_time(job, after=now)
                if job.enabled
                else None
            ),
            created_at=existing.created_at if existing else job.created_at,
            updated_at=now,
        )
        self._jobs[stored.name] = stored
        return stored

    def get_job(self, name: str) -> SchedulerJob | None:
        return self._jobs.get(name)

    def list_jobs(self, *, enabled: bool | None = None) -> list[SchedulerJob]:
        jobs = sorted(self._jobs.values(), key=lambda item: item.name)
        if enabled is None:
            return jobs
        return [job for job in jobs if job.enabled is enabled]

    def delete_job(self, name: str) -> bool:
        return self._jobs.pop(name, None) is not None

    def set_job_enabled(self, name: str, enabled: bool) -> SchedulerJob | None:
        job = self._jobs.get(name)
        if job is None:
            return None
        now = _now_iso()
        updated = replace(
            job,
            enabled=enabled,
            revision=job.revision + 1,
            next_run_at=(
                next_schedule_time(job, after=now)
                if enabled and not job.enabled
                else None
                if not enabled
                else job.next_run_at
            ),
            updated_at=now,
        )
        self._jobs[name] = updated
        return updated

    def record_run_start(
        self,
        job_name: str,
        *,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SchedulerRunRecord:
        record = SchedulerRunRecord(
            run_id=run_id or f"schedrun_{uuid4().hex}",
            job_name=job_name,
            status="running",
            started_at=_now_iso(),
            metadata=dict(metadata or {}),
        )
        self._runs[record.run_id] = record
        return record

    def record_run_finish(
        self,
        run_id: str,
        *,
        status: str,
        output: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SchedulerRunRecord | None:
        existing = self._runs.get(run_id)
        if existing is None:
            return None
        merged_metadata = dict(existing.metadata)
        if metadata:
            merged_metadata.update(metadata)
        updated = SchedulerRunRecord(
            run_id=existing.run_id,
            job_name=existing.job_name,
            status=status,
            started_at=existing.started_at,
            finished_at=_now_iso(),
            output=output,
            error=error,
            metadata=merged_metadata,
        )
        self._runs[run_id] = updated
        return updated

    def list_runs(
        self,
        *,
        job_name: str | None = None,
        limit: int | None = None,
    ) -> list[SchedulerRunRecord]:
        runs = sorted(self._runs.values(), key=lambda item: item.started_at, reverse=True)
        if job_name is not None:
            runs = [run for run in runs if run.job_name == job_name]
        if limit is not None:
            return runs[:limit]
        return runs


class JsonSchedulerBackend(InMemorySchedulerBackend):
    """Single-process JSON compatibility backend for jobs and run history."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        super().__init__()
        self._load()

    def _assert_writable(self) -> None:
        assert_migration_store_writable(
            self.path,
            code="SCHEDULER_STORE_FENCED",
            category="scheduler",
            store_name="JSON scheduler",
        )

    def upsert_job(self, job: SchedulerJob) -> SchedulerJob:
        self._assert_writable()
        stored = super().upsert_job(job)
        self._save()
        return stored

    def delete_job(self, name: str) -> bool:
        self._assert_writable()
        removed = super().delete_job(name)
        if removed:
            self._save()
        return removed

    def set_job_enabled(self, name: str, enabled: bool) -> SchedulerJob | None:
        self._assert_writable()
        updated = super().set_job_enabled(name, enabled)
        if updated is not None:
            self._save()
        return updated

    def record_run_start(
        self,
        job_name: str,
        *,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SchedulerRunRecord:
        self._assert_writable()
        record = super().record_run_start(job_name, run_id=run_id, metadata=metadata)
        self._save()
        return record

    def record_run_finish(
        self,
        run_id: str,
        *,
        status: str,
        output: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SchedulerRunRecord | None:
        self._assert_writable()
        record = super().record_run_finish(
            run_id,
            status=status,
            output=output,
            error=error,
            metadata=metadata,
        )
        if record is not None:
            self._save()
        return record

    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._jobs = {
            item["name"]: SchedulerJob.from_dict(item)
            for item in data.get("jobs", [])
            if isinstance(item, dict) and item.get("name")
        }
        self._runs = {
            item["run_id"]: SchedulerRunRecord.from_dict(item)
            for item in data.get("runs", [])
            if isinstance(item, dict) and item.get("run_id")
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "jobs": [job.to_dict() for job in self.list_jobs()],
            "runs": [run.to_dict() for run in self.list_runs()],
        }
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)


class RuntimeSchedulerBackend:
    """Durable scheduler adapter over the canonical RuntimeStore extension.

    The adapter owns recurrence math and retry policy. The store owns authoritative
    time, atomic occurrence creation, claims, fences, and stale-claim rejection.
    """

    def __init__(self, store: DurableSchedulerStore) -> None:
        self._store = store
        required = (
            "scheduler_now",
            "upsert_scheduler_job",
            "get_scheduler_job",
            "list_scheduler_jobs",
            "delete_scheduler_job",
            "set_scheduler_job_enabled",
            "list_due_scheduler_jobs",
            "list_ready_scheduler_runs",
            "claim_scheduler_job",
            "claim_scheduler_run",
            "claim_scheduler_now",
            "renew_scheduler_claim",
            "bind_scheduler_runtime_run",
            "finish_scheduler_claim",
            "release_scheduler_claim",
            "list_scheduler_runs",
        )
        missing = [name for name in required if not callable(getattr(store, name, None))]
        if missing:
            raise HarnessError(
                code="RUNTIME_STORE_SCHEDULER_REQUIRED",
                category="configuration",
                message="Durable scheduling requires a RuntimeStore at schema v12.",
                retryable=False,
                details={"missing_methods": missing},
            )

    @property
    def store(self) -> DurableSchedulerStore:
        """Return the shared authoritative store for host composition."""
        return self._store

    @staticmethod
    def _validate_claim_options(*, worker_id: str, limit: int, lease_seconds: int) -> None:
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 512:
            raise ValueError("worker_id must contain 1 to 512 characters")
        if isinstance(limit, bool) or not 1 <= limit <= MAX_SCHEDULER_BATCH_SIZE:
            raise ValueError(f"limit must be between 1 and {MAX_SCHEDULER_BATCH_SIZE}")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= MAX_SCHEDULER_LEASE_SECONDS
        ):
            raise ValueError(f"lease_seconds must be between 1 and {MAX_SCHEDULER_LEASE_SECONDS}")

    def upsert_job(self, job: SchedulerJob) -> SchedulerJob:
        if job.model_id is not None or job.provider is not None:
            raise SchedulerConfigurationError(
                "Durable jobs use the worker's immutable model; run one worker per model.",
                schedule=job.schedule,
                timezone=job.timezone,
            )
        candidate = job
        if job.enabled and job.next_run_at is None:
            candidate = replace(
                job,
                next_run_at=next_schedule_time(job, after=self._store.scheduler_now()),
            )
        if not job.enabled and job.next_run_at is not None:
            candidate = replace(job, next_run_at=None)
        return self._store.upsert_scheduler_job(candidate)

    def get_job(self, name: str) -> SchedulerJob | None:
        return self._store.get_scheduler_job(name)

    def list_jobs(self, *, enabled: bool | None = None) -> list[SchedulerJob]:
        return self._store.list_scheduler_jobs(enabled=enabled)

    def delete_job(self, name: str) -> bool:
        return self._store.delete_scheduler_job(name)

    def set_job_enabled(self, name: str, enabled: bool) -> SchedulerJob | None:
        job = self.get_job(name)
        if job is None:
            return None
        next_run_at = (
            next_schedule_time(job, after=self._store.scheduler_now()) if enabled else None
        )
        return self._store.set_scheduler_job_enabled(
            name,
            enabled,
            next_run_at=next_run_at,
        )

    def list_runs(
        self,
        *,
        job_name: str | None = None,
        limit: int | None = None,
    ) -> list[SchedulerRunRecord]:
        return self._store.list_scheduler_runs(job_name=job_name, limit=limit)

    def claim_due_runs(
        self,
        *,
        worker_id: str,
        limit: int = 10,
        lease_seconds: int = 30,
    ) -> list[SchedulerRunClaim]:
        self._validate_claim_options(
            worker_id=worker_id,
            limit=limit,
            lease_seconds=lease_seconds,
        )
        claims: list[SchedulerRunClaim] = []
        ready = self._store.list_ready_scheduler_runs(limit=limit)
        for ready_run in ready:
            claim = self._store.claim_scheduler_run(
                ready_run.record.run_id,
                worker_id=worker_id,
                claim_id=f"claim_{uuid4().hex}",
                lease_seconds=lease_seconds,
            )
            if claim is not None:
                claims.append(claim)
                if len(claims) >= limit:
                    return claims

        due = self._store.list_due_scheduler_jobs(limit=limit - len(claims))
        for due_job in due:
            job = due_job.job
            scheduled_at = job.next_run_at
            if scheduled_at is None:
                continue
            lateness = max(
                0.0,
                (_parse_iso(due_job.observed_at) - _parse_iso(scheduled_at)).total_seconds(),
            )
            skip_reason = None
            if (
                job.misfire_policy == SchedulerMisfirePolicy.SKIP.value
                and lateness > job.misfire_grace_seconds
            ):
                skip_reason = "SCHEDULER_MISFIRE_SKIPPED"
            next_base = (
                scheduled_at
                if job.misfire_policy == SchedulerMisfirePolicy.CATCH_UP.value
                else due_job.observed_at
                if lateness > job.misfire_grace_seconds
                else scheduled_at
            )
            next_run_at = next_schedule_time(job, after=next_base)
            if (
                job.misfire_policy == SchedulerMisfirePolicy.FIRE_ONCE.value
                and _parse_iso(next_run_at) <= _parse_iso(due_job.observed_at)
            ):
                # fire_once promises one late occurrence with old backlog
                # coalesced. Within-grace lateness spanning several intervals
                # must not burst-replay each missed occurrence, so re-anchor
                # the next nominal time past the observation instant.
                next_run_at = next_schedule_time(job, after=due_job.observed_at)
            occurrence_id = scheduler_occurrence_id(
                job_name=job.name,
                job_revision=job.revision,
                scheduled_at=scheduled_at,
            )
            available_at = (
                _parse_iso(scheduled_at)
                + timedelta(seconds=scheduler_jitter_seconds(job, occurrence_id=occurrence_id))
            ).isoformat()
            claim = self._store.claim_scheduler_job(
                job.name,
                expected_revision=job.revision,
                expected_next_run_at=scheduled_at,
                next_run_at=next_run_at,
                available_at=available_at,
                skip_reason=skip_reason,
                worker_id=worker_id,
                claim_id=f"claim_{uuid4().hex}",
                lease_seconds=lease_seconds,
            )
            if claim is not None:
                claims.append(claim)
        return claims

    def claim_now(
        self,
        job_name: str,
        *,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> SchedulerRunClaim | None:
        self._validate_claim_options(worker_id=worker_id, limit=1, lease_seconds=lease_seconds)
        job = self.get_job(job_name)
        if job is None:
            return None
        scheduled_at = self._store.scheduler_now()
        occurrence_id = "schedmanual_" + uuid4().hex
        run_id = scheduler_run_id(occurrence_id=occurrence_id, attempt=1)
        return self._store.claim_scheduler_now(
            job_name,
            occurrence_id=occurrence_id,
            run_id=run_id,
            scheduled_at=scheduled_at,
            worker_id=worker_id,
            claim_id=f"claim_{uuid4().hex}",
            lease_seconds=lease_seconds,
        )

    def renew_claim(
        self,
        claim: SchedulerRunClaim,
        *,
        lease_seconds: int = 30,
    ) -> SchedulerRunClaim:
        self._validate_claim_options(
            worker_id=claim.worker_id,
            limit=1,
            lease_seconds=lease_seconds,
        )
        return self._store.renew_scheduler_claim(claim, lease_seconds=lease_seconds)

    def bind_runtime_run(
        self,
        claim: SchedulerRunClaim,
        *,
        runtime_run_id: str,
    ) -> SchedulerRunClaim:
        return self._store.bind_scheduler_runtime_run(
            claim,
            runtime_run_id=runtime_run_id,
        )

    def finish_claim(
        self,
        claim: SchedulerRunClaim,
        *,
        status: str,
        output: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SchedulerRunRecord:
        settlement_metadata = dict(metadata or {})
        if output is not None and len(output) > MAX_SCHEDULER_OUTPUT_CHARS:
            settlement_metadata.update(
                {
                    "output_preview_truncated": True,
                    "output_character_count": len(output),
                }
            )
            output = output[:MAX_SCHEDULER_OUTPUT_CHARS]
        retry_delay_seconds = None
        if status == "failed" and claim.record.attempt <= claim.job.max_retries:
            retry_delay_seconds = scheduler_retry_delay(
                claim.job,
                failed_attempt=claim.record.attempt,
            )
        return self._store.finish_scheduler_claim(
            claim,
            status=status,
            output=output,
            error=error,
            metadata=settlement_metadata,
            retry_delay_seconds=retry_delay_seconds,
        )

    def release_claim(self, claim: SchedulerRunClaim) -> bool:
        return self._store.release_scheduler_claim(claim)


__all__ = [
    "DurableSchedulerBackend",
    "DurableSchedulerStore",
    "InMemorySchedulerBackend",
    "JsonSchedulerBackend",
    "MAX_SCHEDULER_BATCH_SIZE",
    "MAX_SCHEDULER_LEASE_SECONDS",
    "RuntimeSchedulerBackend",
    "SchedulerBackend",
    "SchedulerCatalog",
    "SchedulerConfigurationError",
    "SchedulerDueJob",
    "SchedulerDueRun",
    "SchedulerJob",
    "SchedulerLeaseLostError",
    "SchedulerMisfirePolicy",
    "SchedulerOverlapPolicy",
    "SchedulerRunClaim",
    "SchedulerRunRecord",
    "next_schedule_time",
    "is_durable_scheduler_backend",
    "scheduler_idempotency_key",
    "scheduler_job_digest",
    "scheduler_jitter_seconds",
    "scheduler_occurrence_id",
    "scheduler_retry_delay",
    "scheduler_run_id",
    "scheduler_fence_path",
    "scheduler_store_path",
]
