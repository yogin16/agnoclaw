"""Bounded, owner-scoped reconciliation for ambiguous external operations."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from .artifacts import ArtifactReference, ArtifactScope, ArtifactStore
from .context import ExecutionContext
from .errors import HarnessError
from .lifecycle import (
    LifecycleTransition,
    RunNotFoundError,
    RunSnapshot,
    RunState,
    TransitionKind,
)
from .operations import (
    EffectClass,
    OperationReconciliation,
    OperationReconciliationVerdict,
    OperationRecord,
    OperationState,
    OperationTransitionError,
)
from .run_handle import HarnessRun
from .store import (
    MAX_RECOVERY_MINIMUM_AGE_SECONDS,
    OperationRevisionConflictError,
    RunOwner,
    RuntimeStore,
)

OPERATION_RECONCILIATION_EVIDENCE_PURPOSE = "operation.reconciliation.evidence"
MAX_OPERATION_RECONCILIATION_BATCH_SIZE = 100
MAX_OPERATION_RECONCILIATION_CONCURRENCY = 32
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MAX_RUN_OPERATION_SAFETY_SCAN = 1000


@dataclass(frozen=True)
class RunOperationSafetyInspection:
    """Bounded view of unresolved external effects for one logical run."""

    ambiguous: OperationRecord | None
    complete: bool

    @property
    def blocks_dispatch(self) -> bool:
        return self.ambiguous is not None or not self.complete


def inspect_run_operation_safety(
    store: RuntimeStore,
    run_id: str,
) -> RunOperationSafetyInspection:
    """Find the first ambiguous effect without silently truncating a long run."""
    records = store.list_run_operations(
        run_id,
        limit=MAX_RUN_OPERATION_SAFETY_SCAN,
    )
    for record in records:
        if record.state is OperationState.UNKNOWN:
            return RunOperationSafetyInspection(ambiguous=record, complete=True)
        if record.state is OperationState.DISPATCHING and record.intent.effect_class in {
            EffectClass.COMPENSATABLE,
            EffectClass.NON_REPEATABLE,
        }:
            return RunOperationSafetyInspection(ambiguous=record, complete=True)
    return RunOperationSafetyInspection(
        ambiguous=None,
        # Exactly hitting the store limit is conservatively incomplete: there
        # may be an unresolved operation after the returned ordered prefix.
        complete=len(records) < MAX_RUN_OPERATION_SAFETY_SCAN,
    )


def model_operation_has_unknown_effects(store: RuntimeStore, run_id: str) -> bool:
    """Return whether any run-owned external effect is unresolved or unscanned."""
    try:
        return inspect_run_operation_safety(store, run_id).blocks_dispatch
    except RunNotFoundError:
        return False


def wait_for_model_operation_reconciliation(
    store: RuntimeStore,
    run_id: str,
) -> RunSnapshot:
    """Idempotently park one nonterminal run at its explicit reconciliation boundary."""
    snapshot = store.get_run(run_id)
    if snapshot.terminal or snapshot.state is RunState.WAITING_FOR_RECONCILIATION:
        return snapshot
    return store.apply_transition(
        LifecycleTransition(
            run_id=run_id,
            kind=TransitionKind.WAIT_FOR_RECONCILIATION,
            # Scope the transition id to the revision being parked: retries of
            # this park replay idempotently, while a later reconciliation cycle
            # (new revision after RESUME) must apply a fresh transition instead
            # of replaying the first cycle's historical decision.
            transition_id=f"{run_id}:wait-reconciliation:r{snapshot.revision}",
            reason_code="MODEL_OPERATION_OUTCOME_UNKNOWN",
        ),
        expected_revision=snapshot.revision,
    ).lifecycle.after


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _require_digest(value: str, *, name: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical sha256 digest")


def _owner_digest(owner: RunOwner) -> str:
    payload = json.dumps(
        {"tenant_id": owner.tenant_id, "user_id": owner.user_id},
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


class OperationReconciliationCursorError(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            code="OPERATION_RECONCILIATION_CURSOR_INVALID",
            category="operation",
            message="The reconciliation cursor is invalid for this owner.",
            retryable=False,
        )


class OperationReconciliationStatus(StrEnum):
    RECONCILED = "reconciled"
    CONTINUED = "continued"
    DEFERRED = "deferred"
    STALE = "stale"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class OperationReconciliationRequest:
    record: OperationRecord

    @property
    def run_id(self) -> str:
        return self.record.intent.run_id


@dataclass(frozen=True, slots=True)
class OperationReconciliationObservation:
    operation_id: str
    expected_revision: int
    operation_digest: str
    verdict: OperationReconciliationVerdict
    evidence_artifacts: tuple[ArtifactReference, ...]
    result_reference: str | None = None
    provider_request_id: str | None = None
    observed_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id.strip():
            raise ValueError("operation_id must be a non-empty string")
        if self.expected_revision < 0:
            raise ValueError("expected_revision cannot be negative")
        _require_digest(self.operation_digest, name="operation_digest")
        object.__setattr__(self, "verdict", OperationReconciliationVerdict(self.verdict))
        object.__setattr__(self, "evidence_artifacts", tuple(self.evidence_artifacts))
        if not 1 <= len(self.evidence_artifacts) <= 16:
            raise ValueError("evidence_artifacts must contain between 1 and 16 items")
        if any(not isinstance(item, ArtifactReference) for item in self.evidence_artifacts):
            raise TypeError("evidence_artifacts must contain ArtifactReference values")
        artifact_ids = tuple(item.artifact_id for item in self.evidence_artifacts)
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("evidence_artifacts must be unique")
        if self.verdict is OperationReconciliationVerdict.SUCCEEDED:
            if self.result_reference not in artifact_ids:
                raise ValueError("successful observation requires a referenced result artifact")
        elif self.result_reference is not None:
            raise ValueError("only a successful observation may include result_reference")
        try:
            observed_at = datetime.fromisoformat(self.observed_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("observed_at must be an ISO-8601 timestamp") from exc
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must include a timezone")

    @property
    def digest(self) -> str:
        payload = json.dumps(
            {
                "operation_id": self.operation_id,
                "expected_revision": self.expected_revision,
                "operation_digest": self.operation_digest,
                "verdict": self.verdict.value,
                "evidence": [
                    [item.artifact_id, item.storage_identity_digest]
                    for item in self.evidence_artifacts
                ],
                "result_reference": self.result_reference,
                "provider_request_id": self.provider_request_id,
                "observed_at": self.observed_at,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


@runtime_checkable
class OperationReconciliationObserver(Protocol):
    """Host-supplied, versioned observer that reads external state without dispatch."""

    async def observe(
        self,
        request: OperationReconciliationRequest,
    ) -> OperationReconciliationObservation | None: ...


@dataclass(frozen=True, slots=True)
class OperationReconciliationItem:
    operation_id: str
    run_id: str
    status: OperationReconciliationStatus
    observed_revision: int
    resulting_state: OperationState | None = None
    resulting_revision: int | None = None
    run_state: RunState | None = None
    reconciliation_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class OperationReconciliationBatch:
    items: tuple[OperationReconciliationItem, ...]
    next_cursor: str | None

    @property
    def reconciled(self) -> int:
        return sum(item.status is OperationReconciliationStatus.RECONCILED for item in self.items)

    @property
    def continued(self) -> int:
        return sum(item.status is OperationReconciliationStatus.CONTINUED for item in self.items)


RecoverRun = Callable[..., Awaitable[HarnessRun]]


async def _commit_store_call(call: Callable[[], Any]) -> tuple[Any, bool]:
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task), False
    except asyncio.CancelledError:
        return await asyncio.shield(task), True


def _encode_cursor(*, owner: RunOwner, after_operation_id: str) -> str:
    payload = json.dumps(
        {"v": 1, "scope": _owner_digest(owner), "after": after_operation_id},
        separators=(",", ":"),
        sort_keys=True,
    )
    token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"operation_reconciliation_v1_{token}"


def _decode_cursor(cursor: str | None, *, owner: RunOwner) -> str | None:
    if cursor is None:
        return None
    try:
        prefix = "operation_reconciliation_v1_"
        if not isinstance(cursor, str) or not cursor.startswith(prefix) or len(cursor) > 4096:
            raise ValueError
        token = cursor.removeprefix(prefix)
        payload = json.loads(
            base64.b64decode(
                token + "=" * (-len(token) % 4), altchars=b"-_", validate=True
            ).decode()
        )
        if (
            not isinstance(payload, dict)
            or payload.get("v") != 1
            or payload.get("scope") != _owner_digest(owner)
            or not isinstance(payload.get("after"), str)
            or not payload["after"].strip()
        ):
            raise ValueError
        return payload["after"]
    except (binascii.Error, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise OperationReconciliationCursorError() from exc


async def reconcile_pending_operations(
    *,
    store: RuntimeStore,
    artifact_store: ArtifactStore,
    observer: OperationReconciliationObserver,
    observer_digest: str,
    recover_run: RecoverRun,
    default_owner: RunOwner,
    context: ExecutionContext | None = None,
    cursor: str | None = None,
    limit: int = 25,
    concurrency: int = 4,
    minimum_age_seconds: int = 30,
) -> OperationReconciliationBatch:
    """Observe and CAS-settle one page; never infer an outcome or replay an effect."""
    _require_digest(observer_digest, name="observer_digest")
    if not callable(getattr(observer, "observe", None)):
        raise TypeError("observer must provide an async observe method")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if (
        not isinstance(concurrency, int)
        or isinstance(concurrency, bool)
        or not 1 <= concurrency <= 32
    ):
        raise ValueError("concurrency must be between 1 and 32")
    if (
        not isinstance(minimum_age_seconds, int)
        or isinstance(minimum_age_seconds, bool)
        or not 0 <= minimum_age_seconds <= MAX_RECOVERY_MINIMUM_AGE_SECONDS
    ):
        raise ValueError("minimum_age_seconds must be between 0 and 86400")
    owner = RunOwner(context.tenant_id, context.user_id) if context is not None else default_owner
    after = _decode_cursor(cursor, owner=owner)
    candidates = await asyncio.to_thread(
        store.list_reconciliation_operations,
        owner=owner,
        after_operation_id=after,
        minimum_age_seconds=minimum_age_seconds,
        limit=limit + 1,
    )
    page = candidates[:limit]
    next_cursor = (
        _encode_cursor(owner=owner, after_operation_id=page[-1].intent.operation_id)
        if len(candidates) > limit
        else None
    )
    gate = asyncio.Semaphore(min(concurrency, limit))

    def outcome(
        record: OperationRecord,
        status: OperationReconciliationStatus,
        *,
        error_code: str | None = None,
    ) -> OperationReconciliationItem:
        return OperationReconciliationItem(
            operation_id=record.intent.operation_id,
            run_id=record.intent.run_id,
            status=status,
            observed_revision=record.revision,
            error_code=error_code,
        )

    async def process(record: OperationRecord) -> OperationReconciliationItem:
        request = OperationReconciliationRequest(record)
        run_state: RunState | None
        if record.state in {
            OperationState.SUCCEEDED,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }:
            try:
                handle = await recover_run(record.intent.run_id, context=context)
                run_state = (await handle.status()).state
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return outcome(
                    record,
                    OperationReconciliationStatus.FAILED,
                    error_code=(
                        exc.code
                        if isinstance(exc, HarnessError)
                        else "OPERATION_RECONCILIATION_CONTINUATION_FAILED"
                    ),
                )
            return OperationReconciliationItem(
                operation_id=record.intent.operation_id,
                run_id=record.intent.run_id,
                status=OperationReconciliationStatus.CONTINUED,
                observed_revision=record.revision,
                resulting_state=record.state,
                resulting_revision=record.revision,
                run_state=run_state,
            )
        try:
            async with gate:
                observation = await observer.observe(request)
            if observation is None:
                return outcome(record, OperationReconciliationStatus.DEFERRED)
            if not isinstance(observation, OperationReconciliationObservation):
                return outcome(
                    record,
                    OperationReconciliationStatus.REJECTED,
                    error_code="OPERATION_RECONCILIATION_OBSERVATION_INVALID",
                )
            if (
                observation.operation_id != record.intent.operation_id
                or observation.expected_revision != record.revision
                or observation.operation_digest != record.digest
            ):
                return outcome(
                    record,
                    OperationReconciliationStatus.REJECTED,
                    error_code="OPERATION_RECONCILIATION_OBSERVATION_MISMATCH",
                )
            scope = ArtifactScope(
                run_id=record.intent.run_id,
                tenant_id=owner.tenant_id,
                user_id=owner.user_id,
            )
            for reference in observation.evidence_artifacts:
                if (
                    reference.scope != scope
                    or reference.purpose != OPERATION_RECONCILIATION_EVIDENCE_PURPOSE
                ):
                    return outcome(
                        record,
                        OperationReconciliationStatus.REJECTED,
                        error_code="OPERATION_RECONCILIATION_EVIDENCE_SCOPE_MISMATCH",
                    )
                await artifact_store.read(reference, offset=0, limit=1)
            identity = hashlib.sha256(
                f"{observer_digest}:{observation.digest}".encode()
            ).hexdigest()[:32]
            reconciliation = OperationReconciliation(
                reconciliation_id=f"oprec_{identity}",
                operation_id=record.intent.operation_id,
                expected_revision=record.revision,
                operation_digest=record.digest,
                verdict=observation.verdict,
                observer_digest=observer_digest,
                evidence_artifact_ids=tuple(
                    item.artifact_id for item in observation.evidence_artifacts
                ),
                result_reference=observation.result_reference,
                provider_request_id=observation.provider_request_id,
                reconciled_at=observation.observed_at,
            )
            decision, cancelled = await _commit_store_call(
                lambda: store.reconcile_operation(
                    record.intent.operation_id,
                    mutation_id=reconciliation.reconciliation_id,
                    reconciliation=reconciliation,
                    evidence_artifacts=observation.evidence_artifacts,
                    owner=owner,
                )
            )
            if cancelled:
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            raise
        except (OperationRevisionConflictError, OperationTransitionError):
            return outcome(record, OperationReconciliationStatus.STALE)
        except Exception as exc:
            return outcome(
                record,
                OperationReconciliationStatus.FAILED,
                error_code=(
                    exc.code if isinstance(exc, HarnessError) else "OPERATION_RECONCILIATION_FAILED"
                ),
            )
        try:
            handle = await recover_run(record.intent.run_id, context=context)
            run_state = (await handle.status()).state
        except asyncio.CancelledError:
            raise
        except Exception:
            run_state = None
        return OperationReconciliationItem(
            operation_id=record.intent.operation_id,
            run_id=record.intent.run_id,
            status=OperationReconciliationStatus.RECONCILED,
            observed_revision=record.revision,
            resulting_state=decision.record.state,
            resulting_revision=decision.record.revision,
            run_state=run_state,
            reconciliation_id=reconciliation.reconciliation_id,
        )

    items = await asyncio.gather(*(process(record) for record in page))
    return OperationReconciliationBatch(items=tuple(items), next_cursor=next_cursor)


__all__ = [
    "MAX_RUN_OPERATION_SAFETY_SCAN",
    "MAX_OPERATION_RECONCILIATION_BATCH_SIZE",
    "MAX_OPERATION_RECONCILIATION_CONCURRENCY",
    "OPERATION_RECONCILIATION_EVIDENCE_PURPOSE",
    "OperationReconciliationBatch",
    "OperationReconciliationCursorError",
    "OperationReconciliationItem",
    "OperationReconciliationObservation",
    "OperationReconciliationObserver",
    "OperationReconciliationRequest",
    "OperationReconciliationStatus",
    "RunOperationSafetyInspection",
    "inspect_run_operation_safety",
    "model_operation_has_unknown_effects",
    "reconcile_pending_operations",
    "wait_for_model_operation_reconciliation",
]
