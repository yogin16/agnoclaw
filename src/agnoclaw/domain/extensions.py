"""Instance-scoped hooks and bounded listeners with explicit delivery ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar
from uuid import uuid4

from agnoclaw.runtime.context import ExecutionContext
from agnoclaw.runtime.store import RuntimeEvent

from ._contracts import Record, fail, frozen_json, identifier
from .components import DomainComponent, resolve, timeout
from .identity import ContractRef, OperationIdentity
from .outputs import OutputSpec


@dataclass(frozen=True)
class Hook(DomainComponent):
    name: str
    schema: OutputSpec
    callback: Callable[[Mapping[str, Any], ExecutionContext], Any] = field(repr=False)
    implementation_id: str = field(kw_only=True)
    version: str = field(default="1", kw_only=True)
    mode: str = field(default="policy", kw_only=True)
    order: int = field(default=0, kw_only=True)
    timeout_seconds: float = field(default=30, kw_only=True)
    kind: ClassVar[str] = "hook"

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        identifier(self.implementation_id, "implementation_id")
        if self.mode not in {"policy", "transform"} or not callable(self.callback):
            raise ValueError("hook needs a callable and policy or transform mode")
        timeout(self.timeout_seconds)

    def dependencies(self) -> tuple[object, ...]:
        return (self.schema,)

    def manifest(self) -> dict[str, Any]:
        return {
            "schema": self.schema.reference,
            "mode": self.mode,
            "order": self.order,
            "implementation_id": self.implementation_id,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class EventType(DomainComponent):
    name: str
    payload: OutputSpec
    version: str = field(default="1", kw_only=True)
    kind: ClassVar[str] = "event"

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)

    def dependencies(self) -> tuple[object, ...]:
        return (self.payload,)

    def manifest(self) -> dict[str, Any]:
        return {"payload": self.payload.reference}

    def event(
        self,
        payload: Mapping[str, Any],
        *,
        source: str,
        event_id: str,
        tenant_id: str,
        causation: tuple[str, ...] = (),
    ) -> DomainEvent:
        return DomainEvent(
            str(self.ref),
            self.digest,
            source,
            event_id,
            tenant_id,
            self.payload.validate_content(payload),
            causation,
        )


@dataclass(frozen=True)
class DomainEvent(Record):
    type_reference: str
    type_digest: str
    source: str
    event_id: str
    tenant_id: str
    payload: Mapping[str, Any]
    causation: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        from ._contracts import digest_string

        ContractRef.parse(self.type_reference)
        digest_string(self.type_digest)
        for name in ("source", "event_id", "tenant_id"):
            identifier(getattr(self, name), name)
        object.__setattr__(self, "payload", frozen_json(self.payload))
        object.__setattr__(self, "causation", tuple(self.causation))
        if len(self.causation) > 64:
            raise ValueError("event causation chain exceeds 64 entries")
        for item in self.causation:
            identifier(item, "causation")

    def identity(self, consumer: str) -> OperationIdentity:
        from agnoclaw.runtime.security import canonical_json_digest

        return OperationIdentity(
            self.tenant_id,
            consumer,
            canonical_json_digest([self.source, self.event_id]),
            self.digest,
        )


@dataclass(frozen=True)
class Delivery:
    event: DomainEvent
    identity: OperationIdentity


@dataclass(frozen=True)
class Listener(DomainComponent):
    name: str
    event_type: EventType
    handle: Callable[[Delivery], None | Awaitable[None]] = field(repr=False)
    implementation_id: str = field(kw_only=True)
    version: str = field(default="1", kw_only=True)
    mode: str = field(default="durable", kw_only=True)
    timeout_seconds: float = field(default=30, kw_only=True)
    kind: ClassVar[str] = "listener"

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        identifier(self.implementation_id, "implementation_id")
        if self.mode not in {"durable", "observer"} or not callable(self.handle):
            raise ValueError("listener needs a callable and durable or observer mode")
        timeout(self.timeout_seconds)

    def dependencies(self) -> tuple[object, ...]:
        return (self.event_type,)

    def manifest(self) -> dict[str, Any]:
        return {
            "event": str(self.event_type.ref),
            "mode": self.mode,
            "implementation_id": self.implementation_id,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class DeliveryResult(Record):
    delivered: tuple[str, ...]
    observer_failures: tuple[str, ...]


class Registration:
    def __init__(self, owner: Extensions, key: str, token: str) -> None:
        self._owner, self._key, self._token = owner, key, token

    def close(self) -> None:
        self._owner._remove(self._key, self._token)

    def __enter__(self) -> Registration:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class Extensions:
    """Explicit registration lifetime, sequential backpressure, no background tasks.

    Durable delivery errors propagate to the calling outbox worker. Handlers use the
    supplied identity in their host transaction; the registry itself is not a receipt
    store. Synchronous callbacks must be short/pure; async timeouts are cooperative.
    Each scope belongs to one event loop. Registration itself starts no tasks.
    """

    def __init__(
        self, *, tenant_id: str, max_registrations: int = 256, max_inflight: int = 32
    ) -> None:
        identifier(tenant_id, "tenant_id")
        if type(max_registrations) is not int or max_registrations < 1:
            raise ValueError("max_registrations must be positive")
        if type(max_inflight) is not int or max_inflight < 1:
            raise ValueError("max_inflight must be positive")
        self.tenant_id, self.max_registrations = tenant_id, max_registrations
        self.max_inflight = max_inflight
        self._entries: dict[str, tuple[str, Hook | Listener]] = {}
        self._closed = False
        self._active: set[asyncio.Task[Any]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def register(self, component: Hook | Listener) -> Registration:
        self._require_open()
        if not isinstance(component, (Hook, Listener)):
            raise TypeError("register expects a Hook or Listener")
        key = f"{component.kind}/{component.ref}"
        if key in self._entries:
            raise ValueError(f"already registered: {key}")
        if len(self._entries) >= self.max_registrations:
            raise ValueError("registration limit reached")
        token = uuid4().hex
        self._entries[key] = (token, component)
        return Registration(self, key, token)

    def _remove(self, key: str, token: str) -> None:
        if key in self._entries and self._entries[key][0] == token:
            del self._entries[key]

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("extension scope is closed")

    def _require_loop(self) -> None:
        current = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not current:
            raise RuntimeError("extension scope belongs to another event loop")
        self._loop = current

    async def _call(self, callback: Callable[..., Any], duration: float, *args: Any) -> Any:
        self._require_open()
        self._require_loop()
        task = asyncio.current_task()
        if task in self._active:
            raise RuntimeError("recursive extension dispatch must use a bounded trigger")
        if len(self._active) >= self.max_inflight:
            raise RuntimeError("extension concurrency limit reached")

        async def invoke() -> Any:
            async with asyncio.timeout(duration):
                return await resolve(callback(*args))

        owned = asyncio.create_task(invoke())
        self._active.add(owned)
        try:
            return await owned
        finally:
            self._active.discard(owned)

    async def apply(
        self, schema: OutputSpec, inputs: Mapping[str, Any], *, context: ExecutionContext
    ) -> Mapping[str, Any]:
        self._require_open()
        if context.tenant_id != self.tenant_id:
            fail("SCOPE_MISMATCH", "Hook context belongs to another tenant.")
        values = frozen_json(schema.validate_content(inputs))
        selected = [
            (key, token, item)
            for key, (token, item) in self._entries.items()
            if isinstance(item, Hook) and item.schema.reference == schema.reference
        ]
        for key, token, hook in sorted(
            selected, key=lambda item: (item[2].order, str(item[2].ref))
        ):
            if self._entries.get(key, (None,))[0] != token:
                continue
            if hook.schema.digest != schema.digest:
                fail("HOOK_SCHEMA_CHANGED", "Hook schema differs from the selected contract.")
            try:
                outcome = await self._call(hook.callback, hook.timeout_seconds, values, context)
                if hook.mode == "policy":
                    if outcome is not True:
                        fail("HOOK_REJECTED", f"Policy hook {hook.ref} did not approve.")
                else:
                    values = frozen_json(schema.validate_content(outcome))
            except Exception:
                fail("HOOK_FAILED", f"Hook {hook.ref} rejected, failed, or timed out.")
        return values

    async def publish(self, event: DomainEvent) -> DeliveryResult:
        self._require_open()
        if event.tenant_id != self.tenant_id:
            fail("SCOPE_MISMATCH", "Event belongs to another tenant.")
        delivered, failures = [], []
        for key, (token, listener) in tuple(self._entries.items()):
            self._require_open()
            if (
                not isinstance(listener, Listener)
                or str(listener.event_type.ref) != event.type_reference
                or self._entries.get(key, (None,))[0] != token
            ):
                continue
            if listener.event_type.digest != event.type_digest:
                fail("EVENT_SCHEMA_CHANGED", "Event and listener contracts differ.")
            listener.event_type.payload.validate_content(event.payload)
            try:
                result = await self._call(
                    listener.handle,
                    listener.timeout_seconds,
                    Delivery(event, event.identity(str(listener.ref))),
                )
                if result is not None:
                    raise TypeError("listener must return None after completing delivery")
                delivered.append(str(listener.ref))
            except Exception:
                if listener.mode == "durable":
                    fail("LISTENER_FAILED", f"Listener {listener.ref} failed; delivery must retry.")
                failures.append(str(listener.ref))
        return DeliveryResult(tuple(delivered), tuple(failures))

    async def aclose(self, *, cancel: bool = False) -> None:
        self._require_loop()
        current = asyncio.current_task()
        if current in self._active:
            raise RuntimeError("close the extension scope outside its callbacks")
        self._closed = True
        self._entries.clear()
        tasks = tuple(self._active)
        if cancel:
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def __aenter__(self) -> Extensions:
        self._require_open()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose(cancel=True)


@dataclass(frozen=True)
class RuntimeListenerExporter:
    """Adapter for RuntimeOutboxWorker; host business outboxes call publish directly."""

    extensions: Extensions
    convert: Callable[[RuntimeEvent], DomainEvent | None]

    async def export(self, events: tuple[RuntimeEvent, ...]) -> None:
        for event in events:
            converted = self.convert(event)
            if converted is not None:
                await self.extensions.publish(converted)


@dataclass(frozen=True)
class TriggerRequest(Record):
    target_reference: str
    target_digest: str
    identity: OperationIdentity
    inputs: Mapping[str, Any]
    causation: tuple[str, ...]

    def __post_init__(self) -> None:
        from ._contracts import digest_string

        ContractRef.parse(self.target_reference)
        digest_string(self.target_digest)
        object.__setattr__(self, "inputs", frozen_json(self.inputs))
        object.__setattr__(self, "causation", tuple(self.causation))


@dataclass(frozen=True)
class Trigger(DomainComponent):
    """Classify an admitted event into work; hosts persist/schedule the request."""

    name: str
    event_type: EventType
    target: DomainComponent
    version: str = field(default="1", kw_only=True)
    max_depth: int = field(default=8, kw_only=True)
    kind: ClassVar[str] = "trigger"

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        if type(self.max_depth) is not int or not 1 <= self.max_depth <= 64:
            raise ValueError("max_depth must be between 1 and 64")
        from .behavior import Workflow
        from .watchers import Watcher

        if isinstance(self.target, Workflow):
            if (
                self.target.steps[0].call.interface.inputs.schema_digest
                != self.event_type.payload.schema_digest
            ):
                raise ValueError("trigger payload must match workflow input schema")
        elif isinstance(self.target, Watcher):
            if self.target.event_type.digest != self.event_type.digest:
                raise ValueError("trigger and watcher must share the same event contract")
        else:
            raise TypeError("trigger target must be a Workflow or Watcher")

    def dependencies(self) -> tuple[object, ...]:
        return (self.event_type, self.target)

    def manifest(self) -> dict[str, Any]:
        return {
            "event": str(self.event_type.ref),
            "target": str(self.target.ref),
            "max_depth": self.max_depth,
        }

    def plan(self, event: DomainEvent, *, context: ExecutionContext) -> TriggerRequest | None:
        if context.tenant_id != event.tenant_id:
            fail("SCOPE_MISMATCH", "Trigger context belongs to another tenant.")
        if (
            event.type_reference != str(self.event_type.ref)
            or event.type_digest != self.event_type.digest
        ):
            fail("EVENT_SCHEMA_CHANGED", "Event does not match the trigger definition.")
        self.event_type.payload.validate_content(event.payload)
        if len(event.causation) >= self.max_depth or str(self.ref) in event.causation:
            return None
        from agnoclaw.runtime.security import canonical_json_digest

        key = event.identity(f"trigger:{self.name}")
        identity = OperationIdentity(
            key.tenant_id,
            key.namespace,
            key.request_id,
            canonical_json_digest([event.digest, self.digest]),
        )
        return TriggerRequest(
            str(self.target.ref),
            self.target.digest,
            identity,
            event.payload,
            (*event.causation, str(self.ref)),
        )
