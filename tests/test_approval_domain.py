from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agnoclaw.runtime.approvals import (
    ApprovalBindingError,
    ApprovalDecision,
    ApprovalExpiredError,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalState,
    expire_approval,
    settle_approval,
)
from agnoclaw.runtime.security import AuthorizationGrant, GrantScope


def _request(**overrides) -> ApprovalRequest:
    values = {
        "request_id": "approval-1",
        "run_id": "run-1",
        "call_id": "call-1",
        "capability_id": "shell@1",
        "capability_digest": "sha256:capability",
        "effect_category": "execute",
        "argument_digest": "sha256:arguments",
        "policy_version": "policy-v1",
        "authority_digest": "sha256:authority",
        "tenant_id": "tenant-1",
        "principal_id": "user-1",
        "session_id": "session-1",
        "requested_at": "2026-08-08T08:00:00Z",
        "expires_at": "2026-08-08T09:00:00Z",
        "nonce": "request-nonce-1",
    }
    values.update(overrides)
    return ApprovalRequest(**values)


def _decision(request: ApprovalRequest, **overrides) -> ApprovalDecision:
    values = {
        "decision_id": "decision-1",
        "request_id": request.request_id,
        "request_digest": request.digest,
        "request_nonce": request.nonce,
        "approved": True,
        "issuer": "approver-1",
        "reason_code": "APPROVED_BY_OPERATOR",
        "grant_scope": GrantScope.RUN,
        "decided_at": "2026-08-08T08:05:00Z",
    }
    values.update(overrides)
    return ApprovalDecision(**values)


def _grant(request: ApprovalRequest, decision: ApprovalDecision, **overrides):
    values = {
        "grant_id": "grant-1",
        "scope": GrantScope.RUN,
        "tenant_id": request.tenant_id,
        "principal_id": request.principal_id,
        "session_id": request.session_id,
        "run_id": request.run_id,
        "capability_ids": (request.capability_id,),
        "capability_digests": (request.capability_digest,),
        "effect_categories": (request.effect_category,),
        "argument_digest": request.argument_digest,
        "policy_version": request.policy_version,
        "authority_digest": request.authority_digest,
        "issuer": decision.issuer,
        "issued_at": decision.decided_at,
        "expires_at": "2026-08-08T08:30:00Z",
        "nonce": "grant-nonce-1",
    }
    values.update(overrides)
    return AuthorizationGrant(**values)


def test_approval_round_trip_and_exact_positive_grant() -> None:
    request = _request()
    decision = _decision(request)
    grant = _grant(request, decision)

    record = settle_approval(
        ApprovalRecord(request=request, updated_at=request.requested_at),
        decision,
        grant=grant,
        occurred_at="2026-08-08T08:05:01Z",
    )

    assert record.state is ApprovalState.APPROVED
    assert record.revision == 1
    assert ApprovalRecord.from_dict(record.to_dict()) == record
    assert ApprovalRequest.from_dict(request.to_dict()).digest == request.digest
    with pytest.raises(FrozenInstanceError):
        record.state = ApprovalState.DENIED


def test_negative_decision_never_accepts_a_grant() -> None:
    request = _request()
    decision = _decision(request, approved=False, reason_code="DENIED_BY_OPERATOR")
    record = settle_approval(
        ApprovalRecord(request=request, updated_at=request.requested_at),
        decision,
        occurred_at="2026-08-08T08:05:01Z",
    )
    assert record.state is ApprovalState.DENIED
    assert record.grant is None

    with pytest.raises(ApprovalBindingError) as caught:
        settle_approval(
            ApprovalRecord(request=request, updated_at=request.requested_at),
            decision,
            grant=_grant(request, decision),
            occurred_at="2026-08-08T08:05:01Z",
        )
    assert caught.value.details == {"request_id": "approval-1", "field": "grant"}


@pytest.mark.parametrize(
    ("decision_override", "grant_override", "field_name"),
    [
        ({"request_digest": "sha256:wrong"}, {}, "request_digest"),
        ({"request_nonce": "wrong"}, {}, "request_nonce"),
        ({}, {"run_id": "run-2"}, "run_id"),
        ({}, {"argument_digest": "sha256:other"}, "argument_digest"),
        ({}, {"policy_version": "policy-v2"}, "policy_version"),
        ({}, {"authority_digest": "sha256:other"}, "authority_digest"),
        ({}, {"capability_digests": ("sha256:other",)}, "capability_digests"),
    ],
)
def test_decision_and_grant_replay_bindings_fail_closed(
    decision_override,
    grant_override,
    field_name,
) -> None:
    request = _request()
    decision = _decision(request, **decision_override)
    grant = _grant(request, decision, **grant_override)
    with pytest.raises(ApprovalBindingError) as caught:
        settle_approval(
            ApprovalRecord(request=request, updated_at=request.requested_at),
            decision,
            grant=grant,
            occurred_at="2026-08-08T08:05:01Z",
        )
    assert caught.value.details["field"] == field_name


def test_expired_approval_cannot_mint_a_grant_and_can_be_recorded() -> None:
    request = _request()
    approved = _decision(request, decided_at="2026-08-08T09:00:00Z")
    with pytest.raises(ApprovalExpiredError):
        settle_approval(
            ApprovalRecord(request=request, updated_at=request.requested_at),
            approved,
            grant=_grant(
                request,
                approved,
                issued_at="2026-08-08T08:59:00Z",
                expires_at="2026-08-08T09:30:00Z",
            ),
            occurred_at="2026-08-08T09:00:00Z",
        )

    expiry = _decision(
        request,
        approved=False,
        issuer="agnoclaw:expiry",
        reason_code="APPROVAL_EXPIRED",
        decided_at="2026-08-08T09:00:00Z",
    )
    record = expire_approval(
        ApprovalRecord(request=request, updated_at=request.requested_at),
        expiry,
        occurred_at="2026-08-08T09:00:00Z",
    )
    assert record.state is ApprovalState.EXPIRED


def test_approval_requires_aware_ordered_timestamps() -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        _request(requested_at="2026-08-08T08:00:00")
    with pytest.raises(ValueError, match="after requested_at"):
        _request(expires_at="2026-08-08T08:00:00Z")
