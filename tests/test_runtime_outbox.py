"""Durable runtime outbox delivery contracts."""

from __future__ import annotations

import asyncio

import pytest

from agnoclaw import (
    DEAD_LETTER_INSPECT_SCOPE,
    DEAD_LETTER_REQUEUE_SCOPE,
    ExecutionContext,
    OutboxDeadLetterConflictError,
    RuntimeDeadLetterAdmin,
    RuntimeOutboxConfig,
    RuntimeOutboxWorker,
)
from agnoclaw.runtime.lifecycle import RunSnapshot
from agnoclaw.runtime.store import RunOwner, RuntimeEventInput, SQLiteRuntimeStore


class RecordingExporter:
    def __init__(self) -> None:
        self.batches = []
        self.fail = False

    async def export(self, events) -> None:
        self.batches.append(events)
        if self.fail:
            raise RuntimeError("private exporter diagnostic")


def _worker(tmp_path, exporter, **config):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(RunSnapshot(run_id="run-1", tenant_id="tenant", user_id="user"))
    store.append_runtime_event(
        RuntimeEventInput(
            event_id="evt_trajectory",
            run_id="run-1",
            event_type="trajectory.run.started",
            occurred_at="2026-08-10T00:00:00+00:00",
            payload={"projection_schema_version": "1.0"},
        )
    )
    worker = RuntimeOutboxWorker(
        store=store,
        exporter=exporter,
        config=RuntimeOutboxConfig(
            owner="test-exporter",
            retry_base_seconds=0,
            retry_max_seconds=0,
            **config,
        ),
    )
    return store, worker


@pytest.mark.asyncio
async def test_worker_exports_ordered_batch_then_acknowledges(tmp_path) -> None:
    exporter = RecordingExporter()
    store, worker = _worker(tmp_path, exporter)

    result = await worker.run_once()

    assert result.succeeded
    assert (result.leased, result.delivered, result.deferred) == (2, 2, 0)
    assert [event.event_type for event in exporter.batches[0]] == [
        "run.created",
        "trajectory.run.started",
    ]
    assert await worker.run_once() == type(result)(leased=0, delivered=0, deferred=0)
    assert store.lease_outbox(owner="independent-exporter") == []


@pytest.mark.asyncio
async def test_worker_defers_whole_batch_without_leaking_failure(tmp_path) -> None:
    exporter = RecordingExporter()
    exporter.fail = True
    _store, worker = _worker(tmp_path, exporter)

    failed = await worker.run_once()

    assert failed.failure_code == "RUNTIME_OUTBOX_EXPORT_FAILED"
    assert (failed.leased, failed.delivered, failed.deferred) == (2, 0, 2)
    assert "private exporter diagnostic" not in repr(failed)
    exporter.fail = False
    recovered = await worker.run_once()
    assert recovered.succeeded
    assert len(exporter.batches) == 2
    assert exporter.batches[0][0].event_id == exporter.batches[1][0].event_id


@pytest.mark.asyncio
async def test_worker_times_out_and_releases_batch_for_retry(tmp_path) -> None:
    class SlowExporter:
        async def export(self, events) -> None:
            await asyncio.sleep(1)

    store, worker = _worker(
        tmp_path,
        SlowExporter(),
        lease_seconds=1,
        delivery_timeout_seconds=0.01,
    )

    result = await worker.run_once()

    assert result.failure_code == "RUNTIME_OUTBOX_EXPORT_TIMEOUT"
    assert len(store.lease_outbox(owner="retry-exporter")) == 2


@pytest.mark.asyncio
async def test_worker_cancellation_releases_live_batch(tmp_path) -> None:
    started = asyncio.Event()

    class BlockingExporter:
        async def export(self, events) -> None:
            started.set()
            await asyncio.Event().wait()

    store, worker = _worker(tmp_path, BlockingExporter())
    task = asyncio.create_task(worker.run_once())
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(store.lease_outbox(owner="retry-exporter")) == 2


@pytest.mark.asyncio
async def test_worker_isolates_dead_letters_and_exact_requeue_recovers(tmp_path) -> None:
    exporter = RecordingExporter()
    exporter.fail = True
    store, worker = _worker(tmp_path, exporter, max_attempts=1)

    quarantined = await worker.run_once()

    assert quarantined.failure_code == "RUNTIME_OUTBOX_EXPORT_DEAD_LETTERED"
    assert (quarantined.dead_lettered, quarantined.deferred) == (1, 1)
    context = ExecutionContext.create(
        user_id="user",
        session_id="session",
        workspace_id="workspace",
        tenant_id="tenant",
        scopes=(DEAD_LETTER_INSPECT_SCOPE, DEAD_LETTER_REQUEUE_SCOPE),
    )
    admin = RuntimeDeadLetterAdmin(store)
    page = await admin.inspect(context=context, reason_code="test_inspection")
    assert len(page.items) == 1
    dead_letter = page.items[0]
    assert dead_letter.reason_code == "export_failed"
    assert "private exporter diagnostic" not in repr(dead_letter)
    await admin.requeue(
        dead_letter,
        context=context,
        reason_code="operator_retry",
        mutation_id="test-requeue-1",
    )
    with pytest.raises(OutboxDeadLetterConflictError):
        await admin.requeue(
            dead_letter,
            context=context,
            reason_code="operator_retry",
            mutation_id="test-requeue-2",
        )

    exporter.fail = False
    isolated_success = await worker.run_once()
    remaining_success = await worker.run_once()

    assert (isolated_success.delivered, isolated_success.deferred) == (1, 1)
    assert remaining_success.succeeded
    assert not (
        await admin.inspect(
            context=context,
            reason_code="verify_empty",
            owner=RunOwner(tenant_id="tenant", user_id="user"),
        )
    ).items


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner", ""),
        ("batch_size", 0),
        ("lease_seconds", 0),
        ("delivery_timeout_seconds", 60),
        ("retry_base_seconds", -1),
        ("retry_max_seconds", 86_401),
        ("max_attempts", 0),
        ("idle_poll_seconds", 0),
    ],
)
def test_outbox_config_rejects_unsafe_bounds(field: str, value) -> None:
    values = {
        "owner": "exporter",
        "batch_size": 50,
        "lease_seconds": 60,
        "delivery_timeout_seconds": 30,
        "retry_base_seconds": 1,
        "retry_max_seconds": 300,
        "max_attempts": 20,
        "idle_poll_seconds": 1,
    }
    values[field] = value

    with pytest.raises(ValueError):
        RuntimeOutboxConfig(**values)
