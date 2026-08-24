from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import agnoclaw.runtime.store as store_module
from agnoclaw.runtime.approvals import (
    ApprovalAlreadySettledError,
    ApprovalDecision,
    ApprovalIdempotencyConflictError,
    ApprovalNotFoundError,
    ApprovalRequest,
    ApprovalRevisionConflictError,
    ApprovalState,
)
from agnoclaw.runtime.errors import HarnessError
from agnoclaw.runtime.lifecycle import (
    LifecycleTransition,
    RunSnapshot,
    RunState,
    TransitionKind,
)
from agnoclaw.runtime.security import AuthorizationGrant, GrantScope
from agnoclaw.runtime.store import RunOwner, SQLiteRuntimeStore


def _time(offset_seconds: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).isoformat()


def _request(*, request_id: str = "approval-1", requested_at: str | None = None):
    requested = requested_at or _time()
    expires = (datetime.fromisoformat(requested) + timedelta(minutes=10)).isoformat()
    return ApprovalRequest(
        request_id=request_id,
        run_id="run-1",
        call_id="call-1",
        capability_id="shell@1",
        capability_digest="sha256:capability",
        effect_category="execute",
        argument_digest="sha256:arguments",
        policy_version="policy-v1",
        authority_digest="sha256:authority",
        tenant_id="tenant-1",
        principal_id="user-1",
        session_id="session-1",
        requested_at=requested,
        expires_at=expires,
        nonce=f"nonce-{request_id}",
    )


def _decision(request: ApprovalRequest, **overrides) -> ApprovalDecision:
    values = {
        "decision_id": "decision-1",
        "request_id": request.request_id,
        "request_digest": request.digest,
        "request_nonce": request.nonce,
        "approved": True,
        "issuer": "operator-1",
        "reason_code": "APPROVED_BY_OPERATOR",
        "decided_at": _time(),
    }
    values.update(overrides)
    return ApprovalDecision(**values)


def _grant(request: ApprovalRequest, decision: ApprovalDecision) -> AuthorizationGrant:
    return AuthorizationGrant(
        grant_id="grant-1",
        scope=GrantScope.RUN,
        tenant_id=request.tenant_id,
        principal_id=request.principal_id,
        session_id=request.session_id,
        run_id=request.run_id,
        capability_ids=(request.capability_id,),
        capability_digests=(request.capability_digest,),
        effect_categories=(request.effect_category,),
        argument_digest=request.argument_digest,
        policy_version=request.policy_version,
        authority_digest=request.authority_digest,
        issuer=decision.issuer,
        issued_at=decision.decided_at,
        expires_at=(datetime.fromisoformat(decision.decided_at) + timedelta(minutes=5)).isoformat(),
        nonce="grant-nonce-1",
    )


def _waiting_store(tmp_path, *, fault_injector=None):
    store = SQLiteRuntimeStore(
        tmp_path / "runtime.db",
        fault_injector=fault_injector,
    )
    store.create_run(
        RunSnapshot(
            run_id="run-1",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
        )
    )
    store.apply_transition(
        LifecycleTransition(
            run_id="run-1",
            kind=TransitionKind.START,
            transition_id="start-1",
        ),
        expected_revision=0,
    )
    return store


def _wait(store: SQLiteRuntimeStore, request: ApprovalRequest):
    return store.apply_transition(
        LifecycleTransition(
            run_id="run-1",
            kind=TransitionKind.WAIT_FOR_APPROVAL,
            transition_id="wait-1",
            pending_request_id=request.request_id,
            occurred_at=request.requested_at,
            reason_code="PERMISSION_APPROVAL_REQUIRED",
        ),
        expected_revision=1,
        approval_request=request,
    )


def test_wait_state_and_request_commit_atomically_and_replay(tmp_path) -> None:
    store = _waiting_store(tmp_path)
    request = _request()

    first = _wait(store, request)
    replayed = _wait(store, request)

    assert first.lifecycle.after.state is RunState.WAITING_FOR_APPROVAL
    assert replayed.lifecycle.idempotent
    assert store.get_approval(request.request_id).request.digest == request.digest
    assert [event.event_type for event in store.list_events("run-1")] == [
        "run.created",
        "run.state.changed",
        "run.state.changed",
        "approval.requested",
    ]
    assert [item.sequence for item in store.lease_outbox(owner="exporter")] == [
        1,
        2,
        3,
        4,
    ]
    store.close()


def test_wait_requires_matching_durable_request(tmp_path) -> None:
    store = _waiting_store(tmp_path)
    request = _request()
    transition = LifecycleTransition(
        run_id="run-1",
        kind=TransitionKind.WAIT_FOR_APPROVAL,
        transition_id="wait-1",
        pending_request_id=request.request_id,
    )
    with pytest.raises(ValueError, match="exactly one"):
        store.apply_transition(transition, expected_revision=1)
    assert store.get_run("run-1").state is RunState.RUNNING
    store.close()


def test_raw_response_cannot_bypass_pending_approval(tmp_path) -> None:
    store = _waiting_store(tmp_path)
    request = _request()
    _wait(store, request)

    with pytest.raises(HarnessError) as caught:
        store.apply_transition(
            LifecycleTransition(
                run_id="run-1",
                kind=TransitionKind.RESPOND,
                transition_id="untrusted-response",
                pending_request_id=request.request_id,
                payload={"approved": True},
            ),
            expected_revision=2,
        )

    assert caught.value.code == "APPROVAL_DECISION_REQUIRED"
    assert store.get_run("run-1").state is RunState.WAITING_FOR_APPROVAL
    assert store.get_approval(request.request_id).state is ApprovalState.PENDING
    store.close()


def test_leaving_wait_atomically_cancels_pending_approval(tmp_path) -> None:
    store = _waiting_store(tmp_path)
    request = _request()
    _wait(store, request)

    cancelled = store.apply_transition(
        LifecycleTransition(
            run_id="run-1",
            kind=TransitionKind.REQUEST_CANCEL,
            transition_id="cancel-1",
            reason_code="RUN_CANCELLED_BY_OPERATOR",
        ),
        expected_revision=2,
    )

    assert cancelled.lifecycle.after.state is RunState.CANCELLING
    record = store.get_approval(request.request_id)
    assert record.state is ApprovalState.CANCELLED
    assert record.decision is not None
    assert record.decision.reason_code == "RUN_CANCELLED_BY_OPERATOR"
    assert [event.event_type for event in store.list_events("run-1")][-2:] == [
        "run.state.changed",
        "approval.cancelled",
    ]
    with pytest.raises(HarnessError) as late:
        store.settle_approval(
            _decision(request),
            expected_revision=record.revision,
            grant=_grant(request, _decision(request)),
        )
    assert late.value.code == "APPROVAL_RUN_NOT_WAITING"
    store.close()


def test_request_fault_rolls_back_wait_state_events_and_row(tmp_path) -> None:
    def fail(stage: str) -> None:
        if stage == "approval.request.after_event":
            raise RuntimeError("injected crash")

    store = _waiting_store(tmp_path, fault_injector=fail)
    request = _request()
    with pytest.raises(RuntimeError, match="injected crash"):
        _wait(store, request)

    assert store.get_run("run-1").state is RunState.RUNNING
    assert len(store.list_events("run-1")) == 2
    with pytest.raises(ApprovalNotFoundError):
        store.get_approval(request.request_id)
    store.close()


def test_approval_settlement_persists_exact_grant_event_and_idempotency(tmp_path) -> None:
    store = _waiting_store(tmp_path)
    request = _request()
    _wait(store, request)
    decision = _decision(request)
    grant = _grant(request, decision)

    first = store.settle_approval(decision, expected_revision=0, grant=grant)
    replayed = store.settle_approval(decision, expected_revision=0, grant=grant)

    assert first.record.state is ApprovalState.APPROVED
    assert first.record.grant == grant
    assert replayed.idempotent
    assert replayed.event.event_id == first.event.event_id
    persisted = store.get_approval(request.request_id)
    assert persisted.grant is not None
    assert persisted.grant.digest == grant.digest
    event = store.list_events("run-1")[-1]
    assert event.event_type == "approval.approved"
    assert "operator-1" not in str(event.to_dict())
    store.close()


def test_denial_owner_isolation_and_conflicts(tmp_path) -> None:
    store = _waiting_store(tmp_path)
    request = _request()
    _wait(store, request)
    owner = RunOwner(tenant_id="tenant-1", user_id="user-1")
    wrong_owner = RunOwner(tenant_id="tenant-2", user_id="user-1")
    decision = _decision(
        request,
        approved=False,
        reason_code="DENIED_BY_OPERATOR",
    )

    with pytest.raises(ApprovalNotFoundError):
        store.get_approval(request.request_id, owner=wrong_owner)
    assert (
        store.list_approvals(
            "run-1",
            states=(ApprovalState.PENDING,),
            owner=owner,
        )[0].request.request_id
        == request.request_id
    )
    with pytest.raises(ApprovalRevisionConflictError):
        store.settle_approval(
            decision,
            expected_revision=1,
            owner=owner,
        )

    settled = store.settle_approval(
        decision,
        expected_revision=0,
        owner=owner,
    )
    assert settled.record.state is ApprovalState.DENIED
    with pytest.raises(ApprovalAlreadySettledError):
        store.settle_approval(
            _decision(request, decision_id="decision-2", approved=False),
            expected_revision=1,
            owner=owner,
        )
    with pytest.raises(ApprovalIdempotencyConflictError):
        store.settle_approval(
            _decision(
                request,
                approved=False,
                reason_code="DIFFERENT",
            ),
            expected_revision=0,
            owner=owner,
        )
    store.close()


def test_expiry_is_explicit_and_durable(monkeypatch, tmp_path) -> None:
    store = _waiting_store(tmp_path)
    requested_at = "2026-08-08T08:00:00+00:00"
    request = _request(requested_at=requested_at)
    _wait(store, request)
    expired_at = "2026-08-08T08:10:01+00:00"
    monkeypatch.setattr(store_module, "_now", lambda: expired_at)
    decision = _decision(
        request,
        approved=False,
        issuer="agnoclaw:expiry",
        reason_code="APPROVAL_EXPIRED",
        decided_at=expired_at,
    )

    result = store.expire_approval(decision, expected_revision=0)

    assert result.record.state is ApprovalState.EXPIRED
    assert store.list_events("run-1")[-1].event_type == "approval.expired"
    store.close()
