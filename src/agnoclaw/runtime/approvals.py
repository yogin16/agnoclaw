"""Durable, content-minimized approval requests, decisions, and grants."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .errors import HarnessError
from .security import AuthorizationGrant, GrantScope, canonical_json_digest

APPROVAL_SCHEMA_VERSION = "1.0"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _require_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > 512:
        raise ValueError(f"{field_name} cannot exceed 512 characters")
    return normalized


def _require_digest(value: str, *, field_name: str) -> str:
    normalized = _require_identifier(value, field_name=field_name)
    if not normalized.startswith("sha256:") or len(normalized) <= len("sha256:"):
        raise ValueError(f"{field_name} must be a sha256: digest")
    return normalized


def _parse_timestamp(value: str, *, field_name: str) -> datetime:
    normalized = _require_identifier(value, field_name=field_name)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a UTC offset")
    return parsed.astimezone(UTC)


def _digest(value: dict[str, Any]) -> str:
    return canonical_json_digest(value)


class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


TERMINAL_APPROVAL_STATES = frozenset(
    {
        ApprovalState.APPROVED,
        ApprovalState.DENIED,
        ApprovalState.EXPIRED,
        ApprovalState.CANCELLED,
    }
)


@dataclass(frozen=True)
class ApprovalRequest:
    """Immutable approval question persisted before any external effect."""

    request_id: str
    run_id: str
    call_id: str
    capability_id: str
    capability_digest: str
    effect_category: str
    argument_digest: str
    policy_version: str
    authority_digest: str
    tenant_id: str
    principal_id: str
    session_id: str
    expires_at: str
    nonce: str
    reason_code: str = "PERMISSION_APPROVAL_REQUIRED"
    requested_at: str = field(default_factory=_now)
    schema_version: str = APPROVAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != APPROVAL_SCHEMA_VERSION:
            raise ValueError(f"unsupported approval schema version '{self.schema_version}'")
        for field_name in (
            "request_id",
            "run_id",
            "call_id",
            "capability_id",
            "effect_category",
            "policy_version",
            "tenant_id",
            "principal_id",
            "session_id",
            "nonce",
            "reason_code",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_identifier(getattr(self, field_name), field_name=field_name),
            )
        for field_name in (
            "capability_digest",
            "argument_digest",
            "authority_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_digest(getattr(self, field_name), field_name=field_name),
            )
        requested = _parse_timestamp(self.requested_at, field_name="requested_at")
        expires = _parse_timestamp(self.expires_at, field_name="expires_at")
        if expires <= requested:
            raise ValueError("approval expires_at must be after requested_at")

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def is_expired(self, *, at: str | None = None) -> bool:
        observed = _parse_timestamp(at or _now(), field_name="observed_at")
        return observed >= _parse_timestamp(self.expires_at, field_name="expires_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "call_id": self.call_id,
            "capability_id": self.capability_id,
            "capability_digest": self.capability_digest,
            "effect_category": self.effect_category,
            "argument_digest": self.argument_digest,
            "policy_version": self.policy_version,
            "authority_digest": self.authority_digest,
            "tenant_id": self.tenant_id,
            "principal_id": self.principal_id,
            "session_id": self.session_id,
            "reason_code": self.reason_code,
            "requested_at": self.requested_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ApprovalRequest:
        return cls(**value)


@dataclass(frozen=True)
class ApprovalDecision:
    """Host-authoritative response bound to one exact request digest and nonce."""

    decision_id: str
    request_id: str
    request_digest: str
    request_nonce: str
    approved: bool
    issuer: str
    reason_code: str
    grant_scope: GrantScope = GrantScope.RUN
    decided_at: str = field(default_factory=_now)
    schema_version: str = APPROVAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != APPROVAL_SCHEMA_VERSION:
            raise ValueError(f"unsupported approval schema version '{self.schema_version}'")
        for field_name in (
            "decision_id",
            "request_id",
            "request_nonce",
            "issuer",
            "reason_code",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_identifier(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "request_digest",
            _require_digest(self.request_digest, field_name="request_digest"),
        )
        if not isinstance(self.approved, bool):
            raise ValueError("approved must be a boolean")
        object.__setattr__(self, "grant_scope", GrantScope(self.grant_scope))
        _parse_timestamp(self.decided_at, field_name="decided_at")

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "request_nonce": self.request_nonce,
            "approved": self.approved,
            "issuer": self.issuer,
            "reason_code": self.reason_code,
            "grant_scope": self.grant_scope.value,
            "decided_at": self.decided_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ApprovalDecision:
        return cls(**value)


@dataclass(frozen=True)
class ApprovalRecord:
    request: ApprovalRequest
    state: ApprovalState = ApprovalState.PENDING
    revision: int = 0
    decision: ApprovalDecision | None = None
    grant: AuthorizationGrant | None = None
    updated_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", ApprovalState(self.state))
        if self.revision < 0:
            raise ValueError("approval revision must be non-negative")
        _parse_timestamp(self.updated_at, field_name="updated_at")
        if self.state is ApprovalState.PENDING:
            if self.revision != 0 or self.decision is not None or self.grant is not None:
                raise ValueError("pending approval cannot contain a decision or grant")
            return
        if self.decision is None:
            raise ValueError("terminal approval requires a decision")
        if self.decision.request_id != self.request.request_id:
            raise ValueError("approval decision and request IDs must match")
        if self.state is ApprovalState.APPROVED:
            if not self.decision.approved or self.grant is None:
                raise ValueError("approved approval requires a positive decision and grant")
        elif self.grant is not None:
            raise ValueError("non-approved approval cannot contain a grant")

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_APPROVAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "state": self.state.value,
            "revision": self.revision,
            "decision": self.decision.to_dict() if self.decision is not None else None,
            "grant": self.grant.to_dict() if self.grant is not None else None,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ApprovalRecord:
        payload = dict(value)
        payload["request"] = ApprovalRequest.from_dict(payload["request"])
        if payload.get("decision") is not None:
            payload["decision"] = ApprovalDecision.from_dict(payload["decision"])
        if payload.get("grant") is not None:
            payload["grant"] = AuthorizationGrant.from_dict(payload["grant"])
        return cls(**payload)


class ApprovalNotFoundError(HarnessError):
    def __init__(self, request_id: str):
        super().__init__(
            code="APPROVAL_NOT_FOUND",
            category="approval",
            message="The requested approval does not exist or is not visible.",
            retryable=False,
            details={"request_id": request_id},
        )


class ApprovalRevisionConflictError(HarnessError):
    def __init__(self, *, request_id: str, expected: int, actual: int):
        super().__init__(
            code="APPROVAL_REVISION_CONFLICT",
            category="approval",
            message="The approval changed before this decision could commit.",
            retryable=True,
            details={
                "request_id": request_id,
                "expected_revision": expected,
                "actual_revision": actual,
            },
        )


class ApprovalIdempotencyConflictError(HarnessError):
    def __init__(self, *, request_id: str, decision_id: str):
        super().__init__(
            code="APPROVAL_IDEMPOTENCY_CONFLICT",
            category="approval",
            message="An approval decision ID was reused with different content.",
            retryable=False,
            details={"request_id": request_id, "decision_id": decision_id},
        )


class ApprovalBindingError(HarnessError):
    def __init__(self, *, request_id: str, field_name: str):
        super().__init__(
            code="APPROVAL_BINDING_MISMATCH",
            category="authorization",
            message="Approval evidence does not match the pending request.",
            retryable=False,
            details={"request_id": request_id, "field": field_name},
        )


class ApprovalExpiredError(HarnessError):
    def __init__(self, request_id: str):
        super().__init__(
            code="APPROVAL_EXPIRED",
            category="approval",
            message="The approval request expired before it was settled.",
            retryable=False,
            details={"request_id": request_id},
        )


class ApprovalAlreadySettledError(HarnessError):
    def __init__(self, *, request_id: str, state: ApprovalState):
        super().__init__(
            code="APPROVAL_ALREADY_SETTLED",
            category="approval",
            message=f"The approval is already {state.value}.",
            retryable=False,
            details={"request_id": request_id, "state": state.value},
        )


def _validate_decision_binding(
    request: ApprovalRequest,
    decision: ApprovalDecision,
) -> None:
    bindings = {
        "request_id": (request.request_id, decision.request_id),
        "request_digest": (request.digest, decision.request_digest),
        "request_nonce": (request.nonce, decision.request_nonce),
    }
    mismatch = next(
        (field_name for field_name, pair in bindings.items() if pair[0] != pair[1]),
        None,
    )
    if mismatch is not None:
        raise ApprovalBindingError(request_id=request.request_id, field_name=mismatch)


def _validate_grant_binding(
    request: ApprovalRequest,
    decision: ApprovalDecision,
    grant: AuthorizationGrant,
) -> None:
    expected_run = request.run_id if grant.scope is GrantScope.RUN else None
    bindings: dict[str, tuple[Any, Any]] = {
        "scope": (decision.grant_scope, grant.scope),
        "tenant_id": (request.tenant_id, grant.tenant_id),
        "principal_id": (request.principal_id, grant.principal_id),
        "session_id": (request.session_id, grant.session_id),
        "run_id": (expected_run, grant.run_id),
        "capability_ids": ((request.capability_id,), grant.capability_ids),
        "capability_digests": (
            (request.capability_digest,),
            grant.capability_digests,
        ),
        "effect_categories": ((request.effect_category,), grant.effect_categories),
        "argument_digest": (request.argument_digest, grant.argument_digest),
        "policy_version": (request.policy_version, grant.policy_version),
        "authority_digest": (request.authority_digest, grant.authority_digest),
        "issuer": (decision.issuer, grant.issuer),
    }
    mismatch = next(
        (field_name for field_name, pair in bindings.items() if pair[0] != pair[1]),
        None,
    )
    if mismatch is not None:
        raise ApprovalBindingError(request_id=request.request_id, field_name=mismatch)
    if _parse_timestamp(grant.expires_at, field_name="grant.expires_at") > _parse_timestamp(
        request.expires_at,
        field_name="request.expires_at",
    ):
        raise ApprovalBindingError(
            request_id=request.request_id,
            field_name="expires_at",
        )


def settle_approval(
    record: ApprovalRecord,
    decision: ApprovalDecision,
    *,
    grant: AuthorizationGrant | None = None,
    occurred_at: str | None = None,
) -> ApprovalRecord:
    """Pure fail-closed settlement reducer for an exact approval request."""
    if record.state is not ApprovalState.PENDING:
        raise ApprovalAlreadySettledError(
            request_id=record.request.request_id,
            state=record.state,
        )
    _validate_decision_binding(record.request, decision)
    observed_at = occurred_at or _now()
    observed = _parse_timestamp(observed_at, field_name="occurred_at")
    decided = _parse_timestamp(decision.decided_at, field_name="decided_at")
    requested = _parse_timestamp(record.request.requested_at, field_name="requested_at")
    expires = _parse_timestamp(record.request.expires_at, field_name="expires_at")
    if decided < requested:
        raise ApprovalBindingError(
            request_id=record.request.request_id,
            field_name="decided_at",
        )
    if observed >= expires or decided >= expires:
        raise ApprovalExpiredError(record.request.request_id)
    if decision.approved:
        if grant is None:
            raise ApprovalBindingError(
                request_id=record.request.request_id,
                field_name="grant",
            )
        _validate_grant_binding(record.request, decision, grant)
        state = ApprovalState.APPROVED
    else:
        if grant is not None:
            raise ApprovalBindingError(
                request_id=record.request.request_id,
                field_name="grant",
            )
        state = ApprovalState.DENIED
    return replace(
        record,
        state=state,
        revision=record.revision + 1,
        decision=decision,
        grant=grant,
        updated_at=observed_at,
    )


def expire_approval(
    record: ApprovalRecord,
    decision: ApprovalDecision,
    *,
    occurred_at: str | None = None,
) -> ApprovalRecord:
    """Record expiry with a negative system decision after the exact deadline."""
    if record.state is not ApprovalState.PENDING:
        raise ApprovalAlreadySettledError(
            request_id=record.request.request_id,
            state=record.state,
        )
    _validate_decision_binding(record.request, decision)
    if decision.approved:
        raise ApprovalBindingError(
            request_id=record.request.request_id,
            field_name="approved",
        )
    observed_at = occurred_at or _now()
    if not record.request.is_expired(at=observed_at):
        raise HarnessError(
            code="APPROVAL_NOT_EXPIRED",
            category="approval",
            message="The approval deadline has not elapsed.",
            retryable=True,
            details={"request_id": record.request.request_id},
        )
    return replace(
        record,
        state=ApprovalState.EXPIRED,
        revision=record.revision + 1,
        decision=decision,
        updated_at=observed_at,
    )


def cancel_approval(
    record: ApprovalRecord,
    decision: ApprovalDecision,
    *,
    occurred_at: str | None = None,
) -> ApprovalRecord:
    """Cancel a pending request when its owning run leaves the wait."""
    if record.state is not ApprovalState.PENDING:
        raise ApprovalAlreadySettledError(
            request_id=record.request.request_id,
            state=record.state,
        )
    _validate_decision_binding(record.request, decision)
    if decision.approved:
        raise ApprovalBindingError(
            request_id=record.request.request_id,
            field_name="approved",
        )
    return replace(
        record,
        state=ApprovalState.CANCELLED,
        revision=record.revision + 1,
        decision=decision,
        updated_at=occurred_at or _now(),
    )


__all__ = [
    "APPROVAL_SCHEMA_VERSION",
    "TERMINAL_APPROVAL_STATES",
    "ApprovalAlreadySettledError",
    "ApprovalBindingError",
    "ApprovalDecision",
    "ApprovalExpiredError",
    "ApprovalIdempotencyConflictError",
    "ApprovalNotFoundError",
    "ApprovalRecord",
    "ApprovalRequest",
    "ApprovalRevisionConflictError",
    "ApprovalState",
    "cancel_approval",
    "expire_approval",
    "settle_approval",
]
