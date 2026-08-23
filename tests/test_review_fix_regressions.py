"""Regression contracts for the 0.12 pre-release review fixes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agnoclaw.runtime.lifecycle import (
    LifecycleTransition,
    RunSnapshot,
    RunState,
    TransitionKind,
)
from agnoclaw.runtime.reconciliation import wait_for_model_operation_reconciliation
from agnoclaw.runtime.run_handle import HarnessRun
from agnoclaw.runtime.scheduler import RuntimeSchedulerBackend, SchedulerJob
from agnoclaw.runtime.store import (
    RunOwner,
    RuntimeEventInput,
    SQLiteRuntimeStore,
    TerminalRecord,
)

OWNER = RunOwner(tenant_id="tenant-1", user_id="user-1")


def _snapshot(run_id: str = "run-1") -> RunSnapshot:
    return RunSnapshot(
        run_id=run_id,
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        metadata={"request": {"kind": "test"}},
    )


def _running_run(store: SQLiteRuntimeStore, run_id: str = "run-1") -> RunSnapshot:
    store.create_run(_snapshot(run_id))
    queued = store.apply_transition(
        LifecycleTransition(
            run_id=run_id,
            kind=TransitionKind.QUEUE,
            transition_id=f"{run_id}:queue",
        ),
        expected_revision=0,
    ).lifecycle.after
    return store.apply_transition(
        LifecycleTransition(
            run_id=run_id,
            kind=TransitionKind.START,
            transition_id=f"{run_id}:start",
        ),
        expected_revision=queued.revision,
    ).lifecycle.after


def test_second_reconciliation_cycle_parks_the_run_again(tmp_path):
    """A resumed run must be parkable again; the first park must not replay."""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    _running_run(store)

    first = wait_for_model_operation_reconciliation(store, "run-1")
    assert first.state is RunState.WAITING_FOR_RECONCILIATION

    resumed = store.apply_transition(
        LifecycleTransition(
            run_id="run-1",
            kind=TransitionKind.RESUME,
            transition_id="run-1:resume-1",
        ),
        expected_revision=first.revision,
    ).lifecycle.after
    assert resumed.state is RunState.RUNNING

    second = wait_for_model_operation_reconciliation(store, "run-1")
    assert second.state is RunState.WAITING_FOR_RECONCILIATION
    assert second.revision > first.revision
    assert store.get_run("run-1").state is RunState.WAITING_FOR_RECONCILIATION


@pytest.mark.asyncio
async def test_events_replay_drains_past_one_page(tmp_path):
    """No-follow and terminal replays must not truncate at the 100-event page."""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    running = _running_run(store)
    total = 120
    for index in range(total):
        store.append_runtime_event(
            RuntimeEventInput(
                event_id=f"evt_{index}",
                run_id="run-1",
                event_type="model.request.started",
                occurred_at=datetime.now(UTC).isoformat(),
                attempt_id="run-1:attempt:1",
                payload={"index": index},
            ),
            owner=OWNER,
        )

    run = HarnessRun(run_id="run-1", store=store, owner=OWNER)
    no_follow = [event async for event in run.events(follow=False)]
    # created + queue + start lifecycle events precede the appended batch.
    assert len(no_follow) == total + 3

    store.apply_transition(
        LifecycleTransition(
            run_id="run-1",
            kind=TransitionKind.COMPLETE,
            transition_id="run-1:complete",
        ),
        expected_revision=running.revision,
        terminal=TerminalRecord(
            run_id="run-1",
            state=RunState.COMPLETED,
            value={"done": True},
        ),
    )
    terminal = [event async for event in run.events()]
    assert len(terminal) == total + 4


def test_detached_scheduler_settlement_replays_idempotently(tmp_path):
    """A retry after a committed detach must replay, not raise lease loss."""
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    backend = RuntimeSchedulerBackend(store)
    backend.upsert_job(
        SchedulerJob(
            name="detachable",
            schedule="1h",
            prompt="perform bounded work",
            next_run_at=(datetime.now(UTC) - timedelta(seconds=60)).isoformat(),
        )
    )
    claims = backend.claim_due_runs(worker_id="worker-a")
    assert len(claims) == 1
    claim = claims[0]

    detached = store.finish_scheduler_claim(claim, status="detached")
    assert detached.status == "detached"

    replayed = store.finish_scheduler_claim(claim, status="detached")
    assert replayed.status == "detached"
    assert replayed.run_id == detached.run_id
