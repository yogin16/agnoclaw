"""Owner-authorized, content-free inspection of durable runs."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .context import ExecutionContext
from .errors import HarnessError
from .lifecycle import RunSnapshot, RunState
from .operations import OperationRecord, OperationState
from .store import RunNotFoundError, RunOwner, RuntimeEvent, RuntimeStore
from .telemetry import RuntimeTelemetryPolicy, normalize_runtime_event_type

RUN_INSPECTION_SCHEMA_VERSION = "1.0"
RUN_INSPECT_SCOPE = "runtime:run:inspect"


class RunRecoveryRecommendation(StrEnum):
    """Content-free operator action suggested by durable state only."""

    NONE = "none"
    WAIT = "wait"
    START = "start"
    RESPOND = "respond"
    REVIEW_APPROVAL = "review_approval"
    RECONCILE = "reconcile"
    RESUME = "resume"


class RunInspectionAuthorizationError(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            code="RUN_INSPECTION_NOT_AUTHORIZED",
            category="authorization",
            message="The execution context cannot inspect this run.",
            retryable=False,
        )


def _recommendation(
    snapshot: RunSnapshot,
    operations: tuple[OperationRecord, ...],
) -> RunRecoveryRecommendation:
    if snapshot.terminal:
        return RunRecoveryRecommendation.NONE
    if any(operation.state is OperationState.UNKNOWN for operation in operations):
        return RunRecoveryRecommendation.RECONCILE
    if snapshot.state is RunState.WAITING_FOR_INPUT:
        return RunRecoveryRecommendation.RESPOND
    if snapshot.state is RunState.WAITING_FOR_APPROVAL:
        return RunRecoveryRecommendation.REVIEW_APPROVAL
    if snapshot.state is RunState.WAITING_FOR_RECONCILIATION:
        return RunRecoveryRecommendation.RECONCILE
    if snapshot.state is RunState.PAUSED:
        return RunRecoveryRecommendation.RESUME
    if snapshot.state is RunState.CREATED:
        return RunRecoveryRecommendation.START
    return RunRecoveryRecommendation.WAIT


@dataclass(frozen=True, slots=True)
class InspectedOperation:
    """Safe operation projection; request target and metadata are never included."""

    operation_id_hash: str
    kind: str
    effect_class: str
    state: str
    revision: int
    dispatch_attempt: int
    updated_at: str

    @classmethod
    def project(
        cls,
        operation: OperationRecord,
        *,
        policy: RuntimeTelemetryPolicy,
    ) -> InspectedOperation:
        return cls(
            operation_id_hash=policy.identifier_digest(
                operation.intent.operation_id,
                domain="operation",
            ),
            kind=operation.intent.kind.value,
            effect_class=operation.intent.effect_class.value,
            state=operation.state.value,
            revision=operation.revision,
            dispatch_attempt=operation.dispatch_attempt,
            updated_at=operation.updated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id_hash": self.operation_id_hash,
            "kind": self.kind,
            "effect_class": self.effect_class,
            "state": self.state,
            "revision": self.revision,
            "dispatch_attempt": self.dispatch_attempt,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class RunInspection:
    """Bounded support/recovery report with no arbitrary runtime content."""

    run_id_hash: str
    state: str
    revision: int
    created_at: str
    updated_at: str
    child_depth: int
    parent_run_id_hash: str | None
    pending_request_id_hash: str | None
    event_count_inspected: int
    events_at_limit: bool
    last_event_sequence: int | None
    last_event_type: str | None
    operation_count_inspected: int
    operations_at_limit: bool
    operation_state_counts: tuple[tuple[str, int], ...]
    operations: tuple[InspectedOperation, ...]
    approval_count_inspected: int
    approvals_at_limit: bool
    pending_approval_count: int
    child_count_inspected: int
    children_at_limit: bool
    child_state_counts: tuple[tuple[str, int], ...]
    artifact_count_inspected: int
    artifacts_at_limit: bool
    artifact_bytes_inspected: int
    terminal_result_available: bool
    terminal_error_available: bool
    recommendation: RunRecoveryRecommendation
    schema_version: str = RUN_INSPECTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RUN_INSPECTION_SCHEMA_VERSION:
            raise ValueError("unsupported run inspection schema version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id_hash": self.run_id_hash,
            "state": self.state,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "child_depth": self.child_depth,
            "parent_run_id_hash": self.parent_run_id_hash,
            "pending_request_id_hash": self.pending_request_id_hash,
            "events": {
                "count_inspected": self.event_count_inspected,
                "at_limit": self.events_at_limit,
                "last_sequence": self.last_event_sequence,
                "last_type": self.last_event_type,
            },
            "operations": {
                "count_inspected": self.operation_count_inspected,
                "at_limit": self.operations_at_limit,
                "state_counts": dict(self.operation_state_counts),
                "items": [item.to_dict() for item in self.operations],
            },
            "approvals": {
                "count_inspected": self.approval_count_inspected,
                "at_limit": self.approvals_at_limit,
                "pending": self.pending_approval_count,
            },
            "children": {
                "count_inspected": self.child_count_inspected,
                "at_limit": self.children_at_limit,
                "state_counts": dict(self.child_state_counts),
            },
            "artifacts": {
                "count_inspected": self.artifact_count_inspected,
                "at_limit": self.artifacts_at_limit,
                "bytes_inspected": self.artifact_bytes_inspected,
            },
            "terminal": {
                "result_available": self.terminal_result_available,
                "error_available": self.terminal_error_available,
            },
            "recommendation": self.recommendation.value,
        }


class RuntimeRunInspector:
    """Build content-free recovery reports after exact owner/scope authorization."""

    def __init__(
        self,
        *,
        store: RuntimeStore,
        policy: RuntimeTelemetryPolicy,
    ) -> None:
        if not isinstance(policy, RuntimeTelemetryPolicy):
            raise TypeError("policy must be a RuntimeTelemetryPolicy")
        self._store = store
        self._policy = policy

    async def inspect(
        self,
        run_id: str,
        *,
        context: ExecutionContext,
        event_limit: int = 1000,
        operation_limit: int = 1000,
        approval_limit: int = 1000,
        artifact_limit: int = 1000,
    ) -> RunInspection:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if not isinstance(context, ExecutionContext):
            raise TypeError("context must be an ExecutionContext")
        if context.user_id is None or RUN_INSPECT_SCOPE not in context.scopes:
            raise RunInspectionAuthorizationError()
        for name, value in (
            ("event_limit", event_limit),
            ("operation_limit", operation_limit),
            ("approval_limit", approval_limit),
            ("artifact_limit", artifact_limit),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
                raise ValueError(f"{name} must be between 1 and 1000")

        owner = RunOwner(tenant_id=context.tenant_id, user_id=context.user_id)
        try:
            snapshot = await asyncio.to_thread(self._store.get_run, run_id, owner=owner)
        except RunNotFoundError as exc:
            raise RunInspectionAuthorizationError() from exc

        events = await self._events(run_id, owner=owner, limit=event_limit)
        operations = tuple(
            await asyncio.to_thread(
                self._store.list_run_operations,
                run_id,
                limit=operation_limit,
                owner=owner,
            )
        )
        approvals = tuple(
            await asyncio.to_thread(
                self._store.list_approvals,
                run_id,
                limit=approval_limit,
                owner=owner,
            )
        )
        children = tuple(
            await asyncio.to_thread(
                self._store.list_children,
                run_id,
                limit=64,
                owner=owner,
            )
        )
        artifacts = tuple(
            await asyncio.to_thread(
                self._store.list_artifacts,
                run_id,
                limit=artifact_limit,
                owner=owner,
            )
        )
        terminal = await asyncio.to_thread(self._store.get_terminal, run_id, owner=owner)

        projected_operations = tuple(
            InspectedOperation.project(item, policy=self._policy) for item in operations
        )
        return RunInspection(
            run_id_hash=self._policy.identifier_digest(snapshot.run_id, domain="run"),
            state=snapshot.state.value,
            revision=snapshot.revision,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
            child_depth=snapshot.child_depth,
            parent_run_id_hash=(
                self._policy.identifier_digest(snapshot.parent_run_id, domain="run")
                if snapshot.parent_run_id is not None
                else None
            ),
            pending_request_id_hash=(
                self._policy.identifier_digest(snapshot.pending_request_id, domain="request")
                if snapshot.pending_request_id is not None
                else None
            ),
            event_count_inspected=len(events),
            events_at_limit=len(events) == event_limit,
            last_event_sequence=events[-1].sequence if events else None,
            last_event_type=(
                normalize_runtime_event_type(events[-1].event_type) if events else None
            ),
            operation_count_inspected=len(operations),
            operations_at_limit=len(operations) == operation_limit,
            operation_state_counts=tuple(
                sorted(Counter(item.state.value for item in operations).items())
            ),
            operations=projected_operations,
            approval_count_inspected=len(approvals),
            approvals_at_limit=len(approvals) == approval_limit,
            pending_approval_count=sum(item.state.value == "pending" for item in approvals),
            child_count_inspected=len(children),
            children_at_limit=len(children) == 64,
            child_state_counts=tuple(
                sorted(Counter(item.state.value for item in children).items())
            ),
            artifact_count_inspected=len(artifacts),
            artifacts_at_limit=len(artifacts) == artifact_limit,
            artifact_bytes_inspected=sum(item.size_bytes for item in artifacts),
            terminal_result_available=terminal is not None and terminal.value is not None,
            terminal_error_available=terminal is not None and terminal.error is not None,
            recommendation=_recommendation(snapshot, operations),
        )

    async def _events(
        self,
        run_id: str,
        *,
        owner: RunOwner,
        limit: int,
    ) -> tuple[RuntimeEvent, ...]:
        items: list[RuntimeEvent] = []
        after_sequence = 0
        while len(items) < limit:
            page = await asyncio.to_thread(
                self._store.list_events,
                run_id,
                after_sequence=after_sequence,
                limit=min(100, limit - len(items)),
                owner=owner,
            )
            if not page:
                break
            items.extend(page)
            after_sequence = page[-1].sequence
            if len(page) < min(100, limit - len(items) + len(page)):
                break
        return tuple(items)


__all__ = [
    "InspectedOperation",
    "RUN_INSPECTION_SCHEMA_VERSION",
    "RUN_INSPECT_SCOPE",
    "RunInspection",
    "RunInspectionAuthorizationError",
    "RunRecoveryRecommendation",
    "RuntimeRunInspector",
]
