"""Persistent scheduler contracts for embedded agnoclaw runtimes."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

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
            metadata=dict(data.get("metadata") or {}),
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
        )


@runtime_checkable
class SchedulerBackend(Protocol):
    """Minimal durable scheduler storage contract."""

    def upsert_job(self, job: SchedulerJob) -> SchedulerJob:
        ...

    def get_job(self, name: str) -> SchedulerJob | None:
        ...

    def list_jobs(self, *, enabled: bool | None = None) -> list[SchedulerJob]:
        ...

    def delete_job(self, name: str) -> bool:
        ...

    def set_job_enabled(self, name: str, enabled: bool) -> SchedulerJob | None:
        ...

    def record_run_start(
        self,
        job_name: str,
        *,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SchedulerRunRecord:
        ...

    def record_run_finish(
        self,
        run_id: str,
        *,
        status: str,
        output: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SchedulerRunRecord | None:
        ...

    def list_runs(
        self,
        *,
        job_name: str | None = None,
        limit: int | None = None,
    ) -> list[SchedulerRunRecord]:
        ...


class InMemorySchedulerBackend:
    """In-process scheduler backend useful for tests and embedded demos."""

    def __init__(self) -> None:
        self._jobs: dict[str, SchedulerJob] = {}
        self._runs: dict[str, SchedulerRunRecord] = {}

    def upsert_job(self, job: SchedulerJob) -> SchedulerJob:
        now = _now_iso()
        existing = self._jobs.get(job.name)
        stored = SchedulerJob(
            name=job.name,
            schedule=job.schedule,
            prompt=job.prompt,
            skill=job.skill,
            isolated=job.isolated,
            model_id=job.model_id,
            provider=job.provider,
            enabled=job.enabled,
            metadata=dict(job.metadata),
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
        updated = SchedulerJob(
            name=job.name,
            schedule=job.schedule,
            prompt=job.prompt,
            skill=job.skill,
            isolated=job.isolated,
            model_id=job.model_id,
            provider=job.provider,
            enabled=enabled,
            metadata=dict(job.metadata),
            created_at=job.created_at,
            updated_at=_now_iso(),
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
    """JSON-file scheduler backend for local durable schedules and run history."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        super().__init__()
        self._load()

    def upsert_job(self, job: SchedulerJob) -> SchedulerJob:
        stored = super().upsert_job(job)
        self._save()
        return stored

    def delete_job(self, name: str) -> bool:
        removed = super().delete_job(name)
        if removed:
            self._save()
        return removed

    def set_job_enabled(self, name: str, enabled: bool) -> SchedulerJob | None:
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
