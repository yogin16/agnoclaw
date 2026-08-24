"""Async-first universal operation gateway over persisted intent and settlement."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .artifacts import ArtifactReference, ArtifactScope, ArtifactStore
from .errors import HarnessError
from .operations import (
    EffectClass,
    OperationIntent,
    OperationRecord,
    OperationSettlement,
    OperationSettlementEvidence,
    OperationState,
    RecoveryAction,
    recovery_action,
)
from .security import thaw_data
from .store import OperationRevisionConflictError, RuntimeStore

Dispatch = Callable[[], Any | Awaitable[Any]]
PreDispatch = Callable[[], None | Awaitable[None]]
ResultReferenceFactory = Callable[[Any], str | Awaitable[str]]
ResultLoader = Callable[[str], Any | Awaitable[Any]]
ResultSerializer = Callable[[Any], Any | Awaitable[Any]]
SettlementEvidenceFactory = Callable[[Any], OperationSettlementEvidence]


@dataclass(frozen=True)
class OperationExecution:
    record: OperationRecord
    value: Any
    replayed: bool = False


class OperationInFlightError(HarnessError):
    def __init__(self, record: OperationRecord):
        super().__init__(
            code="OPERATION_ALREADY_DISPATCHING",
            category="operation",
            message="Another worker owns or may own this operation dispatch.",
            retryable=True,
            details={
                "operation_id": record.intent.operation_id,
                "worker_id": record.worker_id,
                "fence_token": record.fence_token,
            },
        )


class OperationReconciliationRequiredError(HarnessError):
    def __init__(self, record: OperationRecord):
        super().__init__(
            code="OPERATION_RECONCILIATION_REQUIRED",
            category="operation",
            message="The operation outcome is ambiguous and must be reconciled.",
            retryable=False,
            details={
                "operation_id": record.intent.operation_id,
                "effect_class": record.intent.effect_class.value,
                "state": record.state.value,
            },
        )


class OperationDispatchDeferredError(HarnessError):
    """Signal that a composed operation must remain in-flight and fail closed.

    A parent gateway must not manufacture a known failure when a nested operation
    still has an ambiguous external outcome or its safety scan was incomplete.
    Leaving the parent dispatch fenced lets recovery resume it only after the
    nested boundary has been independently settled.
    """

    def __init__(self, *, run_id: str, reason_code: str):
        super().__init__(
            code="OPERATION_DISPATCH_DEFERRED",
            category="operation",
            message="Operation dispatch is deferred at an unresolved nested boundary.",
            retryable=False,
            details={"run_id": run_id, "reason_code": reason_code},
        )


class OperationResultUnavailableError(HarnessError):
    def __init__(self, record: OperationRecord):
        super().__init__(
            code="OPERATION_RESULT_UNAVAILABLE",
            category="operation",
            message=(
                "The operation already succeeded, but its referenced result is not "
                "loaded in this process. Configure a result loader or ArtifactStore."
            ),
            retryable=False,
            details={
                "operation_id": record.intent.operation_id,
                "result_reference": (
                    record.settlement.result_reference if record.settlement else None
                ),
            },
        )


class OperationTerminalError(HarnessError):
    def __init__(self, record: OperationRecord):
        settlement = record.settlement
        safe_error = thaw_data(settlement.safe_error) if settlement is not None else None
        raw_code = safe_error.get("code") if isinstance(safe_error, dict) else None
        code: str = raw_code if isinstance(raw_code, str) else "OPERATION_TERMINAL_FAILURE"
        super().__init__(
            code=code,
            category="operation",
            message=f"Operation ended in state '{record.state.value}'.",
            retryable=False,
            details={
                "operation_id": record.intent.operation_id,
                "state": record.state.value,
                "safe_error": safe_error,
            },
        )
        self.record = record
        self.safe_error = safe_error


class OperationGateway:
    """Persist intent, fence dispatch, and persist one canonical settlement."""

    def __init__(
        self,
        store: RuntimeStore,
        *,
        worker_id: str,
        result_reference_factory: ResultReferenceFactory | None = None,
        result_loader: ResultLoader | None = None,
        artifact_store: ArtifactStore | None = None,
        artifact_purpose: str = "operation_result",
        result_serializer: ResultSerializer | None = None,
        result_cache_size: int = 128,
    ) -> None:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        if result_cache_size < 0:
            raise ValueError("result_cache_size cannot be negative")
        if not isinstance(artifact_purpose, str) or not artifact_purpose.strip():
            raise ValueError("artifact_purpose must be a non-empty string")
        if len(artifact_purpose) > 512:
            raise ValueError("artifact_purpose cannot exceed 512 characters")
        self.store = store
        self.worker_id = worker_id
        self._reference_factory = result_reference_factory or self._digest_reference
        self._result_loader = result_loader
        self._artifact_store = artifact_store
        self._artifact_purpose = artifact_purpose
        self._result_serializer = result_serializer
        self._result_cache_size = result_cache_size
        self._result_cache: OrderedDict[str, Any] = OrderedDict()

    @staticmethod
    def _digest_reference(value: Any) -> str:
        try:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError):
            payload = f"{value.__class__.__module__}.{value.__class__.__qualname__}"
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"result:sha256:{digest}"

    @staticmethod
    async def _resolve(value: Any | Awaitable[Any]) -> Any:
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _safe_error(exc: BaseException, *, code: str) -> dict[str, Any]:
        if isinstance(exc, HarnessError):
            return {
                "code": exc.code,
                "category": exc.category,
                "retryable": exc.retryable,
                "exception_type": exc.__class__.__name__,
            }
        return {
            "code": code,
            "category": "operation",
            "retryable": False,
            "exception_type": exc.__class__.__name__,
        }

    def _cache(self, operation_id: str, value: Any) -> None:
        if self._result_cache_size == 0:
            return
        self._result_cache[operation_id] = value
        self._result_cache.move_to_end(operation_id)
        while len(self._result_cache) > self._result_cache_size:
            self._result_cache.popitem(last=False)

    async def _completed_value(self, record: OperationRecord) -> OperationExecution:
        operation_id = record.intent.operation_id
        if operation_id in self._result_cache:
            value = self._result_cache[operation_id]
            self._result_cache.move_to_end(operation_id)
            return OperationExecution(record=record, value=value, replayed=True)
        reference = record.settlement.result_reference if record.settlement else None
        if reference is None:
            raise OperationResultUnavailableError(record)
        if self._artifact_store is not None:
            artifact = await asyncio.to_thread(self.store.get_artifact, reference)
            value = await self._artifact_store.load_json(artifact)
        elif self._result_loader is not None:
            value = await self._resolve(self._result_loader(reference))
        else:
            raise OperationResultUnavailableError(record)
        self._cache(operation_id, value)
        return OperationExecution(record=record, value=value, replayed=True)

    async def _stage_result(
        self,
        intent: OperationIntent,
        value: Any,
    ) -> tuple[str, ArtifactReference | None]:
        if self._artifact_store is None:
            reference = await self._resolve(self._reference_factory(value))
            if not isinstance(reference, str) or not reference.strip():
                raise HarnessError(
                    code="OPERATION_RESULT_REFERENCE_INVALID",
                    category="operation",
                    message="Result reference factory returned an invalid reference.",
                    retryable=False,
                    details={"operation_id": intent.operation_id},
                )
            return reference, None
        snapshot = await asyncio.to_thread(self.store.get_run, intent.run_id)
        serialized = (
            await self._resolve(self._result_serializer(value))
            if self._result_serializer is not None
            else value
        )
        artifact = await self._artifact_store.stage_json(
            serialized,
            scope=ArtifactScope(
                run_id=snapshot.run_id,
                tenant_id=snapshot.tenant_id,
                user_id=snapshot.user_id,
            ),
            purpose=self._artifact_purpose,
            metadata={
                "operation_id": intent.operation_id,
                "result_slot_id": intent.result_slot_id,
                "attempt_id": intent.attempt_id,
                "kind": intent.kind.value,
            },
        )
        return artifact.artifact_id, artifact

    @staticmethod
    async def _commit_store_call(
        call: Callable[[], Any],
    ) -> tuple[Any, bool]:
        """Finish a ledger mutation even when its caller is cancelled.

        Cancelling ``asyncio.to_thread`` does not stop the database thread.  A
        bare await can therefore let the caller observe cancellation while the
        mutation commits later.  Returning the cancellation bit lets the
        gateway durably classify that boundary before propagating cancellation.
        """
        task = asyncio.create_task(asyncio.to_thread(call))
        try:
            return await asyncio.shield(task), False
        except asyncio.CancelledError:
            return await asyncio.shield(task), True

    async def _cancel_before_dispatch(self, record: OperationRecord) -> OperationRecord:
        decision, _cancelled_again = await self._commit_store_call(
            lambda: self.store.settle_operation(
                record.intent.operation_id,
                mutation_id=f"{record.intent.operation_id}:cancel:pre-dispatch",
                expected_revision=record.revision,
                fence_token=record.fence_token,
                settlement=OperationSettlement(
                    state=OperationState.CANCELLED,
                    safe_error={
                        "code": "OPERATION_CANCELLED_BEFORE_DISPATCH",
                        "category": "operation",
                        "retryable": False,
                    },
                ),
            )
        )
        return decision.record

    async def execute(
        self,
        intent: OperationIntent,
        dispatch: Dispatch,
        *,
        pre_dispatch: PreDispatch | None = None,
        settlement_evidence: SettlementEvidenceFactory | None = None,
    ) -> OperationExecution:
        """Execute once. Existing in-flight work is never implicitly stolen.

        ``pre_dispatch`` is the final no-effect authority checkpoint.  It runs only
        after the operation owns a durable dispatch fence and before the external
        callable is entered.  A failure or cancellation at this boundary is therefore
        settled as known ``failed``/``cancelled`` even for a non-repeatable operation.
        """
        prepared, cancelled = await self._commit_store_call(
            lambda: self.store.prepare_operation(intent)
        )
        record = prepared.record
        if cancelled:
            if record.state is OperationState.PLANNED:
                await self._cancel_before_dispatch(record)
            raise asyncio.CancelledError
        if record.state is OperationState.SUCCEEDED:
            return await self._completed_value(record)
        if record.terminal:
            raise OperationTerminalError(record)
        if record.state is OperationState.DISPATCHING:
            raise OperationInFlightError(record)
        fence_token = record.fence_token + 1
        mutation_id = (
            f"{intent.operation_id}:dispatch:{record.dispatch_attempt + 1}:fence:{fence_token}"
        )
        try:
            dispatching, cancelled = await self._commit_store_call(
                lambda: self.store.begin_operation(
                    intent.operation_id,
                    mutation_id=mutation_id,
                    expected_revision=record.revision,
                    worker_id=self.worker_id,
                    fence_token=fence_token,
                )
            )
        except OperationRevisionConflictError as exc:
            authoritative = await asyncio.to_thread(
                self.store.get_operation,
                intent.operation_id,
            )
            if authoritative.state is OperationState.SUCCEEDED:
                return await self._completed_value(authoritative)
            raise OperationInFlightError(authoritative) from exc
        active = dispatching.record
        if cancelled:
            await self._settle_failure(
                active,
                state=OperationState.CANCELLED,
                error={
                    "code": "OPERATION_CANCELLED_BEFORE_DISPATCH",
                    "category": "operation",
                    "retryable": False,
                },
            )
            raise asyncio.CancelledError
        if pre_dispatch is not None:
            try:
                await self._resolve(pre_dispatch())
            except asyncio.CancelledError:
                await self._settle_failure(
                    active,
                    state=OperationState.CANCELLED,
                    error={
                        "code": "OPERATION_CANCELLED_BEFORE_EXTERNAL_DISPATCH",
                        "category": "operation",
                        "retryable": False,
                    },
                )
                raise
            except Exception as exc:
                settled = await self._settle_failure(
                    active,
                    state=OperationState.FAILED,
                    error=self._safe_error(
                        exc,
                        code="OPERATION_PRE_DISPATCH_FAILED",
                    ),
                )
                raise OperationTerminalError(settled) from exc
        try:
            if intent.timeout_seconds is None:
                value = await self._resolve(dispatch())
            else:
                async with asyncio.timeout(intent.timeout_seconds):
                    value = await self._resolve(dispatch())
        except (OperationDispatchDeferredError, OperationReconciliationRequiredError):
            # This gateway is composing a nested durable operation. The nested
            # ledger owns truth for the ambiguous boundary, so keep this parent
            # DISPATCHING instead of incorrectly settling it as a known failure.
            raise
        except OperationTerminalError as exc:
            if (
                exc.record.intent.run_id == intent.run_id
                and exc.record.intent.operation_id != intent.operation_id
                and exc.record.state is OperationState.UNKNOWN
            ):
                raise OperationReconciliationRequiredError(exc.record) from exc
            ambiguous = intent.effect_class in {
                EffectClass.COMPENSATABLE,
                EffectClass.NON_REPEATABLE,
            }
            settled = await self._settle_failure(
                active,
                state=(OperationState.UNKNOWN if ambiguous else OperationState.FAILED),
                error=self._safe_error(exc, code="OPERATION_DISPATCH_FAILED"),
            )
            raise OperationTerminalError(settled) from exc
        except asyncio.CancelledError as exc:
            if intent.effect_class in {
                EffectClass.COMPENSATABLE,
                EffectClass.NON_REPEATABLE,
            }:
                await self._settle_failure(
                    active,
                    state=OperationState.UNKNOWN,
                    error=self._safe_error(
                        exc,
                        code="OPERATION_CANCELLED_DURING_DISPATCH",
                    ),
                )
            # Read-only/idempotent dispatch remains fenced and recoverable. The
            # worker cancellation is re-raised and never presented as settlement.
            raise
        except Exception as exc:
            ambiguous = intent.effect_class in {
                EffectClass.COMPENSATABLE,
                EffectClass.NON_REPEATABLE,
            }
            settled = await self._settle_failure(
                active,
                state=(OperationState.UNKNOWN if ambiguous else OperationState.FAILED),
                error=self._safe_error(exc, code="OPERATION_DISPATCH_FAILED"),
            )
            raise OperationTerminalError(settled) from exc

        try:
            reference, artifact_reference = await self._stage_result(intent, value)
        except asyncio.CancelledError as exc:
            if intent.effect_class in {
                EffectClass.COMPENSATABLE,
                EffectClass.NON_REPEATABLE,
            }:
                await self._settle_failure(
                    active,
                    state=OperationState.UNKNOWN,
                    error=self._safe_error(
                        exc,
                        code="OPERATION_CANCELLED_AFTER_EXTERNAL_SUCCESS",
                    ),
                )
            raise
        except Exception as exc:
            ambiguous = intent.effect_class in {
                EffectClass.COMPENSATABLE,
                EffectClass.NON_REPEATABLE,
            }
            settled = await self._settle_failure(
                active,
                state=(OperationState.UNKNOWN if ambiguous else OperationState.FAILED),
                error=self._safe_error(
                    exc,
                    code="OPERATION_RESULT_PERSISTENCE_FAILED",
                ),
            )
            raise OperationTerminalError(settled) from exc
        try:
            evidence = (
                settlement_evidence(value)
                if settlement_evidence is not None
                else OperationSettlementEvidence()
            )
            if not isinstance(evidence, OperationSettlementEvidence):
                raise TypeError("settlement evidence factory returned an invalid value")
        except BaseException:
            # Evidence is observability metadata, not part of the provider effect.
            # Once the external call and result staging succeeded, an extractor bug
            # must not strand the operation at an ambiguous post-effect boundary.
            # Persist only the fact that evidence is unavailable, never raw errors.
            evidence = OperationSettlementEvidence(
                usage={
                    "source": "settlement_evidence",
                    "reported": False,
                    "extraction_error": True,
                },
                cost={
                    "source": "settlement_evidence",
                    "reported": False,
                    "extraction_error": True,
                },
            )
        settlement = OperationSettlement(
            state=OperationState.SUCCEEDED,
            result_reference=reference,
            result_slot_id=intent.result_slot_id,
            provider_request_id=evidence.provider_request_id,
            usage=thaw_data(evidence.usage),
            cost=thaw_data(evidence.cost),
        )

        def commit_success() -> Any:
            arguments = {
                "mutation_id": (f"{intent.operation_id}:settle:fence:{active.fence_token}"),
                "expected_revision": active.revision,
                "fence_token": active.fence_token,
                "settlement": settlement,
            }
            if artifact_reference is not None:
                arguments["artifact_reference"] = artifact_reference
            return self.store.settle_operation(intent.operation_id, **arguments)

        decision, cancelled = await self._commit_store_call(commit_success)
        self._cache(intent.operation_id, value)
        if cancelled:
            raise asyncio.CancelledError
        return OperationExecution(record=decision.record, value=value)

    async def _settle_failure(
        self,
        record: OperationRecord,
        *,
        state: OperationState,
        error: dict[str, Any],
    ) -> OperationRecord:
        decision, cancelled = await self._commit_store_call(
            lambda: self.store.settle_operation(
                record.intent.operation_id,
                mutation_id=(f"{record.intent.operation_id}:settle:fence:{record.fence_token}"),
                expected_revision=record.revision,
                fence_token=record.fence_token,
                settlement=OperationSettlement(state=state, safe_error=error),
            )
        )
        if cancelled:
            raise asyncio.CancelledError
        return decision.record

    async def recover_interrupted(
        self,
        operation_id: str,
        *,
        recovery_id: str,
    ) -> OperationRecord:
        """Explicitly reclaim safely replayable work; ambiguous effects fail closed."""
        record = await asyncio.to_thread(self.store.get_operation, operation_id)
        action = recovery_action(record)
        if action is RecoveryAction.RECONCILE:
            raise OperationReconciliationRequiredError(record)
        if action in {RecoveryAction.DISPATCH, RecoveryAction.DO_NOTHING}:
            return record
        decision = await asyncio.to_thread(
            self.store.recover_operation,
            operation_id,
            mutation_id=recovery_id,
            expected_revision=record.revision,
            next_fence_token=record.fence_token + 1,
        )
        return decision.record


__all__ = [
    "OperationDispatchDeferredError",
    "OperationExecution",
    "OperationGateway",
    "OperationInFlightError",
    "OperationReconciliationRequiredError",
    "OperationResultUnavailableError",
    "OperationTerminalError",
    "PreDispatch",
]
