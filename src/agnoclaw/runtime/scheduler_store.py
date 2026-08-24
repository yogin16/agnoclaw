"""Transactional SQLite/PostgreSQL storage mixins for durable scheduler jobs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .errors import HarnessError
from .scheduler import (
    SchedulerDueJob,
    SchedulerDueRun,
    SchedulerJob,
    SchedulerLeaseLostError,
    SchedulerRunClaim,
    SchedulerRunRecord,
    scheduler_job_digest,
    scheduler_occurrence_id,
    scheduler_run_id,
)

_ACTIVE_STATUSES = ("claimed", "running", "detached")
_READY_STATUSES = ("pending", "retry_wait")
_TERMINAL_STATUSES = (
    "completed",
    "failed",
    "cancelled",
    "skipped",
    "dead_lettered",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _lease_token(*, run_id: str, claim_id: str, worker_id: str, fence_token: int) -> str:
    material = _json(
        {
            "claim_id": claim_id,
            "fence_token": fence_token,
            "run_id": run_id,
            "worker_id": worker_id,
        }
    )
    return "sha256:" + hashlib.sha256(material.encode()).hexdigest()


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _job_from_row(row: Any) -> SchedulerJob:
    return SchedulerJob.from_dict(json.loads(row["job_json"]))


def _record_from_row(row: Any) -> SchedulerRunRecord:
    return SchedulerRunRecord.from_dict(json.loads(row["record_json"]))


def _claim_from_row(row: Any) -> SchedulerRunClaim:
    record = _record_from_row(row)
    return SchedulerRunClaim(
        record=record,
        job=SchedulerJob.from_dict(json.loads(row["job_json"])),
        worker_id=str(row["worker_id"]),
        claim_id=str(row["claim_id"]),
        lease_token=str(row["lease_token"]),
        fence_token=int(row["fence_token"]),
        acquired_at=_iso(row["acquired_at"]),
        expires_at=_iso(row["lease_expires_at"]),
    )


def _validate_limit(limit: int, *, allow_none: bool = False) -> None:
    if allow_none and limit is None:
        return
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
        raise ValueError("limit must be between 1 and 1000")


def _validate_claim_input(*, worker_id: str, claim_id: str, lease_seconds: int) -> None:
    for name, value in (("worker_id", worker_id), ("claim_id", claim_id)):
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise ValueError(f"{name} must contain 1 to 512 characters")
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or not 1 <= lease_seconds <= 86_400
    ):
        raise ValueError("lease_seconds must be between 1 and 86400")


def _runtime_binding_conflict(*, run_id: str) -> HarnessError:
    return HarnessError(
        code="SCHEDULER_RUNTIME_BINDING_CONFLICT",
        category="scheduler",
        message="A scheduler attempt is already bound to another lifecycle run.",
        retryable=False,
        details={"schedule_run_id": run_id},
    )


def _new_record(
    *,
    run_id: str,
    occurrence_id: str,
    job: SchedulerJob,
    attempt: int,
    status: str,
    scheduled_at: str,
    available_at: str,
    started_at: str,
    metadata: dict[str, Any] | None = None,
) -> SchedulerRunRecord:
    return SchedulerRunRecord(
        run_id=run_id,
        job_name=job.name,
        status=status,
        started_at=started_at,
        occurrence_id=occurrence_id,
        attempt=attempt,
        scheduled_at=scheduled_at,
        available_at=available_at,
        metadata=dict(metadata or {}),
    )


class SQLiteSchedulerStoreMixin:
    """Schema-v12 scheduler methods for ``SQLiteRuntimeStore``."""

    _connection: sqlite3.Connection

    @staticmethod
    def _scheduler_now(conn: sqlite3.Connection) -> str:
        row = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now') AS now"
        ).fetchone()
        return str(row["now"])

    def scheduler_now(self) -> str:
        with self._lock:  # type: ignore[attr-defined]
            return self._scheduler_now(self._connection)

    def upsert_scheduler_job(self, job: SchedulerJob) -> SchedulerJob:
        with self._transaction() as conn:  # type: ignore[attr-defined]
            existing = conn.execute(
                "SELECT job_json FROM runtime_scheduler_jobs WHERE job_name = ?",
                (job.name,),
            ).fetchone()
            prior = _job_from_row(existing) if existing is not None else None
            now = self._scheduler_now(conn)
            stored = replace(
                job,
                revision=(prior.revision + 1 if prior is not None else max(1, job.revision)),
                created_at=prior.created_at if prior is not None else now,
                updated_at=now,
            )
            conn.execute(
                """
                INSERT INTO runtime_scheduler_jobs(
                    job_name, revision, enabled, next_run_at, job_digest, job_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_name) DO UPDATE SET
                    revision = excluded.revision,
                    enabled = excluded.enabled,
                    next_run_at = excluded.next_run_at,
                    job_digest = excluded.job_digest,
                    job_json = excluded.job_json,
                    updated_at = excluded.updated_at
                """,
                (
                    stored.name,
                    stored.revision,
                    int(stored.enabled),
                    stored.next_run_at,
                    scheduler_job_digest(stored),
                    _json(stored.to_dict()),
                    stored.created_at,
                    stored.updated_at,
                ),
            )
            return stored

    def get_scheduler_job(self, name: str) -> SchedulerJob | None:
        with self._lock:  # type: ignore[attr-defined]
            row = self._connection.execute(
                "SELECT job_json FROM runtime_scheduler_jobs WHERE job_name = ?",
                (name,),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def list_scheduler_jobs(self, *, enabled: bool | None = None) -> list[SchedulerJob]:
        sql = "SELECT job_json FROM runtime_scheduler_jobs"
        parameters: tuple[Any, ...] = ()
        if enabled is not None:
            sql += " WHERE enabled = ?"
            parameters = (int(enabled),)
        sql += " ORDER BY job_name"
        with self._lock:  # type: ignore[attr-defined]
            rows = self._connection.execute(sql, parameters).fetchall()
        return [_job_from_row(row) for row in rows]

    def delete_scheduler_job(self, name: str) -> bool:
        with self._transaction() as conn:  # type: ignore[attr-defined]
            return (
                conn.execute(
                    "DELETE FROM runtime_scheduler_jobs WHERE job_name = ?",
                    (name,),
                ).rowcount
                == 1
            )

    def set_scheduler_job_enabled(
        self,
        name: str,
        enabled: bool,
        *,
        next_run_at: str | None,
    ) -> SchedulerJob | None:
        with self._transaction() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT job_json FROM runtime_scheduler_jobs WHERE job_name = ?",
                (name,),
            ).fetchone()
            if row is None:
                return None
            now = self._scheduler_now(conn)
            stored = replace(
                _job_from_row(row),
                enabled=enabled,
                revision=_job_from_row(row).revision + 1,
                next_run_at=next_run_at,
                updated_at=now,
            )
            conn.execute(
                """
                UPDATE runtime_scheduler_jobs
                SET revision = ?, enabled = ?, next_run_at = ?, job_digest = ?,
                    job_json = ?, updated_at = ?
                WHERE job_name = ?
                """,
                (
                    stored.revision,
                    int(enabled),
                    next_run_at,
                    scheduler_job_digest(stored),
                    _json(stored.to_dict()),
                    now,
                    name,
                ),
            )
            return stored

    def list_due_scheduler_jobs(self, *, limit: int) -> list[SchedulerDueJob]:
        _validate_limit(limit)
        with self._lock:  # type: ignore[attr-defined]
            now = self._scheduler_now(self._connection)
            rows = self._connection.execute(
                """
                SELECT job_json FROM runtime_scheduler_jobs
                WHERE enabled = 1 AND next_run_at IS NOT NULL
                  AND julianday(next_run_at) <= julianday(?)
                ORDER BY julianday(next_run_at), job_name LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [SchedulerDueJob(job=_job_from_row(row), observed_at=now) for row in rows]

    def list_ready_scheduler_runs(self, *, limit: int) -> list[SchedulerDueRun]:
        _validate_limit(limit)
        with self._lock:  # type: ignore[attr-defined]
            now = self._scheduler_now(self._connection)
            rows = self._connection.execute(
                """
                SELECT * FROM runtime_scheduler_runs
                WHERE (
                    status IN ('pending', 'retry_wait')
                    AND julianday(available_at) <= julianday(?)
                ) OR (
                    status IN ('claimed', 'running', 'detached')
                    AND (released_at IS NOT NULL
                         OR julianday(lease_expires_at) <= julianday(?))
                )
                ORDER BY julianday(available_at), run_id LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
        return [
            SchedulerDueRun(
                record=_record_from_row(row),
                job=SchedulerJob.from_dict(json.loads(row["job_json"])),
                observed_at=now,
            )
            for row in rows
        ]

    @staticmethod
    def _sqlite_group_busy(
        conn: sqlite3.Connection,
        *,
        concurrency_key: str | None,
        now: str,
        excluding_run_id: str | None = None,
    ) -> bool:
        if concurrency_key is None:
            return False
        row = conn.execute(
            """
            SELECT 1 FROM runtime_scheduler_runs
            WHERE concurrency_key = ? AND run_id <> ?
              AND status IN ('claimed', 'running') AND released_at IS NULL
              AND julianday(lease_expires_at) > julianday(?) LIMIT 1
            """,
            (concurrency_key, excluding_run_id or "", now),
        ).fetchone()
        return row is not None

    @staticmethod
    def _sqlite_group_backlogged(
        conn: sqlite3.Connection,
        *,
        concurrency_key: str,
    ) -> bool:
        return (
            conn.execute(
                """
                SELECT 1 FROM runtime_scheduler_runs
                WHERE concurrency_key = ? AND status IN ('pending', 'retry_wait')
                LIMIT 1
                """,
                (concurrency_key,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _sqlite_insert_run(
        conn: sqlite3.Connection,
        *,
        record: SchedulerRunRecord,
        job: SchedulerJob,
        worker_id: str | None,
        claim_id: str | None,
        token: str | None,
        fence_token: int,
        acquired_at: str | None,
        expires_at: str | None,
        released_at: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO runtime_scheduler_runs(
                run_id, occurrence_id, job_name, job_revision, attempt, status,
                scheduled_at, available_at, runtime_run_id, concurrency_key,
                record_json, job_json, worker_id, claim_id, lease_token, fence_token,
                acquired_at, renewed_at, lease_expires_at, released_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.run_id,
                record.occurrence_id,
                record.job_name,
                job.revision,
                record.attempt,
                record.status,
                record.scheduled_at,
                record.available_at,
                record.runtime_run_id,
                job.concurrency_key or job.name,
                _json(record.to_dict()),
                _json(job.to_dict()),
                worker_id,
                claim_id,
                token,
                fence_token,
                acquired_at,
                acquired_at,
                expires_at,
                released_at,
            ),
        )

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
    ) -> SchedulerRunClaim | None:
        _validate_claim_input(
            worker_id=worker_id,
            claim_id=claim_id,
            lease_seconds=lease_seconds,
        )
        with self._transaction() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT * FROM runtime_scheduler_jobs WHERE job_name = ?",
                (name,),
            ).fetchone()
            if row is None:
                return None
            job = _job_from_row(row)
            now = self._scheduler_now(conn)
            if (
                not job.enabled
                or job.revision != expected_revision
                or job.next_run_at != expected_next_run_at
                or expected_next_run_at is None
                or _parse(expected_next_run_at) > _parse(now)
            ):
                return None
            occurrence_id = scheduler_occurrence_id(
                job_name=name,
                job_revision=job.revision,
                scheduled_at=expected_next_run_at,
            )
            run_id = scheduler_run_id(occurrence_id=occurrence_id, attempt=1)
            effective_available = available_at or expected_next_run_at
            group_key = job.concurrency_key or job.name
            busy = self._sqlite_group_busy(
                conn,
                concurrency_key=group_key,
                now=now,
            )
            backlogged = self._sqlite_group_backlogged(
                conn,
                concurrency_key=group_key,
            )
            if backlogged and job.overlap_policy == "queue":
                return None
            advanced = replace(job, next_run_at=next_run_at, updated_at=now)
            updated = conn.execute(
                """
                UPDATE runtime_scheduler_jobs
                SET next_run_at = ?, job_json = ?, updated_at = ?
                WHERE job_name = ? AND revision = ? AND next_run_at = ? AND enabled = 1
                """,
                (
                    next_run_at,
                    _json(advanced.to_dict()),
                    now,
                    name,
                    expected_revision,
                    expected_next_run_at,
                ),
            )
            if updated.rowcount != 1:
                return None
            occupied = busy or backlogged
            should_skip = skip_reason is not None or (
                occupied and job.overlap_policy == "skip"
            )
            can_claim = (
                not should_skip
                and not occupied
                and _parse(effective_available) <= _parse(now)
            )
            status = "claimed" if can_claim else "skipped" if should_skip else "pending"
            record = _new_record(
                run_id=run_id,
                occurrence_id=occurrence_id,
                job=job,
                attempt=1,
                status=status,
                scheduled_at=expected_next_run_at,
                available_at=effective_available,
                started_at=now,
                metadata={"job_revision": job.revision},
            )
            if should_skip:
                record = replace(
                    record,
                    finished_at=now,
                    error=skip_reason or "SCHEDULER_OVERLAP_SKIPPED",
                )
            expires_at = (
                (_parse(now) + timedelta(seconds=lease_seconds)).isoformat()
                if can_claim
                else None
            )
            token = (
                _lease_token(
                    run_id=run_id,
                    claim_id=claim_id,
                    worker_id=worker_id,
                    fence_token=1,
                )
                if can_claim
                else None
            )
            self._sqlite_insert_run(
                conn,
                record=record,
                job=job,
                worker_id=worker_id if can_claim else None,
                claim_id=claim_id if can_claim else None,
                token=token,
                fence_token=1 if can_claim else 0,
                acquired_at=now if can_claim else None,
                expires_at=expires_at,
                released_at=now if should_skip else None,
            )
            if not can_claim:
                return None
            claimed = conn.execute(
                "SELECT * FROM runtime_scheduler_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return _claim_from_row(claimed)

    def claim_scheduler_run(
        self,
        run_id: str,
        *,
        worker_id: str,
        claim_id: str,
        lease_seconds: int,
    ) -> SchedulerRunClaim | None:
        _validate_claim_input(
            worker_id=worker_id,
            claim_id=claim_id,
            lease_seconds=lease_seconds,
        )
        with self._transaction() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT * FROM runtime_scheduler_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            record = _record_from_row(row)
            job = SchedulerJob.from_dict(json.loads(row["job_json"]))
            now = self._scheduler_now(conn)
            ready = record.status in _READY_STATUSES and _parse(
                record.available_at or record.started_at
            ) <= _parse(now)
            stale = record.status in _ACTIVE_STATUSES and (
                row["released_at"] is not None
                or row["lease_expires_at"] is None
                or _parse(str(row["lease_expires_at"])) <= _parse(now)
            )
            if not (ready or stale):
                return None
            group_key = job.concurrency_key or job.name
            if self._sqlite_group_busy(
                conn,
                concurrency_key=group_key,
                now=now,
                excluding_run_id=run_id,
            ):
                if job.overlap_policy == "skip":
                    skipped = replace(
                        record,
                        status="skipped",
                        finished_at=now,
                        error="SCHEDULER_OVERLAP_SKIPPED",
                    )
                    conn.execute(
                        """
                        UPDATE runtime_scheduler_runs
                        SET status = 'skipped', record_json = ?, released_at = ?,
                            lease_expires_at = ? WHERE run_id = ?
                        """,
                        (_json(skipped.to_dict()), now, now, run_id),
                    )
                return None
            fence_token = int(row["fence_token"]) + 1
            token = _lease_token(
                run_id=run_id,
                claim_id=claim_id,
                worker_id=worker_id,
                fence_token=fence_token,
            )
            expires_at = (_parse(now) + timedelta(seconds=lease_seconds)).isoformat()
            claimed_record = replace(record, status="claimed")
            conn.execute(
                """
                UPDATE runtime_scheduler_runs SET
                    status = 'claimed', record_json = ?, worker_id = ?, claim_id = ?,
                    lease_token = ?, fence_token = ?, acquired_at = ?, renewed_at = ?,
                    lease_expires_at = ?, released_at = NULL
                WHERE run_id = ?
                """,
                (
                    _json(claimed_record.to_dict()),
                    worker_id,
                    claim_id,
                    token,
                    fence_token,
                    now,
                    now,
                    expires_at,
                    run_id,
                ),
            )
            claimed = conn.execute(
                "SELECT * FROM runtime_scheduler_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return _claim_from_row(claimed)

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
    ) -> SchedulerRunClaim | None:
        _validate_claim_input(
            worker_id=worker_id,
            claim_id=claim_id,
            lease_seconds=lease_seconds,
        )
        with self._transaction() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT job_json FROM runtime_scheduler_jobs WHERE job_name = ?",
                (job_name,),
            ).fetchone()
            if row is None:
                return None
            job = _job_from_row(row)
            now = self._scheduler_now(conn)
            if self._sqlite_group_busy(
                conn,
                concurrency_key=job.concurrency_key or job.name,
                now=now,
            ):
                return None
            token = _lease_token(
                run_id=run_id,
                claim_id=claim_id,
                worker_id=worker_id,
                fence_token=1,
            )
            expires_at = (_parse(now) + timedelta(seconds=lease_seconds)).isoformat()
            record = _new_record(
                run_id=run_id,
                occurrence_id=occurrence_id,
                job=job,
                attempt=1,
                status="claimed",
                scheduled_at=scheduled_at,
                available_at=scheduled_at,
                started_at=now,
                metadata={"manual": True, "job_revision": job.revision},
            )
            self._sqlite_insert_run(
                conn,
                record=record,
                job=job,
                worker_id=worker_id,
                claim_id=claim_id,
                token=token,
                fence_token=1,
                acquired_at=now,
                expires_at=expires_at,
                released_at=None,
            )
            claimed = conn.execute(
                "SELECT * FROM runtime_scheduler_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return _claim_from_row(claimed)

    @staticmethod
    def _sqlite_authoritative_claim(
        conn: sqlite3.Connection,
        claim: SchedulerRunClaim,
        *,
        now: str,
        allow_released: bool = False,
    ) -> Any:
        row = conn.execute(
            "SELECT * FROM runtime_scheduler_runs WHERE run_id = ?",
            (claim.run_id,),
        ).fetchone()
        if (
            row is None
            or row["worker_id"] != claim.worker_id
            or row["claim_id"] != claim.claim_id
            or row["lease_token"] != claim.lease_token
            or int(row["fence_token"]) != claim.fence_token
            or (row["released_at"] is not None and not allow_released)
            or row["lease_expires_at"] is None
            or (_parse(str(row["lease_expires_at"])) <= _parse(now) and not allow_released)
        ):
            raise SchedulerLeaseLostError(run_id=claim.run_id)
        return row

    def renew_scheduler_claim(
        self,
        claim: SchedulerRunClaim,
        *,
        lease_seconds: int,
    ) -> SchedulerRunClaim:
        _validate_claim_input(
            worker_id=claim.worker_id,
            claim_id=claim.claim_id,
            lease_seconds=lease_seconds,
        )
        with self._transaction() as conn:  # type: ignore[attr-defined]
            now = self._scheduler_now(conn)
            self._sqlite_authoritative_claim(conn, claim, now=now)
            expires_at = (_parse(now) + timedelta(seconds=lease_seconds)).isoformat()
            conn.execute(
                """
                UPDATE runtime_scheduler_runs SET renewed_at = ?, lease_expires_at = ?
                WHERE run_id = ?
                """,
                (now, expires_at, claim.run_id),
            )
            row = conn.execute(
                "SELECT * FROM runtime_scheduler_runs WHERE run_id = ?",
                (claim.run_id,),
            ).fetchone()
            return _claim_from_row(row)

    def bind_scheduler_runtime_run(
        self,
        claim: SchedulerRunClaim,
        *,
        runtime_run_id: str,
    ) -> SchedulerRunClaim:
        if not runtime_run_id.strip() or len(runtime_run_id) > 512:
            raise ValueError("runtime_run_id must contain 1 to 512 characters")
        with self._transaction() as conn:  # type: ignore[attr-defined]
            now = self._scheduler_now(conn)
            row = self._sqlite_authoritative_claim(conn, claim, now=now)
            record = _record_from_row(row)
            if record.runtime_run_id not in (None, runtime_run_id):
                raise _runtime_binding_conflict(run_id=claim.run_id)
            bound = replace(record, runtime_run_id=runtime_run_id, status="running")
            conn.execute(
                """
                UPDATE runtime_scheduler_runs
                SET status = 'running', runtime_run_id = ?, record_json = ?
                WHERE run_id = ?
                """,
                (runtime_run_id, _json(bound.to_dict()), claim.run_id),
            )
            updated = conn.execute(
                "SELECT * FROM runtime_scheduler_runs WHERE run_id = ?",
                (claim.run_id,),
            ).fetchone()
            return _claim_from_row(updated)

    def finish_scheduler_claim(
        self,
        claim: SchedulerRunClaim,
        *,
        status: str,
        output: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
        retry_delay_seconds: float | None = None,
    ) -> SchedulerRunRecord:
        if status not in _TERMINAL_STATUSES and status != "detached":
            raise ValueError("unsupported scheduler settlement status")
        with self._transaction() as conn:  # type: ignore[attr-defined]
            now = self._scheduler_now(conn)
            row = self._sqlite_authoritative_claim(
                conn,
                claim,
                now=now,
                allow_released=True,
            )
            existing = _record_from_row(row)
            # A committed detach releases the lease like a terminal settlement,
            # so its retry must replay idempotently instead of tripping the
            # expiry check below as a spurious lease loss.
            if row["released_at"] is not None and (
                existing.status in _TERMINAL_STATUSES or existing.status == "detached"
            ):
                if existing.status != status:
                    raise SchedulerLeaseLostError(run_id=claim.run_id)
                return existing
            if _parse(str(row["lease_expires_at"])) <= _parse(now):
                raise SchedulerLeaseLostError(run_id=claim.run_id)
            merged_metadata = dict(existing.metadata)
            merged_metadata.update(metadata or {})
            settled = replace(
                existing,
                status=status,
                finished_at=None if status == "detached" else now,
                output=output,
                error=error,
                metadata=merged_metadata,
            )
            conn.execute(
                """
                UPDATE runtime_scheduler_runs SET status = ?, record_json = ?,
                    released_at = ?, lease_expires_at = ? WHERE run_id = ?
                """,
                (status, _json(settled.to_dict()), now, now, claim.run_id),
            )
            if retry_delay_seconds is not None:
                if not 0 <= retry_delay_seconds <= 604_800:
                    raise ValueError("retry_delay_seconds must be between 0 and 604800")
                job = SchedulerJob.from_dict(json.loads(row["job_json"]))
                attempt = existing.attempt + 1
                retry_id = scheduler_run_id(
                    occurrence_id=str(existing.occurrence_id),
                    attempt=attempt,
                )
                retry_at = (
                    _parse(now) + timedelta(seconds=retry_delay_seconds)
                ).isoformat()
                retry = _new_record(
                    run_id=retry_id,
                    occurrence_id=str(existing.occurrence_id),
                    job=job,
                    attempt=attempt,
                    status="retry_wait",
                    scheduled_at=str(existing.scheduled_at),
                    available_at=retry_at,
                    started_at=now,
                    metadata={"retry_of": existing.run_id, "job_revision": job.revision},
                )
                self._sqlite_insert_run(
                    conn,
                    record=retry,
                    job=job,
                    worker_id=None,
                    claim_id=None,
                    token=None,
                    fence_token=0,
                    acquired_at=None,
                    expires_at=None,
                    released_at=None,
                )
            return settled

    def release_scheduler_claim(self, claim: SchedulerRunClaim) -> bool:
        with self._transaction() as conn:  # type: ignore[attr-defined]
            now = self._scheduler_now(conn)
            row = self._sqlite_authoritative_claim(
                conn,
                claim,
                now=now,
                allow_released=True,
            )
            if row["released_at"] is not None:
                return False
            if _parse(str(row["lease_expires_at"])) <= _parse(now):
                raise SchedulerLeaseLostError(run_id=claim.run_id)
            record = _record_from_row(row)
            status = "detached" if record.runtime_run_id else "pending"
            released = replace(record, status=status)
            conn.execute(
                """
                UPDATE runtime_scheduler_runs SET status = ?, record_json = ?,
                    released_at = ?, lease_expires_at = ? WHERE run_id = ?
                """,
                (status, _json(released.to_dict()), now, now, claim.run_id),
            )
            return True

    def list_scheduler_runs(
        self,
        *,
        job_name: str | None = None,
        limit: int | None = None,
    ) -> list[SchedulerRunRecord]:
        if limit is not None:
            _validate_limit(limit)
        sql = "SELECT record_json FROM runtime_scheduler_runs"
        parameters: list[Any] = []
        if job_name is not None:
            sql += " WHERE job_name = ?"
            parameters.append(job_name)
        sql += " ORDER BY scheduled_at DESC, attempt DESC, run_id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        with self._lock:  # type: ignore[attr-defined]
            rows = self._connection.execute(sql, tuple(parameters)).fetchall()
        return [_record_from_row(row) for row in rows]


class PostgresSchedulerStoreMixin:
    """Schema-v12 scheduler methods for ``PostgresRuntimeStore``."""

    @staticmethod
    def _postgres_now(conn: Any) -> datetime:
        return conn.execute("SELECT CURRENT_TIMESTAMP AS now").fetchone()["now"]

    def scheduler_now(self) -> str:
        with self._connection() as conn:  # type: ignore[attr-defined]
            return self._postgres_now(conn).isoformat()

    def upsert_scheduler_job(self, job: SchedulerJob) -> SchedulerJob:
        with self._transaction() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                """
                SELECT job_json FROM runtime_scheduler_jobs
                WHERE job_name = %s FOR UPDATE
                """,
                (job.name,),
            ).fetchone()
            prior = _job_from_row(row) if row is not None else None
            now = self._postgres_now(conn).isoformat()
            stored = replace(
                job,
                revision=(prior.revision + 1 if prior is not None else max(1, job.revision)),
                created_at=prior.created_at if prior is not None else now,
                updated_at=now,
            )
            conn.execute(
                """
                INSERT INTO runtime_scheduler_jobs(
                    job_name, revision, enabled, next_run_at, job_digest, job_json,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(job_name) DO UPDATE SET
                    revision = EXCLUDED.revision,
                    enabled = EXCLUDED.enabled,
                    next_run_at = EXCLUDED.next_run_at,
                    job_digest = EXCLUDED.job_digest,
                    job_json = EXCLUDED.job_json,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    stored.name,
                    stored.revision,
                    stored.enabled,
                    stored.next_run_at,
                    scheduler_job_digest(stored),
                    _json(stored.to_dict()),
                    stored.created_at,
                    stored.updated_at,
                ),
            )
            return stored

    def get_scheduler_job(self, name: str) -> SchedulerJob | None:
        with self._connection() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT job_json FROM runtime_scheduler_jobs WHERE job_name = %s",
                (name,),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def list_scheduler_jobs(self, *, enabled: bool | None = None) -> list[SchedulerJob]:
        sql = "SELECT job_json FROM runtime_scheduler_jobs"
        parameters: tuple[Any, ...] = ()
        if enabled is not None:
            sql += " WHERE enabled = %s"
            parameters = (enabled,)
        sql += " ORDER BY job_name"
        with self._connection() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(sql, parameters).fetchall()
        return [_job_from_row(row) for row in rows]

    def delete_scheduler_job(self, name: str) -> bool:
        with self._transaction() as conn:  # type: ignore[attr-defined]
            return (
                conn.execute(
                    "DELETE FROM runtime_scheduler_jobs WHERE job_name = %s",
                    (name,),
                ).rowcount
                == 1
            )

    def set_scheduler_job_enabled(
        self,
        name: str,
        enabled: bool,
        *,
        next_run_at: str | None,
    ) -> SchedulerJob | None:
        with self._transaction() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                """
                SELECT job_json FROM runtime_scheduler_jobs
                WHERE job_name = %s FOR UPDATE
                """,
                (name,),
            ).fetchone()
            if row is None:
                return None
            prior = _job_from_row(row)
            now = self._postgres_now(conn).isoformat()
            stored = replace(
                prior,
                enabled=enabled,
                revision=prior.revision + 1,
                next_run_at=next_run_at,
                updated_at=now,
            )
            conn.execute(
                """
                UPDATE runtime_scheduler_jobs
                SET revision = %s, enabled = %s, next_run_at = %s,
                    job_digest = %s, job_json = %s, updated_at = %s
                WHERE job_name = %s
                """,
                (
                    stored.revision,
                    enabled,
                    next_run_at,
                    scheduler_job_digest(stored),
                    _json(stored.to_dict()),
                    now,
                    name,
                ),
            )
            return stored

    def list_due_scheduler_jobs(self, *, limit: int) -> list[SchedulerDueJob]:
        _validate_limit(limit)
        with self._connection() as conn:  # type: ignore[attr-defined]
            now = self._postgres_now(conn)
            rows = conn.execute(
                """
                SELECT job_json FROM runtime_scheduler_jobs
                WHERE enabled = TRUE AND next_run_at IS NOT NULL
                  AND next_run_at <= CURRENT_TIMESTAMP
                ORDER BY next_run_at, job_name LIMIT %s
                """,
                (limit,),
            ).fetchall()
        observed_at = now.isoformat()
        return [SchedulerDueJob(job=_job_from_row(row), observed_at=observed_at) for row in rows]

    def list_ready_scheduler_runs(self, *, limit: int) -> list[SchedulerDueRun]:
        _validate_limit(limit)
        with self._connection() as conn:  # type: ignore[attr-defined]
            now = self._postgres_now(conn)
            rows = conn.execute(
                """
                SELECT * FROM runtime_scheduler_runs
                WHERE (
                    status IN ('pending', 'retry_wait')
                    AND available_at <= CURRENT_TIMESTAMP
                ) OR (
                    status IN ('claimed', 'running', 'detached')
                    AND (released_at IS NOT NULL
                         OR lease_expires_at <= CURRENT_TIMESTAMP)
                )
                ORDER BY available_at, run_id LIMIT %s
                """,
                (limit,),
            ).fetchall()
        observed_at = now.isoformat()
        return [
            SchedulerDueRun(
                record=_record_from_row(row),
                job=SchedulerJob.from_dict(json.loads(row["job_json"])),
                observed_at=observed_at,
            )
            for row in rows
        ]

    @staticmethod
    def _postgres_group_busy(
        conn: Any,
        *,
        concurrency_key: str | None,
        excluding_run_id: str | None = None,
    ) -> bool:
        if concurrency_key is None:
            return False
        row = conn.execute(
            """
            SELECT 1 FROM runtime_scheduler_runs
            WHERE concurrency_key = %s AND run_id <> %s
              AND status IN ('claimed', 'running') AND released_at IS NULL
              AND lease_expires_at > CURRENT_TIMESTAMP LIMIT 1
            """,
            (concurrency_key, excluding_run_id or ""),
        ).fetchone()
        return row is not None

    @staticmethod
    def _postgres_group_backlogged(conn: Any, *, concurrency_key: str) -> bool:
        return (
            conn.execute(
                """
                SELECT 1 FROM runtime_scheduler_runs
                WHERE concurrency_key = %s AND status IN ('pending', 'retry_wait')
                LIMIT 1
                """,
                (concurrency_key,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _postgres_insert_run(
        conn: Any,
        *,
        record: SchedulerRunRecord,
        job: SchedulerJob,
        worker_id: str | None,
        claim_id: str | None,
        token: str | None,
        fence_token: int,
        acquired_at: str | None,
        expires_at: str | None,
        released_at: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO runtime_scheduler_runs(
                run_id, occurrence_id, job_name, job_revision, attempt, status,
                scheduled_at, available_at, runtime_run_id, concurrency_key,
                record_json, job_json, worker_id, claim_id, lease_token, fence_token,
                acquired_at, renewed_at, lease_expires_at, released_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                record.run_id,
                record.occurrence_id,
                record.job_name,
                job.revision,
                record.attempt,
                record.status,
                record.scheduled_at,
                record.available_at,
                record.runtime_run_id,
                job.concurrency_key or job.name,
                _json(record.to_dict()),
                _json(job.to_dict()),
                worker_id,
                claim_id,
                token,
                fence_token,
                acquired_at,
                acquired_at,
                expires_at,
                released_at,
            ),
        )

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
    ) -> SchedulerRunClaim | None:
        _validate_claim_input(
            worker_id=worker_id,
            claim_id=claim_id,
            lease_seconds=lease_seconds,
        )
        with self._transaction() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                """
                SELECT * FROM runtime_scheduler_jobs
                WHERE job_name = %s FOR UPDATE
                """,
                (name,),
            ).fetchone()
            if row is None:
                return None
            job = _job_from_row(row)
            now_dt = self._postgres_now(conn)
            now = now_dt.isoformat()
            if (
                not job.enabled
                or job.revision != expected_revision
                or job.next_run_at != expected_next_run_at
                or expected_next_run_at is None
                or _parse(expected_next_run_at) > now_dt
            ):
                return None
            occurrence_id = scheduler_occurrence_id(
                job_name=name,
                job_revision=job.revision,
                scheduled_at=expected_next_run_at,
            )
            run_id = scheduler_run_id(occurrence_id=occurrence_id, attempt=1)
            effective_available = available_at or expected_next_run_at
            group_key = job.concurrency_key or job.name
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"agnoclaw.scheduler.group:{group_key}",),
            )
            busy = self._postgres_group_busy(conn, concurrency_key=group_key)
            backlogged = self._postgres_group_backlogged(
                conn,
                concurrency_key=group_key,
            )
            if backlogged and job.overlap_policy == "queue":
                return None
            advanced = replace(job, next_run_at=next_run_at, updated_at=now)
            updated = conn.execute(
                """
                UPDATE runtime_scheduler_jobs
                SET next_run_at = %s, job_json = %s, updated_at = %s
                WHERE job_name = %s AND revision = %s
                  AND next_run_at = %s AND enabled = TRUE
                """,
                (
                    next_run_at,
                    _json(advanced.to_dict()),
                    now,
                    name,
                    expected_revision,
                    expected_next_run_at,
                ),
            )
            if updated.rowcount != 1:
                return None
            occupied = busy or backlogged
            should_skip = skip_reason is not None or (
                occupied and job.overlap_policy == "skip"
            )
            can_claim = not should_skip and not occupied and _parse(effective_available) <= now_dt
            status = "claimed" if can_claim else "skipped" if should_skip else "pending"
            record = _new_record(
                run_id=run_id,
                occurrence_id=occurrence_id,
                job=job,
                attempt=1,
                status=status,
                scheduled_at=expected_next_run_at,
                available_at=effective_available,
                started_at=now,
                metadata={"job_revision": job.revision},
            )
            if should_skip:
                record = replace(
                    record,
                    finished_at=now,
                    error=skip_reason or "SCHEDULER_OVERLAP_SKIPPED",
                )
            expires_at = (
                (now_dt + timedelta(seconds=lease_seconds)).isoformat() if can_claim else None
            )
            token = (
                _lease_token(
                    run_id=run_id,
                    claim_id=claim_id,
                    worker_id=worker_id,
                    fence_token=1,
                )
                if can_claim
                else None
            )
            self._postgres_insert_run(
                conn,
                record=record,
                job=job,
                worker_id=worker_id if can_claim else None,
                claim_id=claim_id if can_claim else None,
                token=token,
                fence_token=1 if can_claim else 0,
                acquired_at=now if can_claim else None,
                expires_at=expires_at,
                released_at=now if should_skip else None,
            )
            if not can_claim:
                return None
            claimed = conn.execute(
                "SELECT * FROM runtime_scheduler_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            return _claim_from_row(claimed)

    def claim_scheduler_run(
        self,
        run_id: str,
        *,
        worker_id: str,
        claim_id: str,
        lease_seconds: int,
    ) -> SchedulerRunClaim | None:
        _validate_claim_input(
            worker_id=worker_id,
            claim_id=claim_id,
            lease_seconds=lease_seconds,
        )
        with self._transaction() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                """
                SELECT * FROM runtime_scheduler_runs
                WHERE run_id = %s FOR UPDATE
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            record = _record_from_row(row)
            job = SchedulerJob.from_dict(json.loads(row["job_json"]))
            now_dt = self._postgres_now(conn)
            now = now_dt.isoformat()
            ready = record.status in _READY_STATUSES and _parse(
                record.available_at or record.started_at
            ) <= now_dt
            stale = record.status in _ACTIVE_STATUSES and (
                row["released_at"] is not None
                or row["lease_expires_at"] is None
                or row["lease_expires_at"] <= now_dt
            )
            if not (ready or stale):
                return None
            group_key = job.concurrency_key or job.name
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"agnoclaw.scheduler.group:{group_key}",),
            )
            if self._postgres_group_busy(
                conn,
                concurrency_key=group_key,
                excluding_run_id=run_id,
            ):
                if job.overlap_policy == "skip":
                    skipped = replace(
                        record,
                        status="skipped",
                        finished_at=now,
                        error="SCHEDULER_OVERLAP_SKIPPED",
                    )
                    conn.execute(
                        """
                        UPDATE runtime_scheduler_runs
                        SET status = 'skipped', record_json = %s,
                            released_at = CURRENT_TIMESTAMP,
                            lease_expires_at = CURRENT_TIMESTAMP
                        WHERE run_id = %s
                        """,
                        (_json(skipped.to_dict()), run_id),
                    )
                return None
            fence_token = int(row["fence_token"]) + 1
            token = _lease_token(
                run_id=run_id,
                claim_id=claim_id,
                worker_id=worker_id,
                fence_token=fence_token,
            )
            expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
            claimed_record = replace(record, status="claimed")
            conn.execute(
                """
                UPDATE runtime_scheduler_runs SET
                    status = 'claimed', record_json = %s, worker_id = %s,
                    claim_id = %s, lease_token = %s, fence_token = %s,
                    acquired_at = %s, renewed_at = %s, lease_expires_at = %s,
                    released_at = NULL WHERE run_id = %s
                """,
                (
                    _json(claimed_record.to_dict()),
                    worker_id,
                    claim_id,
                    token,
                    fence_token,
                    now,
                    now,
                    expires_at,
                    run_id,
                ),
            )
            claimed = conn.execute(
                "SELECT * FROM runtime_scheduler_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            return _claim_from_row(claimed)

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
    ) -> SchedulerRunClaim | None:
        _validate_claim_input(
            worker_id=worker_id,
            claim_id=claim_id,
            lease_seconds=lease_seconds,
        )
        with self._transaction() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                """
                SELECT job_json FROM runtime_scheduler_jobs
                WHERE job_name = %s FOR UPDATE
                """,
                (job_name,),
            ).fetchone()
            if row is None:
                return None
            job = _job_from_row(row)
            group_key = job.concurrency_key or job.name
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"agnoclaw.scheduler.group:{group_key}",),
            )
            if self._postgres_group_busy(
                conn,
                concurrency_key=group_key,
            ):
                return None
            now_dt = self._postgres_now(conn)
            now = now_dt.isoformat()
            token = _lease_token(
                run_id=run_id,
                claim_id=claim_id,
                worker_id=worker_id,
                fence_token=1,
            )
            expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
            record = _new_record(
                run_id=run_id,
                occurrence_id=occurrence_id,
                job=job,
                attempt=1,
                status="claimed",
                scheduled_at=scheduled_at,
                available_at=scheduled_at,
                started_at=now,
                metadata={"manual": True, "job_revision": job.revision},
            )
            self._postgres_insert_run(
                conn,
                record=record,
                job=job,
                worker_id=worker_id,
                claim_id=claim_id,
                token=token,
                fence_token=1,
                acquired_at=now,
                expires_at=expires_at,
                released_at=None,
            )
            claimed = conn.execute(
                "SELECT * FROM runtime_scheduler_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            return _claim_from_row(claimed)

    @staticmethod
    def _postgres_authoritative_claim(
        conn: Any,
        claim: SchedulerRunClaim,
        *,
        now: datetime,
        allow_released: bool = False,
    ) -> Any:
        row = conn.execute(
            """
            SELECT * FROM runtime_scheduler_runs
            WHERE run_id = %s FOR UPDATE
            """,
            (claim.run_id,),
        ).fetchone()
        if (
            row is None
            or row["worker_id"] != claim.worker_id
            or row["claim_id"] != claim.claim_id
            or row["lease_token"] != claim.lease_token
            or int(row["fence_token"]) != claim.fence_token
            or (row["released_at"] is not None and not allow_released)
            or row["lease_expires_at"] is None
            or (row["lease_expires_at"] <= now and not allow_released)
        ):
            raise SchedulerLeaseLostError(run_id=claim.run_id)
        return row

    def renew_scheduler_claim(
        self,
        claim: SchedulerRunClaim,
        *,
        lease_seconds: int,
    ) -> SchedulerRunClaim:
        _validate_claim_input(
            worker_id=claim.worker_id,
            claim_id=claim.claim_id,
            lease_seconds=lease_seconds,
        )
        with self._transaction() as conn:  # type: ignore[attr-defined]
            now = self._postgres_now(conn)
            self._postgres_authoritative_claim(conn, claim, now=now)
            conn.execute(
                """
                UPDATE runtime_scheduler_runs SET renewed_at = CURRENT_TIMESTAMP,
                    lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second')
                WHERE run_id = %s
                """,
                (lease_seconds, claim.run_id),
            )
            row = conn.execute(
                "SELECT * FROM runtime_scheduler_runs WHERE run_id = %s",
                (claim.run_id,),
            ).fetchone()
            return _claim_from_row(row)

    def bind_scheduler_runtime_run(
        self,
        claim: SchedulerRunClaim,
        *,
        runtime_run_id: str,
    ) -> SchedulerRunClaim:
        if not runtime_run_id.strip() or len(runtime_run_id) > 512:
            raise ValueError("runtime_run_id must contain 1 to 512 characters")
        with self._transaction() as conn:  # type: ignore[attr-defined]
            now = self._postgres_now(conn)
            row = self._postgres_authoritative_claim(conn, claim, now=now)
            record = _record_from_row(row)
            if record.runtime_run_id not in (None, runtime_run_id):
                raise _runtime_binding_conflict(run_id=claim.run_id)
            bound = replace(record, runtime_run_id=runtime_run_id, status="running")
            conn.execute(
                """
                UPDATE runtime_scheduler_runs
                SET status = 'running', runtime_run_id = %s, record_json = %s
                WHERE run_id = %s
                """,
                (runtime_run_id, _json(bound.to_dict()), claim.run_id),
            )
            updated = conn.execute(
                "SELECT * FROM runtime_scheduler_runs WHERE run_id = %s",
                (claim.run_id,),
            ).fetchone()
            return _claim_from_row(updated)

    def finish_scheduler_claim(
        self,
        claim: SchedulerRunClaim,
        *,
        status: str,
        output: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
        retry_delay_seconds: float | None = None,
    ) -> SchedulerRunRecord:
        if status not in _TERMINAL_STATUSES and status != "detached":
            raise ValueError("unsupported scheduler settlement status")
        with self._transaction() as conn:  # type: ignore[attr-defined]
            now_dt = self._postgres_now(conn)
            now = now_dt.isoformat()
            row = self._postgres_authoritative_claim(
                conn,
                claim,
                now=now_dt,
                allow_released=True,
            )
            existing = _record_from_row(row)
            # A committed detach releases the lease like a terminal settlement,
            # so its retry must replay idempotently instead of tripping the
            # expiry check below as a spurious lease loss.
            if row["released_at"] is not None and (
                existing.status in _TERMINAL_STATUSES or existing.status == "detached"
            ):
                if existing.status != status:
                    raise SchedulerLeaseLostError(run_id=claim.run_id)
                return existing
            if row["lease_expires_at"] <= now_dt:
                raise SchedulerLeaseLostError(run_id=claim.run_id)
            merged_metadata = dict(existing.metadata)
            merged_metadata.update(metadata or {})
            settled = replace(
                existing,
                status=status,
                finished_at=None if status == "detached" else now,
                output=output,
                error=error,
                metadata=merged_metadata,
            )
            conn.execute(
                """
                UPDATE runtime_scheduler_runs SET status = %s, record_json = %s,
                    released_at = CURRENT_TIMESTAMP,
                    lease_expires_at = CURRENT_TIMESTAMP WHERE run_id = %s
                """,
                (status, _json(settled.to_dict()), claim.run_id),
            )
            if retry_delay_seconds is not None:
                if not 0 <= retry_delay_seconds <= 604_800:
                    raise ValueError("retry_delay_seconds must be between 0 and 604800")
                job = SchedulerJob.from_dict(json.loads(row["job_json"]))
                attempt = existing.attempt + 1
                retry_id = scheduler_run_id(
                    occurrence_id=str(existing.occurrence_id),
                    attempt=attempt,
                )
                retry_at = (now_dt + timedelta(seconds=retry_delay_seconds)).isoformat()
                retry = _new_record(
                    run_id=retry_id,
                    occurrence_id=str(existing.occurrence_id),
                    job=job,
                    attempt=attempt,
                    status="retry_wait",
                    scheduled_at=str(existing.scheduled_at),
                    available_at=retry_at,
                    started_at=now,
                    metadata={"retry_of": existing.run_id, "job_revision": job.revision},
                )
                self._postgres_insert_run(
                    conn,
                    record=retry,
                    job=job,
                    worker_id=None,
                    claim_id=None,
                    token=None,
                    fence_token=0,
                    acquired_at=None,
                    expires_at=None,
                    released_at=None,
                )
            return settled

    def release_scheduler_claim(self, claim: SchedulerRunClaim) -> bool:
        with self._transaction() as conn:  # type: ignore[attr-defined]
            now_dt = self._postgres_now(conn)
            row = self._postgres_authoritative_claim(
                conn,
                claim,
                now=now_dt,
                allow_released=True,
            )
            if row["released_at"] is not None:
                return False
            if row["lease_expires_at"] <= now_dt:
                raise SchedulerLeaseLostError(run_id=claim.run_id)
            record = _record_from_row(row)
            status = "detached" if record.runtime_run_id else "pending"
            released = replace(record, status=status)
            conn.execute(
                """
                UPDATE runtime_scheduler_runs SET status = %s, record_json = %s,
                    released_at = CURRENT_TIMESTAMP,
                    lease_expires_at = CURRENT_TIMESTAMP WHERE run_id = %s
                """,
                (status, _json(released.to_dict()), claim.run_id),
            )
            return True

    def list_scheduler_runs(
        self,
        *,
        job_name: str | None = None,
        limit: int | None = None,
    ) -> list[SchedulerRunRecord]:
        if limit is not None:
            _validate_limit(limit)
        sql = "SELECT record_json FROM runtime_scheduler_runs"
        parameters: list[Any] = []
        if job_name is not None:
            sql += " WHERE job_name = %s"
            parameters.append(job_name)
        sql += " ORDER BY scheduled_at DESC, attempt DESC, run_id DESC"
        if limit is not None:
            sql += " LIMIT %s"
            parameters.append(limit)
        with self._connection() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(sql, tuple(parameters)).fetchall()
        return [_record_from_row(row) for row in rows]


__all__ = ["PostgresSchedulerStoreMixin", "SQLiteSchedulerStoreMixin"]
