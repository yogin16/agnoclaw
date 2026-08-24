"""Owner authorization and content-free durable run inspection contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from agnoclaw.runtime.approvals import ApprovalRequest
from agnoclaw.runtime.context import ExecutionContext
from agnoclaw.runtime.inspection import (
    RUN_INSPECT_SCOPE,
    RunInspectionAuthorizationError,
    RunRecoveryRecommendation,
    RuntimeRunInspector,
)
from agnoclaw.runtime.lifecycle import LifecycleTransition, RunSnapshot, TransitionKind
from agnoclaw.runtime.operations import EffectClass, OperationIntent, OperationKind
from agnoclaw.runtime.store import RuntimeEventInput, SQLiteRuntimeStore
from agnoclaw.runtime.telemetry import RuntimeTelemetryPolicy


def _context(*, user_id: str = "user-1", scopes=(RUN_INSPECT_SCOPE,)) -> ExecutionContext:
    return ExecutionContext.create(
        user_id=user_id,
        session_id="session-1",
        workspace_id="workspace-1",
        tenant_id="tenant-1",
        scopes=scopes,
    )


def _policy() -> RuntimeTelemetryPolicy:
    return RuntimeTelemetryPolicy(identifier_key=b"inspection-key-material-32-bytes!!")


def _waiting_store(tmp_path) -> SQLiteRuntimeStore:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(
        RunSnapshot(
            run_id="run-private-sentinel",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            metadata={"prompt": "RUN_METADATA_SECRET_SENTINEL"},
        )
    )
    store.apply_transition(
        LifecycleTransition(
            run_id="run-private-sentinel",
            kind=TransitionKind.START,
            transition_id="start-private-sentinel",
        ),
        expected_revision=0,
    )
    requested_at = datetime.now(UTC)
    request = ApprovalRequest(
        request_id="approval-private-sentinel",
        run_id="run-private-sentinel",
        call_id="call-private-sentinel",
        capability_id="shell.secret-capability",
        capability_digest="sha256:capability",
        effect_category="execute",
        argument_digest="sha256:arguments",
        policy_version="policy-v1",
        authority_digest="sha256:authority",
        tenant_id="tenant-1",
        principal_id="user-1",
        session_id="session-1",
        requested_at=requested_at.isoformat(),
        expires_at=(requested_at + timedelta(minutes=10)).isoformat(),
        nonce="approval-nonce-private-sentinel",
    )
    store.apply_transition(
        LifecycleTransition(
            run_id="run-private-sentinel",
            kind=TransitionKind.WAIT_FOR_APPROVAL,
            transition_id="wait-private-sentinel",
            pending_request_id=request.request_id,
            reason_code="REASON_PRIVATE_SENTINEL",
            occurred_at=request.requested_at,
        ),
        expected_revision=1,
        approval_request=request,
    )
    store.prepare_operation(
        OperationIntent(
            operation_id="operation-private-sentinel",
            run_id="run-private-sentinel",
            attempt_id="attempt-private-sentinel",
            kind=OperationKind.CAPABILITY,
            target="bank.transfer.secret-target",
            request_digest="sha256:private-request",
            effect_class=EffectClass.NON_REPEATABLE,
            metadata={"arguments": "OPERATION_METADATA_SECRET_SENTINEL"},
        )
    )
    return store


@pytest.mark.asyncio
async def test_inspection_is_authorized_actionable_bounded_and_content_free(tmp_path) -> None:
    store = _waiting_store(tmp_path)
    report = await RuntimeRunInspector(store=store, policy=_policy()).inspect(
        "run-private-sentinel",
        context=_context(),
    )
    encoded = json.dumps(report.to_dict(), sort_keys=True)

    assert report.state == "waiting_for_approval"
    assert report.recommendation is RunRecoveryRecommendation.REVIEW_APPROVAL
    assert report.pending_approval_count == 1
    assert report.operation_state_counts == (("planned", 1),)
    assert report.operations[0].kind == "capability"
    assert report.operations[0].effect_class == "non_repeatable"
    assert report.last_event_type == "operation.planned"
    assert report.event_count_inspected == 5
    assert report.run_id_hash.startswith("hmac-sha256:default:")
    for sentinel in (
        "run-private-sentinel",
        "approval-private-sentinel",
        "operation-private-sentinel",
        "attempt-private-sentinel",
        "start-private-sentinel",
        "wait-private-sentinel",
        "shell.secret-capability",
        "bank.transfer.secret-target",
        "RUN_METADATA_SECRET_SENTINEL",
        "OPERATION_METADATA_SECRET_SENTINEL",
        "REASON_PRIVATE_SENTINEL",
    ):
        assert sentinel not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [
        _context(scopes=()),
        _context(user_id="other-user"),
    ],
)
async def test_inspection_hides_runs_from_missing_scope_or_wrong_owner(tmp_path, context) -> None:
    store = _waiting_store(tmp_path)

    with pytest.raises(RunInspectionAuthorizationError) as caught:
        await RuntimeRunInspector(store=store, policy=_policy()).inspect(
            "run-private-sentinel",
            context=context,
        )

    assert caught.value.code == "RUN_INSPECTION_NOT_AUTHORIZED"


@pytest.mark.asyncio
async def test_inspection_normalizes_custom_event_names_and_marks_limits(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(
        RunSnapshot(
            run_id="run-1",
            tenant_id="tenant-1",
            user_id="user-1",
        )
    )
    store.append_runtime_event(
        RuntimeEventInput(
            event_id="custom-event",
            run_id="run-1",
            event_type="customer.password.secret",
            occurred_at="2026-08-14T08:00:00+00:00",
            payload={"prompt": "CUSTOM_EVENT_SECRET_SENTINEL"},
        )
    )

    report = await RuntimeRunInspector(store=store, policy=_policy()).inspect(
        "run-1",
        context=_context(),
        event_limit=2,
    )

    assert report.last_event_type == "runtime.unknown"
    assert report.events_at_limit
    assert "CUSTOM_EVENT_SECRET_SENTINEL" not in json.dumps(report.to_dict())


@pytest.mark.asyncio
async def test_inspection_rejects_unsafe_limits(tmp_path) -> None:
    inspector = RuntimeRunInspector(store=_waiting_store(tmp_path), policy=_policy())
    with pytest.raises(ValueError, match="event_limit"):
        await inspector.inspect(
            "run-private-sentinel",
            context=_context(),
            event_limit=True,
        )
