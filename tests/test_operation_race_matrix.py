"""Backend-neutral finite crash and cancellation matrix for operation effects."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agnoclaw.runtime import (
    ArtifactNotFoundError,
    ArtifactScope,
    LocalArtifactStore,
    PostgresRuntimeStore,
)
from agnoclaw.runtime.gateway import (
    OperationGateway,
    OperationReconciliationRequiredError,
    OperationTerminalError,
)
from agnoclaw.runtime.lifecycle import RunSnapshot
from agnoclaw.runtime.operations import (
    EffectClass,
    OperationIntent,
    OperationKind,
    OperationReconciliation,
    OperationReconciliationVerdict,
    OperationSettlement,
    OperationState,
    RecoveryAction,
    recovery_action,
)
from agnoclaw.runtime.store import (
    OperationNotFoundError,
    RunOwner,
    RuntimeStore,
    SQLiteRuntimeStore,
)
from agnoclaw.testing import (
    DeterministicEffectDriver,
    EffectBoundary,
    InjectedRuntimeCrash,
    StoreBarrierScript,
    StoreFaultScript,
)

POSTGRES_URL = os.getenv("AGNOCLAW_TEST_POSTGRES_URL")
FaultInjector = Callable[[str], None]


@dataclass
class _Backend:
    name: str
    opener: Callable[[FaultInjector | None], RuntimeStore]
    stores: list[RuntimeStore] = field(default_factory=list)

    def open(self, fault: FaultInjector | None = None) -> RuntimeStore:
        store = self.opener(fault)
        self.stores.append(store)
        return store

    def close(self, store: RuntimeStore) -> None:
        store.close()
        self.stores = [item for item in self.stores if item is not store]

    def close_all(self) -> None:
        while self.stores:
            self.stores.pop().close()


@pytest.fixture(params=("sqlite", "postgres"))
def operation_backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[_Backend]:
    if request.param == "sqlite":
        database = tmp_path / "operation-race-matrix.db"
        backend = _Backend(
            name="sqlite",
            opener=lambda fault: SQLiteRuntimeStore(database, fault_injector=fault),
        )
    else:
        if POSTGRES_URL is None:
            pytest.skip("AGNOCLAW_TEST_POSTGRES_URL is not configured")

        def open_postgres(fault: FaultInjector | None) -> RuntimeStore:
            assert POSTGRES_URL is not None
            return PostgresRuntimeStore(
                POSTGRES_URL,
                min_pool_size=1,
                max_pool_size=4,
                max_waiting=16,
                fault_injector=fault,
            )

        backend = _Backend(name="postgres", opener=open_postgres)
        bootstrap = backend.open()
        assert isinstance(bootstrap, PostgresRuntimeStore)
        with bootstrap._transaction() as conn:  # noqa: SLF001 - isolated conformance DB
            conn.execute(
                """
                TRUNCATE runtime_dead_letter_audit, runtime_runs,
                         runtime_schema_migrations
                RESTART IDENTITY CASCADE
                """
            )
        bootstrap.migrate()
        backend.close(bootstrap)

    try:
        yield backend
    finally:
        backend.close_all()


def _snapshot(run_id: str) -> RunSnapshot:
    return RunSnapshot(
        run_id=run_id,
        tenant_id="tenant-race",
        user_id="user-race",
    )


def _intent(
    operation_id: str,
    run_id: str,
    *,
    effect: EffectClass = EffectClass.NON_REPEATABLE,
) -> OperationIntent:
    return OperationIntent(
        operation_id=operation_id,
        run_id=run_id,
        attempt_id=f"{operation_id}:attempt",
        kind=OperationKind.CAPABILITY,
        target="payments.charge",
        request_digest="sha256:race-matrix",
        effect_class=effect,
        idempotency_key=(
            f"{operation_id}:provider-key"
            if effect is EffectClass.IDEMPOTENT
            else None
        ),
    )


def _settlement(state: OperationState, operation_id: str) -> OperationSettlement:
    if state is OperationState.SUCCEEDED:
        return OperationSettlement(
            state=state,
            result_reference=f"result:{operation_id}",
        )
    return OperationSettlement(
        state=state,
        safe_error={"code": f"MATRIX_{state.value.upper()}"},
    )


def _events(store: RuntimeStore, run_id: str) -> list[str]:
    return [event.event_type for event in store.list_events(run_id)]


def test_transaction_fault_matrix_rolls_back_every_operation_state(
    operation_backend: _Backend,
    tmp_path: Path,
) -> None:
    backend = operation_backend

    run_id = f"{backend.name}-prepare-crash"
    intent = _intent(f"{run_id}:operation", run_id)
    prepare_fault = StoreFaultScript("operation.after_prepare")
    store = backend.open(prepare_fault)
    store.create_run(_snapshot(run_id))
    with pytest.raises(InjectedRuntimeCrash):
        store.prepare_operation(intent)
    prepare_fault.assert_triggered()
    backend.close(store)

    store = backend.open()
    with pytest.raises(OperationNotFoundError):
        store.get_operation(intent.operation_id)
    assert _events(store, run_id) == ["run.created"]
    prepared = store.prepare_operation(intent)
    assert store.prepare_operation(intent).idempotent
    assert prepared.record.state is OperationState.PLANNED
    backend.close(store)

    run_id = f"{backend.name}-dispatch-crash"
    intent = _intent(f"{run_id}:operation", run_id)
    dispatch_fault = StoreFaultScript("operation.after_dispatch")
    store = backend.open(dispatch_fault)
    store.create_run(_snapshot(run_id))
    store.prepare_operation(intent)
    with pytest.raises(InjectedRuntimeCrash):
        store.begin_operation(
            intent.operation_id,
            mutation_id="dispatch",
            expected_revision=0,
            worker_id="worker-race",
            fence_token=1,
        )
    dispatch_fault.assert_triggered()
    backend.close(store)

    store = backend.open()
    assert store.get_operation(intent.operation_id).state is OperationState.PLANNED
    dispatched = store.begin_operation(
        intent.operation_id,
        mutation_id="dispatch",
        expected_revision=0,
        worker_id="worker-race",
        fence_token=1,
    )
    repeated = store.begin_operation(
        intent.operation_id,
        mutation_id="dispatch",
        expected_revision=0,
        worker_id="worker-race",
        fence_token=1,
    )
    assert dispatched.record.state is OperationState.DISPATCHING
    assert repeated.idempotent
    assert _events(store, run_id).count("operation.dispatching") == 1
    backend.close(store)

    for terminal_state in (
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.UNKNOWN,
        OperationState.CANCELLED,
    ):
        run_id = f"{backend.name}-settle-{terminal_state.value}-crash"
        intent = _intent(f"{run_id}:operation", run_id)
        settlement = _settlement(terminal_state, intent.operation_id)
        settle_fault = StoreFaultScript("operation.after_settle")
        store = backend.open(settle_fault)
        store.create_run(_snapshot(run_id))
        store.prepare_operation(intent)
        store.begin_operation(
            intent.operation_id,
            mutation_id="dispatch",
            expected_revision=0,
            worker_id="worker-race",
            fence_token=1,
        )
        with pytest.raises(InjectedRuntimeCrash):
            store.settle_operation(
                intent.operation_id,
                mutation_id="settle",
                expected_revision=1,
                fence_token=1,
                settlement=settlement,
            )
        settle_fault.assert_triggered()
        backend.close(store)

        store = backend.open()
        rolled_back = store.get_operation(intent.operation_id)
        assert rolled_back.state is OperationState.DISPATCHING
        assert rolled_back.revision == 1
        assert rolled_back.settlement is None
        assert "operation.settled" not in _events(store, run_id)
        settled = store.settle_operation(
            intent.operation_id,
            mutation_id="settle",
            expected_revision=1,
            fence_token=1,
            settlement=settlement,
        )
        repeated = store.settle_operation(
            intent.operation_id,
            mutation_id="settle",
            expected_revision=1,
            fence_token=1,
            settlement=settlement,
        )
        assert settled.record.state is terminal_state
        assert repeated.idempotent
        assert _events(store, run_id).count("operation.settled") == 1
        if terminal_state is OperationState.SUCCEEDED:
            assert settled.record.settlement is not None
            assert settled.record.settlement.result_slot_id == intent.result_slot_id
        backend.close(store)

    run_id = f"{backend.name}-recover-crash"
    intent = _intent(
        f"{run_id}:operation",
        run_id,
        effect=EffectClass.IDEMPOTENT,
    )
    recover_fault = StoreFaultScript("operation.after_recover")
    store = backend.open(recover_fault)
    store.create_run(_snapshot(run_id))
    store.prepare_operation(intent)
    dispatch = store.begin_operation(
        intent.operation_id,
        mutation_id="dispatch",
        expected_revision=0,
        worker_id="worker-race",
        fence_token=1,
    )
    with pytest.raises(InjectedRuntimeCrash):
        store.recover_operation(
            intent.operation_id,
            mutation_id="recover",
            expected_revision=dispatch.record.revision,
            next_fence_token=2,
        )
    recover_fault.assert_triggered()
    backend.close(store)

    store = backend.open()
    interrupted = store.get_operation(intent.operation_id)
    assert interrupted.state is OperationState.DISPATCHING
    assert interrupted.fence_token == 1
    recovered = store.recover_operation(
        intent.operation_id,
        mutation_id="recover",
        expected_revision=interrupted.revision,
        next_fence_token=2,
    )
    repeated = store.recover_operation(
        intent.operation_id,
        mutation_id="recover",
        expected_revision=interrupted.revision,
        next_fence_token=2,
    )
    assert recovered.record.state is OperationState.PLANNED
    assert recovered.record.fence_token == 2
    assert repeated.idempotent
    backend.close(store)

    run_id = f"{backend.name}-reconcile-crash"
    intent = _intent(f"{run_id}:operation", run_id)
    reconcile_fault = StoreFaultScript("operation.after_reconcile")
    store = backend.open(reconcile_fault)
    store.create_run(_snapshot(run_id))
    store.prepare_operation(intent)
    dispatch = store.begin_operation(
        intent.operation_id,
        mutation_id="dispatch",
        expected_revision=0,
        worker_id="worker-race",
        fence_token=1,
    )
    unknown = store.settle_operation(
        intent.operation_id,
        mutation_id="unknown",
        expected_revision=dispatch.record.revision,
        fence_token=1,
        settlement=OperationSettlement(
            state=OperationState.UNKNOWN,
            safe_error={"code": "MATRIX_WIRE_LOST"},
        ),
    ).record
    artifact_store = LocalArtifactStore(tmp_path / f"{backend.name}-race-evidence")
    reference = asyncio.run(
        artifact_store.stage_json(
            {"effect": "absent"},
            scope=ArtifactScope(
                run_id=run_id,
                tenant_id="tenant-race",
                user_id="user-race",
            ),
            purpose="operation.reconciliation.evidence",
        )
    )
    reconciliation = OperationReconciliation(
        reconciliation_id=f"{intent.operation_id}:reconciliation",
        operation_id=intent.operation_id,
        expected_revision=unknown.revision,
        operation_digest=unknown.digest,
        verdict=OperationReconciliationVerdict.EFFECT_ABSENT,
        observer_digest="sha256:" + "1" * 64,
        evidence_artifact_ids=(reference.artifact_id,),
    )
    with pytest.raises(InjectedRuntimeCrash):
        store.reconcile_operation(
            intent.operation_id,
            mutation_id=reconciliation.reconciliation_id,
            reconciliation=reconciliation,
            evidence_artifacts=(reference,),
            owner=RunOwner("tenant-race", "user-race"),
        )
    reconcile_fault.assert_triggered()
    backend.close(store)

    store = backend.open()
    rolled_back = store.get_operation(intent.operation_id)
    assert rolled_back.state is OperationState.UNKNOWN
    assert rolled_back.revision == unknown.revision
    with pytest.raises(ArtifactNotFoundError):
        store.get_artifact(reference.artifact_id)
    reconciled = store.reconcile_operation(
        intent.operation_id,
        mutation_id=reconciliation.reconciliation_id,
        reconciliation=reconciliation,
        evidence_artifacts=(reference,),
        owner=RunOwner("tenant-race", "user-race"),
    )
    repeated = store.reconcile_operation(
        intent.operation_id,
        mutation_id=reconciliation.reconciliation_id,
        reconciliation=reconciliation,
        evidence_artifacts=(reference,),
        owner=RunOwner("tenant-race", "user-race"),
    )
    assert reconciled.record.state is OperationState.FAILED
    assert repeated.idempotent
    assert _events(store, run_id).count("operation.reconciled") == 1


@pytest.mark.asyncio
async def test_effect_boundary_matrix_classifies_every_effect_class(
    operation_backend: _Backend,
) -> None:
    backend = operation_backend
    expected_after_dispatch = {
        EffectClass.READ_ONLY: OperationState.DISPATCHING,
        EffectClass.IDEMPOTENT: OperationState.DISPATCHING,
        EffectClass.COMPENSATABLE: OperationState.UNKNOWN,
        EffectClass.NON_REPEATABLE: OperationState.UNKNOWN,
    }

    for effect in EffectClass:
        for boundary in EffectBoundary:
            run_id = f"{backend.name}-{effect.value}-{boundary.value}"
            intent = _intent(f"{run_id}:operation", run_id, effect=effect)
            store = backend.open()
            store.create_run(_snapshot(run_id))
            driver = DeterministicEffectDriver()
            calls = 0

            async def external_effect() -> str:
                nonlocal calls
                calls += 1
                return "charged"

            task = asyncio.create_task(
                OperationGateway(store, worker_id="worker-race").execute(
                    intent,
                    driver.wrap_effect(external_effect),
                    pre_dispatch=driver.pre_dispatch,
                )
            )
            await driver.wait_for(EffectBoundary.PRE_DISPATCH)
            if boundary in {EffectBoundary.BEFORE_EFFECT, EffectBoundary.AFTER_EFFECT}:
                await driver.advance(EffectBoundary.PRE_DISPATCH)
                await driver.wait_for(EffectBoundary.BEFORE_EFFECT)
            if boundary is EffectBoundary.AFTER_EFFECT:
                await driver.advance(EffectBoundary.BEFORE_EFFECT)
                await driver.wait_for(EffectBoundary.AFTER_EFFECT)

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            expected_state = (
                OperationState.CANCELLED
                if boundary is EffectBoundary.PRE_DISPATCH
                else expected_after_dispatch[effect]
            )
            expected_calls = 1 if boundary is EffectBoundary.AFTER_EFFECT else 0
            assert calls == expected_calls
            assert store.get_operation(intent.operation_id).state is expected_state
            backend.close(store)

            reopened = backend.open()
            record = reopened.get_operation(intent.operation_id)
            assert record.state is expected_state
            gateway = OperationGateway(reopened, worker_id="recovery-race")
            if expected_state is OperationState.DISPATCHING:
                recovered = await gateway.recover_interrupted(
                    intent.operation_id,
                    recovery_id=f"{intent.operation_id}:recover",
                )
                assert recovered.state is OperationState.PLANNED
                assert recovery_action(record) is RecoveryAction.RETRY
            elif expected_state is OperationState.UNKNOWN:
                with pytest.raises(OperationReconciliationRequiredError):
                    await gateway.recover_interrupted(
                        intent.operation_id,
                        recovery_id=f"{intent.operation_id}:recover",
                    )
                assert recovery_action(record) is RecoveryAction.RECONCILE
            else:
                with pytest.raises(OperationTerminalError):
                    await gateway.execute(intent, lambda: "must-not-dispatch")
                assert recovery_action(record) is RecoveryAction.DO_NOTHING
            backend.close(reopened)


@pytest.mark.asyncio
async def test_database_commit_order_matrix_is_effect_safe(
    operation_backend: _Backend,
) -> None:
    backend = operation_backend

    for effect in EffectClass:
        run_id = f"{backend.name}-{effect.value}-cancel-after-dispatch-commit"
        intent = _intent(f"{run_id}:operation", run_id, effect=effect)
        dispatch_barrier = StoreBarrierScript("operation.after_dispatch")
        store = backend.open(dispatch_barrier)
        store.create_run(_snapshot(run_id))
        calls = 0

        async def must_not_dispatch() -> str:
            nonlocal calls
            calls += 1
            return "unexpected"

        task = asyncio.create_task(
            OperationGateway(store, worker_id="worker-race").execute(
                intent,
                must_not_dispatch,
            )
        )
        await asyncio.to_thread(dispatch_barrier.wait)
        task.cancel()
        dispatch_barrier.release()
        with pytest.raises(asyncio.CancelledError):
            await task
        dispatch_barrier.assert_reached()
        assert calls == 0
        assert store.get_operation(intent.operation_id).state is OperationState.CANCELLED
        backend.close(store)

        reopened = backend.open()
        assert reopened.get_operation(intent.operation_id).state is OperationState.CANCELLED
        backend.close(reopened)

        run_id = f"{backend.name}-{effect.value}-success-commit-before-cancel"
        intent = _intent(f"{run_id}:operation", run_id, effect=effect)
        settle_barrier = StoreBarrierScript("operation.after_settle")
        store = backend.open(settle_barrier)
        store.create_run(_snapshot(run_id))
        task = asyncio.create_task(
            OperationGateway(store, worker_id="worker-race").execute(
                intent,
                lambda: "charged",
            )
        )
        await asyncio.to_thread(settle_barrier.wait)
        task.cancel()
        settle_barrier.release()
        with pytest.raises(asyncio.CancelledError):
            await task
        settle_barrier.assert_reached()
        committed = store.get_operation(intent.operation_id)
        assert committed.state is OperationState.SUCCEEDED
        assert committed.settlement is not None
        assert committed.settlement.result_slot_id == intent.result_slot_id
        backend.close(store)

        replay_calls = 0
        reopened = backend.open()

        async def must_replay() -> str:
            nonlocal replay_calls
            replay_calls += 1
            return "must-not-run"

        replayed = await OperationGateway(
            reopened,
            worker_id="replay-race",
            result_loader=lambda _reference: "charged",
        ).execute(intent, must_replay)
        assert replayed.replayed
        assert replayed.value == "charged"
        assert replay_calls == 0
        assert _events(reopened, run_id).count("operation.settled") == 1
        backend.close(reopened)
