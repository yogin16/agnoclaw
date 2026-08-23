from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agnoclaw.runtime.scheduler import (
    RuntimeSchedulerBackend,
    SchedulerConfigurationError,
    SchedulerJob,
    SchedulerLeaseLostError,
    SchedulerMisfirePolicy,
    SchedulerOverlapPolicy,
    next_schedule_time,
    scheduler_idempotency_key,
)
from agnoclaw.runtime.store import RUNTIME_SCHEMA_VERSION, SQLiteRuntimeStore


def _past(seconds: int = 60) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _job(name: str = "daily", **changes) -> SchedulerJob:
    return SchedulerJob(
        name=name,
        schedule="1h",
        prompt="perform bounded work",
        next_run_at=_past(),
        **changes,
    )


def test_schema_v12_and_due_claim_are_durable(tmp_path):
    path = tmp_path / "runtime.db"
    store = SQLiteRuntimeStore(path)
    backend = RuntimeSchedulerBackend(store)
    job = backend.upsert_job(_job())

    claims = backend.claim_due_runs(worker_id="worker-a")

    assert store.schema_version == RUNTIME_SCHEMA_VERSION == 12
    assert len(claims) == 1
    claim = claims[0]
    assert claim.job.revision == job.revision == 1
    assert claim.record.occurrence_id is not None
    assert claim.record.attempt == 1
    assert claim.fence_token == 1
    assert scheduler_idempotency_key(claim.record).startswith("scheduler:schedocc_")
    assert backend.get_job("daily").next_run_at == next_schedule_time(
        job,
        after=str(job.next_run_at),
    )

    store.close()
    reopened = SQLiteRuntimeStore(path)
    record = RuntimeSchedulerBackend(reopened).list_runs(job_name="daily")[0]
    assert record.run_id == claim.run_id
    assert record.status == "claimed"


def test_two_workers_never_claim_one_occurrence(tmp_path):
    path = tmp_path / "runtime.db"
    first_store = SQLiteRuntimeStore(path)
    second_store = SQLiteRuntimeStore(path)
    first = RuntimeSchedulerBackend(first_store)
    second = RuntimeSchedulerBackend(second_store)
    first.upsert_job(_job())

    claim = first.claim_due_runs(worker_id="worker-a")[0]

    assert second.claim_due_runs(worker_id="worker-b") == []
    assert second.list_runs()[0].run_id == claim.run_id


def test_reclaimed_attempt_keeps_identity_and_advances_fence(tmp_path):
    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    backend.upsert_job(_job())
    first = backend.claim_due_runs(worker_id="worker-a")[0]
    assert backend.release_claim(first) is True

    second = backend.claim_due_runs(worker_id="worker-b")[0]

    assert second.run_id == first.run_id
    assert second.record.occurrence_id == first.record.occurrence_id
    assert scheduler_idempotency_key(second.record) == scheduler_idempotency_key(first.record)
    assert second.fence_token == first.fence_token + 1
    with pytest.raises(SchedulerLeaseLostError):
        backend.bind_runtime_run(first, runtime_run_id="run_stale")


def test_runtime_binding_survives_detach_and_reclaim(tmp_path):
    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    backend.upsert_job(_job())
    first = backend.claim_due_runs(worker_id="worker-a")[0]
    first = backend.bind_runtime_run(first, runtime_run_id="run_authoritative")
    assert backend.release_claim(first) is True

    detached = backend.list_runs()[0]
    assert detached.status == "detached"
    assert detached.runtime_run_id == "run_authoritative"

    second = backend.claim_due_runs(worker_id="worker-b")[0]
    assert second.run_id == first.run_id
    assert second.record.runtime_run_id == "run_authoritative"
    rebound = backend.bind_runtime_run(second, runtime_run_id="run_authoritative")
    assert rebound.record.status == "running"


def test_known_failure_creates_exactly_one_retry_attempt(tmp_path):
    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    backend.upsert_job(_job(max_retries=1, retry_delay_seconds=0, retry_max_delay_seconds=0))
    first = backend.claim_due_runs(worker_id="worker-a")[0]

    failed = backend.finish_claim(first, status="failed", error="KNOWN_FAILURE")
    second = backend.claim_due_runs(worker_id="worker-b")[0]

    assert failed.status == "failed"
    assert second.record.attempt == 2
    assert second.record.occurrence_id == first.record.occurrence_id
    assert second.run_id != first.run_id
    backend.finish_claim(second, status="failed", error="KNOWN_FAILURE")
    assert len(backend.list_runs()) == 2
    assert backend.claim_due_runs(worker_id="worker-c") == []


def test_misfire_skip_records_terminal_history_without_execution(tmp_path):
    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    backend.upsert_job(
        _job(
            misfire_policy=SchedulerMisfirePolicy.SKIP.value,
            misfire_grace_seconds=0,
        )
    )

    assert backend.claim_due_runs(worker_id="worker-a") == []
    record = backend.list_runs()[0]
    assert record.status == "skipped"
    assert record.error == "SCHEDULER_MISFIRE_SKIPPED"


def test_fire_once_misfire_coalesces_backlog(tmp_path):
    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    backend.upsert_job(
        SchedulerJob(
            name="frequent",
            schedule="1s",
            prompt="coalesce me",
            next_run_at=_past(3_600),
            misfire_grace_seconds=0,
        )
    )

    first = backend.claim_due_runs(worker_id="worker-a")[0]
    advanced = backend.get_job("frequent")

    assert advanced is not None
    assert datetime.fromisoformat(advanced.next_run_at) > datetime.now(UTC)
    backend.finish_claim(first, status="completed")
    assert backend.claim_due_runs(worker_id="worker-b") == []


def test_fire_once_coalesces_within_grace_backlog_too(tmp_path):
    """Multi-interval lateness inside the grace window fires once, not as a burst."""
    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    backend.upsert_job(
        SchedulerJob(
            name="frequent",
            schedule="1m",
            prompt="coalesce me",
            next_run_at=_past(240),
            misfire_grace_seconds=300,
        )
    )

    first = backend.claim_due_runs(worker_id="worker-a")[0]
    advanced = backend.get_job("frequent")

    assert advanced is not None
    assert datetime.fromisoformat(advanced.next_run_at) > datetime.now(UTC)
    backend.finish_claim(first, status="completed")
    assert backend.claim_due_runs(worker_id="worker-b") == []


def test_concurrency_group_queues_second_job_until_first_settles(tmp_path):
    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    backend.upsert_job(_job("a", concurrency_key="serial"))
    backend.upsert_job(_job("b", concurrency_key="serial"))

    claims = backend.claim_due_runs(worker_id="worker-a", limit=2)
    assert len(claims) == 1
    pending = [record for record in backend.list_runs() if record.status == "pending"]
    assert len(pending) == 1

    backend.finish_claim(claims[0], status="completed", output="ok")
    second = backend.claim_due_runs(worker_id="worker-b")
    assert len(second) == 1
    assert second[0].record.job_name != claims[0].record.job_name


def test_queue_policy_bounds_one_backlog_per_concurrency_group(tmp_path):
    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    backend.upsert_job(_job("a", concurrency_key="serial"))
    backend.upsert_job(_job("b", concurrency_key="serial"))
    backend.upsert_job(_job("c", concurrency_key="serial"))

    assert len(backend.claim_due_runs(worker_id="worker-a", limit=3)) == 1

    records = backend.list_runs()
    assert len([record for record in records if record.status == "claimed"]) == 1
    assert len([record for record in records if record.status == "pending"]) == 1
    assert len(records) == 2
    still_due = [
        job.name
        for job in backend.list_jobs()
        if datetime.fromisoformat(job.next_run_at) <= datetime.now(UTC)
    ]
    assert len(still_due) == 1


def test_future_jitter_alone_bounds_one_backlog_per_concurrency_group(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agnoclaw.runtime.scheduler.scheduler_jitter_seconds",
        lambda _job, *, occurrence_id: 3_600,
    )
    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    backend.upsert_job(_job("a", concurrency_key="serial", jitter_seconds=3_600))
    backend.upsert_job(_job("b", concurrency_key="serial", jitter_seconds=3_600))

    assert backend.claim_due_runs(worker_id="worker-a", limit=2) == []

    records = backend.list_runs()
    assert len(records) == 1
    assert records[0].status == "pending"
    still_due = [
        job.name
        for job in backend.list_jobs()
        if datetime.fromisoformat(job.next_run_at) <= datetime.now(UTC)
    ]
    assert len(still_due) == 1


def test_overlap_skip_is_terminal(tmp_path):
    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    backend.upsert_job(_job("a", concurrency_key="serial"))
    backend.upsert_job(
        _job(
            "b",
            concurrency_key="serial",
            overlap_policy=SchedulerOverlapPolicy.SKIP.value,
        )
    )

    assert len(backend.claim_due_runs(worker_id="worker-a", limit=2)) == 1
    skipped = [record for record in backend.list_runs() if record.status == "skipped"]
    assert len(skipped) == 1
    assert skipped[0].error == "SCHEDULER_OVERLAP_SKIPPED"


def test_job_updates_revision_without_mutating_claim_snapshot(tmp_path):
    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    first_job = backend.upsert_job(_job())
    claim = backend.claim_due_runs(worker_id="worker-a")[0]
    updated = backend.upsert_job(replace(first_job, prompt="new behavior", next_run_at=_past()))

    assert updated.revision == first_job.revision + 1
    assert claim.job.prompt == "perform bounded work"
    assert backend.get_job("daily").prompt == "new behavior"
    assert claim.record.metadata["job_revision"] == first_job.revision


def test_enable_disable_resets_nominal_clock(tmp_path):
    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    backend.upsert_job(_job())

    disabled = backend.set_job_enabled("daily", False)
    assert disabled is not None and not disabled.enabled and disabled.next_run_at is None
    assert backend.claim_due_runs(worker_id="worker-a") == []

    enabled = backend.set_job_enabled("daily", True)
    assert enabled is not None and enabled.enabled and enabled.next_run_at is not None
    assert datetime.fromisoformat(enabled.next_run_at) > datetime.now(UTC)


def test_manual_claim_is_durable_and_does_not_advance_schedule(tmp_path):
    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    stored = backend.upsert_job(_job())

    claim = backend.claim_now("daily", worker_id="operator")

    assert claim is not None
    assert claim.record.metadata["manual"] is True
    assert claim.record.occurrence_id.startswith("schedmanual_")
    assert backend.get_job("daily").next_run_at == stored.next_run_at


def test_invalid_timezone_and_limits_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="IANA timezone"):
        _job(timezone="Mars/Olympus")

    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    with pytest.raises(ValueError, match="limit"):
        backend.claim_due_runs(worker_id="worker", limit=0)
    with pytest.raises(ValueError, match="lease_seconds"):
        backend.claim_due_runs(worker_id="worker", lease_seconds=0)


def test_cron_preserves_local_hour_across_dst_and_skips_nonexistent_time():
    daily = SchedulerJob(
        name="new-york-nine",
        schedule="0 9 * * *",
        prompt="run at nine",
        timezone="America/New_York",
    )
    nonexistent = replace(daily, name="new-york-two-thirty", schedule="30 2 * * *")

    assert next_schedule_time(daily, after="2026-03-07T14:00:00+00:00") == (
        "2026-03-08T13:00:00+00:00"
    )
    assert next_schedule_time(nonexistent, after="2026-03-07T07:31:00+00:00") == (
        "2026-03-09T06:30:00+00:00"
    )

    ambiguous = replace(daily, name="new-york-one-thirty", schedule="30 1 * * *")
    assert next_schedule_time(ambiguous, after="2026-10-31T05:31:00+00:00") == (
        "2026-11-01T05:30:00+00:00"
    )
    assert next_schedule_time(ambiguous, after="2026-11-01T05:31:00+00:00") == (
        "2026-11-02T06:30:00+00:00"
    )


def test_durable_jobs_reject_mutable_model_overrides(tmp_path):
    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))

    with pytest.raises(SchedulerConfigurationError, match="immutable model"):
        backend.upsert_job(_job(model_id="another-model"))


def test_metadata_is_bounded_and_output_preview_marks_truncation(tmp_path):
    with pytest.raises(ValueError, match="JSON serializable"):
        _job(metadata={"bad": object()})
    with pytest.raises(ValueError, match="65536"):
        _job(metadata={"large": "x" * 65_536})

    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    backend.upsert_job(_job())
    claim = backend.claim_due_runs(worker_id="worker-a")[0]
    record = backend.finish_claim(claim, status="completed", output="x" * 5_000)

    assert len(record.output or "") == 4_096
    assert record.metadata["output_preview_truncated"] is True
    assert record.metadata["output_character_count"] == 5_000
