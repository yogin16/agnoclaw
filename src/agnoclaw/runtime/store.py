"""Canonical run ledger contract and transactional SQLite reference store."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from .approvals import (
    ApprovalAlreadySettledError,
    ApprovalDecision,
    ApprovalIdempotencyConflictError,
    ApprovalNotFoundError,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalRevisionConflictError,
    ApprovalState,
)
from .approvals import (
    cancel_approval as reduce_approval_cancellation,
)
from .approvals import (
    expire_approval as reduce_approval_expiry,
)
from .approvals import (
    settle_approval as reduce_approval_settlement,
)
from .artifacts import ArtifactNotFoundError, ArtifactReference, ArtifactScope
from .children import ChildJoinPolicy, ChildRunContractError, ChildRunSpec
from .errors import HarnessError
from .leases import (
    LeaseKind,
    LeaseReleaseDecision,
    RunLeaseClaim,
    RuntimeLease,
    RuntimeLeaseClaimReleasedError,
    RuntimeLeaseLostError,
    RuntimeLeaseTerminalRunError,
    RuntimeLeaseUnavailableError,
    lease_token,
    run_lease_key,
    session_lease_key,
)
from .lifecycle import (
    TERMINAL_RUN_STATES,
    LifecycleIdempotencyConflictError,
    LifecycleTransition,
    RunNotFoundError,
    RunRevisionConflictError,
    RunSnapshot,
    RunState,
    TransitionDecision,
    TransitionKind,
    reduce_lifecycle,
)
from .operations import (
    OperationIntent,
    OperationReconciliation,
    OperationRecord,
    OperationSettlement,
    OperationState,
    begin_operation_dispatch,
    operation_settlement_measurements,
    reset_operation_for_recovery,
    settle_operation,
)
from .operations import (
    reconcile_operation as reduce_operation_reconciliation,
)
from .scheduler_store import SQLiteSchedulerStoreMixin
from .security import AuthorizationGrant, freeze_data, thaw_data

RUNTIME_SCHEMA_VERSION = 12
EVENT_SCHEMA_VERSION = "1.0"
MAX_RUNTIME_EVENT_PAYLOAD_BYTES = 65_536
MAX_OUTBOX_DEFER_SECONDS = 86_400
MAX_RECOVERY_MINIMUM_AGE_SECONDS = 86_400


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        thaw_data(freeze_data(value)),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validate_reason_code(reason_code: str) -> None:
    if (
        not isinstance(reason_code, str)
        or re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", reason_code) is None
    ):
        raise ValueError("reason_code must be a safe lowercase operational identifier")


def _validate_sha256(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError(f"{field_name} must be a canonical sha256 digest")


def _validate_mutation_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > 256
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value) is None
    ):
        raise ValueError("mutation_id must be a safe non-empty identifier of at most 256 chars")


def _dead_letter_mutation_digest(
    *,
    outbox_id: int,
    expected_dead_lettered_at: str,
    owner: RunOwner,
    operator_digest: str,
    authority_digest: str,
    reason_code: str,
    delay_seconds: float,
) -> str:
    canonical = _canonical_json(
        {
            "outbox_id": outbox_id,
            "expected_dead_lettered_at": expected_dead_lettered_at,
            "owner": {"tenant_id": owner.tenant_id, "user_id": owner.user_id},
            "operator_digest": operator_digest,
            "authority_digest": authority_digest,
            "reason_code": reason_code,
            "delay_seconds": delay_seconds,
        }
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _snapshot_to_dict(snapshot: RunSnapshot) -> dict[str, Any]:
    return {
        "schema_version": snapshot.schema_version,
        "run_id": snapshot.run_id,
        "state": snapshot.state.value,
        "revision": snapshot.revision,
        "tenant_id": snapshot.tenant_id,
        "user_id": snapshot.user_id,
        "session_id": snapshot.session_id,
        "parent_run_id": snapshot.parent_run_id,
        "root_run_id": snapshot.root_run_id,
        "child_depth": snapshot.child_depth,
        "created_at": snapshot.created_at,
        "updated_at": snapshot.updated_at,
        "steering_open": snapshot.steering_open,
        "pending_request_id": snapshot.pending_request_id,
        "last_transition_id": snapshot.last_transition_id,
        "last_reason_code": snapshot.last_reason_code,
        "metadata": thaw_data(snapshot.metadata),
    }


def _snapshot_from_json(value: str) -> RunSnapshot:
    payload = json.loads(value)
    return RunSnapshot(**payload)


@dataclass(frozen=True)
class RunOwner:
    """Exact owner tuple used for store-level defense in depth."""

    tenant_id: str | None
    user_id: str | None


@dataclass(frozen=True)
class RuntimeEvent:
    """Immutable append-only event committed by the runtime ledger."""

    run_id: str
    sequence: int
    event_type: str
    payload: Any = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: f"evt_{uuid4().hex}")
    occurred_at: str = field(default_factory=_now)
    schema_version: str = EVENT_SCHEMA_VERSION
    attempt_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if self.sequence <= 0:
            raise ValueError("event sequence must be positive")
        if not isinstance(self.event_type, str) or not self.event_type.strip():
            raise ValueError("event_type must be a non-empty string")
        object.__setattr__(self, "payload", freeze_data(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "attempt_id": self.attempt_id,
            "payload": thaw_data(self.payload),
        }


@dataclass(frozen=True)
class RuntimeEventInput:
    """Sequence-free, idempotent event proposed for the authoritative ledger."""

    event_id: str
    run_id: str
    event_type: str
    occurred_at: str
    payload: Any = field(default_factory=dict)
    attempt_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("event_id", "run_id", "event_type", "occurred_at"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
            if len(value) > 512:
                raise ValueError(f"{field_name} cannot exceed 512 characters")
        if self.attempt_id is not None and (
            not isinstance(self.attempt_id, str)
            or not self.attempt_id.strip()
            or len(self.attempt_id) > 512
        ):
            raise ValueError("attempt_id must be a non-empty string of at most 512 characters")
        try:
            occurred_at = datetime.fromisoformat(self.occurred_at)
        except ValueError as exc:
            raise ValueError("occurred_at must be an ISO-8601 timestamp") from exc
        if occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must include a UTC offset")
        frozen_payload = freeze_data(self.payload)
        if len(_canonical_json(frozen_payload).encode("utf-8")) > MAX_RUNTIME_EVENT_PAYLOAD_BYTES:
            raise ValueError(
                f"payload cannot exceed {MAX_RUNTIME_EVENT_PAYLOAD_BYTES} encoded bytes"
            )
        object.__setattr__(self, "payload", frozen_payload)

    def semantic_value(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "attempt_id": self.attempt_id,
            "payload": thaw_data(self.payload),
        }


@dataclass(frozen=True)
class RuntimeEventAppendDecision:
    event: RuntimeEvent
    appended: bool
    idempotent: bool = False


def _require_event_artifact_binding(
    proposed: RuntimeEventInput,
    reference: ArtifactReference | None,
) -> None:
    if reference is None:
        return
    payload = thaw_data(proposed.payload)
    if not isinstance(payload, dict) or payload.get("artifact_id") != reference.artifact_id:
        raise HarnessError(
            code="ARTIFACT_EVENT_MISMATCH",
            category="artifact",
            message="The event does not exactly reference its committed artifact.",
            retryable=False,
            details={"run_id": proposed.run_id},
        )


@dataclass(frozen=True)
class CreateRunDecision:
    snapshot: RunSnapshot
    event: RuntimeEvent
    created: bool
    idempotent: bool = False


@dataclass(frozen=True)
class StoredTransitionDecision:
    lifecycle: TransitionDecision
    event: RuntimeEvent


@dataclass(frozen=True)
class StoredApprovalDecision:
    record: ApprovalRecord
    event: RuntimeEvent
    applied: bool
    idempotent: bool = False


@dataclass(frozen=True)
class TerminalRecord:
    """Serializable terminal result or safe error projection."""

    run_id: str
    state: RunState
    value: Any = None
    error: Any = None
    recorded_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", RunState(self.state))
        if self.state not in TERMINAL_RUN_STATES:
            raise ValueError("terminal record requires a terminal run state")
        if self.state == RunState.COMPLETED and self.error is not None:
            raise ValueError("completed terminal record cannot contain an error")
        if self.state != RunState.COMPLETED and self.value is not None:
            raise ValueError("non-completed terminal record cannot contain a result value")
        object.__setattr__(self, "value", freeze_data(self.value))
        object.__setattr__(self, "error", freeze_data(self.error))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state.value,
            "value": thaw_data(self.value),
            "error": thaw_data(self.error),
            "recorded_at": self.recorded_at,
        }


def _settlement_digest(
    transition: LifecycleTransition,
    terminal: TerminalRecord | None,
) -> str:
    canonical = _canonical_json(
        {
            "transition_digest": transition.digest,
            "terminal": terminal.to_dict() if terminal is not None else None,
        }
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _operation_mutation_digest(kind: str, payload: Any) -> str:
    canonical = _canonical_json({"kind": kind, "payload": payload})
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class OutboxItem:
    outbox_id: int
    run_id: str
    sequence: int
    event: RuntimeEvent
    attempts: int
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: str | None


@dataclass(frozen=True)
class DeadLetterItem:
    outbox_id: int
    run_id: str
    sequence: int
    event: RuntimeEvent
    attempts: int
    reason_code: str
    dead_lettered_at: str


class DeadLetterAuditAction(StrEnum):
    INSPECTED = "inspected"
    REQUEUED = "requeued"


@dataclass(frozen=True)
class DeadLetterAuditRecord:
    audit_sequence: int
    audit_id: str
    action: DeadLetterAuditAction
    owner: RunOwner
    operator_digest: str
    authority_digest: str
    reason_code: str
    requested_after_outbox_id: int | None
    requested_limit: int | None
    result_count: int
    first_outbox_id: int | None
    last_outbox_id: int | None
    outbox_id: int | None
    run_id: str | None
    expected_dead_lettered_at: str | None
    delay_seconds: float
    mutation_id: str | None
    mutation_digest: str | None
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", DeadLetterAuditAction(self.action))


@dataclass(frozen=True)
class DeadLetterInspectionDecision:
    items: tuple[DeadLetterItem, ...]
    audit: DeadLetterAuditRecord


@dataclass(frozen=True)
class DeadLetterRequeueDecision:
    audit: DeadLetterAuditRecord
    idempotent: bool = False


@dataclass(frozen=True)
class RetentionDecision:
    run_id: str
    pruned_through_sequence: int
    deleted_events: int
    deleted_outbox_items: int


@dataclass(frozen=True)
class StoredOperationDecision:
    record: OperationRecord
    event: RuntimeEvent
    applied: bool
    idempotent: bool = False


@dataclass(frozen=True)
class StoredLeaseClaimDecision:
    claim: RunLeaseClaim
    event: RuntimeEvent
    acquired: bool
    idempotent: bool = False
    reclaimed: bool = False


class StartIdempotencyConflictError(HarnessError):
    def __init__(self, *, scope: str, key: str):
        super().__init__(
            code="RUN_START_IDEMPOTENCY_CONFLICT",
            category="lifecycle",
            message="The idempotency key was reused for a different run request.",
            retryable=False,
            details={"scope": scope, "idempotency_key": key},
        )


class EventCursorError(HarnessError):
    def __init__(self, message: str):
        super().__init__(
            code="RUN_EVENT_CURSOR_INVALID",
            category="lifecycle",
            message=message,
            retryable=False,
        )


class EventCursorExpiredError(HarnessError):
    def __init__(self, *, run_id: str, pruned_through_sequence: int):
        super().__init__(
            code="RUN_EVENT_CURSOR_EXPIRED",
            category="lifecycle",
            message="The requested event position is older than retained history.",
            retryable=False,
            details={
                "run_id": run_id,
                "pruned_through_sequence": pruned_through_sequence,
                "resume_after_sequence": pruned_through_sequence,
                "resume_cursor": encode_event_cursor(
                    run_id=run_id,
                    sequence=pruned_through_sequence,
                ),
            },
        )


class RuntimeEventIdempotencyConflictError(HarnessError):
    def __init__(self, event_id: str):
        super().__init__(
            code="RUNTIME_EVENT_IDEMPOTENCY_CONFLICT",
            category="runtime_store",
            message="The event ID was reused with different semantics.",
            retryable=False,
            details={"event_id": event_id},
        )


class RuntimeEventTerminalRunError(HarnessError):
    def __init__(self, run_id: str):
        super().__init__(
            code="RUNTIME_EVENT_TERMINAL_RUN",
            category="runtime_store",
            message="New trajectory events cannot be appended after terminal settlement.",
            retryable=False,
            details={"run_id": run_id},
        )


class RuntimeRetentionError(HarnessError):
    def __init__(self, *, code: str, run_id: str, message: str):
        super().__init__(
            code=code,
            category="runtime_store",
            message=message,
            retryable=True,
            details={"run_id": run_id},
        )


class OperationIdempotencyConflictError(HarnessError):
    def __init__(self, *, operation_id: str, mutation_id: str):
        super().__init__(
            code="OPERATION_IDEMPOTENCY_CONFLICT",
            category="operation",
            message="An operation mutation ID was reused with different intent.",
            retryable=False,
            details={"operation_id": operation_id, "mutation_id": mutation_id},
        )


class OperationNotFoundError(HarnessError):
    def __init__(self, operation_id: str):
        super().__init__(
            code="OPERATION_NOT_FOUND",
            category="operation",
            message="The operation was not found or is not visible to this principal.",
            retryable=False,
            details={"operation_id": operation_id},
        )


class OperationRevisionConflictError(HarnessError):
    def __init__(self, *, operation_id: str, expected: int, actual: int):
        super().__init__(
            code="OPERATION_REVISION_CONFLICT",
            category="operation",
            message="The operation changed before this mutation committed.",
            retryable=True,
            details={
                "operation_id": operation_id,
                "expected_revision": expected,
                "actual_revision": actual,
            },
        )


class OutboxLeaseError(HarnessError):
    def __init__(self, outbox_id: int):
        super().__init__(
            code="OUTBOX_LEASE_INVALID",
            category="runtime_store",
            message="The outbox item is not owned by this lease.",
            retryable=True,
            details={"outbox_id": outbox_id},
        )


class OutboxDeadLetterConflictError(HarnessError):
    def __init__(self, outbox_id: int):
        super().__init__(
            code="OUTBOX_DEAD_LETTER_CONFLICT",
            category="runtime_store",
            message="The dead-letter item changed or is no longer quarantined.",
            retryable=True,
            details={"outbox_id": outbox_id},
        )


class OutboxDeadLetterMutationConflictError(HarnessError):
    def __init__(self, mutation_id: str):
        super().__init__(
            code="OUTBOX_DEAD_LETTER_MUTATION_CONFLICT",
            category="runtime_store",
            message="The dead-letter mutation ID was reused with different semantics.",
            retryable=False,
            details={"mutation_id": mutation_id},
        )


class RuntimeStoreDependencyError(HarnessError):
    def __init__(self, *, backend: str, extra: str):
        super().__init__(
            code="RUNTIME_STORE_DEPENDENCY_MISSING",
            category="runtime_store",
            message=(
                f"The {backend} runtime store requires optional dependencies. "
                f"Install with: pip install 'agnoclaw[{extra}]'"
            ),
            retryable=False,
            details={"backend": backend, "install_extra": extra},
        )


class RuntimeStoreOverloadedError(HarnessError):
    def __init__(self, *, backend: str, retry_after_seconds: float):
        super().__init__(
            code="RUNTIME_STORE_OVERLOADED",
            category="runtime_store",
            message="Runtime-store capacity is exhausted; retry after backoff.",
            retryable=True,
            details={
                "backend": backend,
                "retry_after_seconds": retry_after_seconds,
            },
        )


class RuntimeStoreConnectionLostError(HarnessError):
    """The connection failed after acquisition, so mutation outcome may be unknown."""

    def __init__(self, *, backend: str):
        super().__init__(
            code="RUNTIME_STORE_CONNECTION_LOST",
            category="runtime_store",
            message=(
                "The runtime-store connection was lost. Re-read authoritative state "
                "before deciding whether to retry."
            ),
            retryable=False,
            details={
                "backend": backend,
                "reconciliation_required": True,
            },
        )


class RuntimeStoreReadOnlyError(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            code="RUNTIME_STORE_READ_ONLY",
            category="persistence",
            message="The RuntimeStore was opened for inspection only.",
            retryable=False,
        )


def encode_event_cursor(*, run_id: str, sequence: int) -> str:
    """Encode a run-bound opaque reconnect cursor."""
    if sequence < 0:
        raise ValueError("cursor sequence must be non-negative")
    raw = _canonical_json({"v": 1, "run_id": run_id, "sequence": sequence})
    token = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
    return f"cursor_v1_{token}"


def decode_event_cursor(cursor: str, *, run_id: str) -> int:
    """Decode a cursor and enforce that it belongs to the requested run."""
    if not isinstance(cursor, str) or not cursor.startswith("cursor_v1_"):
        raise EventCursorError("Event cursor has an unsupported format.")
    token = cursor.removeprefix("cursor_v1_")
    try:
        padded = token + ("=" * (-len(token) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception as exc:
        raise EventCursorError("Event cursor could not be decoded.") from exc
    if payload.get("v") != 1 or payload.get("run_id") != run_id:
        raise EventCursorError("Event cursor does not belong to this run.")
    sequence = payload.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise EventCursorError("Event cursor sequence is invalid.")
    return sequence


@runtime_checkable
class RuntimeStore(Protocol):
    """One authoritative run/event/outbox extension boundary."""

    @property
    def schema_version(self) -> int: ...

    def close(self) -> None: ...

    def create_run(
        self,
        snapshot: RunSnapshot,
        *,
        idempotency_scope: str | None = None,
        idempotency_key: str | None = None,
        request_digest: str | None = None,
        child_spec: ChildRunSpec | None = None,
    ) -> CreateRunDecision: ...

    def get_run(self, run_id: str, *, owner: RunOwner | None = None) -> RunSnapshot: ...

    def list_children(
        self,
        parent_run_id: str,
        *,
        limit: int = 64,
        owner: RunOwner | None = None,
    ) -> list[RunSnapshot]: ...

    def get_child_spec(
        self,
        child_run_id: str,
        *,
        owner: RunOwner | None = None,
    ) -> ChildRunSpec: ...

    def apply_transition(
        self,
        transition: LifecycleTransition,
        *,
        expected_revision: int,
        terminal: TerminalRecord | None = None,
        approval_request: ApprovalRequest | None = None,
    ) -> StoredTransitionDecision: ...

    def get_approval(
        self,
        request_id: str,
        *,
        owner: RunOwner | None = None,
    ) -> ApprovalRecord: ...

    def list_approvals(
        self,
        run_id: str,
        *,
        states: tuple[ApprovalState, ...] | None = None,
        limit: int = 100,
        owner: RunOwner | None = None,
    ) -> list[ApprovalRecord]: ...

    def settle_approval(
        self,
        decision: ApprovalDecision,
        *,
        expected_revision: int,
        grant: AuthorizationGrant | None = None,
        owner: RunOwner | None = None,
    ) -> StoredApprovalDecision: ...

    def expire_approval(
        self,
        decision: ApprovalDecision,
        *,
        expected_revision: int,
        owner: RunOwner | None = None,
    ) -> StoredApprovalDecision: ...

    def get_terminal(
        self,
        run_id: str,
        *,
        owner: RunOwner | None = None,
    ) -> TerminalRecord | None: ...

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        owner: RunOwner | None = None,
    ) -> list[RuntimeEvent]: ...

    def append_runtime_event(
        self,
        proposed: RuntimeEventInput,
        *,
        owner: RunOwner | None = None,
        artifact_reference: ArtifactReference | None = None,
    ) -> RuntimeEventAppendDecision: ...

    def lease_outbox(
        self,
        *,
        owner: str,
        limit: int = 100,
        lease_seconds: int = 30,
    ) -> list[OutboxItem]: ...

    def acknowledge_outbox(self, *, outbox_id: int, lease_token: str) -> None: ...

    def defer_outbox(
        self,
        *,
        outbox_id: int,
        lease_token: str,
        delay_seconds: float = 0,
    ) -> None: ...

    def dead_letter_outbox(
        self,
        *,
        outbox_id: int,
        lease_token: str,
        reason_code: str,
    ) -> DeadLetterItem: ...

    def inspect_dead_letters(
        self,
        *,
        owner: RunOwner,
        operator_digest: str,
        authority_digest: str,
        reason_code: str,
        after_outbox_id: int = 0,
        limit: int = 100,
    ) -> DeadLetterInspectionDecision: ...

    def requeue_dead_letter(
        self,
        *,
        owner: RunOwner,
        operator_digest: str,
        authority_digest: str,
        reason_code: str,
        mutation_id: str,
        outbox_id: int,
        expected_dead_lettered_at: str,
        delay_seconds: float = 0,
    ) -> DeadLetterRequeueDecision: ...

    def list_dead_letter_audit(
        self,
        *,
        owner: RunOwner,
        after_audit_sequence: int = 0,
        limit: int = 100,
    ) -> list[DeadLetterAuditRecord]: ...

    def prune_run_events(
        self,
        run_id: str,
        *,
        through_sequence: int,
        owner: RunOwner | None = None,
    ) -> RetentionDecision: ...

    def prepare_operation(self, intent: OperationIntent) -> StoredOperationDecision: ...

    def get_operation(
        self,
        operation_id: str,
        *,
        owner: RunOwner | None = None,
    ) -> OperationRecord: ...

    def list_run_operations(
        self,
        run_id: str,
        *,
        limit: int = 100,
        owner: RunOwner | None = None,
    ) -> list[OperationRecord]: ...

    def begin_operation(
        self,
        operation_id: str,
        *,
        mutation_id: str,
        expected_revision: int,
        worker_id: str,
        fence_token: int,
    ) -> StoredOperationDecision: ...

    def recover_operation(
        self,
        operation_id: str,
        *,
        mutation_id: str,
        expected_revision: int,
        next_fence_token: int,
    ) -> StoredOperationDecision: ...

    def settle_operation(
        self,
        operation_id: str,
        *,
        mutation_id: str,
        expected_revision: int,
        fence_token: int,
        settlement: OperationSettlement,
        artifact_reference: ArtifactReference | None = None,
    ) -> StoredOperationDecision: ...

    def reconcile_operation(
        self,
        operation_id: str,
        *,
        mutation_id: str,
        reconciliation: OperationReconciliation,
        evidence_artifacts: tuple[ArtifactReference, ...],
        owner: RunOwner,
    ) -> StoredOperationDecision: ...

    def get_artifact(
        self,
        artifact_id: str,
        *,
        owner: RunOwner | None = None,
    ) -> ArtifactReference: ...

    def list_artifacts(
        self,
        run_id: str,
        *,
        limit: int = 100,
        owner: RunOwner | None = None,
    ) -> list[ArtifactReference]: ...

    def list_artifact_storage_keys(self, *, include_deleted: bool = False) -> list[str]: ...

    def list_recoverable_operations(
        self,
        *,
        limit: int = 100,
    ) -> list[OperationRecord]: ...

    def list_reconciliation_operations(
        self,
        *,
        owner: RunOwner,
        after_operation_id: str | None = None,
        minimum_age_seconds: int = 30,
        limit: int = 100,
    ) -> list[OperationRecord]: ...

    def list_recoverable_runs(
        self,
        *,
        owner: RunOwner,
        after_run_id: str | None = None,
        minimum_age_seconds: int = 30,
        limit: int = 100,
    ) -> list[RunSnapshot]: ...

    def acquire_run_lease(
        self,
        run_id: str,
        *,
        worker_id: str,
        claim_id: str,
        lease_seconds: int = 30,
        owner: RunOwner | None = None,
    ) -> StoredLeaseClaimDecision: ...

    def renew_run_lease(
        self,
        claim: RunLeaseClaim,
        *,
        lease_seconds: int = 30,
    ) -> RunLeaseClaim: ...

    def release_run_lease(self, claim: RunLeaseClaim) -> LeaseReleaseDecision: ...


class SQLiteRuntimeStore(SQLiteSchedulerStoreMixin):
    """Transactional single-node RuntimeStore and schema migration authority."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5000,
        fault_injector: Callable[[str], None] | None = None,
        read_only: bool = False,
    ) -> None:
        self.path = str(path)
        if not isinstance(read_only, bool):
            raise TypeError("read_only must be a boolean")
        if read_only and self.path == ":memory:":
            raise ValueError("read-only RuntimeStore requires a persisted database")
        self.read_only = read_only
        connection_target = self.path
        connection_is_uri = False
        if self.path != ":memory:":
            db_path = Path(self.path).expanduser().resolve(strict=read_only)
            if not read_only:
                db_path.parent.mkdir(parents=True, exist_ok=True)
            self.path = str(db_path)
            if read_only:
                connection_target = f"{db_path.as_uri()}?mode=ro"
                connection_is_uri = True
            else:
                connection_target = self.path
        self._lock = threading.RLock()
        self._fault_injector = fault_injector
        self._connection = sqlite3.connect(
            connection_target,
            isolation_level=None,
            check_same_thread=False,
            uri=connection_is_uri,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._connection.execute("PRAGMA foreign_keys = ON")
        if read_only:
            self._connection.execute("PRAGMA query_only = ON")
        elif self.path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
        if read_only:
            try:
                current_schema = self.schema_version
            except sqlite3.DatabaseError as exc:
                self._connection.close()
                raise ValueError(
                    "read-only RuntimeStore requires a current initialized schema"
                ) from exc
            if current_schema != RUNTIME_SCHEMA_VERSION:
                self._connection.close()
                raise ValueError(
                    f"read-only RuntimeStore requires schema version {RUNTIME_SCHEMA_VERSION}"
                )
        else:
            self.migrate()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SQLiteRuntimeStore:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    @contextmanager
    def _transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise RuntimeStoreReadOnlyError()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def migrate(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS runtime_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_runs (
                run_id TEXT PRIMARY KEY,
                tenant_id TEXT,
                user_id TEXT,
                session_id TEXT,
                state TEXT NOT NULL,
                revision INTEGER NOT NULL,
                next_sequence INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                authority_updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS runtime_runs_owner_idx
            ON runtime_runs(tenant_id, user_id, session_id, updated_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_transitions (
                run_id TEXT NOT NULL,
                transition_id TEXT NOT NULL,
                transition_digest TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                event_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY(run_id, transition_id),
                FOREIGN KEY(run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_events (
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                transition_id TEXT,
                operation_id TEXT,
                occurred_at TEXT NOT NULL,
                event_json TEXT NOT NULL,
                PRIMARY KEY(run_id, sequence),
                FOREIGN KEY(run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_outbox (
                outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at TEXT NOT NULL,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                delivered_at TEXT,
                dead_lettered_at TEXT,
                dead_letter_reason_code TEXT,
                UNIQUE(run_id, sequence),
                FOREIGN KEY(run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS runtime_outbox_ready_idx
            ON runtime_outbox(status, available_at, lease_expires_at, outbox_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_dead_letter_audit (
                audit_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                audit_id TEXT NOT NULL UNIQUE,
                action TEXT NOT NULL CHECK(action IN ('inspected', 'requeued')),
                tenant_id TEXT,
                user_id TEXT,
                operator_digest TEXT NOT NULL,
                authority_digest TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                requested_after_outbox_id INTEGER,
                requested_limit INTEGER,
                result_count INTEGER NOT NULL,
                first_outbox_id INTEGER,
                last_outbox_id INTEGER,
                outbox_id INTEGER,
                run_id TEXT,
                expected_dead_lettered_at TEXT,
                delay_seconds REAL NOT NULL DEFAULT 0,
                mutation_id TEXT UNIQUE,
                mutation_digest TEXT,
                created_at TEXT NOT NULL,
                CHECK(result_count >= 0),
                CHECK(delay_seconds >= 0 AND delay_seconds <= 86400),
                CHECK(
                    (action = 'inspected' AND result_count >= 0
                        AND requested_after_outbox_id IS NOT NULL
                        AND requested_after_outbox_id >= 0
                        AND requested_limit >= 1 AND requested_limit <= 1000
                        AND outbox_id IS NULL AND run_id IS NULL
                        AND expected_dead_lettered_at IS NULL
                        AND delay_seconds = 0 AND mutation_id IS NULL
                        AND mutation_digest IS NULL)
                    OR
                    (action = 'requeued' AND result_count = 1
                        AND requested_after_outbox_id IS NULL
                        AND requested_limit IS NULL
                        AND first_outbox_id = outbox_id
                        AND last_outbox_id = outbox_id
                        AND outbox_id IS NOT NULL AND run_id IS NOT NULL
                        AND expected_dead_lettered_at IS NOT NULL
                        AND mutation_id IS NOT NULL AND mutation_digest IS NOT NULL)
                )
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS runtime_dead_letter_audit_owner_idx
            ON runtime_dead_letter_audit(tenant_id, user_id, audit_sequence)
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_start_idempotency (
                scope TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                run_id TEXT NOT NULL,
                event_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY(scope, idempotency_key),
                FOREIGN KEY(run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_terminal_records (
                run_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                value_json TEXT,
                error_json TEXT,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_event_retention (
                run_id TEXT PRIMARY KEY,
                pruned_through_sequence INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_operations (
                operation_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                state TEXT NOT NULL,
                revision INTEGER NOT NULL,
                effect_class TEXT NOT NULL,
                fence_token INTEGER NOT NULL,
                intent_digest TEXT NOT NULL,
                record_json TEXT NOT NULL,
                prepared_event_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
            )
            """,
            """
            DROP INDEX IF EXISTS runtime_operations_recovery_idx
            """,
            """
            CREATE INDEX IF NOT EXISTS runtime_operations_dispatch_queue_idx
            ON runtime_operations(updated_at, operation_id)
            WHERE state IN ('planned', 'dispatching')
            """,
            """
            CREATE INDEX IF NOT EXISTS runtime_operations_run_reconcile_idx
            ON runtime_operations(run_id, operation_id)
            WHERE state IN ('unknown', 'succeeded', 'failed', 'cancelled')
               OR (state = 'dispatching'
                   AND effect_class IN ('compensatable', 'non_repeatable'))
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_artifacts (
                artifact_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                tenant_id TEXT,
                user_id TEXT,
                checksum TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                storage_key TEXT NOT NULL,
                reference_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                deleted_at TEXT,
                FOREIGN KEY(run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS runtime_artifacts_owner_idx
            ON runtime_artifacts(tenant_id, user_id, run_id, created_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_operation_artifacts (
                operation_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(operation_id, role),
                FOREIGN KEY(operation_id) REFERENCES runtime_operations(operation_id)
                    ON DELETE CASCADE,
                FOREIGN KEY(artifact_id) REFERENCES runtime_artifacts(artifact_id)
                    ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_operation_mutations (
                operation_id TEXT NOT NULL,
                mutation_id TEXT NOT NULL,
                mutation_digest TEXT NOT NULL,
                record_json TEXT NOT NULL,
                event_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(operation_id, mutation_id),
                FOREIGN KEY(operation_id) REFERENCES runtime_operations(operation_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_approval_requests (
                request_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                state TEXT NOT NULL,
                revision INTEGER NOT NULL,
                request_digest TEXT NOT NULL,
                record_json TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS runtime_approval_requests_run_idx
            ON runtime_approval_requests(run_id, state, updated_at, request_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS runtime_approval_requests_expiry_idx
            ON runtime_approval_requests(state, expires_at, request_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_approval_decisions (
                decision_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                decision_digest TEXT NOT NULL,
                record_json TEXT NOT NULL,
                event_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(request_id) REFERENCES runtime_approval_requests(request_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_authorization_grants (
                grant_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                nonce TEXT NOT NULL UNIQUE,
                grant_digest TEXT NOT NULL,
                record_json TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(request_id) REFERENCES runtime_approval_requests(request_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_children (
                child_run_id TEXT PRIMARY KEY,
                parent_run_id TEXT NOT NULL,
                root_run_id TEXT NOT NULL,
                child_depth INTEGER NOT NULL,
                delegation_id TEXT NOT NULL,
                spec_digest TEXT NOT NULL,
                spec_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(parent_run_id, delegation_id),
                FOREIGN KEY(child_run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE,
                FOREIGN KEY(parent_run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE,
                FOREIGN KEY(root_run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS runtime_children_parent_idx
            ON runtime_children(parent_run_id, created_at, child_run_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_execution_leases (
                lease_key TEXT PRIMARY KEY,
                lease_kind TEXT NOT NULL,
                run_id TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                claim_id TEXT NOT NULL,
                lease_token TEXT NOT NULL,
                fence_token INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                claim_event_json TEXT,
                FOREIGN KEY(run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS runtime_execution_leases_active_idx
            ON runtime_execution_leases(lease_kind, expires_at, lease_key)
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_scheduler_jobs (
                job_name TEXT PRIMARY KEY,
                revision INTEGER NOT NULL CHECK(revision >= 1),
                enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                next_run_at TEXT,
                job_digest TEXT NOT NULL,
                job_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS runtime_scheduler_jobs_due_idx
            ON runtime_scheduler_jobs(enabled, next_run_at, job_name)
            """,
            """
            CREATE TABLE IF NOT EXISTS runtime_scheduler_runs (
                run_id TEXT PRIMARY KEY,
                occurrence_id TEXT NOT NULL,
                job_name TEXT NOT NULL,
                job_revision INTEGER NOT NULL CHECK(job_revision >= 1),
                attempt INTEGER NOT NULL CHECK(attempt >= 1),
                status TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                available_at TEXT NOT NULL,
                runtime_run_id TEXT,
                concurrency_key TEXT NOT NULL,
                record_json TEXT NOT NULL,
                job_json TEXT NOT NULL,
                worker_id TEXT,
                claim_id TEXT,
                lease_token TEXT,
                fence_token INTEGER NOT NULL DEFAULT 0 CHECK(fence_token >= 0),
                acquired_at TEXT,
                renewed_at TEXT,
                lease_expires_at TEXT,
                released_at TEXT,
                UNIQUE(occurrence_id, attempt)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS runtime_scheduler_runs_ready_idx
            ON runtime_scheduler_runs(status, available_at, lease_expires_at, run_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS runtime_scheduler_runs_group_idx
            ON runtime_scheduler_runs(concurrency_key, status, lease_expires_at, run_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS runtime_scheduler_runs_history_idx
            ON runtime_scheduler_runs(job_name, scheduled_at DESC, attempt DESC)
            """,
        ]
        with self._transaction() as conn:
            for statement in statements:
                conn.execute(statement)
            run_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(runtime_runs)").fetchall()
            }
            if "authority_updated_at" not in run_columns:
                conn.execute("ALTER TABLE runtime_runs ADD COLUMN authority_updated_at TEXT")
            conn.execute(
                "UPDATE runtime_runs SET authority_updated_at = ? "
                "WHERE authority_updated_at IS NULL",
                (_now(),),
            )
            conn.execute("DROP INDEX IF EXISTS runtime_runs_recovery_idx")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS runtime_runs_executable_owner_idx
                ON runtime_runs(tenant_id, user_id, run_id, authority_updated_at)
                WHERE state IN ('queued', 'running', 'cancelling')
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS runtime_runs_reconciliation_owner_idx
                ON runtime_runs(tenant_id, user_id, authority_updated_at, run_id)
                WHERE state = 'waiting_for_reconciliation'
                """
            )
            event_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(runtime_events)").fetchall()
            }
            if "transition_id" not in event_columns:
                conn.execute("ALTER TABLE runtime_events ADD COLUMN transition_id TEXT")
            if "operation_id" not in event_columns:
                conn.execute("ALTER TABLE runtime_events ADD COLUMN operation_id TEXT")
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS runtime_events_transition_idx
                ON runtime_events(run_id, transition_id)
                WHERE transition_id IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS runtime_events_operation_idx
                ON runtime_events(run_id, operation_id, sequence)
                WHERE operation_id IS NOT NULL
                """
            )
            transition_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(runtime_transitions)").fetchall()
            }
            if "event_json" not in transition_columns:
                conn.execute("ALTER TABLE runtime_transitions ADD COLUMN event_json TEXT")
            idempotency_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(runtime_start_idempotency)").fetchall()
            }
            if "event_json" not in idempotency_columns:
                conn.execute("ALTER TABLE runtime_start_idempotency ADD COLUMN event_json TEXT")
            outbox_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(runtime_outbox)").fetchall()
            }
            if "dead_lettered_at" not in outbox_columns:
                conn.execute("ALTER TABLE runtime_outbox ADD COLUMN dead_lettered_at TEXT")
            if "dead_letter_reason_code" not in outbox_columns:
                conn.execute("ALTER TABLE runtime_outbox ADD COLUMN dead_letter_reason_code TEXT")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS runtime_outbox_ready_v8_idx
                ON runtime_outbox(available_at, lease_expires_at, outbox_id)
                WHERE delivered_at IS NULL AND dead_lettered_at IS NULL
                """
            )
            conn.execute(
                """
                UPDATE runtime_transitions
                SET event_json = (
                    SELECT event_json FROM runtime_events
                    WHERE runtime_events.run_id = runtime_transitions.run_id
                      AND runtime_events.transition_id = runtime_transitions.transition_id
                )
                WHERE event_json IS NULL
                """
            )
            conn.execute(
                """
                UPDATE runtime_start_idempotency
                SET event_json = (
                    SELECT event_json FROM runtime_events
                    WHERE runtime_events.run_id = runtime_start_idempotency.run_id
                      AND runtime_events.sequence = 1
                )
                WHERE event_json IS NULL
                """
            )
            for version in range(1, RUNTIME_SCHEMA_VERSION + 1):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO runtime_schema_migrations(version, applied_at)
                    VALUES (?, ?)
                    """,
                    (version, _now()),
                )

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM runtime_schema_migrations"
            ).fetchone()
        return int(row["version"])

    @staticmethod
    def _event_from_json(value: str) -> RuntimeEvent:
        return RuntimeEvent(**json.loads(value))

    @staticmethod
    def _owner_matches(snapshot: RunSnapshot, owner: RunOwner | None) -> bool:
        return owner is None or (
            snapshot.tenant_id == owner.tenant_id and snapshot.user_id == owner.user_id
        )

    def _get_run_in_transaction(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        *,
        owner: RunOwner | None = None,
    ) -> RunSnapshot:
        row = conn.execute(
            "SELECT snapshot_json FROM runtime_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        snapshot = _snapshot_from_json(row["snapshot_json"])
        if not self._owner_matches(snapshot, owner):
            raise RunNotFoundError(run_id)
        return snapshot

    def get_run(self, run_id: str, *, owner: RunOwner | None = None) -> RunSnapshot:
        with self._lock:
            return self._get_run_in_transaction(self._connection, run_id, owner=owner)

    def list_children(
        self,
        parent_run_id: str,
        *,
        limit: int = 64,
        owner: RunOwner | None = None,
    ) -> list[RunSnapshot]:
        if isinstance(limit, bool) or not 1 <= limit <= 64:
            raise ValueError("child list limit must be between 1 and 64")
        with self._lock:
            self._get_run_in_transaction(self._connection, parent_run_id, owner=owner)
            rows = self._connection.execute(
                """
                SELECT run.snapshot_json
                FROM runtime_children AS child
                JOIN runtime_runs AS run ON run.run_id = child.child_run_id
                WHERE child.parent_run_id = ?
                ORDER BY child.created_at, child.child_run_id
                LIMIT ?
                """,
                (parent_run_id, limit),
            ).fetchall()
        return [_snapshot_from_json(row["snapshot_json"]) for row in rows]

    def get_child_spec(
        self,
        child_run_id: str,
        *,
        owner: RunOwner | None = None,
    ) -> ChildRunSpec:
        with self._lock:
            self._get_run_in_transaction(self._connection, child_run_id, owner=owner)
            row = self._connection.execute(
                "SELECT spec_json FROM runtime_children WHERE child_run_id = ?",
                (child_run_id,),
            ).fetchone()
        if row is None:
            raise ChildRunContractError(
                code="CHILD_RUN_NOT_FOUND",
                message="The run is not a visible declared child.",
                details={"child_run_id": child_run_id},
            )
        return ChildRunSpec.from_dict(json.loads(row["spec_json"]))

    def _append_event(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        event_type: str,
        occurred_at: str,
        payload: Any,
        attempt_id: str | None = None,
        transition_id: str | None = None,
        operation_id: str | None = None,
        event_id: str | None = None,
    ) -> RuntimeEvent:
        row = conn.execute(
            "SELECT next_sequence FROM runtime_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        sequence = int(row["next_sequence"])
        event = RuntimeEvent(
            event_id=event_id or f"evt_{uuid4().hex}",
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            occurred_at=occurred_at,
            attempt_id=attempt_id,
            payload=payload,
        )
        event_json = _canonical_json(event.to_dict())
        conn.execute(
            """
            INSERT INTO runtime_events(
                run_id, sequence, event_id, event_type, transition_id, operation_id,
                occurred_at, event_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                sequence,
                event.event_id,
                event_type,
                transition_id,
                operation_id,
                occurred_at,
                event_json,
            ),
        )
        conn.execute(
            "UPDATE runtime_runs SET next_sequence = ? WHERE run_id = ?",
            (sequence + 1, run_id),
        )
        conn.execute(
            """
            INSERT INTO runtime_outbox(
                run_id, sequence, event_json, available_at
            ) VALUES (?, ?, ?, ?)
            """,
            (run_id, sequence, event_json, occurred_at),
        )
        return event

    def append_runtime_event(
        self,
        proposed: RuntimeEventInput,
        *,
        owner: RunOwner | None = None,
        artifact_reference: ArtifactReference | None = None,
    ) -> RuntimeEventAppendDecision:
        """Append one minimized event, optional artifact reference, and outbox atomically."""
        _require_event_artifact_binding(proposed, artifact_reference)
        with self._transaction() as conn:
            snapshot = self._get_run_in_transaction(
                conn,
                proposed.run_id,
                owner=owner,
            )
            existing_row = conn.execute(
                "SELECT event_json FROM runtime_events WHERE event_id = ?",
                (proposed.event_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._event_from_json(existing_row["event_json"])
                existing_value = {
                    "event_id": existing.event_id,
                    "run_id": existing.run_id,
                    "event_type": existing.event_type,
                    "occurred_at": existing.occurred_at,
                    "attempt_id": existing.attempt_id,
                    "payload": thaw_data(existing.payload),
                }
                if existing_value != proposed.semantic_value():
                    raise RuntimeEventIdempotencyConflictError(proposed.event_id)
                return RuntimeEventAppendDecision(
                    event=existing,
                    appended=False,
                    idempotent=True,
                )
            if snapshot.terminal:
                raise RuntimeEventTerminalRunError(proposed.run_id)
            if artifact_reference is not None:
                self._commit_runtime_artifact(
                    conn,
                    snapshot=snapshot,
                    reference=artifact_reference,
                )
                self._fault("runtime_event.after_artifact")
            event = self._append_event(
                conn,
                event_id=proposed.event_id,
                run_id=proposed.run_id,
                event_type=proposed.event_type,
                occurred_at=proposed.occurred_at,
                attempt_id=proposed.attempt_id,
                payload=proposed.payload,
            )
            self._fault("runtime_event.after_event")
            return RuntimeEventAppendDecision(event=event, appended=True)

    def create_run(
        self,
        snapshot: RunSnapshot,
        *,
        idempotency_scope: str | None = None,
        idempotency_key: str | None = None,
        request_digest: str | None = None,
        child_spec: ChildRunSpec | None = None,
    ) -> CreateRunDecision:
        supplied = (idempotency_scope, idempotency_key, request_digest)
        if any(item is not None for item in supplied) and not all(
            isinstance(item, str) and item.strip() for item in supplied
        ):
            raise ValueError(
                "idempotency_scope, idempotency_key, and request_digest must be supplied together"
            )
        if (snapshot.parent_run_id is None) != (child_spec is None):
            raise ChildRunContractError(
                code="CHILD_DECLARATION_REQUIRED",
                message=(
                    "Child lineage and its declared child specification must be supplied together."
                ),
            )
        if child_spec is not None and idempotency_scope is None:
            raise ChildRunContractError(
                code="CHILD_IDEMPOTENCY_REQUIRED",
                message="Declared child creation requires an owner-scoped delegation key.",
            )
        with self._transaction() as conn:
            if idempotency_scope is not None:
                existing = conn.execute(
                    """
                    SELECT request_digest, run_id, event_json
                    FROM runtime_start_idempotency
                    WHERE scope = ? AND idempotency_key = ?
                    """,
                    (idempotency_scope, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["request_digest"] != request_digest:
                        raise StartIdempotencyConflictError(
                            scope=idempotency_scope,
                            key=str(idempotency_key),
                        )
                    original = self._get_run_in_transaction(conn, existing["run_id"])
                    event_json = existing["event_json"]
                    if event_json is None:
                        first_event = conn.execute(
                            """
                            SELECT event_json FROM runtime_events
                            WHERE run_id = ? AND sequence = 1
                            """,
                            (original.run_id,),
                        ).fetchone()
                        event_json = first_event["event_json"]
                    return CreateRunDecision(
                        snapshot=original,
                        event=self._event_from_json(event_json),
                        created=False,
                        idempotent=True,
                    )
            if child_spec is not None:
                if (
                    snapshot.run_id != child_spec.child_run_id
                    or snapshot.parent_run_id != child_spec.parent_run_id
                    or snapshot.root_run_id != child_spec.root_run_id
                    or snapshot.child_depth != child_spec.depth
                ):
                    raise ChildRunContractError(
                        code="CHILD_SNAPSHOT_MISMATCH",
                        message="The child snapshot does not exactly bind its declaration.",
                    )
                parent = self._get_run_in_transaction(conn, child_spec.parent_run_id)
                existing_delegation = conn.execute(
                    """
                    SELECT child_run_id FROM runtime_children
                    WHERE parent_run_id = ? AND delegation_id = ?
                    """,
                    (parent.run_id, child_spec.delegation_id),
                ).fetchone()
                if existing_delegation is not None:
                    raise ChildRunContractError(
                        code="CHILD_DELEGATION_CONFLICT",
                        message="A parent delegation ID already names another child run.",
                        details={"parent_run_id": parent.run_id},
                    )
                parent_spec_row = conn.execute(
                    "SELECT spec_json FROM runtime_children WHERE child_run_id = ?",
                    (parent.run_id,),
                ).fetchone()
                parent_spec = (
                    ChildRunSpec.from_dict(json.loads(parent_spec_row["spec_json"]))
                    if parent_spec_row is not None
                    else None
                )
                direct = conn.execute(
                    "SELECT COUNT(*) AS count FROM runtime_children WHERE parent_run_id = ?",
                    (parent.run_id,),
                ).fetchone()
                child_spec.validate_parent(
                    parent,
                    child_owner=(snapshot.tenant_id, snapshot.user_id),
                    direct_children=int(direct["count"]),
                    parent_spec=parent_spec,
                )
            try:
                conn.execute(
                    """
                    INSERT INTO runtime_runs(
                        run_id, tenant_id, user_id, session_id, state, revision,
                        next_sequence, snapshot_json, created_at, updated_at,
                        authority_updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.run_id,
                        snapshot.tenant_id,
                        snapshot.user_id,
                        snapshot.session_id,
                        snapshot.state.value,
                        snapshot.revision,
                        _canonical_json(_snapshot_to_dict(snapshot)),
                        snapshot.created_at,
                        snapshot.updated_at,
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise HarnessError(
                    code="RUN_ALREADY_EXISTS",
                    category="lifecycle",
                    message=f"Run '{snapshot.run_id}' already exists.",
                    retryable=False,
                    details={"run_id": snapshot.run_id},
                ) from exc
            event = self._append_event(
                conn,
                run_id=snapshot.run_id,
                event_type="run.created",
                occurred_at=snapshot.created_at,
                payload={
                    "state": snapshot.state.value,
                    "revision": snapshot.revision,
                    **(
                        {
                            "parent_run_id": child_spec.parent_run_id,
                            "root_run_id": child_spec.root_run_id,
                            "child_depth": child_spec.depth,
                            "delegation_digest": child_spec.digest,
                        }
                        if child_spec is not None
                        else {}
                    ),
                },
            )
            self._fault("create.after_event")
            if child_spec is not None:
                conn.execute(
                    """
                    INSERT INTO runtime_children(
                        child_run_id, parent_run_id, root_run_id, child_depth,
                        delegation_id, spec_digest, spec_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        child_spec.child_run_id,
                        child_spec.parent_run_id,
                        child_spec.root_run_id,
                        child_spec.depth,
                        child_spec.delegation_id,
                        child_spec.digest,
                        _canonical_json(child_spec.to_dict()),
                        snapshot.created_at,
                    ),
                )
                self._fault("create_child.after_relation")
                self._append_event(
                    conn,
                    run_id=child_spec.parent_run_id,
                    event_type="run.child.created",
                    occurred_at=snapshot.created_at,
                    payload={
                        "child_run_id": child_spec.child_run_id,
                        "root_run_id": child_spec.root_run_id,
                        "child_depth": child_spec.depth,
                        "purpose_code": child_spec.purpose_code,
                        "delegation_digest": child_spec.digest,
                        "join_policy": child_spec.join_policy.value,
                        "cancellation_policy": child_spec.cancellation_policy.value,
                    },
                )
                self._fault("create_child.after_parent_event")
            if idempotency_scope is not None:
                conn.execute(
                    """
                    INSERT INTO runtime_start_idempotency(
                        scope, idempotency_key, request_digest, run_id,
                        event_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        idempotency_scope,
                        idempotency_key,
                        request_digest,
                        snapshot.run_id,
                        _canonical_json(event.to_dict()),
                        snapshot.created_at,
                    ),
                )
            return CreateRunDecision(snapshot=snapshot, event=event, created=True)

    @staticmethod
    def _decision_to_json(decision: TransitionDecision) -> str:
        return _canonical_json(
            {
                "before": _snapshot_to_dict(decision.before),
                "after": _snapshot_to_dict(decision.after),
                "applied": decision.applied,
                "idempotent": decision.idempotent,
            }
        )

    @staticmethod
    def _decision_from_json(
        value: str,
        transition: LifecycleTransition,
    ) -> TransitionDecision:
        payload = json.loads(value)
        return TransitionDecision(
            before=RunSnapshot(**payload["before"]),
            after=RunSnapshot(**payload["after"]),
            transition=transition,
            applied=bool(payload["applied"]),
            idempotent=bool(payload["idempotent"]),
        )

    def _validate_child_join(self, conn: sqlite3.Connection, parent_run_id: str) -> None:
        rows = conn.execute(
            """
            SELECT run.snapshot_json, child.spec_json
            FROM runtime_children AS child
            JOIN runtime_runs AS run ON run.run_id = child.child_run_id
            WHERE child.parent_run_id = ?
            """,
            (parent_run_id,),
        ).fetchall()
        pending = []
        failed = []
        for row in rows:
            child = _snapshot_from_json(row["snapshot_json"])
            spec = ChildRunSpec.from_dict(json.loads(row["spec_json"]))
            if not child.terminal:
                pending.append(child.run_id)
            elif (
                spec.join_policy is ChildJoinPolicy.ALL_SUCCESS
                and child.state is not RunState.COMPLETED
            ):
                failed.append(child.run_id)
        if pending:
            raise ChildRunContractError(
                code="CHILD_JOIN_PENDING",
                message="The parent cannot complete while required children are unsettled.",
                details={"parent_run_id": parent_run_id, "pending_count": len(pending)},
            )
        if failed:
            raise ChildRunContractError(
                code="CHILD_JOIN_FAILED",
                message="An all-success child join contains a non-success terminal child.",
                details={"parent_run_id": parent_run_id, "failed_count": len(failed)},
            )

    def _request_descendant_cancellation(
        self,
        conn: sqlite3.Connection,
        parent: RunSnapshot,
        transition: LifecycleTransition,
    ) -> None:
        rows = conn.execute(
            """
            WITH RECURSIVE descendants(child_run_id, child_depth) AS (
                SELECT child_run_id, child_depth FROM runtime_children
                WHERE parent_run_id = ?
                UNION ALL
                SELECT child.child_run_id, child.child_depth
                FROM runtime_children AS child
                JOIN descendants ON child.parent_run_id = descendants.child_run_id
            )
            SELECT run.snapshot_json
            FROM descendants
            JOIN runtime_runs AS run ON run.run_id = descendants.child_run_id
            ORDER BY descendants.child_depth, descendants.child_run_id
            """,
            (parent.run_id,),
        ).fetchall()
        for row in rows:
            child = _snapshot_from_json(row["snapshot_json"])
            if child.terminal or child.state in {
                RunState.CANCELLING,
                RunState.WAITING_FOR_RECONCILIATION,
            }:
                continue
            child_transition = LifecycleTransition(
                run_id=child.run_id,
                kind=TransitionKind.REQUEST_CANCEL,
                transition_id=f"{transition.transition_id}:child:{child.run_id}",
                occurred_at=transition.occurred_at,
                reason_code="PARENT_CANCELLATION_PROPAGATED",
            )
            decision = reduce_lifecycle(child, child_transition)
            conn.execute(
                """
                UPDATE runtime_runs
                SET state = ?, revision = ?, snapshot_json = ?, updated_at = ?,
                    authority_updated_at = ?
                WHERE run_id = ? AND revision = ?
                """,
                (
                    decision.after.state.value,
                    decision.after.revision,
                    _canonical_json(_snapshot_to_dict(decision.after)),
                    decision.after.updated_at,
                    _now(),
                    child.run_id,
                    child.revision,
                ),
            )
            event = self._append_event(
                conn,
                run_id=child.run_id,
                event_type="run.state.changed",
                occurred_at=child_transition.occurred_at,
                transition_id=child_transition.transition_id,
                payload={
                    "transition_id": child_transition.transition_id,
                    "transition": child_transition.kind.value,
                    "before": child.state.value,
                    "after": decision.after.state.value,
                    "revision": decision.after.revision,
                    "reason_code": child_transition.reason_code,
                },
            )
            conn.execute(
                """
                INSERT INTO runtime_transitions(
                    run_id, transition_id, transition_digest, decision_json,
                    event_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    child.run_id,
                    child_transition.transition_id,
                    _settlement_digest(child_transition, None),
                    self._decision_to_json(decision),
                    _canonical_json(event.to_dict()),
                    child_transition.occurred_at,
                ),
            )
            if child.state is RunState.WAITING_FOR_APPROVAL:
                self._cancel_pending_approval(conn, child, child_transition)
        self._fault("child_cancellation.after_descendants")

    def _append_child_settled(
        self,
        conn: sqlite3.Connection,
        child: RunSnapshot,
        transition: LifecycleTransition,
        terminal_state: RunState,
    ) -> None:
        if child.parent_run_id is None:
            return
        spec_row = conn.execute(
            "SELECT spec_json FROM runtime_children WHERE child_run_id = ?",
            (child.run_id,),
        ).fetchone()
        if spec_row is None:
            raise ChildRunContractError(
                code="CHILD_RELATION_MISSING",
                message="A child terminal transition is missing its durable relation.",
            )
        spec = ChildRunSpec.from_dict(json.loads(spec_row["spec_json"]))
        self._append_event(
            conn,
            run_id=child.parent_run_id,
            event_type="run.child.settled",
            occurred_at=transition.occurred_at,
            payload={
                "child_run_id": child.run_id,
                "child_state": terminal_state.value,
                "child_depth": child.child_depth,
                "delegation_digest": spec.digest,
                "join_policy": spec.join_policy.value,
            },
        )
        self._fault("child_settlement.after_parent_event")

    def _approval_wait_record(
        self,
        conn: sqlite3.Connection,
        snapshot: RunSnapshot,
    ) -> ApprovalRecord:
        request_id = snapshot.pending_request_id
        if request_id is None:
            raise HarnessError(
                code="APPROVAL_EVIDENCE_MISSING",
                category="approval",
                message="Approval-waiting run is missing its request binding.",
                retryable=False,
                details={"run_id": snapshot.run_id},
            )
        row = conn.execute(
            "SELECT record_json FROM runtime_approval_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise HarnessError(
                code="APPROVAL_EVIDENCE_MISSING",
                category="approval",
                message="Approval-waiting run is missing durable request evidence.",
                retryable=False,
                details={"request_id": request_id},
            )
        return self._approval_from_json(row["record_json"])

    def _validate_approval_response(
        self,
        conn: sqlite3.Connection,
        snapshot: RunSnapshot,
        transition: LifecycleTransition,
    ) -> None:
        record = self._approval_wait_record(conn, snapshot)
        payload = thaw_data(transition.payload)
        expected_digest = record.decision.digest if record.decision is not None else None
        if (
            record.state is ApprovalState.PENDING
            or payload.get("approval_state") != record.state.value
            or payload.get("decision_digest") != expected_digest
        ):
            raise HarnessError(
                code="APPROVAL_DECISION_REQUIRED",
                category="approval",
                message="The pending approval must settle before the run can respond.",
                retryable=False,
                details={"request_id": record.request.request_id},
            )

    def _cancel_pending_approval(
        self,
        conn: sqlite3.Connection,
        snapshot: RunSnapshot,
        transition: LifecycleTransition,
    ) -> None:
        record = self._approval_wait_record(conn, snapshot)
        if record.state is not ApprovalState.PENDING:
            return
        decision = ApprovalDecision(
            decision_id=f"{transition.transition_id}:approval-cancel",
            request_id=record.request.request_id,
            request_digest=record.request.digest,
            request_nonce=record.request.nonce,
            approved=False,
            issuer="agnoclaw:lifecycle-v1",
            reason_code=transition.reason_code or "RUN_LEFT_APPROVAL_WAIT",
            decided_at=transition.occurred_at,
        )
        after = reduce_approval_cancellation(
            record,
            decision,
            occurred_at=transition.occurred_at,
        )
        conn.execute(
            """
            UPDATE runtime_approval_requests
            SET state = ?, revision = ?, record_json = ?, updated_at = ?
            WHERE request_id = ? AND revision = ? AND state = ?
            """,
            (
                after.state.value,
                after.revision,
                _canonical_json(after.to_dict()),
                after.updated_at,
                record.request.request_id,
                record.revision,
                ApprovalState.PENDING.value,
            ),
        )
        event = self._append_event(
            conn,
            run_id=snapshot.run_id,
            event_type="approval.cancelled",
            occurred_at=transition.occurred_at,
            payload={
                "request_id": record.request.request_id,
                "decision_id": decision.decision_id,
                "state": after.state.value,
                "revision": after.revision,
                "reason_code": decision.reason_code,
            },
        )
        conn.execute(
            """
            INSERT INTO runtime_approval_decisions(
                decision_id, request_id, decision_digest, record_json,
                event_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                decision.decision_id,
                decision.request_id,
                decision.digest,
                _canonical_json(decision.to_dict()),
                _canonical_json(event.to_dict()),
                decision.decided_at,
            ),
        )

    def apply_transition(
        self,
        transition: LifecycleTransition,
        *,
        expected_revision: int,
        terminal: TerminalRecord | None = None,
        approval_request: ApprovalRequest | None = None,
    ) -> StoredTransitionDecision:
        if terminal is not None and terminal.run_id != transition.run_id:
            raise ValueError("terminal record and transition run_id must match")
        is_approval_wait = transition.kind is TransitionKind.WAIT_FOR_APPROVAL
        if is_approval_wait != (approval_request is not None):
            raise ValueError("wait-for-approval transitions require exactly one approval request")
        if approval_request is not None and (
            approval_request.run_id != transition.run_id
            or approval_request.request_id != transition.pending_request_id
        ):
            raise ValueError("approval request must match the transition run and pending request")
        settlement_digest = _settlement_digest(transition, terminal)
        with self._transaction() as conn:
            prior = conn.execute(
                """
                SELECT transition_digest, decision_json, event_json
                FROM runtime_transitions
                WHERE run_id = ? AND transition_id = ?
                """,
                (transition.run_id, transition.transition_id),
            ).fetchone()
            if prior is not None:
                if prior["transition_digest"] != settlement_digest:
                    raise LifecycleIdempotencyConflictError(
                        run_id=transition.run_id,
                        transition_id=transition.transition_id,
                    )
                decision = self._decision_from_json(prior["decision_json"], transition)
                if approval_request is not None:
                    approval_row = conn.execute(
                        """
                        SELECT request_digest FROM runtime_approval_requests
                        WHERE request_id = ? AND run_id = ?
                        """,
                        (approval_request.request_id, approval_request.run_id),
                    ).fetchone()
                    if (
                        approval_row is None
                        or approval_row["request_digest"] != approval_request.digest
                    ):
                        raise LifecycleIdempotencyConflictError(
                            run_id=transition.run_id,
                            transition_id=transition.transition_id,
                        )
                event_json = prior["event_json"]
                if event_json is None:
                    event_row = conn.execute(
                        """
                        SELECT event_json FROM runtime_events
                        WHERE run_id = ? AND transition_id = ?
                        """,
                        (transition.run_id, transition.transition_id),
                    ).fetchone()
                    event_json = event_row["event_json"]
                return StoredTransitionDecision(
                    lifecycle=TransitionDecision(
                        before=decision.before,
                        after=decision.after,
                        transition=transition,
                        applied=False,
                        idempotent=True,
                    ),
                    event=self._event_from_json(event_json),
                )
            snapshot = self._get_run_in_transaction(conn, transition.run_id)
            if snapshot.revision != expected_revision:
                raise RunRevisionConflictError(
                    run_id=transition.run_id,
                    expected=expected_revision,
                    actual=snapshot.revision,
                )
            if (
                snapshot.state is RunState.WAITING_FOR_APPROVAL
                and transition.kind is TransitionKind.RESPOND
            ):
                self._validate_approval_response(conn, snapshot, transition)
            if transition.kind is TransitionKind.COMPLETE:
                self._validate_child_join(conn, snapshot.run_id)
            decision = reduce_lifecycle(snapshot, transition)
            if terminal is not None and terminal.state != decision.after.state:
                raise ValueError("terminal record state must match the transition target")
            if decision.after.state in TERMINAL_RUN_STATES and terminal is None:
                raise ValueError("terminal transitions require a terminal record")
            if decision.after.state not in TERMINAL_RUN_STATES and terminal is not None:
                raise ValueError("non-terminal transitions cannot include a terminal record")
            if transition.kind in {
                TransitionKind.REQUEST_CANCEL,
                TransitionKind.FAIL,
                TransitionKind.FAIL_WITH_UNKNOWN_EFFECTS,
                TransitionKind.EXPIRE,
            }:
                self._request_descendant_cancellation(conn, snapshot, transition)
            updated = conn.execute(
                """
                UPDATE runtime_runs
                SET state = ?, revision = ?, snapshot_json = ?, updated_at = ?,
                    authority_updated_at = ?
                WHERE run_id = ? AND revision = ?
                """,
                (
                    decision.after.state.value,
                    decision.after.revision,
                    _canonical_json(_snapshot_to_dict(decision.after)),
                    decision.after.updated_at,
                    _now(),
                    transition.run_id,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                current = self._get_run_in_transaction(conn, transition.run_id)
                raise RunRevisionConflictError(
                    run_id=transition.run_id,
                    expected=expected_revision,
                    actual=current.revision,
                )
            self._fault("transition.after_state")
            if terminal is not None:
                conn.execute(
                    """
                    INSERT INTO runtime_terminal_records(
                        run_id, state, value_json, error_json, recorded_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        terminal.run_id,
                        terminal.state.value,
                        (
                            _canonical_json(thaw_data(terminal.value))
                            if terminal.value is not None
                            else None
                        ),
                        (
                            _canonical_json(thaw_data(terminal.error))
                            if terminal.error is not None
                            else None
                        ),
                        terminal.recorded_at,
                    ),
                )
            event = self._append_event(
                conn,
                run_id=transition.run_id,
                event_type="run.state.changed",
                occurred_at=transition.occurred_at,
                payload={
                    "transition_id": transition.transition_id,
                    "transition": transition.kind.value,
                    "before": snapshot.state.value,
                    "after": decision.after.state.value,
                    "revision": decision.after.revision,
                    "reason_code": transition.reason_code,
                },
                transition_id=transition.transition_id,
            )
            conn.execute(
                """
                INSERT INTO runtime_transitions(
                    run_id, transition_id, transition_digest, decision_json,
                    event_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    transition.run_id,
                    transition.transition_id,
                    settlement_digest,
                    self._decision_to_json(decision),
                    _canonical_json(event.to_dict()),
                    transition.occurred_at,
                ),
            )
            if approval_request is not None:
                approval_record = ApprovalRecord(
                    request=approval_request,
                    updated_at=approval_request.requested_at,
                )
                try:
                    conn.execute(
                        """
                        INSERT INTO runtime_approval_requests(
                            request_id, run_id, state, revision, request_digest,
                            record_json, expires_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            approval_request.request_id,
                            approval_request.run_id,
                            approval_record.state.value,
                            approval_record.revision,
                            approval_request.digest,
                            _canonical_json(approval_record.to_dict()),
                            approval_request.expires_at,
                            approval_request.requested_at,
                            approval_record.updated_at,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise LifecycleIdempotencyConflictError(
                        run_id=transition.run_id,
                        transition_id=transition.transition_id,
                    ) from exc
                self._append_event(
                    conn,
                    run_id=approval_request.run_id,
                    event_type="approval.requested",
                    occurred_at=approval_request.requested_at,
                    payload={
                        "request_id": approval_request.request_id,
                        "call_id": approval_request.call_id,
                        "capability_id": approval_request.capability_id,
                        "capability_digest": approval_request.capability_digest,
                        "effect_category": approval_request.effect_category,
                        "argument_digest": approval_request.argument_digest,
                        "policy_version": approval_request.policy_version,
                        "authority_digest": approval_request.authority_digest,
                        "request_digest": approval_request.digest,
                        "expires_at": approval_request.expires_at,
                        "reason_code": approval_request.reason_code,
                    },
                )
                self._fault("approval.request.after_event")
            if (
                snapshot.state is RunState.WAITING_FOR_APPROVAL
                and decision.after.state is not RunState.WAITING_FOR_APPROVAL
                and transition.kind is not TransitionKind.RESPOND
            ):
                self._cancel_pending_approval(conn, snapshot, transition)
            if decision.after.state in TERMINAL_RUN_STATES:
                self._append_child_settled(
                    conn,
                    snapshot,
                    transition,
                    decision.after.state,
                )
            self._fault("transition.after_event")
            return StoredTransitionDecision(lifecycle=decision, event=event)

    @staticmethod
    def _approval_from_json(value: str) -> ApprovalRecord:
        return ApprovalRecord.from_dict(json.loads(value))

    def _get_approval_in_transaction(
        self,
        conn: sqlite3.Connection,
        request_id: str,
        *,
        owner: RunOwner | None = None,
    ) -> ApprovalRecord:
        row = conn.execute(
            "SELECT record_json FROM runtime_approval_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise ApprovalNotFoundError(request_id)
        record = self._approval_from_json(row["record_json"])
        try:
            self._get_run_in_transaction(conn, record.request.run_id, owner=owner)
        except RunNotFoundError as exc:
            raise ApprovalNotFoundError(request_id) from exc
        return record

    def get_approval(
        self,
        request_id: str,
        *,
        owner: RunOwner | None = None,
    ) -> ApprovalRecord:
        with self._lock:
            return self._get_approval_in_transaction(
                self._connection,
                request_id,
                owner=owner,
            )

    def list_approvals(
        self,
        run_id: str,
        *,
        states: tuple[ApprovalState, ...] | None = None,
        limit: int = 100,
        owner: RunOwner | None = None,
    ) -> list[ApprovalRecord]:
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        normalized_states = tuple(ApprovalState(state) for state in (states or ()))
        with self._lock:
            self._get_run_in_transaction(self._connection, run_id, owner=owner)
            query = "SELECT record_json FROM runtime_approval_requests WHERE run_id = ?"
            params: list[Any] = [run_id]
            if normalized_states:
                placeholders = ",".join("?" for _ in normalized_states)
                query += f" AND state IN ({placeholders})"
                params.extend(state.value for state in normalized_states)
            query += " ORDER BY created_at, request_id LIMIT ?"
            params.append(limit)
            rows = self._connection.execute(query, tuple(params)).fetchall()
        return [self._approval_from_json(row["record_json"]) for row in rows]

    def _settle_approval(
        self,
        decision: ApprovalDecision,
        *,
        expected_revision: int,
        grant: AuthorizationGrant | None,
        owner: RunOwner | None,
        expiry: bool,
    ) -> StoredApprovalDecision:
        with self._transaction() as conn:
            prior = conn.execute(
                """
                SELECT request_id, decision_digest, event_json
                FROM runtime_approval_decisions WHERE decision_id = ?
                """,
                (decision.decision_id,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["request_id"] != decision.request_id
                    or prior["decision_digest"] != decision.digest
                ):
                    raise ApprovalIdempotencyConflictError(
                        request_id=decision.request_id,
                        decision_id=decision.decision_id,
                    )
                record = self._get_approval_in_transaction(
                    conn,
                    decision.request_id,
                    owner=owner,
                )
                return StoredApprovalDecision(
                    record=record,
                    event=self._event_from_json(prior["event_json"]),
                    applied=False,
                    idempotent=True,
                )

            record = self._get_approval_in_transaction(
                conn,
                decision.request_id,
                owner=owner,
            )
            run = self._get_run_in_transaction(
                conn,
                record.request.run_id,
                owner=owner,
            )
            if (
                run.state is not RunState.WAITING_FOR_APPROVAL
                or run.pending_request_id != decision.request_id
            ):
                raise HarnessError(
                    code="APPROVAL_RUN_NOT_WAITING",
                    category="approval",
                    message="The run is no longer waiting for this approval.",
                    retryable=False,
                    details={"request_id": decision.request_id},
                )
            if record.revision != expected_revision:
                raise ApprovalRevisionConflictError(
                    request_id=decision.request_id,
                    expected=expected_revision,
                    actual=record.revision,
                )
            if record.state is not ApprovalState.PENDING:
                raise ApprovalAlreadySettledError(
                    request_id=decision.request_id,
                    state=record.state,
                )
            occurred_at = _now()
            if expiry:
                if grant is not None:
                    raise ValueError("expired approvals cannot include a grant")
                after = reduce_approval_expiry(
                    record,
                    decision,
                    occurred_at=occurred_at,
                )
            else:
                after = reduce_approval_settlement(
                    record,
                    decision,
                    grant=grant,
                    occurred_at=occurred_at,
                )
            updated = conn.execute(
                """
                UPDATE runtime_approval_requests
                SET state = ?, revision = ?, record_json = ?, updated_at = ?
                WHERE request_id = ? AND revision = ? AND state = ?
                """,
                (
                    after.state.value,
                    after.revision,
                    _canonical_json(after.to_dict()),
                    after.updated_at,
                    decision.request_id,
                    expected_revision,
                    ApprovalState.PENDING.value,
                ),
            )
            if updated.rowcount != 1:
                current = self._get_approval_in_transaction(
                    conn,
                    decision.request_id,
                    owner=owner,
                )
                raise ApprovalRevisionConflictError(
                    request_id=decision.request_id,
                    expected=expected_revision,
                    actual=current.revision,
                )
            self._fault("approval.settle.after_state")
            if after.grant is not None:
                try:
                    conn.execute(
                        """
                        INSERT INTO runtime_authorization_grants(
                            grant_id, request_id, nonce, grant_digest, record_json,
                            expires_at, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            after.grant.grant_id,
                            decision.request_id,
                            after.grant.nonce,
                            after.grant.digest,
                            _canonical_json(after.grant.to_dict()),
                            after.grant.expires_at,
                            after.grant.issued_at,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise HarnessError(
                        code="AUTHORIZATION_GRANT_REPLAY",
                        category="authorization",
                        message="Authorization grant evidence was already consumed.",
                        retryable=False,
                        details={"request_id": decision.request_id},
                    ) from exc
            event = self._append_event(
                conn,
                run_id=record.request.run_id,
                event_type=f"approval.{after.state.value}",
                occurred_at=after.updated_at,
                payload={
                    "request_id": decision.request_id,
                    "decision_id": decision.decision_id,
                    "state": after.state.value,
                    "revision": after.revision,
                    "reason_code": decision.reason_code,
                    "issuer_digest": (
                        "sha256:" + hashlib.sha256(decision.issuer.encode("utf-8")).hexdigest()
                    ),
                    "grant_digest": (after.grant.digest if after.grant is not None else None),
                },
            )
            conn.execute(
                """
                INSERT INTO runtime_approval_decisions(
                    decision_id, request_id, decision_digest, record_json,
                    event_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.request_id,
                    decision.digest,
                    _canonical_json(decision.to_dict()),
                    _canonical_json(event.to_dict()),
                    decision.decided_at,
                ),
            )
            self._fault("approval.settle.after_event")
            return StoredApprovalDecision(
                record=after,
                event=event,
                applied=True,
            )

    def settle_approval(
        self,
        decision: ApprovalDecision,
        *,
        expected_revision: int,
        grant: AuthorizationGrant | None = None,
        owner: RunOwner | None = None,
    ) -> StoredApprovalDecision:
        return self._settle_approval(
            decision,
            expected_revision=expected_revision,
            grant=grant,
            owner=owner,
            expiry=False,
        )

    def expire_approval(
        self,
        decision: ApprovalDecision,
        *,
        expected_revision: int,
        owner: RunOwner | None = None,
    ) -> StoredApprovalDecision:
        return self._settle_approval(
            decision,
            expected_revision=expected_revision,
            grant=None,
            owner=owner,
            expiry=True,
        )

    @staticmethod
    def _operation_from_json(value: str) -> OperationRecord:
        return OperationRecord.from_dict(json.loads(value))

    def _get_operation_in_transaction(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        *,
        owner: RunOwner | None = None,
    ) -> OperationRecord:
        row = conn.execute(
            "SELECT record_json FROM runtime_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise OperationNotFoundError(operation_id)
        record = self._operation_from_json(row["record_json"])
        try:
            self._get_run_in_transaction(conn, record.intent.run_id, owner=owner)
        except RunNotFoundError as exc:
            raise OperationNotFoundError(operation_id) from exc
        return record

    def get_operation(
        self,
        operation_id: str,
        *,
        owner: RunOwner | None = None,
    ) -> OperationRecord:
        with self._lock:
            return self._get_operation_in_transaction(
                self._connection,
                operation_id,
                owner=owner,
            )

    def list_run_operations(
        self,
        run_id: str,
        *,
        limit: int = 100,
        owner: RunOwner | None = None,
    ) -> list[OperationRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            self._get_run_in_transaction(self._connection, run_id, owner=owner)
            rows = self._connection.execute(
                "SELECT record_json FROM runtime_operations WHERE run_id = ? "
                "ORDER BY operation_id LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [self._operation_from_json(row["record_json"]) for row in rows]

    def prepare_operation(self, intent: OperationIntent) -> StoredOperationDecision:
        with self._transaction() as conn:
            existing = conn.execute(
                """
                SELECT intent_digest, record_json, prepared_event_json
                FROM runtime_operations WHERE operation_id = ?
                """,
                (intent.operation_id,),
            ).fetchone()
            if existing is not None:
                if existing["intent_digest"] != intent.digest:
                    raise OperationIdempotencyConflictError(
                        operation_id=intent.operation_id,
                        mutation_id="prepare",
                    )
                return StoredOperationDecision(
                    record=self._operation_from_json(existing["record_json"]),
                    event=self._event_from_json(existing["prepared_event_json"]),
                    applied=False,
                    idempotent=True,
                )
            snapshot = self._get_run_in_transaction(conn, intent.run_id)
            if snapshot.terminal:
                raise HarnessError(
                    code="OPERATION_RUN_TERMINAL",
                    category="operation",
                    message="A new operation cannot be prepared for a terminal run.",
                    retryable=False,
                    details={"run_id": intent.run_id, "operation_id": intent.operation_id},
                )
            record = OperationRecord(intent=intent, updated_at=intent.prepared_at)
            event = self._append_event(
                conn,
                run_id=intent.run_id,
                event_type="operation.planned",
                occurred_at=intent.prepared_at,
                attempt_id=intent.attempt_id,
                operation_id=intent.operation_id,
                payload={
                    "operation_id": intent.operation_id,
                    "kind": intent.kind.value,
                    "target": intent.target,
                    "effect_class": intent.effect_class.value,
                    "request_digest": intent.request_digest,
                    "result_slot_id": intent.result_slot_id,
                    "revision": record.revision,
                },
            )
            conn.execute(
                """
                INSERT INTO runtime_operations(
                    operation_id, run_id, attempt_id, state, revision, effect_class,
                    fence_token, intent_digest, record_json, prepared_event_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.operation_id,
                    intent.run_id,
                    intent.attempt_id,
                    record.state.value,
                    record.revision,
                    intent.effect_class.value,
                    record.fence_token,
                    intent.digest,
                    _canonical_json(record.to_dict()),
                    _canonical_json(event.to_dict()),
                    intent.prepared_at,
                    record.updated_at,
                ),
            )
            self._fault("operation.after_prepare")
            return StoredOperationDecision(record=record, event=event, applied=True)

    def _mutate_operation(
        self,
        operation_id: str,
        *,
        mutation_id: str,
        mutation_kind: str,
        mutation_payload: Any,
        expected_revision: int,
        reducer: Callable[[OperationRecord], OperationRecord],
        event_type: str,
        artifact_reference: ArtifactReference | None = None,
        owner: RunOwner | None = None,
        reconciliation: OperationReconciliation | None = None,
        evidence_artifacts: tuple[ArtifactReference, ...] = (),
    ) -> StoredOperationDecision:
        if not isinstance(mutation_id, str) or not mutation_id.strip():
            raise ValueError("mutation_id must be a non-empty string")
        digest = _operation_mutation_digest(mutation_kind, mutation_payload)
        with self._transaction() as conn:
            if owner is not None:
                self._get_operation_in_transaction(conn, operation_id, owner=owner)
            prior = conn.execute(
                """
                SELECT mutation_digest, record_json, event_json
                FROM runtime_operation_mutations
                WHERE operation_id = ? AND mutation_id = ?
                """,
                (operation_id, mutation_id),
            ).fetchone()
            if prior is not None:
                if prior["mutation_digest"] != digest:
                    raise OperationIdempotencyConflictError(
                        operation_id=operation_id,
                        mutation_id=mutation_id,
                    )
                return StoredOperationDecision(
                    record=self._operation_from_json(prior["record_json"]),
                    event=self._event_from_json(prior["event_json"]),
                    applied=False,
                    idempotent=True,
                )
            current = self._get_operation_in_transaction(conn, operation_id, owner=owner)
            if current.revision != expected_revision:
                raise OperationRevisionConflictError(
                    operation_id=operation_id,
                    expected=expected_revision,
                    actual=current.revision,
                )
            updated_record = reducer(current)
            if reconciliation is not None:
                self._commit_reconciliation_artifacts(
                    conn,
                    record=updated_record,
                    reconciliation=reconciliation,
                    references=evidence_artifacts,
                )
            updated = conn.execute(
                """
                UPDATE runtime_operations
                SET state = ?, revision = ?, fence_token = ?, record_json = ?, updated_at = ?
                WHERE operation_id = ? AND revision = ?
                """,
                (
                    updated_record.state.value,
                    updated_record.revision,
                    updated_record.fence_token,
                    _canonical_json(updated_record.to_dict()),
                    updated_record.updated_at,
                    operation_id,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                actual = self._get_operation_in_transaction(conn, operation_id)
                raise OperationRevisionConflictError(
                    operation_id=operation_id,
                    expected=expected_revision,
                    actual=actual.revision,
                )
            if artifact_reference is not None:
                self._commit_operation_artifact(
                    conn,
                    record=updated_record,
                    reference=artifact_reference,
                )
            event = self._append_event(
                conn,
                run_id=updated_record.intent.run_id,
                event_type=event_type,
                occurred_at=updated_record.updated_at,
                attempt_id=updated_record.intent.attempt_id,
                operation_id=operation_id,
                payload={
                    "operation_id": operation_id,
                    "state": updated_record.state.value,
                    "revision": updated_record.revision,
                    "dispatch_attempt": updated_record.dispatch_attempt,
                    "fence_token": updated_record.fence_token,
                    "result_slot_id": updated_record.intent.result_slot_id,
                    **operation_settlement_measurements(updated_record.settlement),
                    **(
                        {
                            "reconciliation_id": reconciliation.reconciliation_id,
                            "verdict": reconciliation.verdict.value,
                            "observer_digest": reconciliation.observer_digest,
                            "evidence_artifact_ids": list(reconciliation.evidence_artifact_ids),
                        }
                        if reconciliation is not None
                        else {}
                    ),
                },
            )
            conn.execute(
                """
                INSERT INTO runtime_operation_mutations(
                    operation_id, mutation_id, mutation_digest,
                    record_json, event_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    mutation_id,
                    digest,
                    _canonical_json(updated_record.to_dict()),
                    _canonical_json(event.to_dict()),
                    updated_record.updated_at,
                ),
            )
            self._fault(f"operation.after_{mutation_kind}")
            return StoredOperationDecision(
                record=updated_record,
                event=event,
                applied=True,
            )

    def _commit_runtime_artifact(
        self,
        conn: sqlite3.Connection,
        *,
        snapshot: RunSnapshot,
        reference: ArtifactReference,
    ) -> None:
        if reference.scope != ArtifactScope(
            run_id=snapshot.run_id,
            tenant_id=snapshot.tenant_id,
            user_id=snapshot.user_id,
        ):
            raise HarnessError(
                code="ARTIFACT_SCOPE_MISMATCH",
                category="artifact",
                message="Artifact scope does not match the authoritative run owner.",
                retryable=False,
                details={"run_id": snapshot.run_id},
            )
        encoded = _canonical_json(reference.to_dict())
        existing = conn.execute(
            "SELECT reference_json FROM runtime_artifacts WHERE artifact_id = ?",
            (reference.artifact_id,),
        ).fetchone()
        if existing is not None:
            prior_reference = ArtifactReference.from_dict(json.loads(existing["reference_json"]))
            if prior_reference.storage_identity_digest != reference.storage_identity_digest:
                raise HarnessError(
                    code="ARTIFACT_IDEMPOTENCY_CONFLICT",
                    category="artifact",
                    message="Artifact address was reused with different immutable metadata.",
                    retryable=False,
                    details={"artifact_id": reference.artifact_id},
                )
        conn.execute(
            """
            INSERT INTO runtime_artifacts(
                artifact_id, run_id, tenant_id, user_id, checksum, size_bytes,
                storage_key, reference_json, created_at, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(artifact_id) DO NOTHING
            """,
            (
                reference.artifact_id,
                snapshot.run_id,
                snapshot.tenant_id,
                snapshot.user_id,
                reference.checksum,
                reference.size_bytes,
                reference.storage_key,
                encoded,
                reference.staged_at,
            ),
        )

    def _commit_operation_artifact(
        self,
        conn: sqlite3.Connection,
        *,
        record: OperationRecord,
        reference: ArtifactReference,
    ) -> None:
        settlement = record.settlement
        if (
            record.state is not OperationState.SUCCEEDED
            or settlement is None
            or settlement.result_reference != reference.artifact_id
        ):
            raise HarnessError(
                code="ARTIFACT_SETTLEMENT_MISMATCH",
                category="artifact",
                message="An artifact reference must exactly match a successful settlement.",
                retryable=False,
                details={"operation_id": record.intent.operation_id},
            )
        if settlement.result_slot_id != record.intent.result_slot_id:
            raise HarnessError(
                code="OPERATION_RESULT_SLOT_MISMATCH",
                category="operation",
                message="The artifact does not fulfill the operation's result identity.",
                retryable=False,
                details={"operation_id": record.intent.operation_id},
            )
        snapshot = self._get_run_in_transaction(conn, record.intent.run_id)
        self._commit_runtime_artifact(conn, snapshot=snapshot, reference=reference)
        prior = conn.execute(
            """
            SELECT artifact_id FROM runtime_operation_artifacts
            WHERE operation_id = ? AND role = 'result'
            """,
            (record.intent.operation_id,),
        ).fetchone()
        if prior is not None and prior["artifact_id"] != reference.artifact_id:
            raise HarnessError(
                code="OPERATION_RESULT_SLOT_ALREADY_FULFILLED",
                category="operation",
                message="The operation result identity already has a different artifact.",
                retryable=False,
                details={"operation_id": record.intent.operation_id},
            )
        conn.execute(
            """
            INSERT INTO runtime_operation_artifacts(operation_id, artifact_id, role, created_at)
            VALUES (?, ?, 'result', ?)
            ON CONFLICT(operation_id, role) DO NOTHING
            """,
            (record.intent.operation_id, reference.artifact_id, record.updated_at),
        )
        self._append_event(
            conn,
            run_id=snapshot.run_id,
            event_type="artifact.committed",
            occurred_at=record.updated_at,
            attempt_id=record.intent.attempt_id,
            operation_id=record.intent.operation_id,
            payload={
                "artifact_id": reference.artifact_id,
                "role": "result",
                "result_slot_id": record.intent.result_slot_id,
                "checksum": reference.checksum,
                "size_bytes": reference.size_bytes,
                "media_type": reference.media_type,
            },
        )
        self._fault("artifact.after_reference")

    def _commit_reconciliation_artifacts(
        self,
        conn: sqlite3.Connection,
        *,
        record: OperationRecord,
        reconciliation: OperationReconciliation,
        references: tuple[ArtifactReference, ...],
    ) -> None:
        if tuple(item.artifact_id for item in references) != reconciliation.evidence_artifact_ids:
            raise HarnessError(
                code="OPERATION_RECONCILIATION_EVIDENCE_MISMATCH",
                category="operation",
                message="Evidence references do not match the reconciliation manifest.",
                retryable=False,
                details={"operation_id": record.intent.operation_id},
            )
        snapshot = self._get_run_in_transaction(conn, record.intent.run_id)
        expected_scope = ArtifactScope(
            run_id=snapshot.run_id,
            tenant_id=snapshot.tenant_id,
            user_id=snapshot.user_id,
        )
        for index, reference in enumerate(references):
            if reference.scope != expected_scope:
                raise HarnessError(
                    code="OPERATION_RECONCILIATION_EVIDENCE_SCOPE_MISMATCH",
                    category="operation",
                    message="Reconciliation evidence is outside the operation owner scope.",
                    retryable=False,
                    details={"operation_id": record.intent.operation_id},
                )
            encoded = _canonical_json(reference.to_dict())
            existing = conn.execute(
                "SELECT reference_json FROM runtime_artifacts WHERE artifact_id = ?",
                (reference.artifact_id,),
            ).fetchone()
            if existing is not None and existing["reference_json"] != encoded:
                raise HarnessError(
                    code="ARTIFACT_IDEMPOTENCY_CONFLICT",
                    category="artifact",
                    message="Artifact address was reused with different immutable metadata.",
                    retryable=False,
                    details={"artifact_id": reference.artifact_id},
                )
            conn.execute(
                """
                INSERT INTO runtime_artifacts(
                    artifact_id, run_id, tenant_id, user_id, checksum, size_bytes,
                    storage_key, reference_json, created_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(artifact_id) DO NOTHING
                """,
                (
                    reference.artifact_id,
                    snapshot.run_id,
                    snapshot.tenant_id,
                    snapshot.user_id,
                    reference.checksum,
                    reference.size_bytes,
                    reference.storage_key,
                    encoded,
                    reference.staged_at,
                ),
            )
            role = f"reconciliation:{reconciliation.reconciliation_id}:{index}"
            conn.execute(
                """
                INSERT INTO runtime_operation_artifacts(
                    operation_id, artifact_id, role, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(operation_id, role) DO NOTHING
                """,
                (
                    record.intent.operation_id,
                    reference.artifact_id,
                    role,
                    reconciliation.reconciled_at,
                ),
            )

    def begin_operation(
        self,
        operation_id: str,
        *,
        mutation_id: str,
        expected_revision: int,
        worker_id: str,
        fence_token: int,
    ) -> StoredOperationDecision:
        return self._mutate_operation(
            operation_id,
            mutation_id=mutation_id,
            mutation_kind="dispatch",
            mutation_payload={
                "expected_revision": expected_revision,
                "worker_id": worker_id,
                "fence_token": fence_token,
            },
            expected_revision=expected_revision,
            reducer=lambda record: begin_operation_dispatch(
                record,
                worker_id=worker_id,
                fence_token=fence_token,
            ),
            event_type="operation.dispatching",
        )

    def recover_operation(
        self,
        operation_id: str,
        *,
        mutation_id: str,
        expected_revision: int,
        next_fence_token: int,
    ) -> StoredOperationDecision:
        return self._mutate_operation(
            operation_id,
            mutation_id=mutation_id,
            mutation_kind="recover",
            mutation_payload={
                "expected_revision": expected_revision,
                "next_fence_token": next_fence_token,
            },
            expected_revision=expected_revision,
            reducer=lambda record: reset_operation_for_recovery(
                record,
                next_fence_token=next_fence_token,
            ),
            event_type="operation.recovery.planned",
        )

    def settle_operation(
        self,
        operation_id: str,
        *,
        mutation_id: str,
        expected_revision: int,
        fence_token: int,
        settlement: OperationSettlement,
        artifact_reference: ArtifactReference | None = None,
    ) -> StoredOperationDecision:
        return self._mutate_operation(
            operation_id,
            mutation_id=mutation_id,
            mutation_kind="settle",
            mutation_payload={
                "expected_revision": expected_revision,
                "fence_token": fence_token,
                "settlement": settlement.to_dict(),
                "artifact_reference": (
                    artifact_reference.to_dict() if artifact_reference is not None else None
                ),
            },
            expected_revision=expected_revision,
            reducer=lambda record: settle_operation(
                record,
                settlement,
                fence_token=fence_token,
            ),
            event_type="operation.settled",
            artifact_reference=artifact_reference,
        )

    def reconcile_operation(
        self,
        operation_id: str,
        *,
        mutation_id: str,
        reconciliation: OperationReconciliation,
        evidence_artifacts: tuple[ArtifactReference, ...],
        owner: RunOwner,
    ) -> StoredOperationDecision:
        result_artifact = next(
            (
                item
                for item in evidence_artifacts
                if item.artifact_id == reconciliation.result_reference
            ),
            None,
        )
        return self._mutate_operation(
            operation_id,
            mutation_id=mutation_id,
            mutation_kind="reconcile",
            mutation_payload={
                "reconciliation": reconciliation.to_dict(),
                "evidence_artifacts": [item.to_dict() for item in evidence_artifacts],
            },
            expected_revision=reconciliation.expected_revision,
            reducer=lambda record: reduce_operation_reconciliation(record, reconciliation),
            event_type="operation.reconciled",
            artifact_reference=result_artifact,
            owner=owner,
            reconciliation=reconciliation,
            evidence_artifacts=evidence_artifacts,
        )

    def get_artifact(
        self,
        artifact_id: str,
        *,
        owner: RunOwner | None = None,
    ) -> ArtifactReference:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT run_id, reference_json FROM runtime_artifacts
                WHERE artifact_id = ? AND deleted_at IS NULL
                """,
                (artifact_id,),
            ).fetchone()
            if row is None:
                raise ArtifactNotFoundError(artifact_id)
            try:
                self._get_run_in_transaction(self._connection, row["run_id"], owner=owner)
            except RunNotFoundError as exc:
                raise ArtifactNotFoundError(artifact_id) from exc
            return ArtifactReference.from_dict(json.loads(row["reference_json"]))

    def list_artifacts(
        self,
        run_id: str,
        *,
        limit: int = 100,
        owner: RunOwner | None = None,
    ) -> list[ArtifactReference]:
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            self._get_run_in_transaction(self._connection, run_id, owner=owner)
            rows = self._connection.execute(
                """
                SELECT reference_json FROM runtime_artifacts
                WHERE run_id = ? AND deleted_at IS NULL
                ORDER BY created_at ASC, artifact_id ASC LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [ArtifactReference.from_dict(json.loads(row["reference_json"])) for row in rows]

    def list_artifact_storage_keys(self, *, include_deleted: bool = False) -> list[str]:
        condition = "" if include_deleted else " WHERE deleted_at IS NULL"
        with self._lock:
            rows = self._connection.execute(
                f"SELECT storage_key FROM runtime_artifacts{condition} ORDER BY storage_key"
            ).fetchall()
        return [str(row["storage_key"]) for row in rows]

    def list_recoverable_operations(self, *, limit: int = 100) -> list[OperationRecord]:
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT record_json FROM runtime_operations
                WHERE state IN ('planned', 'dispatching')
                ORDER BY updated_at ASC, operation_id ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._operation_from_json(row["record_json"]) for row in rows]

    def list_reconciliation_operations(
        self,
        *,
        owner: RunOwner,
        after_operation_id: str | None = None,
        minimum_age_seconds: int = 30,
        limit: int = 100,
    ) -> list[OperationRecord]:
        if not isinstance(owner, RunOwner):
            raise TypeError("owner must be a RunOwner")
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if (
            not isinstance(minimum_age_seconds, int)
            or isinstance(minimum_age_seconds, bool)
            or not 0 <= minimum_age_seconds <= MAX_RECOVERY_MINIMUM_AGE_SECONDS
        ):
            raise ValueError("minimum_age_seconds must be between 0 and 86400")
        if after_operation_id is not None and (
            not isinstance(after_operation_id, str) or not after_operation_id.strip()
        ):
            raise ValueError("after_operation_id must be a non-empty string")
        updated_before = (datetime.now(UTC) - timedelta(seconds=minimum_age_seconds)).isoformat()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT operation.record_json FROM runtime_operations AS operation
                JOIN runtime_runs AS run ON run.run_id = operation.run_id
                WHERE run.tenant_id IS ? AND run.user_id IS ?
                  AND run.state = 'waiting_for_reconciliation'
                  AND operation.operation_id > ?
                  AND run.authority_updated_at <= ?
                  AND (
                    operation.state IN ('unknown', 'succeeded', 'failed', 'cancelled') OR (
                      operation.state = 'dispatching'
                      AND operation.effect_class IN ('compensatable', 'non_repeatable')
                    )
                  )
                ORDER BY operation.operation_id ASC LIMIT ?
                """,
                (
                    owner.tenant_id,
                    owner.user_id,
                    after_operation_id or "",
                    updated_before,
                    limit,
                ),
            ).fetchall()
        return [self._operation_from_json(row["record_json"]) for row in rows]

    def list_recoverable_runs(
        self,
        *,
        owner: RunOwner,
        after_run_id: str | None = None,
        minimum_age_seconds: int = 30,
        limit: int = 100,
    ) -> list[RunSnapshot]:
        """List executable nonterminal runs for one exact owner."""
        if not isinstance(owner, RunOwner):
            raise TypeError("owner must be a RunOwner")
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if (
            not isinstance(minimum_age_seconds, int)
            or isinstance(minimum_age_seconds, bool)
            or not 0 <= minimum_age_seconds <= MAX_RECOVERY_MINIMUM_AGE_SECONDS
        ):
            raise ValueError("minimum_age_seconds must be between 0 and 86400")
        if after_run_id is not None and (
            not isinstance(after_run_id, str) or not after_run_id.strip()
        ):
            raise ValueError("after_run_id must be a non-empty string")
        after = after_run_id or ""
        updated_before = (datetime.now(UTC) - timedelta(seconds=minimum_age_seconds)).isoformat()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT snapshot_json FROM runtime_runs
                WHERE tenant_id IS ? AND user_id IS ?
                  AND state IN ('queued', 'running', 'cancelling') AND run_id > ?
                  AND authority_updated_at <= ?
                ORDER BY run_id ASC LIMIT ?
                """,
                (
                    owner.tenant_id,
                    owner.user_id,
                    after,
                    updated_before,
                    limit,
                ),
            ).fetchall()
        return [_snapshot_from_json(row["snapshot_json"]) for row in rows]

    @staticmethod
    def _lease_from_row(row: sqlite3.Row) -> RuntimeLease:
        return RuntimeLease(
            lease_key=row["lease_key"],
            kind=LeaseKind(row["lease_kind"]),
            run_id=row["run_id"],
            worker_id=row["worker_id"],
            claim_id=row["claim_id"],
            lease_token=row["lease_token"],
            fence_token=int(row["fence_token"]),
            acquired_at=row["acquired_at"],
            expires_at=row["expires_at"],
        )

    @classmethod
    def _lease_claim_from_rows(
        cls,
        rows: dict[str, sqlite3.Row],
        *,
        run_key: str,
        session_key: str,
    ) -> RunLeaseClaim:
        return RunLeaseClaim(
            run=cls._lease_from_row(rows[run_key]),
            session=cls._lease_from_row(rows[session_key]),
        )

    @staticmethod
    def _validate_lease_request(
        *,
        worker_id: str,
        claim_id: str,
        lease_seconds: int,
    ) -> None:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        if not isinstance(claim_id, str) or not claim_id.strip():
            raise ValueError("claim_id must be a non-empty string")
        if len(worker_id) > 512 or len(claim_id) > 512:
            raise ValueError("worker_id and claim_id cannot exceed 512 characters")
        if lease_seconds <= 0 or lease_seconds > 86_400:
            raise ValueError("lease_seconds must be between 1 and 86400")

    def acquire_run_lease(
        self,
        run_id: str,
        *,
        worker_id: str,
        claim_id: str,
        lease_seconds: int = 30,
        owner: RunOwner | None = None,
    ) -> StoredLeaseClaimDecision:
        """Atomically claim both run ownership and its exact session lane."""
        self._validate_lease_request(
            worker_id=worker_id,
            claim_id=claim_id,
            lease_seconds=lease_seconds,
        )
        with self._transaction() as conn:
            snapshot = self._get_run_in_transaction(conn, run_id, owner=owner)
            if snapshot.terminal:
                raise RuntimeLeaseTerminalRunError(run_id=run_id)
            run_key = run_lease_key(run_id)
            session_key = session_lease_key(
                tenant_id=snapshot.tenant_id,
                session_id=snapshot.session_id,
                run_id=run_id,
            )
            keys = (run_key, session_key)
            selected = conn.execute(
                """
                SELECT * FROM runtime_execution_leases
                WHERE lease_key IN (?, ?)
                """,
                keys,
            ).fetchall()
            rows = {str(row["lease_key"]): row for row in selected}
            now = datetime.now(UTC)

            if len(rows) == 2 and all(
                row["claim_id"] == claim_id and row["worker_id"] == worker_id
                for row in rows.values()
            ):
                if any(row["released_at"] is not None for row in rows.values()):
                    raise RuntimeLeaseClaimReleasedError(run_id=run_id)
                if all(datetime.fromisoformat(row["expires_at"]) > now for row in rows.values()):
                    event_json = rows[run_key]["claim_event_json"]
                    if event_json is None:
                        raise HarnessError(
                            code="RUNTIME_LEASE_EVIDENCE_MISSING",
                            category="runtime_store",
                            message="Active lease is missing its acquisition evidence.",
                            retryable=False,
                            details={"run_id": run_id},
                        )
                    return StoredLeaseClaimDecision(
                        claim=self._lease_claim_from_rows(
                            rows,
                            run_key=run_key,
                            session_key=session_key,
                        ),
                        event=self._event_from_json(event_json),
                        acquired=False,
                        idempotent=True,
                    )
                raise RuntimeLeaseLostError(run_id=run_id, kind=LeaseKind.RUN)

            for key in keys:
                row = rows.get(key)
                if row is None or row["released_at"] is not None:
                    continue
                existing_expiry = datetime.fromisoformat(row["expires_at"])
                if existing_expiry > now:
                    raise RuntimeLeaseUnavailableError(
                        run_id=run_id,
                        kind=LeaseKind(row["lease_kind"]),
                        retry_after_seconds=(existing_expiry - now).total_seconds(),
                    )

            acquired_at = now.isoformat()
            expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
            reclaimed = bool(rows)
            for kind, key in (
                (LeaseKind.RUN, run_key),
                (LeaseKind.SESSION, session_key),
            ):
                previous = rows.get(key)
                fence_token = int(previous["fence_token"]) + 1 if previous is not None else 1
                conn.execute(
                    """
                    INSERT INTO runtime_execution_leases(
                        lease_key, lease_kind, run_id, worker_id, claim_id,
                        lease_token, fence_token, acquired_at, renewed_at,
                        expires_at, released_at, claim_event_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                    ON CONFLICT(lease_key) DO UPDATE SET
                        lease_kind = excluded.lease_kind,
                        run_id = excluded.run_id,
                        worker_id = excluded.worker_id,
                        claim_id = excluded.claim_id,
                        lease_token = excluded.lease_token,
                        fence_token = excluded.fence_token,
                        acquired_at = excluded.acquired_at,
                        renewed_at = excluded.renewed_at,
                        expires_at = excluded.expires_at,
                        released_at = NULL,
                        claim_event_json = NULL
                    """,
                    (
                        key,
                        kind.value,
                        run_id,
                        worker_id,
                        claim_id,
                        lease_token(claim_id=claim_id, kind=kind, lease_key=key),
                        fence_token,
                        acquired_at,
                        acquired_at,
                        expires_at,
                    ),
                )
            self._fault("lease.acquire.after_rows")
            event = self._append_event(
                conn,
                run_id=run_id,
                event_type="run.lease.acquired",
                occurred_at=acquired_at,
                payload={
                    "claim_id": claim_id,
                    "worker_id": worker_id,
                    "run_fence_token": (
                        int(rows[run_key]["fence_token"]) + 1 if run_key in rows else 1
                    ),
                    "session_fence_token": (
                        int(rows[session_key]["fence_token"]) + 1 if session_key in rows else 1
                    ),
                    "expires_at": expires_at,
                    "reclaimed": reclaimed,
                },
            )
            event_json = _canonical_json(event.to_dict())
            conn.execute(
                """
                UPDATE runtime_execution_leases SET claim_event_json = ?
                WHERE lease_key IN (?, ?) AND claim_id = ?
                """,
                (event_json, run_key, session_key, claim_id),
            )
            self._fault("lease.acquire.after_event")
            selected = conn.execute(
                """
                SELECT * FROM runtime_execution_leases
                WHERE lease_key IN (?, ?)
                """,
                keys,
            ).fetchall()
            claimed_rows = {str(row["lease_key"]): row for row in selected}
            return StoredLeaseClaimDecision(
                claim=self._lease_claim_from_rows(
                    claimed_rows,
                    run_key=run_key,
                    session_key=session_key,
                ),
                event=event,
                acquired=True,
                reclaimed=reclaimed,
            )

    def renew_run_lease(
        self,
        claim: RunLeaseClaim,
        *,
        lease_seconds: int = 30,
    ) -> RunLeaseClaim:
        self._validate_lease_request(
            worker_id=claim.worker_id,
            claim_id=claim.claim_id,
            lease_seconds=lease_seconds,
        )
        keys = (claim.run.lease_key, claim.session.lease_key)
        expected = {claim.run.lease_key: claim.run, claim.session.lease_key: claim.session}
        with self._transaction() as conn:
            selected = conn.execute(
                "SELECT * FROM runtime_execution_leases WHERE lease_key IN (?, ?)",
                keys,
            ).fetchall()
            rows = {str(row["lease_key"]): row for row in selected}
            now = datetime.now(UTC)
            for key, lease in expected.items():
                row = rows.get(key)
                if (
                    row is None
                    or row["claim_id"] != claim.claim_id
                    or row["worker_id"] != claim.worker_id
                    or row["lease_token"] != lease.lease_token
                    or int(row["fence_token"]) != lease.fence_token
                    or row["released_at"] is not None
                    or datetime.fromisoformat(row["expires_at"]) <= now
                ):
                    raise RuntimeLeaseLostError(run_id=claim.run_id, kind=lease.kind)
            expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
            for key, lease in expected.items():
                updated = conn.execute(
                    """
                    UPDATE runtime_execution_leases
                    SET renewed_at = ?, expires_at = ?
                    WHERE lease_key = ? AND claim_id = ? AND worker_id = ?
                      AND lease_token = ? AND fence_token = ? AND released_at IS NULL
                      AND expires_at > ?
                    """,
                    (
                        now.isoformat(),
                        expires_at,
                        key,
                        claim.claim_id,
                        claim.worker_id,
                        lease.lease_token,
                        lease.fence_token,
                        now.isoformat(),
                    ),
                )
                if updated.rowcount != 1:
                    raise RuntimeLeaseLostError(run_id=claim.run_id, kind=lease.kind)
            selected = conn.execute(
                "SELECT * FROM runtime_execution_leases WHERE lease_key IN (?, ?)",
                keys,
            ).fetchall()
            renewed_rows = {str(row["lease_key"]): row for row in selected}
            return self._lease_claim_from_rows(
                renewed_rows,
                run_key=claim.run.lease_key,
                session_key=claim.session.lease_key,
            )

    def release_run_lease(self, claim: RunLeaseClaim) -> LeaseReleaseDecision:
        keys = (claim.run.lease_key, claim.session.lease_key)
        expected = {claim.run.lease_key: claim.run, claim.session.lease_key: claim.session}
        with self._transaction() as conn:
            selected = conn.execute(
                "SELECT * FROM runtime_execution_leases WHERE lease_key IN (?, ?)",
                keys,
            ).fetchall()
            rows = {str(row["lease_key"]): row for row in selected}
            if len(rows) == 2 and all(
                row["claim_id"] == claim.claim_id
                and row["worker_id"] == claim.worker_id
                and row["lease_token"] == expected[key].lease_token
                and int(row["fence_token"]) == expected[key].fence_token
                and row["released_at"] is not None
                for key, row in rows.items()
            ):
                return LeaseReleaseDecision(
                    run_id=claim.run_id,
                    claim_id=claim.claim_id,
                    released=False,
                    idempotent=True,
                )
            now = datetime.now(UTC)
            for key, lease in expected.items():
                row = rows.get(key)
                if (
                    row is None
                    or row["claim_id"] != claim.claim_id
                    or row["worker_id"] != claim.worker_id
                    or row["lease_token"] != lease.lease_token
                    or int(row["fence_token"]) != lease.fence_token
                    or row["released_at"] is not None
                    or datetime.fromisoformat(row["expires_at"]) <= now
                ):
                    raise RuntimeLeaseLostError(run_id=claim.run_id, kind=lease.kind)
            for key, lease in expected.items():
                updated = conn.execute(
                    """
                    UPDATE runtime_execution_leases
                    SET released_at = ?, expires_at = ?
                    WHERE lease_key = ? AND claim_id = ? AND worker_id = ?
                      AND lease_token = ? AND fence_token = ? AND released_at IS NULL
                      AND expires_at > ?
                    """,
                    (
                        now.isoformat(),
                        now.isoformat(),
                        key,
                        claim.claim_id,
                        claim.worker_id,
                        lease.lease_token,
                        lease.fence_token,
                        now.isoformat(),
                    ),
                )
                if updated.rowcount != 1:
                    raise RuntimeLeaseLostError(run_id=claim.run_id, kind=lease.kind)
            return LeaseReleaseDecision(
                run_id=claim.run_id,
                claim_id=claim.claim_id,
                released=True,
            )

    def get_terminal(
        self,
        run_id: str,
        *,
        owner: RunOwner | None = None,
    ) -> TerminalRecord | None:
        with self._lock:
            self._get_run_in_transaction(self._connection, run_id, owner=owner)
            row = self._connection.execute(
                "SELECT * FROM runtime_terminal_records WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return TerminalRecord(
            run_id=run_id,
            state=RunState(row["state"]),
            value=json.loads(row["value_json"]) if row["value_json"] is not None else None,
            error=json.loads(row["error_json"]) if row["error_json"] is not None else None,
            recorded_at=row["recorded_at"],
        )

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        owner: RunOwner | None = None,
    ) -> list[RuntimeEvent]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            self._get_run_in_transaction(self._connection, run_id, owner=owner)
            retention = self._connection.execute(
                """
                SELECT pruned_through_sequence FROM runtime_event_retention
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if retention is not None and after_sequence < int(retention["pruned_through_sequence"]):
                raise EventCursorExpiredError(
                    run_id=run_id,
                    pruned_through_sequence=int(retention["pruned_through_sequence"]),
                )
            rows = self._connection.execute(
                """
                SELECT event_json FROM runtime_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence ASC LIMIT ?
                """,
                (run_id, after_sequence, limit),
            ).fetchall()
        return [self._event_from_json(row["event_json"]) for row in rows]

    def lease_outbox(
        self,
        *,
        owner: str,
        limit: int = 100,
        lease_seconds: int = 30,
    ) -> list[OutboxItem]:
        if not owner.strip():
            raise ValueError("outbox lease owner must be non-empty")
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = datetime.now(UTC)
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        token = f"lease_{uuid4().hex}"
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT outbox_id FROM runtime_outbox
                WHERE delivered_at IS NULL
                  AND dead_lettered_at IS NULL
                  AND available_at <= ?
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                ORDER BY outbox_id ASC LIMIT ?
                """,
                (now.isoformat(), now.isoformat(), limit),
            ).fetchall()
            ids = [int(row["outbox_id"]) for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE runtime_outbox
                SET status = 'leased', attempts = attempts + 1,
                    lease_owner = ?, lease_token = ?, lease_expires_at = ?
                WHERE outbox_id IN ({placeholders})
                """,
                (owner, token, expires, *ids),
            )
            leased = conn.execute(
                f"""
                SELECT * FROM runtime_outbox
                WHERE outbox_id IN ({placeholders})
                ORDER BY outbox_id ASC
                """,
                ids,
            ).fetchall()
        return [
            OutboxItem(
                outbox_id=int(row["outbox_id"]),
                run_id=row["run_id"],
                sequence=int(row["sequence"]),
                event=self._event_from_json(row["event_json"]),
                attempts=int(row["attempts"]),
                lease_owner=row["lease_owner"],
                lease_token=row["lease_token"],
                lease_expires_at=row["lease_expires_at"],
            )
            for row in leased
        ]

    def acknowledge_outbox(self, *, outbox_id: int, lease_token: str) -> None:
        with self._transaction() as conn:
            updated = conn.execute(
                """
                UPDATE runtime_outbox
                SET status = 'delivered', delivered_at = ?,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                WHERE outbox_id = ? AND lease_token = ? AND delivered_at IS NULL
                  AND dead_lettered_at IS NULL
                  AND lease_expires_at > ?
                """,
                (_now(), outbox_id, lease_token, _now()),
            )
            if updated.rowcount != 1:
                raise OutboxLeaseError(outbox_id)

    def defer_outbox(
        self,
        *,
        outbox_id: int,
        lease_token: str,
        delay_seconds: float = 0,
    ) -> None:
        if not 0 <= delay_seconds <= MAX_OUTBOX_DEFER_SECONDS:
            raise ValueError(f"delay_seconds must be between 0 and {MAX_OUTBOX_DEFER_SECONDS}")
        now = datetime.now(UTC)
        available_at = (now + timedelta(seconds=delay_seconds)).isoformat()
        with self._transaction() as conn:
            updated = conn.execute(
                """
                UPDATE runtime_outbox
                SET status = 'pending', available_at = ?,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                WHERE outbox_id = ? AND lease_token = ? AND delivered_at IS NULL
                  AND dead_lettered_at IS NULL
                  AND lease_expires_at > ?
                """,
                (available_at, outbox_id, lease_token, now.isoformat()),
            )
            if updated.rowcount != 1:
                raise OutboxLeaseError(outbox_id)

    def dead_letter_outbox(
        self,
        *,
        outbox_id: int,
        lease_token: str,
        reason_code: str,
    ) -> DeadLetterItem:
        _validate_reason_code(reason_code)
        now = _now()
        with self._transaction() as conn:
            updated = conn.execute(
                """
                UPDATE runtime_outbox
                SET status = 'dead_lettered', dead_lettered_at = ?,
                    dead_letter_reason_code = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL
                WHERE outbox_id = ? AND lease_token = ? AND delivered_at IS NULL
                  AND dead_lettered_at IS NULL AND lease_expires_at > ?
                """,
                (now, reason_code, outbox_id, lease_token, now),
            )
            if updated.rowcount != 1:
                raise OutboxLeaseError(outbox_id)
            row = conn.execute(
                "SELECT * FROM runtime_outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
        return self._dead_letter_from_row(row)

    def inspect_dead_letters(
        self,
        *,
        owner: RunOwner,
        operator_digest: str,
        authority_digest: str,
        reason_code: str,
        after_outbox_id: int = 0,
        limit: int = 100,
    ) -> DeadLetterInspectionDecision:
        if after_outbox_id < 0:
            raise ValueError("after_outbox_id cannot be negative")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        _validate_sha256(operator_digest, field_name="operator_digest")
        _validate_sha256(authority_digest, field_name="authority_digest")
        _validate_reason_code(reason_code)
        audit_id = f"dla_{uuid4().hex}"
        created_at = _now()
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT outbox.* FROM runtime_outbox AS outbox
                JOIN runtime_runs AS run ON run.run_id = outbox.run_id
                WHERE outbox.dead_lettered_at IS NOT NULL
                  AND outbox.outbox_id > ?
                  AND run.tenant_id IS ? AND run.user_id IS ?
                ORDER BY outbox.outbox_id ASC LIMIT ?
                """,
                (after_outbox_id, owner.tenant_id, owner.user_id, limit),
            ).fetchall()
            outbox_ids = [int(row["outbox_id"]) for row in rows]
            inserted = conn.execute(
                """
                INSERT INTO runtime_dead_letter_audit(
                    audit_id, action, tenant_id, user_id, operator_digest,
                    authority_digest, reason_code, requested_after_outbox_id,
                    requested_limit, result_count, first_outbox_id, last_outbox_id,
                    delay_seconds, created_at
                ) VALUES (?, 'inspected', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    audit_id,
                    owner.tenant_id,
                    owner.user_id,
                    operator_digest,
                    authority_digest,
                    reason_code,
                    after_outbox_id,
                    limit,
                    len(outbox_ids),
                    outbox_ids[0] if outbox_ids else None,
                    outbox_ids[-1] if outbox_ids else None,
                    created_at,
                ),
            )
            audit_row = conn.execute(
                "SELECT * FROM runtime_dead_letter_audit WHERE audit_sequence = ?",
                (inserted.lastrowid,),
            ).fetchone()
        return DeadLetterInspectionDecision(
            items=tuple(self._dead_letter_from_row(row) for row in rows),
            audit=self._dead_letter_audit_from_row(audit_row),
        )

    def requeue_dead_letter(
        self,
        *,
        owner: RunOwner,
        operator_digest: str,
        authority_digest: str,
        reason_code: str,
        mutation_id: str,
        outbox_id: int,
        expected_dead_lettered_at: str,
        delay_seconds: float = 0,
    ) -> DeadLetterRequeueDecision:
        if not 0 <= delay_seconds <= MAX_OUTBOX_DEFER_SECONDS:
            raise ValueError(f"delay_seconds must be between 0 and {MAX_OUTBOX_DEFER_SECONDS}")
        if not expected_dead_lettered_at.strip():
            raise ValueError("expected_dead_lettered_at must be non-empty")
        if outbox_id <= 0:
            raise ValueError("outbox_id must be positive")
        _validate_sha256(operator_digest, field_name="operator_digest")
        _validate_sha256(authority_digest, field_name="authority_digest")
        _validate_reason_code(reason_code)
        _validate_mutation_id(mutation_id)
        mutation_digest = _dead_letter_mutation_digest(
            outbox_id=outbox_id,
            expected_dead_lettered_at=expected_dead_lettered_at,
            owner=owner,
            operator_digest=operator_digest,
            authority_digest=authority_digest,
            reason_code=reason_code,
            delay_seconds=delay_seconds,
        )
        available_at = (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()
        with self._transaction() as conn:
            prior = conn.execute(
                """
                SELECT * FROM runtime_dead_letter_audit
                WHERE mutation_id = ? AND tenant_id IS ? AND user_id IS ?
                """,
                (mutation_id, owner.tenant_id, owner.user_id),
            ).fetchone()
            if prior is not None:
                if prior["mutation_digest"] != mutation_digest:
                    raise OutboxDeadLetterMutationConflictError(mutation_id)
                return DeadLetterRequeueDecision(
                    audit=self._dead_letter_audit_from_row(prior),
                    idempotent=True,
                )
            if (
                conn.execute(
                    "SELECT 1 FROM runtime_dead_letter_audit WHERE mutation_id = ?",
                    (mutation_id,),
                ).fetchone()
                is not None
            ):
                raise OutboxDeadLetterMutationConflictError(mutation_id)
            row = conn.execute(
                """
                SELECT outbox.* FROM runtime_outbox AS outbox
                JOIN runtime_runs AS run ON run.run_id = outbox.run_id
                WHERE outbox.outbox_id = ?
                  AND run.tenant_id IS ? AND run.user_id IS ?
                """,
                (outbox_id, owner.tenant_id, owner.user_id),
            ).fetchone()
            if row is None:
                raise OutboxDeadLetterConflictError(outbox_id)
            if row["dead_lettered_at"] != expected_dead_lettered_at:
                raise OutboxDeadLetterConflictError(outbox_id)
            updated = conn.execute(
                """
                UPDATE runtime_outbox
                SET status = 'pending', available_at = ?, dead_lettered_at = NULL,
                    dead_letter_reason_code = NULL
                WHERE outbox_id = ? AND dead_lettered_at = ? AND delivered_at IS NULL
                """,
                (available_at, outbox_id, expected_dead_lettered_at),
            )
            if updated.rowcount != 1:
                raise OutboxDeadLetterConflictError(outbox_id)
            self._fault("dead_letter_requeue_after_update")
            audit_id = f"dla_{uuid4().hex}"
            created_at = _now()
            inserted = conn.execute(
                """
                INSERT INTO runtime_dead_letter_audit(
                    audit_id, action, tenant_id, user_id, operator_digest,
                    authority_digest, reason_code, result_count, first_outbox_id,
                    last_outbox_id, outbox_id, run_id, expected_dead_lettered_at,
                    delay_seconds, mutation_id, mutation_digest, created_at
                ) VALUES (?, 'requeued', ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    owner.tenant_id,
                    owner.user_id,
                    operator_digest,
                    authority_digest,
                    reason_code,
                    outbox_id,
                    outbox_id,
                    outbox_id,
                    row["run_id"],
                    expected_dead_lettered_at,
                    delay_seconds,
                    mutation_id,
                    mutation_digest,
                    created_at,
                ),
            )
            audit_row = conn.execute(
                "SELECT * FROM runtime_dead_letter_audit WHERE audit_sequence = ?",
                (inserted.lastrowid,),
            ).fetchone()
        return DeadLetterRequeueDecision(
            audit=self._dead_letter_audit_from_row(audit_row),
        )

    def list_dead_letter_audit(
        self,
        *,
        owner: RunOwner,
        after_audit_sequence: int = 0,
        limit: int = 100,
    ) -> list[DeadLetterAuditRecord]:
        if after_audit_sequence < 0:
            raise ValueError("after_audit_sequence cannot be negative")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM runtime_dead_letter_audit
                WHERE audit_sequence > ? AND tenant_id IS ? AND user_id IS ?
                ORDER BY audit_sequence ASC LIMIT ?
                """,
                (after_audit_sequence, owner.tenant_id, owner.user_id, limit),
            ).fetchall()
        return [self._dead_letter_audit_from_row(row) for row in rows]

    def _dead_letter_from_row(self, row: sqlite3.Row) -> DeadLetterItem:
        return DeadLetterItem(
            outbox_id=int(row["outbox_id"]),
            run_id=row["run_id"],
            sequence=int(row["sequence"]),
            event=self._event_from_json(row["event_json"]),
            attempts=int(row["attempts"]),
            reason_code=row["dead_letter_reason_code"],
            dead_lettered_at=row["dead_lettered_at"],
        )

    @staticmethod
    def _dead_letter_audit_from_row(row: sqlite3.Row) -> DeadLetterAuditRecord:
        return DeadLetterAuditRecord(
            audit_sequence=int(row["audit_sequence"]),
            audit_id=row["audit_id"],
            action=DeadLetterAuditAction(row["action"]),
            owner=RunOwner(tenant_id=row["tenant_id"], user_id=row["user_id"]),
            operator_digest=row["operator_digest"],
            authority_digest=row["authority_digest"],
            reason_code=row["reason_code"],
            requested_after_outbox_id=(
                int(row["requested_after_outbox_id"])
                if row["requested_after_outbox_id"] is not None
                else None
            ),
            requested_limit=(
                int(row["requested_limit"]) if row["requested_limit"] is not None else None
            ),
            result_count=int(row["result_count"]),
            first_outbox_id=(
                int(row["first_outbox_id"]) if row["first_outbox_id"] is not None else None
            ),
            last_outbox_id=(
                int(row["last_outbox_id"]) if row["last_outbox_id"] is not None else None
            ),
            outbox_id=int(row["outbox_id"]) if row["outbox_id"] is not None else None,
            run_id=row["run_id"],
            expected_dead_lettered_at=row["expected_dead_lettered_at"],
            delay_seconds=float(row["delay_seconds"]),
            mutation_id=row["mutation_id"],
            mutation_digest=row["mutation_digest"],
            created_at=row["created_at"],
        )

    def prune_run_events(
        self,
        run_id: str,
        *,
        through_sequence: int,
        owner: RunOwner | None = None,
    ) -> RetentionDecision:
        if through_sequence <= 0:
            raise ValueError("through_sequence must be positive")
        with self._transaction() as conn:
            snapshot = self._get_run_in_transaction(conn, run_id, owner=owner)
            if not snapshot.terminal:
                raise RuntimeRetentionError(
                    code="RUNTIME_RETENTION_RUN_ACTIVE",
                    run_id=run_id,
                    message="Event retention only prunes terminal runs.",
                )
            maximum = conn.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) AS sequence
                FROM runtime_events WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            target = min(through_sequence, max(0, int(maximum["sequence"]) - 1))
            current = conn.execute(
                """
                SELECT pruned_through_sequence FROM runtime_event_retention
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            prior = int(current["pruned_through_sequence"]) if current is not None else 0
            if target <= prior:
                return RetentionDecision(run_id, prior, 0, 0)
            pending = conn.execute(
                """
                SELECT COUNT(*) AS count FROM runtime_outbox
                WHERE run_id = ? AND sequence <= ? AND delivered_at IS NULL
                  AND dead_lettered_at IS NULL
                """,
                (run_id, target),
            ).fetchone()
            if int(pending["count"]) > 0:
                raise RuntimeRetentionError(
                    code="RUNTIME_RETENTION_EXPORT_PENDING",
                    run_id=run_id,
                    message="Events cannot be pruned before outbox delivery settles.",
                )
            deleted_events = conn.execute(
                "DELETE FROM runtime_events WHERE run_id = ? AND sequence <= ?",
                (run_id, target),
            ).rowcount
            deleted_outbox = conn.execute(
                """
                DELETE FROM runtime_outbox
                WHERE run_id = ? AND sequence <= ? AND delivered_at IS NOT NULL
                """,
                (run_id, target),
            ).rowcount
            conn.execute(
                """
                INSERT INTO runtime_event_retention(
                    run_id, pruned_through_sequence, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    pruned_through_sequence = excluded.pruned_through_sequence,
                    updated_at = excluded.updated_at
                """,
                (run_id, target, _now()),
            )
            return RetentionDecision(
                run_id=run_id,
                pruned_through_sequence=target,
                deleted_events=int(deleted_events),
                deleted_outbox_items=int(deleted_outbox),
            )


__all__ = [
    "CreateRunDecision",
    "DeadLetterAuditAction",
    "DeadLetterAuditRecord",
    "DeadLetterInspectionDecision",
    "DeadLetterItem",
    "DeadLetterRequeueDecision",
    "EVENT_SCHEMA_VERSION",
    "EventCursorError",
    "EventCursorExpiredError",
    "MAX_OUTBOX_DEFER_SECONDS",
    "MAX_RUNTIME_EVENT_PAYLOAD_BYTES",
    "OutboxItem",
    "OutboxDeadLetterConflictError",
    "OutboxDeadLetterMutationConflictError",
    "OutboxLeaseError",
    "OperationIdempotencyConflictError",
    "OperationNotFoundError",
    "OperationRevisionConflictError",
    "RUNTIME_SCHEMA_VERSION",
    "RetentionDecision",
    "RunOwner",
    "RuntimeEvent",
    "RuntimeEventAppendDecision",
    "RuntimeEventIdempotencyConflictError",
    "RuntimeEventInput",
    "RuntimeEventTerminalRunError",
    "RuntimeStore",
    "RuntimeStoreConnectionLostError",
    "RuntimeStoreDependencyError",
    "RuntimeStoreOverloadedError",
    "RuntimeRetentionError",
    "SQLiteRuntimeStore",
    "StartIdempotencyConflictError",
    "StoredApprovalDecision",
    "StoredLeaseClaimDecision",
    "StoredTransitionDecision",
    "StoredOperationDecision",
    "TerminalRecord",
    "decode_event_cursor",
    "encode_event_cursor",
]
