"""Public deterministic fault/effect-driving contracts."""

from __future__ import annotations

import asyncio

import pytest

from agnoclaw.runtime.gateway import OperationGateway
from agnoclaw.runtime.lifecycle import RunSnapshot
from agnoclaw.runtime.operations import (
    EffectClass,
    OperationIntent,
    OperationKind,
    OperationState,
)
from agnoclaw.runtime.store import OperationNotFoundError, SQLiteRuntimeStore
from agnoclaw.testing import (
    DeterministicEffectDriver,
    EffectBoundary,
    InjectedRuntimeCrash,
    StoreBarrierScript,
    StoreBarrierTimeoutError,
    StoreFaultScript,
)


def _intent(*, effect: EffectClass = EffectClass.NON_REPEATABLE) -> OperationIntent:
    return OperationIntent(
        operation_id="operation-1",
        run_id="run-1",
        attempt_id="attempt-1",
        kind=OperationKind.CAPABILITY,
        target="payments.charge",
        request_digest="sha256:request",
        effect_class=effect,
    )


def _store(tmp_path, *, fault_injector=None) -> SQLiteRuntimeStore:
    store = SQLiteRuntimeStore(
        tmp_path / "runtime.db",
        fault_injector=fault_injector,
    )
    store.create_run(RunSnapshot(run_id="run-1"))
    return store


def test_store_fault_script_crashes_at_exact_occurrence_and_reopen_sees_rollback(tmp_path):
    fault = StoreFaultScript("operation.after_prepare", occurrence=1)
    store = _store(tmp_path, fault_injector=fault)

    with pytest.raises(InjectedRuntimeCrash) as crash:
        store.prepare_operation(_intent())
    store.close()

    reopened = SQLiteRuntimeStore(tmp_path / "runtime.db")
    with pytest.raises(OperationNotFoundError):
        reopened.get_operation("operation-1")
    assert [event.event_type for event in reopened.list_events("run-1")] == ["run.created"]
    assert crash.value.stage == "operation.after_prepare"
    assert fault.hits("operation.after_prepare") == 1
    fault.assert_triggered()
    reopened.close()


@pytest.mark.asyncio
async def test_store_barrier_script_blocks_only_the_exact_occurrence():
    barrier = StoreBarrierScript("operation.after_settle", occurrence=2)

    barrier("operation.after_settle")
    blocked = asyncio.create_task(
        asyncio.to_thread(barrier, "operation.after_settle")
    )
    await asyncio.to_thread(barrier.wait)

    assert barrier.reached
    assert barrier.hits("operation.after_settle") == 2
    barrier.release()
    await blocked
    barrier.assert_reached()


def test_store_barrier_script_times_out_instead_of_hanging():
    barrier = StoreBarrierScript("operation.after_dispatch", timeout=0.01)

    with pytest.raises(StoreBarrierTimeoutError) as failure:
        barrier("operation.after_dispatch")

    assert failure.value.stage == "operation.after_dispatch"
    assert failure.value.occurrence == 1


@pytest.mark.asyncio
async def test_effect_driver_exposes_each_authoritative_external_boundary(tmp_path):
    store = _store(tmp_path)
    driver = DeterministicEffectDriver()
    calls = 0

    async def effect() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"charged": True}

    task = asyncio.create_task(
        OperationGateway(store, worker_id="worker-1").execute(
            _intent(),
            driver.wrap_effect(effect),
            pre_dispatch=driver.pre_dispatch,
        )
    )

    await driver.wait_for(EffectBoundary.PRE_DISPATCH)
    assert store.get_operation("operation-1").state is OperationState.DISPATCHING
    assert calls == 0
    await driver.advance(EffectBoundary.PRE_DISPATCH)

    await driver.wait_for(EffectBoundary.BEFORE_EFFECT)
    assert calls == 0
    await driver.advance(EffectBoundary.BEFORE_EFFECT)

    await driver.wait_for(EffectBoundary.AFTER_EFFECT)
    assert calls == 1
    assert store.get_operation("operation-1").state is OperationState.DISPATCHING
    await driver.advance(EffectBoundary.AFTER_EFFECT)

    completed = await task
    assert completed.value == {"charged": True}
    assert completed.record.state is OperationState.SUCCEEDED
    assert completed.record.settlement is not None
    assert completed.record.settlement.result_slot_id == _intent().result_slot_id
    assert driver.history == (
        EffectBoundary.PRE_DISPATCH,
        EffectBoundary.BEFORE_EFFECT,
        EffectBoundary.AFTER_EFFECT,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancel_at", "expected_state", "expected_calls"),
    [
        (EffectBoundary.PRE_DISPATCH, OperationState.CANCELLED, 0),
        (EffectBoundary.AFTER_EFFECT, OperationState.UNKNOWN, 1),
    ],
)
async def test_effect_driver_proves_cancel_before_vs_after_effect_race(
    tmp_path,
    cancel_at,
    expected_state,
    expected_calls,
):
    store = _store(tmp_path)
    driver = DeterministicEffectDriver()
    calls = 0

    async def effect() -> str:
        nonlocal calls
        calls += 1
        return "charged"

    task = asyncio.create_task(
        OperationGateway(store, worker_id="worker-1").execute(
            _intent(),
            driver.wrap_effect(effect),
            pre_dispatch=driver.pre_dispatch,
        )
    )
    await driver.wait_for(EffectBoundary.PRE_DISPATCH)
    if cancel_at is EffectBoundary.AFTER_EFFECT:
        await driver.advance(EffectBoundary.PRE_DISPATCH)
        await driver.advance(EffectBoundary.BEFORE_EFFECT)
        await driver.wait_for(EffectBoundary.AFTER_EFFECT)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = store.get_operation("operation-1")
    assert record.state is expected_state
    assert calls == expected_calls
