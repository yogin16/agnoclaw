"""Transactional operation-intent and settlement contracts for SQLite."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from agnoclaw.runtime.lifecycle import RunSnapshot
from agnoclaw.runtime.operations import (
    EffectClass,
    OperationIntent,
    OperationKind,
    OperationSettlement,
    OperationState,
)
from agnoclaw.runtime.store import (
    OperationIdempotencyConflictError,
    OperationNotFoundError,
    OperationRevisionConflictError,
    RunOwner,
    SQLiteRuntimeStore,
)


def _intent(
    operation_id: str = "operation-1",
    *,
    effect: EffectClass = EffectClass.READ_ONLY,
    key: str | None = None,
) -> OperationIntent:
    return OperationIntent(
        operation_id=operation_id,
        run_id="run-1",
        attempt_id="attempt-1",
        kind=OperationKind.CAPABILITY,
        target="example.lookup",
        request_digest="sha256:request",
        effect_class=effect,
        idempotency_key=key,
    )


def _store(tmp_path, *, fault_injector=None):
    store = SQLiteRuntimeStore(
        tmp_path / "runtime.db",
        fault_injector=fault_injector,
    )
    store.create_run(
        RunSnapshot(
            run_id="run-1",
            tenant_id="tenant-1",
            user_id="user-1",
        )
    )
    return store


def test_prepare_dispatch_settle_is_one_ordered_trajectory(tmp_path):
    store = _store(tmp_path)
    prepared = store.prepare_operation(_intent())
    dispatching = store.begin_operation(
        "operation-1",
        mutation_id="dispatch-1",
        expected_revision=0,
        worker_id="worker-1",
        fence_token=1,
    )
    settled = store.settle_operation(
        "operation-1",
        mutation_id="settle-1",
        expected_revision=1,
        fence_token=1,
        settlement=OperationSettlement(
            state=OperationState.SUCCEEDED,
            result_reference="artifact:sha256:result",
            usage={"input_tokens": 10, "output_tokens": 4, "private": "secret"},
            cost={"microusd": 7, "account": "private"},
        ),
    )

    assert prepared.event.sequence == 2
    assert prepared.event.payload["result_slot_id"] == prepared.record.intent.result_slot_id
    assert dispatching.event.sequence == 3
    assert settled.event.sequence == 4
    assert settled.event.payload["result_slot_id"] == prepared.record.intent.result_slot_id
    assert settled.event.payload["usage"] == {"input_tokens": 10, "output_tokens": 4}
    assert settled.event.payload["cost"] == {"microusd": 7}
    assert "secret" not in str(settled.event.payload)
    assert "private" not in str(settled.event.payload)
    assert settled.record.settlement is not None
    assert settled.record.settlement.result_slot_id == prepared.record.intent.result_slot_id
    assert store.get_operation("operation-1").state is OperationState.SUCCEEDED
    assert [event.event_type for event in store.list_events("run-1")] == [
        "run.created",
        "operation.planned",
        "operation.dispatching",
        "operation.settled",
    ]
    assert store.list_recoverable_operations() == []


def test_prepare_and_mutations_are_exactly_idempotent(tmp_path):
    store = _store(tmp_path)
    intent = _intent()
    first = store.prepare_operation(intent)
    repeated = store.prepare_operation(intent)
    dispatch = store.begin_operation(
        "operation-1",
        mutation_id="dispatch-1",
        expected_revision=0,
        worker_id="worker-1",
        fence_token=1,
    )
    repeat_dispatch = store.begin_operation(
        "operation-1",
        mutation_id="dispatch-1",
        expected_revision=0,
        worker_id="worker-1",
        fence_token=1,
    )

    assert repeated.idempotent and repeated.event == first.event
    assert repeat_dispatch.idempotent and repeat_dispatch.event == dispatch.event
    with pytest.raises(OperationIdempotencyConflictError):
        store.prepare_operation(
            OperationIntent(**{**intent.to_dict(), "request_digest": "sha256:different"})
        )
    with pytest.raises(OperationIdempotencyConflictError):
        store.begin_operation(
            "operation-1",
            mutation_id="dispatch-1",
            expected_revision=0,
            worker_id="different-worker",
            fence_token=1,
        )


def test_operation_revision_cas_has_one_winner_across_connections(tmp_path):
    path = tmp_path / "runtime.db"
    first = SQLiteRuntimeStore(path)
    second = SQLiteRuntimeStore(path)
    first.create_run(RunSnapshot(run_id="run-1"))
    first.prepare_operation(_intent())

    def begin(args):
        store, worker = args
        try:
            return store.begin_operation(
                "operation-1",
                mutation_id=f"dispatch-{worker}",
                expected_revision=0,
                worker_id=worker,
                fence_token=1,
            )
        except OperationRevisionConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(begin, [(first, "one"), (second, "two")]))

    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, OperationRevisionConflictError) for item in results) == 1


def test_safe_interrupted_operation_can_be_recovered_with_new_fence(tmp_path):
    store = _store(tmp_path)
    store.prepare_operation(_intent(effect=EffectClass.IDEMPOTENT, key="provider-idempotency-key"))
    dispatching = store.begin_operation(
        "operation-1",
        mutation_id="dispatch-1",
        expected_revision=0,
        worker_id="worker-1",
        fence_token=1,
    )

    recovered = store.recover_operation(
        "operation-1",
        mutation_id="recover-1",
        expected_revision=dispatching.record.revision,
        next_fence_token=2,
    )

    assert recovered.record.state is OperationState.PLANNED
    assert recovered.record.fence_token == 2
    assert store.list_recoverable_operations() == [recovered.record]


def test_operation_fault_rolls_back_record_event_mutation_and_outbox(tmp_path):
    def fail(stage: str) -> None:
        if stage == "operation.after_dispatch":
            raise RuntimeError("injected operation crash")

    store = _store(tmp_path, fault_injector=fail)
    store.prepare_operation(_intent())

    with pytest.raises(RuntimeError, match="injected operation crash"):
        store.begin_operation(
            "operation-1",
            mutation_id="dispatch-1",
            expected_revision=0,
            worker_id="worker-1",
            fence_token=1,
        )

    assert store.get_operation("operation-1").state is OperationState.PLANNED
    assert [event.event_type for event in store.list_events("run-1")] == [
        "run.created",
        "operation.planned",
    ]
    assert [item.sequence for item in store.lease_outbox(owner="exporter")] == [1, 2]


def test_operation_owner_is_hidden_at_store_boundary(tmp_path):
    store = _store(tmp_path)
    store.prepare_operation(_intent())

    assert (
        store.get_operation(
            "operation-1",
            owner=RunOwner(tenant_id="tenant-1", user_id="user-1"),
        ).intent.operation_id
        == "operation-1"
    )
    with pytest.raises(OperationNotFoundError):
        store.get_operation(
            "operation-1",
            owner=RunOwner(tenant_id="tenant-2", user_id="user-1"),
        )
