"""Admission-bound capability materialization through the durable operation gateway."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .capabilities import (
    CapabilityConcurrency,
    CapabilityKind,
    CapabilityLifetime,
    CapabilityRegistry,
    CapabilitySpec,
)
from .capability_schema import (
    preflight_capability_arguments,
    validate_capability_arguments,
)
from .runtime.errors import HarnessError
from .runtime.gateway import OperationExecution, OperationGateway
from .runtime.operations import OperationKind, OperationRecord
from .runtime.security import (
    AdmissionEnvelope,
    canonical_json_digest,
    freeze_data,
    thaw_data,
)
from .runtime.store import RunOwner

CapabilityInvoker = Callable[[Any, Mapping[str, Any]], Any | Awaitable[Any]]
CapabilityPreDispatch = Callable[[], None | Awaitable[None]]
CapabilityResultTransformer = Callable[[Any], Any | Awaitable[Any]]

_RESERVED_METADATA = frozenset(
    {
        "admission_digest",
        "argument_bytes",
        "authority_digest",
        "capability_digest",
        "profile",
        "session_digest",
    }
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: Any) -> str:
    return canonical_json_digest(value)


def _session_digest(session_id: str) -> str:
    return _digest(["capability-session", session_id])


@dataclass(frozen=True)
class CapabilityExecution:
    """One selected capability and its authoritative operation settlement."""

    spec: CapabilitySpec
    operation: OperationExecution

    @property
    def value(self) -> Any:
        return self.operation.value

    @property
    def replayed(self) -> bool:
        return self.operation.replayed


class CapabilityExecutor:
    """Small capability data plane over a registry and ``OperationGateway``.

    The executor owns only resources created by registered factories. The caller owns
    the registry, gateway, runtime store, artifact store, and admission boundary.
    Durable/service calls require a frozen ``AdmissionEnvelope`` and reauthorize the
    exact run owner before an operation intent can be prepared.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        gateway: OperationGateway,
        *,
        invoker: CapabilityInvoker | None = None,
        max_argument_bytes: int = 65_536,
        max_metadata_bytes: int = 4_096,
    ) -> None:
        if not isinstance(registry, CapabilityRegistry):
            raise TypeError("registry must be a CapabilityRegistry")
        if not isinstance(gateway, OperationGateway):
            raise TypeError("gateway must be an OperationGateway")
        if invoker is not None and not callable(invoker):
            raise TypeError("invoker must be callable")
        if max_argument_bytes <= 0 or max_metadata_bytes <= 0:
            raise ValueError("capability argument and metadata budgets must be positive")
        self.registry = registry
        self.gateway = gateway
        self._invoker = invoker
        self._max_argument_bytes = max_argument_bytes
        self._max_metadata_bytes = max_metadata_bytes
        self._state_lock = threading.RLock()
        self._resources: dict[tuple[str, ...], Any] = {}
        self._active_resources: dict[tuple[str, ...], int] = {}
        self._active_calls = 0
        self._dispatch_locks: dict[tuple[str, ...], threading.Lock] = {}
        self._closed = False

    @staticmethod
    def _safe_factory_error(spec: CapabilitySpec, exc: BaseException) -> HarnessError:
        return HarnessError(
            code="CAPABILITY_MATERIALIZATION_FAILED",
            category="capability",
            message=f"Capability '{spec.name}' could not be materialized.",
            retryable=False,
            details={
                "capability": spec.name,
                "exception_type": exc.__class__.__name__,
            },
        )

    def _require_open(self) -> None:
        with self._state_lock:
            if self._closed:
                raise HarnessError(
                    code="CAPABILITY_EXECUTOR_CLOSED",
                    category="lifecycle",
                    message="Capability executor is closed.",
                    retryable=False,
                )

    @staticmethod
    def _resolve_session_id(
        *,
        admission: AdmissionEnvelope | None,
        session_id: str | None,
    ) -> str | None:
        admitted = admission.identity.session_id if admission is not None else None
        if admitted is not None and session_id is not None and admitted != session_id:
            raise HarnessError(
                code="CAPABILITY_SESSION_CONFLICT",
                category="authorization",
                message="Explicit session identity conflicts with admission.",
                retryable=False,
            )
        resolved = admitted or session_id
        if resolved is not None and (not isinstance(resolved, str) or not resolved.strip()):
            raise ValueError("session_id must be a non-empty string when supplied")
        return resolved.strip() if resolved is not None else None

    @staticmethod
    def _authorized_scopes(
        *,
        profile: str,
        admission: AdmissionEnvelope | None,
        granted_scopes: Sequence[str],
    ) -> frozenset[str]:
        if profile in {"durable", "service"} and admission is None:
            raise HarnessError(
                code="CAPABILITY_ADMISSION_REQUIRED",
                category="authorization",
                message="Durable and service capability calls require trusted admission.",
                retryable=False,
                details={"profile": profile},
            )
        if admission is not None and granted_scopes:
            raise HarnessError(
                code="CAPABILITY_SCOPE_SOURCE_CONFLICT",
                category="authorization",
                message="Scopes must come from admission when admission is supplied.",
                retryable=False,
            )
        raw_scopes = admission.identity.scopes if admission is not None else granted_scopes
        if any(not isinstance(scope, str) or not scope.strip() for scope in raw_scopes):
            raise ValueError("granted_scopes must contain non-empty strings")
        return frozenset(scope.strip() for scope in raw_scopes)

    @staticmethod
    def _resource_key(
        spec: CapabilitySpec,
        *,
        session_id: str | None,
    ) -> tuple[str, ...] | None:
        if spec.lifetime is CapabilityLifetime.RUN:
            return None
        if spec.lifetime is CapabilityLifetime.SESSION:
            if session_id is None:
                raise HarnessError(
                    code="CAPABILITY_SESSION_REQUIRED",
                    category="capability",
                    message=f"Session-scoped capability '{spec.name}' needs a session_id.",
                    retryable=False,
                    details={"capability": spec.name},
                )
            return ("session", _session_digest(session_id), spec.digest)
        return ("process_pool", spec.digest)

    def _materialize_cached_sync(
        self,
        spec: CapabilitySpec,
        key: tuple[str, ...] | None,
    ) -> Any:
        with self._state_lock:
            if self._closed:
                raise HarnessError(
                    code="CAPABILITY_EXECUTOR_CLOSED",
                    category="lifecycle",
                    message="Capability executor is closed.",
                    retryable=False,
                )
            if key is not None and key in self._resources:
                resource = self._resources[key]
                self._active_resources[key] = self._active_resources.get(key, 0) + 1
                self._active_calls += 1
                return resource
            try:
                resource = spec.materialize()
            except HarnessError:
                raise
            except BaseException as exc:
                raise self._safe_factory_error(spec, exc) from exc
            if inspect.isawaitable(resource):
                closer = getattr(resource, "close", None)
                if callable(closer):
                    closer()
                raise HarnessError(
                    code="CAPABILITY_FACTORY_ASYNC_UNSUPPORTED",
                    category="capability",
                    message=(
                        "Capability factories must return a materialized resource; "
                        "async work belongs in the resource invocation."
                    ),
                    retryable=False,
                    details={"capability": spec.name},
                )
            if key is not None:
                self._resources[key] = resource
                self._active_resources[key] = 1
            self._active_calls += 1
            return resource

    async def _materialize(
        self,
        spec: CapabilitySpec,
        key: tuple[str, ...] | None,
    ) -> Any:
        task = asyncio.create_task(asyncio.to_thread(self._materialize_cached_sync, spec, key))
        cancelled = False
        while True:
            try:
                resource = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            self._release_active(key)
            if key is None:
                await self._close_resource(resource)
            raise asyncio.CancelledError
        return resource

    def _release_active(self, key: tuple[str, ...] | None) -> None:
        with self._state_lock:
            if self._active_calls <= 0:  # pragma: no cover - internal invariant
                raise RuntimeError("capability active-call accounting underflow")
            self._active_calls -= 1
            if key is None:
                return
            active = self._active_resources.get(key, 0)
            if active <= 1:
                self._active_resources.pop(key, None)
            else:
                self._active_resources[key] = active - 1

    def _dispatch_lock(self, key: tuple[str, ...]) -> threading.Lock:
        with self._state_lock:
            return self._dispatch_locks.setdefault(key, threading.Lock())

    @staticmethod
    async def _acquire_lock(lock: threading.Lock) -> None:
        """Acquire a cross-loop lock without orphaning it when the waiter cancels."""
        task = asyncio.create_task(asyncio.to_thread(lock.acquire))
        cancelled = False
        while True:
            try:
                acquired = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
        if not acquired:  # pragma: no cover - blocking acquire returns True
            raise RuntimeError("capability serialization lock was not acquired")
        if cancelled:
            lock.release()
            raise asyncio.CancelledError

    async def _invoke(self, resource: Any, arguments: Mapping[str, Any]) -> Any:
        invoker = self._invoker
        if invoker is not None:
            if inspect.iscoroutinefunction(invoker):
                result = invoker(resource, arguments)
            else:
                result = await self._run_sync_call(lambda: invoker(resource, arguments))
        else:
            target = resource if callable(resource) else getattr(resource, "entrypoint", None)
            if not callable(target):
                raise HarnessError(
                    code="CAPABILITY_INVOKER_REQUIRED",
                    category="capability",
                    message="Materialized capability is not callable; configure an invoker.",
                    retryable=False,
                )
            if inspect.iscoroutinefunction(target):
                result = target(**dict(arguments))
            else:
                result = await self._run_sync_call(lambda: target(**dict(arguments)))
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    async def _run_sync_call(call: Callable[[], Any]) -> Any:
        """Observe an uncancellable thread call through completion before unwinding."""
        task = asyncio.create_task(asyncio.to_thread(call))
        cancelled = False
        while True:
            try:
                value = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            raise asyncio.CancelledError
        return value

    @staticmethod
    async def _close_resource(resource: Any) -> None:
        async_closer = getattr(resource, "aclose", None)
        if callable(async_closer):
            result = async_closer()
            if inspect.isawaitable(result):
                await result
            return
        closer = getattr(resource, "close", None)
        if callable(closer):
            result = await CapabilityExecutor._run_sync_call(closer)
            if inspect.isawaitable(result):
                await result

    @staticmethod
    async def _close_resources(resources: Sequence[Any]) -> None:
        outcomes = await asyncio.gather(
            *(CapabilityExecutor._close_resource(item) for item in resources),
            return_exceptions=True,
        )
        failures = tuple(item for item in outcomes if isinstance(item, BaseException))
        if failures:
            raise HarnessError(
                code="CAPABILITY_RESOURCE_CLOSE_FAILED",
                category="lifecycle",
                message="One or more capability resources could not be closed.",
                retryable=False,
                details={
                    "failure_count": len(failures),
                    "exception_types": tuple(
                        sorted({item.__class__.__name__ for item in failures})
                    ),
                },
            )

    async def _dispatch(
        self,
        spec: CapabilitySpec,
        *,
        arguments: Mapping[str, Any],
        resource_key: tuple[str, ...] | None,
        result_transformer: CapabilityResultTransformer | None,
    ) -> Any:
        resource = await self._materialize(spec, resource_key)
        serialization_lock: threading.Lock | None = None
        if spec.concurrency is CapabilityConcurrency.SERIALIZED:
            serialization_lock = (
                self._dispatch_lock(resource_key) if resource_key is not None else threading.Lock()
            )
        try:
            if serialization_lock is not None:
                await self._acquire_lock(serialization_lock)
            try:
                result = await self._invoke(resource, arguments)
                if result_transformer is not None:
                    transformed = result_transformer(result)
                    result = await transformed if inspect.isawaitable(transformed) else transformed
                return result
            finally:
                if serialization_lock is not None and serialization_lock.locked():
                    serialization_lock.release()
        finally:
            self._release_active(resource_key)
            if resource_key is None:
                await self._close_resource(resource)

    async def execute(
        self,
        reference: str,
        *,
        operation_id: str,
        run_id: str,
        attempt_id: str,
        arguments: Mapping[str, Any] | None = None,
        profile: str = "durable",
        admission: AdmissionEnvelope | None = None,
        granted_scopes: Sequence[str] = (),
        session_id: str | None = None,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
        safe_metadata: Mapping[str, Any] | None = None,
        pre_dispatch: CapabilityPreDispatch | None = None,
        result_transformer: CapabilityResultTransformer | None = None,
    ) -> CapabilityExecution:
        """Authorize, persist intent, materialize, dispatch, and settle once."""
        self._require_open()
        scopes = self._authorized_scopes(
            profile=profile,
            admission=admission,
            granted_scopes=granted_scopes,
        )
        if admission is not None:
            owner = RunOwner(
                tenant_id=admission.identity.tenant_id,
                user_id=admission.identity.user_id,
            )
            await asyncio.to_thread(self.gateway.store.get_run, run_id, owner=owner)
        spec = self.registry.resolve(reference)
        spec.require_profile(profile)
        missing = tuple(sorted(set(spec.required_scopes) - scopes))
        if missing:
            raise HarnessError(
                code="CAPABILITY_SCOPE_REQUIRED",
                category="authorization",
                message=f"Capability '{spec.name}' requires additional scopes.",
                retryable=False,
                details={"capability": spec.name, "missing_scopes": missing},
            )
        resolved_session = self._resolve_session_id(
            admission=admission,
            session_id=session_id,
        )
        resource_key = self._resource_key(spec, session_id=resolved_session)

        raw_arguments = dict(arguments or {})
        preflight_capability_arguments(raw_arguments, capability=spec.name)
        if spec.kind is CapabilityKind.CHILD_RUN and idempotency_key is None:
            delegation_id = raw_arguments.get("delegation_id")
            if isinstance(delegation_id, str) and delegation_id.strip():
                idempotency_key = delegation_id.strip()
        frozen_arguments = freeze_data(raw_arguments)
        canonical_arguments = _canonical_json(thaw_data(frozen_arguments))
        argument_bytes = len(canonical_arguments.encode("utf-8"))
        if argument_bytes > self._max_argument_bytes:
            raise HarnessError(
                code="CAPABILITY_ARGUMENT_BUDGET_EXCEEDED",
                category="capability",
                message="Capability arguments exceed the configured byte budget.",
                retryable=False,
                details={"argument_bytes": argument_bytes, "maximum": self._max_argument_bytes},
            )
        validate_capability_arguments(
            thaw_data(spec.input_schema),
            thaw_data(frozen_arguments),
            capability=spec.name,
        )

        metadata = dict(safe_metadata or {})
        reserved = tuple(sorted(_RESERVED_METADATA.intersection(metadata)))
        if reserved:
            raise HarnessError(
                code="CAPABILITY_METADATA_RESERVED",
                category="capability",
                message="Capability operation metadata contains reserved fields.",
                retryable=False,
                details={"reserved_fields": reserved},
            )
        frozen_metadata = freeze_data(metadata)
        metadata_bytes = len(_canonical_json(thaw_data(frozen_metadata)).encode("utf-8"))
        if metadata_bytes > self._max_metadata_bytes:
            raise HarnessError(
                code="CAPABILITY_METADATA_BUDGET_EXCEEDED",
                category="capability",
                message="Capability metadata exceeds the configured byte budget.",
                retryable=False,
                details={"metadata_bytes": metadata_bytes, "maximum": self._max_metadata_bytes},
            )

        authority_digest = admission.authority_digest if admission is not None else None
        request_digest = _digest(
            {
                "arguments": thaw_data(frozen_arguments),
                "authority_digest": authority_digest,
                "capability_digest": spec.digest,
                "idempotency_key": idempotency_key,
                "profile": profile,
            }
        )
        intent_metadata = {
            **thaw_data(frozen_metadata),
            "argument_bytes": argument_bytes,
            "authority_digest": authority_digest,
            "session_digest": (
                _session_digest(resolved_session) if resolved_session is not None else None
            ),
        }
        intent = spec.operation_intent(
            operation_id=operation_id,
            run_id=run_id,
            attempt_id=attempt_id,
            request_digest=request_digest,
            profile=profile,
            idempotency_key=idempotency_key,
            timeout_seconds=timeout_seconds,
            metadata=intent_metadata,
        )
        operation = await self.gateway.execute(
            intent,
            lambda: self._dispatch(
                spec,
                arguments=thaw_data(frozen_arguments),
                resource_key=resource_key,
                result_transformer=result_transformer,
            ),
            pre_dispatch=pre_dispatch,
        )
        return CapabilityExecution(spec=spec, operation=operation)

    async def recover_interrupted(
        self,
        operation_id: str,
        *,
        recovery_id: str,
        admission: AdmissionEnvelope,
    ) -> OperationRecord:
        """Re-fence a safely retryable operation without dispatching it."""
        self._require_open()
        if not isinstance(admission, AdmissionEnvelope):
            raise TypeError("admission must be an AdmissionEnvelope")
        owner = RunOwner(
            tenant_id=admission.identity.tenant_id,
            user_id=admission.identity.user_id,
        )
        record = await asyncio.to_thread(
            self.gateway.store.get_operation,
            operation_id,
            owner=owner,
        )
        if (
            record.intent.kind is not OperationKind.CAPABILITY
            or "capability_digest" not in record.intent.metadata
        ):
            raise HarnessError(
                code="CAPABILITY_OPERATION_REQUIRED",
                category="capability",
                message="The operation is not a capability execution.",
                retryable=False,
                details={"operation_id": operation_id},
            )
        return await self.gateway.recover_interrupted(
            operation_id,
            recovery_id=recovery_id,
        )

    async def release_session(self, session_id: str) -> int:
        """Close idle resources for one session; active calls fail retryably."""
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        digest = _session_digest(session_id.strip())
        with self._state_lock:
            keys = tuple(key for key in self._resources if key[:2] == ("session", digest))
            active = tuple(key for key in keys if self._active_resources.get(key, 0))
            if active:
                raise HarnessError(
                    code="CAPABILITY_SESSION_BUSY",
                    category="lifecycle",
                    message="Session capability resources are still in use.",
                    retryable=True,
                    details={"active_resources": len(active)},
                )
            resources = [self._resources.pop(key) for key in keys]
            for key in keys:
                self._dispatch_locks.pop(key, None)
        await self._close_resources(resources)
        return len(resources)

    async def aclose(self) -> None:
        """Close every owned cached resource after all calls become idle."""
        with self._state_lock:
            if self._closed:
                return
            active = self._active_calls
            if active:
                raise HarnessError(
                    code="CAPABILITY_EXECUTOR_BUSY",
                    category="lifecycle",
                    message="Capability executor still has active calls.",
                    retryable=True,
                    details={"active_calls": active},
                )
            self._closed = True
            resources = list(self._resources.values())
            self._resources.clear()
            self._dispatch_locks.clear()
        await self._close_resources(resources)


__all__ = [
    "CapabilityExecution",
    "CapabilityExecutor",
    "CapabilityInvoker",
    "CapabilityPreDispatch",
    "CapabilityResultTransformer",
]
