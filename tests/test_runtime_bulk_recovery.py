"""Bounded startup discovery and owner-safe bulk recovery contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agnoclaw import AgentHarness, HarnessConfig
from agnoclaw.runtime import (
    ExecutionContext,
    RunSnapshot,
    RunState,
    RuntimeRecoveryCursorError,
    RuntimeRecoveryStatus,
    SQLiteRuntimeStore,
    recover_pending_runs,
)
from agnoclaw.runtime.store import RunOwner


def _harness(tmp_path, store, *, tenant_id="tenant-a", user_id="user-a"):
    with patch("agnoclaw.agent.Agent", return_value=MagicMock()):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            return AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path / "workspace",
                config=HarnessConfig(enable_plugins=False),
                include_default_tools=False,
                runtime_store=store,
                tenant_id=tenant_id,
                user_id=user_id,
            )


def _snapshot(run_id: str, state: RunState, *, tenant="tenant-a", user="user-a"):
    return RunSnapshot(
        run_id=run_id,
        state=state,
        tenant_id=tenant,
        user_id=user,
        session_id=f"session-{run_id}",
    )


def test_store_lists_only_exact_owner_executable_states_with_stable_keyset(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    for run_id, state in (
        ("run-c", RunState.CANCELLING),
        ("run-a", RunState.QUEUED),
        ("run-b", RunState.RUNNING),
        ("run-paused", RunState.PAUSED),
        ("run-input", RunState.WAITING_FOR_INPUT),
        ("run-approval", RunState.WAITING_FOR_APPROVAL),
        ("run-reconcile", RunState.WAITING_FOR_RECONCILIATION),
        ("run-created", RunState.CREATED),
        ("run-complete", RunState.COMPLETED),
    ):
        store.create_run(_snapshot(run_id, state))
    store.create_run(_snapshot("run-other", RunState.RUNNING, tenant="tenant-b"))
    store.create_run(_snapshot("run-null", RunState.RUNNING, tenant=None, user=None))

    assert store.list_recoverable_runs(owner=RunOwner("tenant-a", "user-a")) == []

    first = store.list_recoverable_runs(
        owner=RunOwner("tenant-a", "user-a"), minimum_age_seconds=0, limit=2
    )
    second = store.list_recoverable_runs(
        owner=RunOwner("tenant-a", "user-a"),
        after_run_id=first[-1].run_id,
        minimum_age_seconds=0,
        limit=2,
    )

    assert [item.run_id for item in first] == ["run-a", "run-b"]
    assert [item.run_id for item in second] == ["run-c"]
    assert [
        item.run_id
        for item in store.list_recoverable_runs(
            owner=RunOwner(None, None), minimum_age_seconds=0
        )
    ] == ["run-null"]
    with pytest.raises(TypeError):
        store.list_recoverable_runs(owner=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.list_recoverable_runs(owner=RunOwner(None, None), limit=0)
    with pytest.raises(ValueError):
        store.list_recoverable_runs(owner=RunOwner(None, None), after_run_id="")


def test_recovery_age_uses_store_time_not_caller_snapshot_time(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(
        RunSnapshot(
            run_id="clock-skewed",
            state=RunState.QUEUED,
            updated_at="2000-01-01T00:00:00+00:00",
        )
    )

    assert store.list_recoverable_runs(owner=RunOwner(None, None)) == []
    assert store.list_recoverable_runs(
        owner=RunOwner(None, None), minimum_age_seconds=0
    )


@pytest.mark.asyncio
async def test_facade_recovers_page_skips_live_lease_and_binds_cursor_to_owner(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(_snapshot("run-a", RunState.QUEUED))
    store.create_run(_snapshot("run-b", RunState.RUNNING))
    store.create_run(_snapshot("run-paused", RunState.PAUSED))
    store.create_run(_snapshot("run-other", RunState.RUNNING, tenant="tenant-b"))
    store.acquire_run_lease(
        "run-b",
        worker_id="live-worker",
        claim_id="live-claim",
        owner=RunOwner("tenant-a", "user-a"),
    )
    harness = _harness(tmp_path, store)

    first = await harness.recover_pending_runs(limit=1, minimum_age_seconds=0)
    assert first.recovered == 1
    assert first.lease_busy == first.failed == 0
    assert first.items[0].run_id == "run-a"
    assert first.items[0].state is RunState.FAILED
    assert first.next_cursor is not None

    second = await harness.recover_pending_runs(
        cursor=first.next_cursor, limit=1, minimum_age_seconds=0
    )
    assert second.recovered == second.failed == 0
    assert second.lease_busy == 1
    assert second.items[0].run_id == "run-b"
    assert second.items[0].error_code == "RUNTIME_LEASE_UNAVAILABLE"
    assert second.next_cursor is None
    assert store.get_run("run-paused").state is RunState.PAUSED
    assert store.get_run("run-other").state is RunState.RUNNING

    wrong_owner = ExecutionContext.create(
        user_id="user-a",
        session_id=None,
        workspace_id=str(tmp_path / "workspace"),
        tenant_id="tenant-b",
    )
    with pytest.raises(RuntimeRecoveryCursorError):
        await harness.recover_pending_runs(
            context=wrong_owner,
            cursor=first.next_cursor,
            minimum_age_seconds=0,
        )

    repeated = await harness.recover_pending_runs(limit=5, minimum_age_seconds=0)
    assert [item.run_id for item in repeated.items] == ["run-b"]
    await harness.aclose()


@pytest.mark.asyncio
async def test_coordinator_minimizes_unexpected_failures_and_preserves_order(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(_snapshot("run-a", RunState.RUNNING, tenant=None, user=None))
    store.create_run(_snapshot("run-b", RunState.RUNNING, tenant=None, user=None))

    async def fail(run_id, *, context=None):
        del context
        if run_id == "run-a":
            await asyncio.sleep(0.01)
        raise RuntimeError("secret downstream failure")

    batch = await recover_pending_runs(
        store=store,
        recover_run=fail,
        default_owner=RunOwner(None, None),
        limit=2,
        concurrency=2,
        minimum_age_seconds=0,
    )

    assert [item.run_id for item in batch.items] == ["run-a", "run-b"]
    assert all(item.status is RuntimeRecoveryStatus.FAILED for item in batch.items)
    assert all(item.error_code == "RUN_RECOVERY_FAILED" for item in batch.items)
    assert "secret" not in repr(batch)


@pytest.mark.asyncio
async def test_coordinator_cancellation_propagates_and_bounds_configuration(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(_snapshot("run-a", RunState.RUNNING, tenant=None, user=None))
    entered = asyncio.Event()

    async def block(_run_id, *, context=None):
        del context
        entered.set()
        await asyncio.Event().wait()
        return SimpleNamespace()

    task = asyncio.create_task(
        recover_pending_runs(
            store=store,
            recover_run=block,
            default_owner=RunOwner(None, None),
            minimum_age_seconds=0,
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for kwargs in (
        {"limit": 0},
        {"limit": 101},
        {"concurrency": 0},
        {"concurrency": 33},
        {"minimum_age_seconds": -1},
        {"minimum_age_seconds": 86_401},
    ):
        with pytest.raises(ValueError):
            await recover_pending_runs(
                store=store,
                recover_run=block,
                default_owner=RunOwner(None, None),
                **kwargs,
            )
