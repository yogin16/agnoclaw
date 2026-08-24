"""Evidence-bound operation reconciliation and lifecycle continuation contracts."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from agnoclaw import AgentHarness, HarnessConfig
from agnoclaw.runtime import (
    OPERATION_RECONCILIATION_EVIDENCE_PURPOSE,
    ArtifactScope,
    EffectClass,
    ExecutionContext,
    LocalArtifactStore,
    OperationIntent,
    OperationKind,
    OperationReconciliationCursorError,
    OperationReconciliationObservation,
    OperationReconciliationStatus,
    OperationReconciliationVerdict,
    OperationSettlement,
    OperationState,
    RunOwner,
    RunSnapshot,
    RunState,
    SQLiteRuntimeStore,
)

OBSERVER_DIGEST = "sha256:" + "1" * 64


class StaticObserver:
    def __init__(self, observation=None, *, error: BaseException | None = None):
        self.observation = observation
        self.error = error
        self.calls = 0

    async def observe(self, _request):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.observation


def _harness(tmp_path, store, artifacts, *, tenant="tenant-a", user="user-a"):
    with patch("agnoclaw.agent.Agent", return_value=MagicMock()):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            return AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path / "workspace",
                config=HarnessConfig(enable_plugins=False),
                include_default_tools=False,
                runtime_store=store,
                artifact_store=artifacts,
                tenant_id=tenant,
                user_id=user,
            )


def _intent(run_id: str) -> OperationIntent:
    return OperationIntent(
        operation_id=f"{run_id}:model:1",
        run_id=run_id,
        attempt_id=f"{run_id}:attempt:1",
        kind=OperationKind.MODEL,
        target="custom:model",
        request_digest="sha256:request",
        effect_class=EffectClass.NON_REPEATABLE,
    )


def _ambiguous(
    store: SQLiteRuntimeStore,
    run_id: str,
    *,
    state: RunState = RunState.WAITING_FOR_RECONCILIATION,
    tenant: str | None = "tenant-a",
    user: str | None = "user-a",
):
    store.create_run(
        RunSnapshot(
            run_id=run_id,
            state=state,
            tenant_id=tenant,
            user_id=user,
            session_id=f"session-{run_id}",
        )
    )
    prepared = store.prepare_operation(_intent(run_id))
    dispatching = store.begin_operation(
        f"{run_id}:model:1",
        mutation_id=f"{run_id}:dispatch",
        expected_revision=prepared.record.revision,
        worker_id="lost-worker",
        fence_token=1,
    )
    return store.settle_operation(
        f"{run_id}:model:1",
        mutation_id=f"{run_id}:unknown",
        expected_revision=dispatching.record.revision,
        fence_token=dispatching.record.fence_token,
        settlement=OperationSettlement(
            state=OperationState.UNKNOWN,
            safe_error={"code": "WIRE_LOST", "retryable": False},
        ),
    ).record


async def _evidence(artifacts, record, value, *, user="user-a"):
    return await artifacts.stage_json(
        value,
        scope=ArtifactScope(
            run_id=record.intent.run_id,
            tenant_id="tenant-a",
            user_id=user,
        ),
        purpose=OPERATION_RECONCILIATION_EVIDENCE_PURPOSE,
    )


def _observation(record, reference, verdict, *, result=False):
    return OperationReconciliationObservation(
        operation_id=record.intent.operation_id,
        expected_revision=record.revision,
        operation_digest=record.digest,
        verdict=verdict,
        evidence_artifacts=(reference,),
        result_reference=reference.artifact_id if result else None,
        provider_request_id="provider-request-1",
    )


@pytest.mark.asyncio
async def test_successful_reconciliation_completes_waiting_run_without_replay(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    record = _ambiguous(store, "run-1")
    reference = await _evidence(artifacts, record, {"content": "observed result"})
    observer = StaticObserver(
        _observation(
            record,
            reference,
            OperationReconciliationVerdict.SUCCEEDED,
            result=True,
        )
    )
    harness = _harness(tmp_path, store, artifacts)

    batch = await harness.reconcile_pending_operations(
        observer,
        observer_digest=OBSERVER_DIGEST,
        minimum_age_seconds=0,
    )

    assert batch.reconciled == 1
    assert batch.items[0].status is OperationReconciliationStatus.RECONCILED
    assert batch.items[0].resulting_state is OperationState.SUCCEEDED
    assert batch.items[0].run_state is RunState.COMPLETED
    assert store.get_terminal("run-1").value == {"content": "observed result"}
    assert [event.event_type for event in store.list_events("run-1")].count(
        "operation.dispatching"
    ) == 1
    reconciliation_event = next(
        event for event in store.list_events("run-1") if event.event_type == "operation.reconciled"
    )
    assert reconciliation_event.payload["observer_digest"] == OBSERVER_DIGEST
    await harness.aclose()


@pytest.mark.asyncio
async def test_effect_absent_becomes_known_failure_and_never_replays(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    record = _ambiguous(store, "run-1")
    reference = await _evidence(artifacts, record, {"effect": "absent"})
    harness = _harness(tmp_path, store, artifacts)

    batch = await harness.reconcile_pending_operations(
        StaticObserver(
            _observation(
                record,
                reference,
                OperationReconciliationVerdict.EFFECT_ABSENT,
            )
        ),
        observer_digest=OBSERVER_DIGEST,
        minimum_age_seconds=0,
    )

    operation = store.get_operation("run-1:model:1")
    assert batch.items[0].run_state is RunState.FAILED
    assert operation.state is OperationState.FAILED
    assert operation.settlement.safe_error["code"] == "OPERATION_RECONCILED_EFFECT_ABSENT"
    assert store.get_run("run-1").state is RunState.FAILED
    await harness.aclose()


def test_store_discovery_is_exact_owner_wait_state_and_keyset_bounded(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    _ambiguous(store, "operation-a")
    _ambiguous(store, "operation-b")
    _ambiguous(store, "other-owner", tenant="tenant-b")
    _ambiguous(store, "still-running", state=RunState.RUNNING)

    first = store.list_reconciliation_operations(
        owner=RunOwner("tenant-a", "user-a"), minimum_age_seconds=0, limit=1
    )
    second = store.list_reconciliation_operations(
        owner=RunOwner("tenant-a", "user-a"),
        after_operation_id=first[0].intent.operation_id,
        minimum_age_seconds=0,
        limit=2,
    )

    assert [item.intent.run_id for item in first] == ["operation-a"]
    assert [item.intent.run_id for item in second] == ["operation-b"]
    with pytest.raises(ValueError):
        store.list_reconciliation_operations(owner=RunOwner(None, None), limit=0)
    with pytest.raises(ValueError):
        store.list_reconciliation_operations(
            owner=RunOwner(None, None), after_operation_id=""
        )


@pytest.mark.asyncio
async def test_cursor_is_owner_bound_and_deferred_items_remain_waiting(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    _ambiguous(store, "run-a")
    _ambiguous(store, "run-b")
    observer = StaticObserver()
    harness = _harness(tmp_path, store, artifacts)

    first = await harness.reconcile_pending_operations(
        observer,
        observer_digest=OBSERVER_DIGEST,
        limit=1,
        minimum_age_seconds=0,
    )
    assert first.items[0].status is OperationReconciliationStatus.DEFERRED
    assert first.next_cursor is not None
    wrong_owner = ExecutionContext.create(
        user_id="user-a",
        session_id=None,
        workspace_id=str(tmp_path / "workspace"),
        tenant_id="tenant-b",
    )
    with pytest.raises(OperationReconciliationCursorError):
        await harness.reconcile_pending_operations(
            observer,
            observer_digest=OBSERVER_DIGEST,
            context=wrong_owner,
            cursor=first.next_cursor,
            minimum_age_seconds=0,
        )
    assert store.get_run("run-a").state is RunState.WAITING_FOR_RECONCILIATION
    await harness.aclose()


@pytest.mark.asyncio
async def test_unbound_or_cross_owner_evidence_is_rejected_without_mutation(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    record = _ambiguous(store, "run-1")
    wrong_scope = await _evidence(artifacts, record, {"effect": "present"}, user="user-b")
    harness = _harness(tmp_path, store, artifacts)

    batch = await harness.reconcile_pending_operations(
        StaticObserver(
            _observation(
                record,
                wrong_scope,
                OperationReconciliationVerdict.SUCCEEDED,
                result=True,
            )
        ),
        observer_digest=OBSERVER_DIGEST,
        minimum_age_seconds=0,
    )

    assert batch.items[0].status is OperationReconciliationStatus.REJECTED
    assert batch.items[0].error_code == (
        "OPERATION_RECONCILIATION_EVIDENCE_SCOPE_MISMATCH"
    )
    assert store.get_operation("run-1:model:1").state is OperationState.UNKNOWN
    await harness.aclose()


@pytest.mark.asyncio
async def test_observer_errors_are_safe_and_concurrent_cas_has_one_winner(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    record = _ambiguous(store, "run-1")
    harness = _harness(tmp_path, store, artifacts)
    failed = await harness.reconcile_pending_operations(
        StaticObserver(error=RuntimeError("secret observer credential")),
        observer_digest=OBSERVER_DIGEST,
        minimum_age_seconds=0,
    )
    assert failed.items[0].error_code == "OPERATION_RECONCILIATION_FAILED"
    assert "secret" not in str(failed.items[0])

    reference = await _evidence(artifacts, record, {"effect": "absent"})
    observation = _observation(
        record,
        reference,
        OperationReconciliationVerdict.EFFECT_ABSENT,
    )
    first, second = await asyncio.gather(
        harness.reconcile_pending_operations(
            StaticObserver(observation),
            observer_digest=OBSERVER_DIGEST,
            minimum_age_seconds=0,
        ),
        harness.reconcile_pending_operations(
            StaticObserver(observation),
            observer_digest=OBSERVER_DIGEST,
            minimum_age_seconds=0,
        ),
    )
    statuses = {first.items[0].status, second.items[0].status}
    assert OperationReconciliationStatus.RECONCILED in statuses
    assert statuses <= {
        OperationReconciliationStatus.RECONCILED,
        OperationReconciliationStatus.STALE,
    }, [first.items[0].error_code, second.items[0].error_code]
    assert sum(
        event.event_type == "operation.reconciled" for event in store.list_events("run-1")
    ) == 1
    await harness.aclose()


@pytest.mark.asyncio
async def test_cancellation_propagates_and_does_not_settle_unobserved_effect(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    _ambiguous(store, "run-1")
    entered = asyncio.Event()

    class BlockingObserver:
        async def observe(self, _request):
            entered.set()
            await asyncio.Event().wait()

    harness = _harness(tmp_path, store, artifacts)
    task = asyncio.create_task(
        harness.reconcile_pending_operations(
            BlockingObserver(),
            observer_digest=OBSERVER_DIGEST,
            minimum_age_seconds=0,
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get_operation("run-1:model:1").state is OperationState.UNKNOWN
    await harness.aclose()


@pytest.mark.asyncio
async def test_cancellation_waits_for_inflight_authoritative_commit(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    record = _ambiguous(store, "run-1")
    reference = await _evidence(artifacts, record, {"effect": "absent"})
    observation = _observation(
        record,
        reference,
        OperationReconciliationVerdict.EFFECT_ABSENT,
    )
    entered = threading.Event()
    release = threading.Event()
    original = store.reconcile_operation

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return original(*args, **kwargs)

    store.reconcile_operation = blocked  # type: ignore[method-assign]
    harness = _harness(tmp_path, store, artifacts)
    task = asyncio.create_task(
        harness.reconcile_pending_operations(
            StaticObserver(observation),
            observer_digest=OBSERVER_DIGEST,
            minimum_age_seconds=0,
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.get_operation("run-1:model:1").state is OperationState.FAILED
    assert store.get_run("run-1").state is RunState.WAITING_FOR_RECONCILIATION
    continued = await harness.reconcile_pending_operations(
        StaticObserver(error=AssertionError("settled operations must not be re-observed")),
        observer_digest=OBSERVER_DIGEST,
        minimum_age_seconds=0,
    )
    assert continued.continued == 1
    assert continued.items[0].status is OperationReconciliationStatus.CONTINUED
    assert continued.items[0].run_state is RunState.FAILED
    await harness.aclose()
