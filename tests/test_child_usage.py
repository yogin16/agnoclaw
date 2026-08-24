"""Agno usage extraction and declared-child budget enforcement contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from agno.metrics import RunMetrics
from agno.run.agent import RunOutput

from agnoclaw.runtime.children import (
    ChildRunBudget,
    ChildRunContractError,
    ChildRunSpec,
)
from agnoclaw.runtime.lifecycle import (
    LifecycleTransition,
    RunRevisionConflictError,
    RunSnapshot,
    RunState,
    TransitionKind,
)
from agnoclaw.runtime.operations import OperationSettlement, OperationState
from agnoclaw.runtime.security import thaw_data
from agnoclaw.runtime.store import RunOwner, SQLiteRuntimeStore
from agnoclaw.runtime.usage import (
    agno_result_settlement_evidence,
    assess_child_budget,
    record_child_budget_assessment,
    supervise_child_deadline,
)


def _spec(*, budget: ChildRunBudget | None = None) -> ChildRunSpec:
    return ChildRunSpec(
        child_run_id="run-child",
        parent_run_id="run-parent",
        root_run_id="run-parent",
        depth=1,
        delegation_id="delegation-1",
        purpose_code="research",
        budget=budget or ChildRunBudget(),
    )


def _settlement(*, tokens: int | None, cost_microusd: int | None) -> OperationSettlement:
    return OperationSettlement(
        state=OperationState.SUCCEEDED,
        result_reference="result:child",
        usage={
            "reported": tokens is not None,
            **({"total_tokens": tokens} if tokens is not None else {}),
        },
        cost={
            "reported": cost_microusd is not None,
            **({"microusd": cost_microusd} if cost_microusd is not None else {}),
        },
        settled_at="2026-08-11T00:00:00+00:00",
    )


def _child_store(tmp_path, spec: ChildRunSpec) -> tuple[SQLiteRuntimeStore, RunOwner]:
    owner = RunOwner("tenant-1", "user-1")
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(
        RunSnapshot(
            run_id=spec.parent_run_id,
            state=RunState.RUNNING,
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
        )
    )
    store.create_run(
        RunSnapshot(
            run_id=spec.child_run_id,
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            session_id=f"child:{spec.child_run_id}",
            parent_run_id=spec.parent_run_id,
            root_run_id=spec.root_run_id,
            child_depth=spec.depth,
        ),
        idempotency_scope="tenant-1:user-1",
        idempotency_key=f"child:{spec.parent_run_id}:{spec.delegation_id}",
        request_digest="sha256:" + "1" * 64,
        child_spec=spec,
    )
    return store, owner


def test_agno_metrics_extract_stable_tokens_cost_duration_and_request_id():
    result = RunOutput(
        content="done",
        metrics=RunMetrics(
            input_tokens=10,
            output_tokens=5,
            total_tokens=0,
            cache_read_tokens=3,
            reasoning_tokens=2,
            cost=0.0000011,
            duration=0.1234,
        ),
        model_provider_data={
            "request_id": "provider-request-1",
            "private_trace": "must-not-persist",
        },
    )

    evidence = agno_result_settlement_evidence(result)
    usage = thaw_data(evidence.usage)
    cost = thaw_data(evidence.cost)

    assert evidence.provider_request_id == "provider-request-1"
    assert usage["total_tokens"] == 15
    assert usage["cache_read_tokens"] == 3
    assert usage["reasoning_tokens"] == 2
    assert usage["duration_milliseconds"] == 124
    assert usage["reported"] is True
    assert cost == {
        "currency": "USD",
        "source": "agno.run.metrics",
        "reported": True,
        "microusd": 2,
    }
    assert "private_trace" not in str(evidence)


def test_missing_or_invalid_agno_metrics_are_explicitly_unverified():
    evidence = agno_result_settlement_evidence(
        SimpleNamespace(
            metrics=RunMetrics(),
            model_provider_data={"request_id": "x" * 513, "secret": "hidden"},
        )
    )

    assert evidence.provider_request_id is None
    assert thaw_data(evidence.usage)["reported"] is False
    assert thaw_data(evidence.cost)["reported"] is False
    assessment = assess_child_budget(
        ChildRunBudget(max_tokens=10, max_cost_microusd=10),
        _settlement(tokens=None, cost_microusd=None),
    )
    assert assessment.exceeded is False
    assert assessment.fully_verified is False
    assert assessment.unverified_dimensions == ("tokens", "cost")


def test_budget_assessment_identifies_each_reported_excess():
    assessment = assess_child_budget(
        ChildRunBudget(max_tokens=100, max_cost_microusd=10),
        _settlement(tokens=101, cost_microusd=11),
    )

    assert assessment.exceeded is True
    assert assessment.fully_verified is True
    assert assessment.exceeded_dimensions == ("tokens", "cost")
    assert thaw_data(assessment.measured) == {
        "total_tokens": 101,
        "cost_microusd": 11,
    }


def test_budget_observation_event_is_content_minimized_and_idempotent(tmp_path):
    spec = _spec(budget=ChildRunBudget(max_tokens=100, max_cost_microusd=10))
    store, owner = _child_store(tmp_path, spec)
    settlement = _settlement(tokens=90, cost_microusd=9)

    first = record_child_budget_assessment(
        store=store,
        owner=owner,
        spec=spec,
        settlement=settlement,
    )
    repeated = record_child_budget_assessment(
        store=store,
        owner=owner,
        spec=spec,
        settlement=settlement,
    )

    observations = [
        event
        for event in store.list_events(spec.child_run_id, owner=owner)
        if event.event_type == "run.child.budget.observed"
    ]
    assert first == repeated
    assert len(observations) == 1
    assert observations[0].payload["measured"] == {
        "total_tokens": 90,
        "cost_microusd": 9,
    }
    assert observations[0].payload["fully_verified"] is True
    assert set(observations[0].payload) == {
        "child_spec_digest",
        "limits",
        "measured",
        "exceeded_dimensions",
        "unverified_dimensions",
        "fully_verified",
    }


@pytest.mark.asyncio
async def test_deadline_supervisor_requests_authoritative_cancel(monkeypatch, tmp_path):
    spec = _spec(budget=ChildRunBudget(timeout_seconds=1))
    store, _owner = _child_store(tmp_path, spec)
    child = store.get_run(spec.child_run_id)
    child = store.apply_transition(
        LifecycleTransition(
            run_id=spec.child_run_id,
            kind=TransitionKind.QUEUE,
            transition_id="queue-child",
        ),
        expected_revision=child.revision,
    ).lifecycle.after
    store.apply_transition(
        LifecycleTransition(
            run_id=spec.child_run_id,
            kind=TransitionKind.START,
            transition_id="start-child",
        ),
        expected_revision=child.revision,
    )

    async def immediate_sleep(_seconds):
        return None

    monkeypatch.setattr("agnoclaw.runtime.usage.asyncio.sleep", immediate_sleep)
    worker = asyncio.create_task(asyncio.Event().wait())
    failures: dict[str, BaseException] = {}

    await supervise_child_deadline(
        store=store,
        run_id=spec.child_run_id,
        spec=spec,
        worker_task=worker,
        failures=failures,
    )

    with pytest.raises(asyncio.CancelledError):
        await worker
    assert failures == {}
    assert store.get_run(spec.child_run_id).state is RunState.CANCELLING
    timeout_events = [
        event
        for event in store.list_events(spec.child_run_id)
        if event.payload.get("reason_code") == "CHILD_TIMEOUT_EXCEEDED"
    ]
    assert len(timeout_events) == 1


@pytest.mark.asyncio
async def test_deadline_conflict_exhaustion_fails_closed(monkeypatch):
    spec = _spec(budget=ChildRunBudget(timeout_seconds=1))

    class ConflictingStore:
        def get_run(self, _run_id):
            return RunSnapshot(run_id=spec.child_run_id, state=RunState.RUNNING)

        def apply_transition(self, _transition, *, expected_revision):
            raise RunRevisionConflictError(
                run_id=spec.child_run_id,
                expected=expected_revision,
                actual=expected_revision + 1,
            )

    async def immediate_sleep(_seconds):
        return None

    monkeypatch.setattr("agnoclaw.runtime.usage.asyncio.sleep", immediate_sleep)
    worker = asyncio.create_task(asyncio.Event().wait())
    failures: dict[str, BaseException] = {}

    await supervise_child_deadline(
        store=ConflictingStore(),  # type: ignore[arg-type]
        run_id=spec.child_run_id,
        spec=spec,
        worker_task=worker,
        failures=failures,
    )

    with pytest.raises(asyncio.CancelledError):
        await worker
    failure = failures[spec.child_run_id]
    assert isinstance(failure, ChildRunContractError)
    assert failure.code == "CHILD_TIMEOUT_ENFORCEMENT_CONFLICT"
