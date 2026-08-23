"""End-to-end OperationGateway intent/dispatch/settlement contracts."""

from __future__ import annotations

import asyncio
import threading

import pytest

from agnoclaw.runtime.artifacts import ArtifactCorruptError, LocalArtifactStore
from agnoclaw.runtime.gateway import (
    OperationGateway,
    OperationInFlightError,
    OperationReconciliationRequiredError,
    OperationResultUnavailableError,
    OperationTerminalError,
)
from agnoclaw.runtime.lifecycle import RunSnapshot
from agnoclaw.runtime.operations import (
    EffectClass,
    OperationIntent,
    OperationKind,
    OperationSettlementEvidence,
    OperationState,
)
from agnoclaw.runtime.store import SQLiteRuntimeStore


def _intent(
    *,
    operation_id: str = "operation-1",
    effect: EffectClass = EffectClass.READ_ONLY,
    key: str | None = None,
    timeout: float | None = None,
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
        timeout_seconds=timeout,
    )


def _store(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(RunSnapshot(run_id="run-1"))
    return store


@pytest.mark.asyncio
async def test_gateway_persists_before_dispatch_and_replays_cached_result(tmp_path):
    store = _store(tmp_path)
    gateway = OperationGateway(store, worker_id="worker-1")
    calls = 0

    async def dispatch():
        nonlocal calls
        calls += 1
        events = store.list_events("run-1")
        assert events[-1].event_type == "operation.dispatching"
        return {"answer": 42}

    first = await gateway.execute(_intent(), dispatch)
    replayed = await gateway.execute(_intent(), dispatch)

    assert calls == 1
    assert first.record.state is OperationState.SUCCEEDED
    assert replayed.replayed and replayed.value == {"answer": 42}
    assert [event.event_type for event in store.list_events("run-1")] == [
        "run.created",
        "operation.planned",
        "operation.dispatching",
        "operation.settled",
    ]


@pytest.mark.asyncio
async def test_gateway_commits_content_minimized_settlement_evidence_once(tmp_path):
    store = _store(tmp_path)
    gateway = OperationGateway(store, worker_id="worker-1")
    evidence_calls = 0

    def evidence(value):
        nonlocal evidence_calls
        evidence_calls += 1
        assert value == {"answer": 42}
        return OperationSettlementEvidence(
            provider_request_id="provider-request-7",
            usage={"reported": True, "total_tokens": 37},
            cost={"reported": True, "currency": "USD", "microusd": 9},
        )

    first = await gateway.execute(
        _intent(),
        lambda: {"answer": 42},
        settlement_evidence=evidence,
    )
    replayed = await gateway.execute(
        _intent(),
        lambda: {"answer": "must-not-run"},
        settlement_evidence=evidence,
    )

    settlement = first.record.settlement
    assert settlement is not None
    assert settlement.provider_request_id == "provider-request-7"
    assert settlement.usage["total_tokens"] == 37
    assert settlement.cost["microusd"] == 9
    assert replayed.replayed is True
    assert evidence_calls == 1


@pytest.mark.asyncio
async def test_evidence_extractor_failure_cannot_strand_successful_effect(tmp_path):
    store = _store(tmp_path)
    gateway = OperationGateway(store, worker_id="worker-1")

    def broken_evidence(_value):
        raise RuntimeError("provider-secret-must-not-persist")

    completed = await gateway.execute(
        _intent(effect=EffectClass.NON_REPEATABLE),
        lambda: "external-success",
        settlement_evidence=broken_evidence,
    )

    settlement = completed.record.settlement
    assert completed.record.state is OperationState.SUCCEEDED
    assert settlement is not None
    assert settlement.usage == {
        "source": "settlement_evidence",
        "reported": False,
        "extraction_error": True,
    }
    assert settlement.cost["extraction_error"] is True
    assert "provider-secret-must-not-persist" not in str(completed.record.to_dict())


@pytest.mark.asyncio
async def test_restarted_gateway_requires_or_uses_result_loader(tmp_path):
    store = _store(tmp_path)
    references: dict[str, object] = {}

    async def reference(value):
        references["artifact:result"] = value
        return "artifact:result"

    first = OperationGateway(
        store,
        worker_id="worker-1",
        result_reference_factory=reference,
    )
    await first.execute(_intent(), lambda: "value")

    with pytest.raises(OperationResultUnavailableError):
        await OperationGateway(store, worker_id="worker-2").execute(
            _intent(),
            lambda: "must-not-run",
        )
    restored = await OperationGateway(
        store,
        worker_id="worker-3",
        result_loader=lambda ref: references[ref],
    ).execute(_intent(), lambda: "must-not-run")
    assert restored.replayed and restored.value == "value"


@pytest.mark.asyncio
async def test_restarted_gateway_loads_atomically_committed_result_artifact(tmp_path):
    store = _store(tmp_path)
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    first = OperationGateway(
        store,
        worker_id="worker-1",
        artifact_store=artifacts,
        result_serializer=lambda value: {"content": value},
        result_cache_size=0,
    )

    completed = await first.execute(_intent(), lambda: "durable-value")
    restarted = OperationGateway(
        store,
        worker_id="worker-2",
        artifact_store=artifacts,
        result_cache_size=0,
    )
    replayed = await restarted.execute(_intent(), lambda: "must-not-run")

    assert completed.record.state is OperationState.SUCCEEDED
    assert completed.record.settlement is not None
    assert completed.record.settlement.result_reference is not None
    reference = store.get_artifact(completed.record.settlement.result_reference)
    assert reference.metadata["result_slot_id"] == completed.record.intent.result_slot_id
    assert replayed.replayed and replayed.value == {"content": "durable-value"}
    assert [event.event_type for event in store.list_events("run-1")] == [
        "run.created",
        "operation.planned",
        "operation.dispatching",
        "artifact.committed",
        "operation.settled",
    ]


@pytest.mark.asyncio
async def test_gateway_commits_an_explicit_internal_artifact_purpose(tmp_path):
    store = _store(tmp_path)
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    gateway = OperationGateway(
        store,
        worker_id="worker-1",
        artifact_store=artifacts,
        artifact_purpose="run_request_checkpoint",
        result_cache_size=0,
    )

    completed = await gateway.execute(_intent(), lambda: {"checkpoint": True})
    reference = store.get_artifact(completed.record.settlement.result_reference)

    assert reference.purpose == "run_request_checkpoint"
    assert await artifacts.load_json(reference) == {"checkpoint": True}


@pytest.mark.asyncio
async def test_restarted_gateway_reports_missing_committed_bytes_as_corruption(tmp_path):
    store = _store(tmp_path)
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    first = OperationGateway(
        store,
        worker_id="worker-1",
        artifact_store=artifacts,
        result_cache_size=0,
    )
    completed = await first.execute(_intent(), lambda: {"content": "value"})
    reference = store.get_artifact(completed.record.settlement.result_reference)
    (artifacts._objects / reference.storage_key).unlink()

    with pytest.raises(ArtifactCorruptError):
        await OperationGateway(
            store,
            worker_id="worker-2",
            artifact_store=artifacts,
            result_cache_size=0,
        ).execute(_intent(), lambda: "must-not-run")


@pytest.mark.asyncio
async def test_gateway_persists_only_safe_failure_not_raw_exception(tmp_path):
    store = _store(tmp_path)
    gateway = OperationGateway(store, worker_id="worker-1")

    def fail():
        raise RuntimeError("secret-token-must-not-persist")

    with pytest.raises(OperationTerminalError) as failure:
        await gateway.execute(_intent(), fail)

    assert failure.value.record.state is OperationState.FAILED
    serialized = str(store.get_operation("operation-1").to_dict())
    assert "secret-token-must-not-persist" not in serialized
    assert "RuntimeError" in serialized


@pytest.mark.asyncio
async def test_non_repeatable_failure_is_unknown_and_requires_reconciliation(tmp_path):
    store = _store(tmp_path)
    gateway = OperationGateway(store, worker_id="worker-1")

    with pytest.raises(OperationTerminalError) as failure:
        await gateway.execute(
            _intent(effect=EffectClass.NON_REPEATABLE),
            lambda: (_ for _ in ()).throw(RuntimeError("wire dropped")),
        )

    assert failure.value.record.state is OperationState.UNKNOWN
    with pytest.raises(OperationReconciliationRequiredError):
        await gateway.recover_interrupted("operation-1", recovery_id="recover-1")


@pytest.mark.asyncio
async def test_pre_dispatch_failure_is_known_before_non_repeatable_effect(tmp_path):
    store = _store(tmp_path)
    gateway = OperationGateway(store, worker_id="worker-1")
    dispatch_calls = 0

    async def dispatch():
        nonlocal dispatch_calls
        dispatch_calls += 1
        return "must-not-run"

    def reject():
        raise RuntimeError("lease was lost")

    with pytest.raises(OperationTerminalError) as failure:
        await gateway.execute(
            _intent(effect=EffectClass.NON_REPEATABLE),
            dispatch,
            pre_dispatch=reject,
        )

    assert dispatch_calls == 0
    assert failure.value.record.state is OperationState.FAILED
    assert failure.value.safe_error["code"] == "OPERATION_PRE_DISPATCH_FAILED"


@pytest.mark.asyncio
async def test_pre_dispatch_cancellation_never_enters_external_effect(tmp_path):
    store = _store(tmp_path)
    gateway = OperationGateway(store, worker_id="worker-1")
    entered = asyncio.Event()
    dispatch_calls = 0

    async def blocked_gate():
        entered.set()
        await asyncio.Event().wait()

    async def dispatch():
        nonlocal dispatch_calls
        dispatch_calls += 1

    task = asyncio.create_task(
        gateway.execute(
            _intent(effect=EffectClass.NON_REPEATABLE),
            dispatch,
            pre_dispatch=blocked_gate,
        )
    )
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert dispatch_calls == 0
    assert store.get_operation("operation-1").state is OperationState.CANCELLED


@pytest.mark.asyncio
async def test_waiter_cancellation_leaves_safe_effect_fenced_for_explicit_recovery(tmp_path):
    store = _store(tmp_path)
    gateway = OperationGateway(store, worker_id="worker-1")
    entered = asyncio.Event()

    async def blocked():
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(gateway.execute(_intent(), blocked))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    interrupted = store.get_operation("operation-1")
    assert interrupted.state is OperationState.DISPATCHING
    recovered = await gateway.recover_interrupted(
        "operation-1",
        recovery_id="recover-1",
    )
    assert recovered.state is OperationState.PLANNED


@pytest.mark.asyncio
async def test_non_repeatable_cancellation_settles_unknown(tmp_path):
    store = _store(tmp_path)
    gateway = OperationGateway(store, worker_id="worker-1")
    entered = asyncio.Event()

    async def blocked():
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        gateway.execute(
            _intent(effect=EffectClass.NON_REPEATABLE),
            blocked,
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.get_operation("operation-1").state is OperationState.UNKNOWN


@pytest.mark.asyncio
async def test_timeout_is_failed_for_read_only_and_unknown_for_non_repeatable(tmp_path):
    first_store = _store(tmp_path / "first")
    first = OperationGateway(first_store, worker_id="worker-1")
    with pytest.raises(OperationTerminalError) as safe_timeout:
        await first.execute(
            _intent(timeout=0.01),
            lambda: asyncio.sleep(60),
        )
    assert safe_timeout.value.record.state is OperationState.FAILED

    second_store = _store(tmp_path / "second")
    second = OperationGateway(second_store, worker_id="worker-1")
    with pytest.raises(OperationTerminalError) as ambiguous_timeout:
        await second.execute(
            _intent(effect=EffectClass.NON_REPEATABLE, timeout=0.01),
            lambda: asyncio.sleep(60),
        )
    assert ambiguous_timeout.value.record.state is OperationState.UNKNOWN


@pytest.mark.asyncio
async def test_gateway_never_steals_existing_dispatch_implicitly(tmp_path):
    store = _store(tmp_path)
    intent = _intent()
    store.prepare_operation(intent)
    store.begin_operation(
        intent.operation_id,
        mutation_id="dispatch-owner",
        expected_revision=0,
        worker_id="owner",
        fence_token=1,
    )

    with pytest.raises(OperationInFlightError):
        await OperationGateway(store, worker_id="contender").execute(
            intent,
            lambda: "must-not-run",
        )


@pytest.mark.asyncio
async def test_cancellation_during_fence_commit_never_reaches_external_dispatch(tmp_path):
    store = _store(tmp_path)
    gateway = OperationGateway(store, worker_id="worker-1")
    entered = threading.Event()
    release = threading.Event()
    original_begin = store.begin_operation
    dispatch_calls = 0

    def blocked_begin(*args, **kwargs):
        entered.set()
        release.wait(timeout=2)
        return original_begin(*args, **kwargs)

    store.begin_operation = blocked_begin  # type: ignore[method-assign]

    async def dispatch():
        nonlocal dispatch_calls
        dispatch_calls += 1
        return "must-not-run"

    task = asyncio.create_task(gateway.execute(_intent(), dispatch))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert dispatch_calls == 0
    assert store.get_operation("operation-1").state is OperationState.CANCELLED


@pytest.mark.asyncio
async def test_cancellation_after_external_success_is_durably_unknown(tmp_path):
    store = _store(tmp_path)
    entered = asyncio.Event()

    async def blocked_reference(_value):
        entered.set()
        await asyncio.Event().wait()

    gateway = OperationGateway(
        store,
        worker_id="worker-1",
        result_reference_factory=blocked_reference,
    )
    task = asyncio.create_task(
        gateway.execute(
            _intent(effect=EffectClass.NON_REPEATABLE),
            lambda: "external-result",
        )
    )
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    record = store.get_operation("operation-1")
    assert record.state is OperationState.UNKNOWN
    assert record.settlement is not None
    assert record.settlement.safe_error["code"] == ("OPERATION_CANCELLED_AFTER_EXTERNAL_SUCCESS")


@pytest.mark.asyncio
async def test_settled_terminal_conflict_surfaces_terminal_not_inflight(tmp_path):
    """A revision conflict against a settled FAILED operation is terminal, not retryable."""
    store = _store(tmp_path)
    intent = _intent()
    stale = store.prepare_operation(intent)
    assert stale.record.state is OperationState.PLANNED

    def _boom():
        raise RuntimeError("dispatch failed")

    with pytest.raises(OperationTerminalError):
        await OperationGateway(store, worker_id="worker-1").execute(intent, _boom)
    assert store.get_operation(intent.operation_id).state is OperationState.FAILED

    class _StalePrepareStore:
        def __init__(self, inner, decision):
            self._inner = inner
            self._decision = decision

        def prepare_operation(self, _intent):
            return self._decision

        def __getattr__(self, name):
            return getattr(self._inner, name)

    # A distinct attempt/fence keeps the mutation id fresh so the contender
    # reaches the revision CAS (and its conflict handler) rather than the
    # mutation-identity idempotency check.
    from dataclasses import replace as _replace

    doctored = _replace(
        stale,
        record=_replace(stale.record, dispatch_attempt=5, fence_token=7),
    )
    contender = OperationGateway(
        _StalePrepareStore(store, doctored),
        worker_id="contender",
    )
    with pytest.raises(OperationTerminalError) as caught:
        await contender.execute(intent, lambda: "must-not-run")
    assert caught.value.record.state is OperationState.FAILED
