from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agnoclaw.runtime.scheduler import (
    RuntimeSchedulerBackend,
    SchedulerJob,
    SchedulerLeaseLostError,
)
from agnoclaw.runtime.store import SQLiteRuntimeStore


def _due(name: str = "race") -> SchedulerJob:
    return SchedulerJob(
        name=name,
        schedule="1h",
        prompt="race-safe work",
        next_run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )


def test_many_sqlite_workers_claim_exactly_one_occurrence(tmp_path):
    path = tmp_path / "runtime.db"
    bootstrap = RuntimeSchedulerBackend(SQLiteRuntimeStore(path))
    bootstrap.upsert_job(_due())

    def claim(index: int):
        store = SQLiteRuntimeStore(path)
        try:
            return RuntimeSchedulerBackend(store).claim_due_runs(
                worker_id=f"worker-{index}",
                limit=1,
            )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(claim, range(64)))

    claims = [claim for batch in results for claim in batch]
    assert len(claims) == 1
    assert claims[0].fence_token == 1
    assert len(bootstrap.list_runs()) == 1


def test_stale_claim_loses_every_mutation_after_reclaim(tmp_path):
    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    backend.upsert_job(_due())
    stale = backend.claim_due_runs(worker_id="old")[0]
    backend.release_claim(stale)
    current = backend.claim_due_runs(worker_id="new")[0]

    mutations = (
        lambda: backend.renew_claim(stale),
        lambda: backend.bind_runtime_run(stale, runtime_run_id="stale-runtime"),
        lambda: backend.finish_claim(stale, status="completed"),
        lambda: backend.release_claim(stale),
    )
    for mutate in mutations:
        with pytest.raises(SchedulerLeaseLostError):
            mutate()

    assert backend.bind_runtime_run(
        current,
        runtime_run_id="authoritative-runtime",
    ).record.runtime_run_id == "authoritative-runtime"


def test_concurrent_job_update_cannot_mutate_claim_snapshot(tmp_path):
    path = tmp_path / "runtime.db"
    first = RuntimeSchedulerBackend(SQLiteRuntimeStore(path))
    second = RuntimeSchedulerBackend(SQLiteRuntimeStore(path))
    original = first.upsert_job(_due())

    with ThreadPoolExecutor(max_workers=2) as pool:
        claim_future = pool.submit(first.claim_due_runs, worker_id="worker")
        update_future = pool.submit(
            second.upsert_job,
            replace(original, prompt="new behavior"),
        )
        claim_batch = claim_future.result()
        updated = update_future.result()

    if claim_batch:
        assert claim_batch[0].job.prompt == "race-safe work"
        assert claim_batch[0].job.revision == 1
    assert updated.revision == 2
    assert first.get_job("race").prompt == "new behavior"


def test_reopen_preserves_retry_attempt_and_fence_history(tmp_path):
    path = tmp_path / "runtime.db"
    store = SQLiteRuntimeStore(path)
    backend = RuntimeSchedulerBackend(store)
    backend.upsert_job(
        replace(
            _due(),
            max_retries=1,
            retry_delay_seconds=0,
            retry_max_delay_seconds=0,
        )
    )
    first = backend.claim_due_runs(worker_id="worker-1")[0]
    backend.finish_claim(first, status="failed", error="KNOWN")
    store.close()

    reopened = SQLiteRuntimeStore(path)
    retry = RuntimeSchedulerBackend(reopened).claim_due_runs(worker_id="worker-2")[0]

    assert retry.record.attempt == 2
    assert retry.record.occurrence_id == first.record.occurrence_id
    assert retry.fence_token == 1
    assert len(RuntimeSchedulerBackend(reopened).list_runs()) == 2
