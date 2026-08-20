"""Pure, versioned run lifecycle domain and an in-memory conformance reference."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ..commands import Fork, Pause, Respond, Resume, RunCommand, Steer, command_to_dict
from .errors import HarnessError
from .security import freeze_data, thaw_data


class RunState(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_RECONCILIATION = "waiting_for_reconciliation"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_WITH_UNKNOWN_EFFECTS = "failed_with_unknown_effects"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_RUN_STATES = frozenset(
    {
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.FAILED_WITH_UNKNOWN_EFFECTS,
        RunState.CANCELLED,
        RunState.EXPIRED,
    }
)


class TransitionKind(StrEnum):
    QUEUE = "queue"
    START = "start"
    WAIT_FOR_INPUT = "wait_for_input"
    WAIT_FOR_APPROVAL = "wait_for_approval"
    WAIT_FOR_RECONCILIATION = "wait_for_reconciliation"
    PAUSE = "pause"
    RESUME = "resume"
    RESPOND = "respond"
    STEER = "steer"
    CLOSE_STEERING = "close_steering"
    REQUEST_CANCEL = "request_cancel"
    CONFIRM_CANCEL = "confirm_cancel"
    COMPLETE = "complete"
    FAIL = "fail"
    FAIL_WITH_UNKNOWN_EFFECTS = "fail_with_unknown_effects"
    EXPIRE = "expire"


_ALLOWED_FROM: dict[TransitionKind, frozenset[RunState]] = {
    TransitionKind.QUEUE: frozenset({RunState.CREATED}),
    TransitionKind.START: frozenset({RunState.CREATED, RunState.QUEUED}),
    TransitionKind.WAIT_FOR_INPUT: frozenset({RunState.RUNNING}),
    TransitionKind.WAIT_FOR_APPROVAL: frozenset({RunState.RUNNING}),
    TransitionKind.WAIT_FOR_RECONCILIATION: frozenset({RunState.RUNNING, RunState.CANCELLING}),
    TransitionKind.PAUSE: frozenset(
        {
            RunState.QUEUED,
            RunState.RUNNING,
            RunState.WAITING_FOR_INPUT,
            RunState.WAITING_FOR_APPROVAL,
        }
    ),
    TransitionKind.RESUME: frozenset({RunState.PAUSED, RunState.WAITING_FOR_RECONCILIATION}),
    TransitionKind.RESPOND: frozenset({RunState.WAITING_FOR_INPUT, RunState.WAITING_FOR_APPROVAL}),
    TransitionKind.STEER: frozenset({RunState.CREATED, RunState.QUEUED, RunState.RUNNING}),
    TransitionKind.CLOSE_STEERING: frozenset({RunState.CREATED, RunState.QUEUED, RunState.RUNNING}),
    TransitionKind.REQUEST_CANCEL: frozenset(set(RunState) - TERMINAL_RUN_STATES),
    TransitionKind.CONFIRM_CANCEL: frozenset({RunState.CANCELLING}),
    TransitionKind.COMPLETE: frozenset({RunState.RUNNING}),
    TransitionKind.FAIL: frozenset(set(RunState) - TERMINAL_RUN_STATES),
    TransitionKind.FAIL_WITH_UNKNOWN_EFFECTS: frozenset(
        {
            RunState.RUNNING,
            RunState.WAITING_FOR_INPUT,
            RunState.WAITING_FOR_APPROVAL,
            RunState.WAITING_FOR_RECONCILIATION,
            RunState.CANCELLING,
        }
    ),
    TransitionKind.EXPIRE: frozenset(set(RunState) - TERMINAL_RUN_STATES),
}


_TARGET_STATE: dict[TransitionKind, RunState | None] = {
    TransitionKind.QUEUE: RunState.QUEUED,
    TransitionKind.START: RunState.RUNNING,
    TransitionKind.WAIT_FOR_INPUT: RunState.WAITING_FOR_INPUT,
    TransitionKind.WAIT_FOR_APPROVAL: RunState.WAITING_FOR_APPROVAL,
    TransitionKind.WAIT_FOR_RECONCILIATION: RunState.WAITING_FOR_RECONCILIATION,
    TransitionKind.PAUSE: RunState.PAUSED,
    TransitionKind.RESUME: RunState.RUNNING,
    TransitionKind.RESPOND: RunState.RUNNING,
    TransitionKind.STEER: None,
    TransitionKind.CLOSE_STEERING: None,
    TransitionKind.REQUEST_CANCEL: RunState.CANCELLING,
    TransitionKind.CONFIRM_CANCEL: RunState.CANCELLED,
    TransitionKind.COMPLETE: RunState.COMPLETED,
    TransitionKind.FAIL: RunState.FAILED,
    TransitionKind.FAIL_WITH_UNKNOWN_EFFECTS: RunState.FAILED_WITH_UNKNOWN_EFFECTS,
    TransitionKind.EXPIRE: RunState.EXPIRED,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _require_id(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _digest(value: Any) -> str:
    canonical = json.dumps(
        thaw_data(freeze_data(value)),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


class RunNotFoundError(HarnessError):
    def __init__(self, run_id: str):
        super().__init__(
            code="RUN_NOT_FOUND",
            category="lifecycle",
            message="The requested run does not exist or is not visible.",
            retryable=False,
            details={"run_id": run_id},
        )


class RunAlreadyExistsError(HarnessError):
    def __init__(self, run_id: str):
        super().__init__(
            code="RUN_ALREADY_EXISTS",
            category="lifecycle",
            message=f"Run '{run_id}' already exists.",
            retryable=False,
            details={"run_id": run_id},
        )


class RunRevisionConflictError(HarnessError):
    def __init__(self, *, run_id: str, expected: int, actual: int):
        super().__init__(
            code="RUN_REVISION_CONFLICT",
            category="lifecycle",
            message=f"Run '{run_id}' changed before this operation could commit.",
            retryable=True,
            details={"run_id": run_id, "expected_revision": expected, "actual_revision": actual},
        )


class InvalidRunTransitionError(HarnessError):
    def __init__(self, *, run_id: str, state: RunState, transition: TransitionKind):
        super().__init__(
            code="RUN_TRANSITION_INVALID",
            category="lifecycle",
            message=(
                f"Transition '{transition.value}' is invalid while run '{run_id}' is {state.value}."
            ),
            retryable=False,
            details={"run_id": run_id, "state": state.value, "transition": transition.value},
        )


class RunTerminalError(HarnessError):
    def __init__(self, *, run_id: str, state: RunState):
        super().__init__(
            code="RUN_TERMINAL_IMMUTABLE",
            category="lifecycle",
            message=f"Run '{run_id}' is terminal ({state.value}) and cannot transition.",
            retryable=False,
            details={"run_id": run_id, "state": state.value},
        )


class LifecycleIdempotencyConflictError(HarnessError):
    def __init__(self, *, run_id: str, transition_id: str):
        super().__init__(
            code="RUN_TRANSITION_IDEMPOTENCY_CONFLICT",
            category="lifecycle",
            message="A lifecycle transition ID was reused with different content.",
            retryable=False,
            details={"run_id": run_id, "transition_id": transition_id},
        )


class SteeringClosedError(HarnessError):
    def __init__(self, run_id: str):
        super().__init__(
            code="RUN_STEERING_CLOSED",
            category="lifecycle",
            message=f"Run '{run_id}' has passed its steering safe point.",
            retryable=False,
            details={"run_id": run_id},
        )


@dataclass(frozen=True)
class RunSnapshot:
    """Authorized lifecycle projection for one logical run."""

    run_id: str
    state: RunState = RunState.CREATED
    revision: int = 0
    schema_version: str = "1.0"
    tenant_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    parent_run_id: str | None = None
    root_run_id: str | None = None
    child_depth: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    steering_open: bool = True
    pending_request_id: str | None = None
    last_transition_id: str | None = None
    last_reason_code: str | None = None
    metadata: Any = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _require_id(self.run_id, field_name="run_id"))
        object.__setattr__(self, "state", RunState(self.state))
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        if (
            isinstance(self.child_depth, bool)
            or not isinstance(self.child_depth, int)
            or not 0 <= self.child_depth <= 16
        ):
            raise ValueError("child_depth must be between 0 and 16")
        if self.parent_run_id is None:
            if self.root_run_id is not None or self.child_depth != 0:
                raise ValueError("root runs cannot declare child lineage")
        else:
            object.__setattr__(
                self,
                "parent_run_id",
                _require_id(self.parent_run_id, field_name="parent_run_id"),
            )
            object.__setattr__(
                self,
                "root_run_id",
                _require_id(str(self.root_run_id or ""), field_name="root_run_id"),
            )
            if self.child_depth <= 0:
                raise ValueError("child_depth must be positive for a child run")
            if self.run_id in {self.parent_run_id, self.root_run_id}:
                raise ValueError("child run lineage must use distinct run identities")
        if self.pending_request_id is not None:
            object.__setattr__(
                self,
                "pending_request_id",
                _require_id(self.pending_request_id, field_name="pending_request_id"),
            )
        object.__setattr__(self, "metadata", freeze_data(self.metadata))

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_RUN_STATES


@dataclass(frozen=True)
class LifecycleTransition:
    run_id: str
    kind: TransitionKind
    transition_id: str
    occurred_at: str = field(default_factory=_now)
    reason_code: str | None = None
    pending_request_id: str | None = None
    payload: Any = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _require_id(self.run_id, field_name="run_id"))
        object.__setattr__(
            self, "transition_id", _require_id(self.transition_id, field_name="transition_id")
        )
        object.__setattr__(self, "kind", TransitionKind(self.kind))
        if self.pending_request_id is not None:
            object.__setattr__(
                self,
                "pending_request_id",
                _require_id(self.pending_request_id, field_name="pending_request_id"),
            )
        object.__setattr__(self, "payload", freeze_data(self.payload))

    @property
    def digest(self) -> str:
        return _digest(
            {
                "run_id": self.run_id,
                "kind": self.kind.value,
                "transition_id": self.transition_id,
                "reason_code": self.reason_code,
                "pending_request_id": self.pending_request_id,
                "payload": thaw_data(self.payload),
            }
        )


@dataclass(frozen=True)
class TransitionDecision:
    before: RunSnapshot
    after: RunSnapshot
    transition: LifecycleTransition
    applied: bool
    idempotent: bool = False


@dataclass(frozen=True)
class CommandDecision:
    command: RunCommand
    snapshot: RunSnapshot
    transition: LifecycleTransition | None = None
    fork_from_step: int | str | None = None


def reduce_lifecycle(
    snapshot: RunSnapshot,
    transition: LifecycleTransition,
) -> TransitionDecision:
    """Apply one transition without I/O, locks, clocks, or hidden mutation."""
    if snapshot.run_id != transition.run_id:
        raise ValueError("snapshot and transition run_id must match")
    if snapshot.terminal:
        # Repeated cancellation observes the authoritative terminal state.
        if snapshot.state == RunState.CANCELLED and transition.kind in {
            TransitionKind.REQUEST_CANCEL,
            TransitionKind.CONFIRM_CANCEL,
        }:
            return TransitionDecision(
                snapshot, snapshot, transition, applied=False, idempotent=True
            )
        raise RunTerminalError(run_id=snapshot.run_id, state=snapshot.state)
    if transition.kind not in _ALLOWED_FROM or snapshot.state not in _ALLOWED_FROM[transition.kind]:
        raise InvalidRunTransitionError(
            run_id=snapshot.run_id,
            state=snapshot.state,
            transition=transition.kind,
        )
    if transition.kind == TransitionKind.STEER and not snapshot.steering_open:
        raise SteeringClosedError(snapshot.run_id)
    if transition.kind == TransitionKind.RESPOND:
        if not snapshot.pending_request_id:
            raise InvalidRunTransitionError(
                run_id=snapshot.run_id,
                state=snapshot.state,
                transition=transition.kind,
            )
        if transition.pending_request_id != snapshot.pending_request_id:
            raise HarnessError(
                code="RUN_RESPONSE_REQUEST_MISMATCH",
                category="lifecycle",
                message="Response does not match the run's pending request.",
                retryable=False,
                details={
                    "run_id": snapshot.run_id,
                    "request_id": transition.pending_request_id,
                },
            )
    if (
        transition.kind
        in {
            TransitionKind.WAIT_FOR_INPUT,
            TransitionKind.WAIT_FOR_APPROVAL,
        }
        and not transition.pending_request_id
    ):
        raise ValueError(f"{transition.kind.value} requires pending_request_id")

    target = _TARGET_STATE[transition.kind] or snapshot.state
    pending_request = snapshot.pending_request_id
    if transition.kind in {
        TransitionKind.WAIT_FOR_INPUT,
        TransitionKind.WAIT_FOR_APPROVAL,
    }:
        pending_request = transition.pending_request_id
    elif transition.kind in {
        TransitionKind.RESPOND,
        TransitionKind.REQUEST_CANCEL,
        TransitionKind.CONFIRM_CANCEL,
        TransitionKind.COMPLETE,
        TransitionKind.FAIL,
        TransitionKind.FAIL_WITH_UNKNOWN_EFFECTS,
        TransitionKind.EXPIRE,
    }:
        pending_request = None

    after = replace(
        snapshot,
        state=target,
        revision=snapshot.revision + 1,
        updated_at=transition.occurred_at,
        steering_open=(
            False
            if transition.kind
            in {
                TransitionKind.CLOSE_STEERING,
                TransitionKind.REQUEST_CANCEL,
                TransitionKind.COMPLETE,
                TransitionKind.FAIL,
                TransitionKind.FAIL_WITH_UNKNOWN_EFFECTS,
                TransitionKind.EXPIRE,
            }
            else snapshot.steering_open
        ),
        pending_request_id=pending_request,
        last_transition_id=transition.transition_id,
        last_reason_code=transition.reason_code,
    )
    return TransitionDecision(snapshot, after, transition, applied=True)


def command_decision(snapshot: RunSnapshot, command: RunCommand) -> CommandDecision:
    """Map one public command to a lifecycle intent without performing effects."""
    if isinstance(command, Fork):
        if snapshot.state == RunState.EXPIRED:
            raise RunTerminalError(run_id=snapshot.run_id, state=snapshot.state)
        return CommandDecision(
            command=command,
            snapshot=snapshot,
            fork_from_step=command.from_step,
        )
    if isinstance(command, Pause):
        kind = TransitionKind.PAUSE
        reason = command.reason
        pending_request_id = None
        payload: Any = {}
    elif isinstance(command, Resume):
        kind = TransitionKind.RESUME
        reason = None
        pending_request_id = None
        payload = {}
    elif isinstance(command, Respond):
        kind = TransitionKind.RESPOND
        reason = None
        pending_request_id = command.request_id
        payload = {"response_digest": _digest(thaw_data(command.payload))}
    elif isinstance(command, Steer):
        kind = TransitionKind.STEER
        reason = None
        pending_request_id = None
        payload = {"instruction_digest": _digest(command.instruction)}
    else:  # pragma: no cover - closed union defensive check
        raise TypeError(f"Unsupported run command: {type(command).__name__}")
    transition = LifecycleTransition(
        run_id=snapshot.run_id,
        kind=kind,
        transition_id=command.command_id,
        reason_code=reason,
        pending_request_id=pending_request_id,
        payload=payload,
    )
    return CommandDecision(command=command, snapshot=snapshot, transition=transition)


class InMemoryLifecycleStore:
    """Thread-safe reference implementation for reducer/store conformance tests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, RunSnapshot] = {}
        self._decisions: dict[tuple[str, str], tuple[str, TransitionDecision]] = {}

    def create(self, snapshot: RunSnapshot) -> RunSnapshot:
        with self._lock:
            if snapshot.run_id in self._runs:
                raise RunAlreadyExistsError(snapshot.run_id)
            self._runs[snapshot.run_id] = snapshot
            return snapshot

    def get(self, run_id: str) -> RunSnapshot:
        with self._lock:
            try:
                return self._runs[run_id]
            except KeyError:
                raise RunNotFoundError(run_id) from None

    def apply(
        self,
        transition: LifecycleTransition,
        *,
        expected_revision: int,
    ) -> TransitionDecision:
        with self._lock:
            prior = self._decisions.get((transition.run_id, transition.transition_id))
            if prior is not None:
                prior_digest, prior_decision = prior
                if prior_digest != transition.digest:
                    raise LifecycleIdempotencyConflictError(
                        run_id=transition.run_id,
                        transition_id=transition.transition_id,
                    )
                return replace(prior_decision, applied=False, idempotent=True)
            snapshot = self.get(transition.run_id)
            if snapshot.revision != expected_revision:
                raise RunRevisionConflictError(
                    run_id=transition.run_id,
                    expected=expected_revision,
                    actual=snapshot.revision,
                )
            decision = reduce_lifecycle(snapshot, transition)
            self._runs[transition.run_id] = decision.after
            self._decisions[(transition.run_id, transition.transition_id)] = (
                transition.digest,
                decision,
            )
            return decision

    def command(
        self,
        run_id: str,
        command: RunCommand,
        *,
        expected_revision: int,
    ) -> CommandDecision:
        with self._lock:
            snapshot = self.get(run_id)
            intent = command_decision(snapshot, command)
            if intent.transition is None:
                if snapshot.revision != expected_revision:
                    raise RunRevisionConflictError(
                        run_id=run_id,
                        expected=expected_revision,
                        actual=snapshot.revision,
                    )
                return intent
            transition_decision = self.apply(
                intent.transition,
                expected_revision=expected_revision,
            )
            return replace(intent, snapshot=transition_decision.after)


def command_digest(command: RunCommand) -> str:
    """Public deterministic digest used for command idempotency evidence."""
    return _digest(command_to_dict(command))


__all__ = [
    "CommandDecision",
    "InMemoryLifecycleStore",
    "InvalidRunTransitionError",
    "LifecycleIdempotencyConflictError",
    "LifecycleTransition",
    "RunAlreadyExistsError",
    "RunNotFoundError",
    "RunRevisionConflictError",
    "RunSnapshot",
    "RunState",
    "RunTerminalError",
    "SteeringClosedError",
    "TERMINAL_RUN_STATES",
    "TransitionDecision",
    "TransitionKind",
    "command_decision",
    "command_digest",
    "reduce_lifecycle",
]
