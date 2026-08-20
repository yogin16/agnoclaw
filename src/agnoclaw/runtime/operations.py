"""Pure operation/effect domain for dispatch settlement and crash decisions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .errors import HarnessError
from .security import freeze_data, thaw_data

OPERATION_SCHEMA_VERSION = "1.0"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _require_identifier(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > 512:
        raise ValueError(f"{field_name} cannot exceed 512 characters")


def operation_result_slot_id(operation_id: str) -> str:
    """Return the canonical, content-independent result identity for an operation."""
    _require_identifier(operation_id, field_name="operation_id")
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return f"operation-result:v1:{digest}"


class OperationKind(StrEnum):
    MODEL = "model"
    CAPABILITY = "capability"


class EffectClass(StrEnum):
    """Externally observable replay semantics, never inferred from a tool name."""

    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    COMPENSATABLE = "compensatable"
    NON_REPEATABLE = "non_repeatable"


class OperationState(StrEnum):
    PLANNED = "planned"
    DISPATCHING = "dispatching"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


TERMINAL_OPERATION_STATES = frozenset(
    {
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.UNKNOWN,
        OperationState.CANCELLED,
    }
)
_MEASURED_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "audio_input_tokens",
    "audio_output_tokens",
    "audio_total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
_MAX_MEASUREMENT = 2**63 - 1


class RecoveryAction(StrEnum):
    DISPATCH = "dispatch"
    RETRY = "retry"
    RECONCILE = "reconcile"
    DO_NOTHING = "do_nothing"


class OperationReconciliationVerdict(StrEnum):
    """Independently observed outcome for an ambiguous external dispatch."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EFFECT_ABSENT = "effect_absent"


@dataclass(frozen=True)
class OperationIntent:
    """Immutable, content-minimized intent persisted before external dispatch."""

    operation_id: str
    run_id: str
    attempt_id: str
    kind: OperationKind
    target: str
    request_digest: str
    effect_class: EffectClass
    result_slot_id: str = ""
    idempotency_key: str | None = None
    timeout_seconds: float | None = None
    metadata: Any = field(default_factory=dict)
    schema_version: str = OPERATION_SCHEMA_VERSION
    prepared_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        for name in ("operation_id", "run_id", "attempt_id", "target"):
            _require_identifier(getattr(self, name), field_name=name)
        if self.schema_version != OPERATION_SCHEMA_VERSION:
            raise ValueError(f"unsupported operation schema version '{self.schema_version}'")
        object.__setattr__(self, "kind", OperationKind(self.kind))
        object.__setattr__(self, "effect_class", EffectClass(self.effect_class))
        expected_result_slot = operation_result_slot_id(self.operation_id)
        if self.result_slot_id and self.result_slot_id != expected_result_slot:
            raise ValueError("result_slot_id does not match the canonical operation identity")
        object.__setattr__(self, "result_slot_id", expected_result_slot)
        if not self.request_digest.startswith("sha256:"):
            raise ValueError("request_digest must be a sha256: digest")
        if self.effect_class is EffectClass.IDEMPOTENT and not self.idempotency_key:
            raise ValueError("idempotent operations require an idempotency_key")
        if self.idempotency_key is not None:
            _require_identifier(self.idempotency_key, field_name="idempotency_key")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        object.__setattr__(self, "metadata", freeze_data(self.metadata))

    @property
    def digest(self) -> str:
        payload = self.to_dict()
        # Observation time is evidence, not semantic request identity. A caller
        # reconstructing the same intent after restart must receive the original.
        payload.pop("prepared_at")
        # The result slot is a deterministic projection of operation_id. Keeping it
        # outside the digest preserves idempotency with pre-slot development records.
        payload.pop("result_slot_id")
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "kind": self.kind.value,
            "target": self.target,
            "request_digest": self.request_digest,
            "effect_class": self.effect_class.value,
            "result_slot_id": self.result_slot_id,
            "idempotency_key": self.idempotency_key,
            "timeout_seconds": self.timeout_seconds,
            "metadata": thaw_data(self.metadata),
            "prepared_at": self.prepared_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> OperationIntent:
        return cls(**value)


@dataclass(frozen=True)
class OperationSettlement:
    """Safe terminal projection; large or sensitive values belong in artifacts."""

    state: OperationState
    result_reference: str | None = None
    result_slot_id: str | None = None
    safe_error: Any = None
    provider_request_id: str | None = None
    usage: Any = field(default_factory=dict)
    cost: Any = field(default_factory=dict)
    settled_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", OperationState(self.state))
        if self.state not in TERMINAL_OPERATION_STATES:
            raise ValueError("operation settlement requires a terminal state")
        if self.state is OperationState.SUCCEEDED and self.safe_error is not None:
            raise ValueError("successful settlement cannot contain safe_error")
        if self.state is not OperationState.SUCCEEDED and self.result_reference is not None:
            raise ValueError("non-success settlement cannot contain a result reference")
        if self.state is not OperationState.SUCCEEDED and self.result_slot_id is not None:
            raise ValueError("non-success settlement cannot fulfill a result slot")
        if self.result_reference is not None:
            _require_identifier(self.result_reference, field_name="result_reference")
        if self.result_slot_id is not None:
            _require_identifier(self.result_slot_id, field_name="result_slot_id")
        if self.provider_request_id is not None:
            _require_identifier(
                self.provider_request_id,
                field_name="provider_request_id",
            )
        object.__setattr__(self, "safe_error", freeze_data(self.safe_error))
        object.__setattr__(self, "usage", freeze_data(self.usage))
        object.__setattr__(self, "cost", freeze_data(self.cost))

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "result_reference": self.result_reference,
            "result_slot_id": self.result_slot_id,
            "safe_error": thaw_data(self.safe_error),
            "provider_request_id": self.provider_request_id,
            "usage": thaw_data(self.usage),
            "cost": thaw_data(self.cost),
            "settled_at": self.settled_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> OperationSettlement:
        return cls(**value)


def operation_settlement_measurements(
    settlement: OperationSettlement | None,
) -> dict[str, Any]:
    """Return only bounded numeric usage/cost evidence safe for a runtime event."""
    if settlement is None:
        return {}
    usage_value = thaw_data(settlement.usage)
    usage: dict[str, int] = {}
    if isinstance(usage_value, dict):
        for name in _MEASURED_TOKEN_FIELDS:
            value = usage_value.get(name)
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= _MAX_MEASUREMENT
            ):
                usage[name] = value
    cost_value = thaw_data(settlement.cost)
    cost: dict[str, int] = {}
    if isinstance(cost_value, dict):
        value = cost_value.get("microusd")
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= _MAX_MEASUREMENT
        ):
            cost["microusd"] = value
    return {
        **({"usage": usage} if usage else {}),
        **({"cost": cost} if cost else {}),
    }


class OperationResultSlotMismatchError(HarnessError):
    def __init__(self, *, operation_id: str, expected: str, actual: str):
        super().__init__(
            code="OPERATION_RESULT_SLOT_MISMATCH",
            category="operation",
            message="The operation result does not fulfill its pre-provisioned identity.",
            retryable=False,
            details={
                "operation_id": operation_id,
                "expected_result_slot_id": expected,
                "actual_result_slot_id": actual,
            },
        )


@dataclass(frozen=True)
class OperationSettlementEvidence:
    """Optional content-minimized provider evidence captured with success."""

    provider_request_id: str | None = None
    usage: Any = field(default_factory=dict)
    cost: Any = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.provider_request_id is not None:
            _require_identifier(
                self.provider_request_id,
                field_name="provider_request_id",
            )
        object.__setattr__(self, "usage", freeze_data(self.usage))
        object.__setattr__(self, "cost", freeze_data(self.cost))


@dataclass(frozen=True)
class OperationRecord:
    intent: OperationIntent
    state: OperationState = OperationState.PLANNED
    revision: int = 0
    dispatch_attempt: int = 0
    fence_token: int = 0
    worker_id: str | None = None
    settlement: OperationSettlement | None = None
    updated_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", OperationState(self.state))
        if self.revision < 0 or self.dispatch_attempt < 0 or self.fence_token < 0:
            raise ValueError("operation counters cannot be negative")
        if self.worker_id is not None:
            _require_identifier(self.worker_id, field_name="worker_id")
        if self.state in TERMINAL_OPERATION_STATES:
            if self.settlement is None or self.settlement.state is not self.state:
                raise ValueError("terminal operation requires matching settlement")
            if self.state is OperationState.SUCCEEDED:
                expected = self.intent.result_slot_id
                actual = self.settlement.result_slot_id
                if actual is not None and actual != expected:
                    raise OperationResultSlotMismatchError(
                        operation_id=self.intent.operation_id,
                        expected=expected,
                        actual=actual,
                    )
                if actual is None:
                    object.__setattr__(
                        self,
                        "settlement",
                        replace(self.settlement, result_slot_id=expected),
                    )
        elif self.settlement is not None:
            raise ValueError("nonterminal operation cannot contain settlement")

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_OPERATION_STATES

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.to_dict(),
            "state": self.state.value,
            "revision": self.revision,
            "dispatch_attempt": self.dispatch_attempt,
            "fence_token": self.fence_token,
            "worker_id": self.worker_id,
            "settlement": (self.settlement.to_dict() if self.settlement is not None else None),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> OperationRecord:
        payload = dict(value)
        payload["intent"] = OperationIntent.from_dict(payload["intent"])
        if payload.get("settlement") is not None:
            payload["settlement"] = OperationSettlement.from_dict(payload["settlement"])
        return cls(**payload)


@dataclass(frozen=True)
class OperationReconciliation:
    """Evidence-bound authority to settle one exact ambiguous operation revision."""

    reconciliation_id: str
    operation_id: str
    expected_revision: int
    operation_digest: str
    verdict: OperationReconciliationVerdict
    observer_digest: str
    evidence_artifact_ids: tuple[str, ...]
    result_reference: str | None = None
    provider_request_id: str | None = None
    reconciled_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        _require_identifier(self.reconciliation_id, field_name="reconciliation_id")
        _require_identifier(self.operation_id, field_name="operation_id")
        if self.expected_revision < 0:
            raise ValueError("expected_revision cannot be negative")
        for name in ("operation_digest", "observer_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
                raise ValueError(f"{name} must be a canonical sha256 digest")
        object.__setattr__(self, "verdict", OperationReconciliationVerdict(self.verdict))
        object.__setattr__(self, "evidence_artifact_ids", tuple(self.evidence_artifact_ids))
        if not 1 <= len(self.evidence_artifact_ids) <= 16:
            raise ValueError("evidence_artifact_ids must contain between 1 and 16 items")
        if len(set(self.evidence_artifact_ids)) != len(self.evidence_artifact_ids):
            raise ValueError("evidence_artifact_ids must be unique")
        for artifact_id in self.evidence_artifact_ids:
            _require_identifier(artifact_id, field_name="evidence_artifact_id")
        if self.result_reference is not None:
            _require_identifier(self.result_reference, field_name="result_reference")
        if self.provider_request_id is not None:
            _require_identifier(self.provider_request_id, field_name="provider_request_id")
        if self.verdict is OperationReconciliationVerdict.SUCCEEDED:
            if self.result_reference is None:
                raise ValueError("successful reconciliation requires a result_reference")
            if self.result_reference not in self.evidence_artifact_ids:
                raise ValueError("result_reference must be included in evidence_artifact_ids")
        elif self.result_reference is not None:
            raise ValueError("only successful reconciliation may include a result_reference")

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciliation_id": self.reconciliation_id,
            "operation_id": self.operation_id,
            "expected_revision": self.expected_revision,
            "operation_digest": self.operation_digest,
            "verdict": self.verdict.value,
            "observer_digest": self.observer_digest,
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
            "result_reference": self.result_reference,
            "provider_request_id": self.provider_request_id,
            "reconciled_at": self.reconciled_at,
        }


class OperationTransitionError(HarnessError):
    def __init__(self, *, operation_id: str, state: OperationState, action: str):
        super().__init__(
            code="OPERATION_TRANSITION_INVALID",
            category="operation",
            message=f"Cannot {action} operation from state '{state.value}'.",
            retryable=False,
            details={"operation_id": operation_id, "state": state.value, "action": action},
        )


class OperationFenceError(HarnessError):
    def __init__(self, *, operation_id: str, expected: int, actual: int):
        super().__init__(
            code="OPERATION_FENCE_STALE",
            category="operation",
            message="A stale worker attempted to settle an operation.",
            retryable=False,
            details={
                "operation_id": operation_id,
                "expected_fence_token": expected,
                "actual_fence_token": actual,
            },
        )


def begin_operation_dispatch(
    record: OperationRecord,
    *,
    worker_id: str,
    fence_token: int,
    occurred_at: str | None = None,
) -> OperationRecord:
    """Claim one dispatch attempt using a monotonically increasing fence token."""
    _require_identifier(worker_id, field_name="worker_id")
    if record.terminal or record.state is not OperationState.PLANNED:
        raise OperationTransitionError(
            operation_id=record.intent.operation_id,
            state=record.state,
            action="begin dispatch for",
        )
    if fence_token <= record.fence_token:
        raise OperationFenceError(
            operation_id=record.intent.operation_id,
            expected=record.fence_token + 1,
            actual=fence_token,
        )
    return replace(
        record,
        state=OperationState.DISPATCHING,
        revision=record.revision + 1,
        dispatch_attempt=record.dispatch_attempt + 1,
        fence_token=fence_token,
        worker_id=worker_id,
        updated_at=occurred_at or _now(),
    )


def reset_operation_for_recovery(
    record: OperationRecord,
    *,
    next_fence_token: int,
    occurred_at: str | None = None,
) -> OperationRecord:
    """Return a safely replayable interrupted dispatch to planned state."""
    if record.state is not OperationState.DISPATCHING:
        raise OperationTransitionError(
            operation_id=record.intent.operation_id,
            state=record.state,
            action="recover",
        )
    if recovery_action(record) is not RecoveryAction.RETRY:
        raise OperationTransitionError(
            operation_id=record.intent.operation_id,
            state=record.state,
            action="retry unsafely",
        )
    if next_fence_token <= record.fence_token:
        raise OperationFenceError(
            operation_id=record.intent.operation_id,
            expected=record.fence_token + 1,
            actual=next_fence_token,
        )
    return replace(
        record,
        state=OperationState.PLANNED,
        revision=record.revision + 1,
        fence_token=next_fence_token,
        worker_id=None,
        updated_at=occurred_at or _now(),
    )


def settle_operation(
    record: OperationRecord,
    settlement: OperationSettlement,
    *,
    fence_token: int,
    occurred_at: str | None = None,
) -> OperationRecord:
    """Settle a fenced dispatch, or cancel an intent before dispatch begins.

    ``PLANNED -> CANCELLED`` is the one deliberately fence-free external-effect
    transition: the persisted intent exists, but dispatch has provably not
    started. Every other settlement must own the exact active dispatch fence.
    """
    pre_dispatch_cancel = (
        record.state is OperationState.PLANNED and settlement.state is OperationState.CANCELLED
    )
    if record.terminal or (
        record.state is not OperationState.DISPATCHING and not pre_dispatch_cancel
    ):
        raise OperationTransitionError(
            operation_id=record.intent.operation_id,
            state=record.state,
            action="settle",
        )
    if fence_token != record.fence_token:
        raise OperationFenceError(
            operation_id=record.intent.operation_id,
            expected=record.fence_token,
            actual=fence_token,
        )
    if settlement.state is OperationState.SUCCEEDED:
        expected = record.intent.result_slot_id
        actual = settlement.result_slot_id
        if actual is not None and actual != expected:
            raise OperationResultSlotMismatchError(
                operation_id=record.intent.operation_id,
                expected=expected,
                actual=actual,
            )
        if actual is None:
            settlement = replace(settlement, result_slot_id=expected)
    return replace(
        record,
        state=settlement.state,
        revision=record.revision + 1,
        settlement=settlement,
        updated_at=occurred_at or settlement.settled_at,
    )


def recovery_action(record: OperationRecord) -> RecoveryAction:
    """Classify interruption without assuming an external effect did or did not occur."""
    if record.state is OperationState.UNKNOWN:
        return RecoveryAction.RECONCILE
    if record.terminal:
        return RecoveryAction.DO_NOTHING
    if record.state is OperationState.PLANNED:
        return RecoveryAction.DISPATCH
    effect = record.intent.effect_class
    if effect is EffectClass.READ_ONLY:
        return RecoveryAction.RETRY
    if effect is EffectClass.IDEMPOTENT and record.intent.idempotency_key:
        return RecoveryAction.RETRY
    return RecoveryAction.RECONCILE


def reconcile_operation(
    record: OperationRecord,
    reconciliation: OperationReconciliation,
) -> OperationRecord:
    """Fence stale dispatch and settle ambiguity from independently bound evidence."""
    if (
        reconciliation.operation_id != record.intent.operation_id
        or reconciliation.expected_revision != record.revision
        or reconciliation.operation_digest != record.digest
    ):
        raise OperationTransitionError(
            operation_id=record.intent.operation_id,
            state=record.state,
            action="reconcile with mismatched evidence for",
        )
    if recovery_action(record) is not RecoveryAction.RECONCILE:
        raise OperationTransitionError(
            operation_id=record.intent.operation_id,
            state=record.state,
            action="reconcile",
        )
    if reconciliation.verdict is OperationReconciliationVerdict.SUCCEEDED:
        settlement = OperationSettlement(
            state=OperationState.SUCCEEDED,
            result_reference=reconciliation.result_reference,
            result_slot_id=record.intent.result_slot_id,
            provider_request_id=reconciliation.provider_request_id,
            settled_at=reconciliation.reconciled_at,
        )
    else:
        code = (
            "OPERATION_RECONCILED_EFFECT_ABSENT"
            if reconciliation.verdict is OperationReconciliationVerdict.EFFECT_ABSENT
            else "OPERATION_RECONCILED_EXTERNAL_FAILURE"
        )
        settlement = OperationSettlement(
            state=OperationState.FAILED,
            safe_error={"code": code, "category": "operation", "retryable": False},
            provider_request_id=reconciliation.provider_request_id,
            settled_at=reconciliation.reconciled_at,
        )
    return replace(
        record,
        state=settlement.state,
        revision=record.revision + 1,
        fence_token=record.fence_token + 1,
        worker_id=None,
        settlement=settlement,
        updated_at=reconciliation.reconciled_at,
    )


__all__ = [
    "EffectClass",
    "OPERATION_SCHEMA_VERSION",
    "OperationFenceError",
    "OperationIntent",
    "OperationKind",
    "OperationReconciliation",
    "OperationReconciliationVerdict",
    "OperationRecord",
    "OperationResultSlotMismatchError",
    "OperationSettlement",
    "OperationSettlementEvidence",
    "operation_settlement_measurements",
    "OperationState",
    "OperationTransitionError",
    "RecoveryAction",
    "TERMINAL_OPERATION_STATES",
    "begin_operation_dispatch",
    "recovery_action",
    "reconcile_operation",
    "reset_operation_for_recovery",
    "operation_result_slot_id",
    "settle_operation",
]
