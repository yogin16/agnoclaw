"""Content-free durable telemetry projection and OpenTelemetry bridge."""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .approvals import ApprovalState
from .lifecycle import RunState, TransitionKind
from .operations import EffectClass, OperationKind, OperationReconciliationVerdict, OperationState
from .security import thaw_data
from .store import RuntimeEvent

RUNTIME_TELEMETRY_SCHEMA_VERSION = "1.0"
_SAFE_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_ATTRIBUTE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_MAX_COUNTER = 2**63 - 1
KNOWN_RUNTIME_EVENT_TYPES = frozenset(
    {
        "approval.approved",
        "approval.cancelled",
        "approval.denied",
        "approval.expired",
        "approval.requested",
        "artifact.committed",
        "operation.dispatching",
        "operation.planned",
        "operation.reconciled",
        "operation.recovery.planned",
        "operation.settled",
        "run.child.budget.observed",
        "run.child.created",
        "run.child.output.validated",
        "run.child.settled",
        "run.created",
        "run.lease.acquired",
        "run.output.segment",
        "run.state.changed",
    }
)
_RUN_STATES = frozenset(item.value for item in RunState)
_OPERATION_STATES = frozenset(item.value for item in OperationState)
_APPROVAL_STATES = frozenset(item.value for item in ApprovalState)
_TRANSITIONS = frozenset(item.value for item in TransitionKind)
_OPERATION_KINDS = frozenset(item.value for item in OperationKind)
_EFFECT_CLASSES = frozenset(item.value for item in EffectClass)
_RECONCILIATION_VERDICTS = frozenset(item.value for item in OperationReconciliationVerdict)


def _safe_code(value: Any) -> str | None:
    if isinstance(value, str) and _SAFE_CODE.fullmatch(value):
        return value
    return None


def _known_code(value: Any, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def normalize_runtime_event_type(value: str) -> str:
    """Bound event-cardinality and prevent caller-controlled values entering telemetry."""
    return value if value in KNOWN_RUNTIME_EVENT_TYPES else "runtime.unknown"


def _nonnegative_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value < 0 or value > _MAX_COUNTER:
        return None
    return value


def _mapping(value: Any) -> dict[str, Any]:
    thawed = thaw_data(value)
    return thawed if isinstance(thawed, dict) else {}


def _nested(payload: dict[str, Any], *path: str) -> Any:
    value: Any = payload
    for name in path:
        if not isinstance(value, dict):
            return None
        value = value.get(name)
    return value


@dataclass(frozen=True, slots=True)
class RuntimeTelemetryPolicy:
    """Projection policy; the identifier key never enters records or repr output."""

    identifier_key: bytes = field(repr=False, compare=False)
    key_id: str = "default"
    service_name: str = "agnoclaw"
    include_event_linkage: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.identifier_key, bytes) or len(self.identifier_key) < 32:
            raise ValueError("identifier_key must contain at least 32 bytes")
        for name in ("key_id", "service_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SAFE_CODE.fullmatch(value):
                raise ValueError(f"{name} must be a stable low-cardinality token")
        if not isinstance(self.include_event_linkage, bool):
            raise ValueError("include_event_linkage must be a boolean")

    def identifier_digest(self, value: str, *, domain: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("telemetry identifiers must be non-empty strings")
        if not isinstance(domain, str) or not _SAFE_CODE.fullmatch(domain):
            raise ValueError("telemetry identifier domains must be stable tokens")
        digest = hmac.new(
            self.identifier_key,
            f"{domain}\x00{value}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{self.key_id}:{digest}"


TelemetryScalar = str | int | float | bool


@dataclass(frozen=True, slots=True)
class RuntimeTelemetryRecord:
    """One safe, bounded projection of a committed runtime event."""

    event_type: str
    occurred_at: str
    attributes: tuple[tuple[str, TelemetryScalar], ...]
    schema_version: str = RUNTIME_TELEMETRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RUNTIME_TELEMETRY_SCHEMA_VERSION:
            raise ValueError("unsupported runtime telemetry schema version")
        if not _EVENT_TYPE.fullmatch(self.event_type):
            raise ValueError("event_type must be a stable lowercase token")
        if not isinstance(self.occurred_at, str) or not self.occurred_at:
            raise ValueError("occurred_at must be non-empty")
        try:
            occurred_at = datetime.fromisoformat(self.occurred_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("occurred_at must be an ISO-8601 timestamp") from exc
        if occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must include a UTC offset")
        if tuple(sorted(self.attributes)) != self.attributes:
            raise ValueError("telemetry attributes must be canonical")
        names = [name for name, _ in self.attributes]
        if len(names) != len(set(names)):
            raise ValueError("telemetry attribute names must be unique")
        if len(self.attributes) > 32:
            raise ValueError("telemetry records cannot exceed 32 attributes")
        for name, value in self.attributes:
            if not isinstance(name, str) or not _ATTRIBUTE_NAME.fullmatch(name):
                raise ValueError("telemetry attribute names must be stable tokens")
            if isinstance(value, str):
                if not value or len(value) > 512:
                    raise ValueError("telemetry string attributes must contain 1 to 512 characters")
            elif isinstance(value, bool):
                continue
            elif isinstance(value, int):
                if not -_MAX_COUNTER <= value <= _MAX_COUNTER:
                    raise ValueError("telemetry integer attributes must be bounded")
            elif isinstance(value, float):
                if not math.isfinite(value) or not -_MAX_COUNTER <= value <= _MAX_COUNTER:
                    raise ValueError("telemetry float attributes must be finite and bounded")
            else:
                raise TypeError("telemetry attributes must be scalar values")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class RuntimeTelemetryBatch:
    """Projected records plus low-cardinality aggregate measurements."""

    records: tuple[RuntimeTelemetryRecord, ...]
    event_type_counts: tuple[tuple[str, int], ...]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_microusd: int = 0
    schema_version: str = RUNTIME_TELEMETRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RUNTIME_TELEMETRY_SCHEMA_VERSION:
            raise ValueError("unsupported runtime telemetry batch schema version")
        if not 1 <= len(self.records) <= 1000:
            raise ValueError("telemetry batches must contain between 1 and 1000 records")
        expected = tuple(sorted(Counter(item.event_type for item in self.records).items()))
        if self.event_type_counts != expected:
            raise ValueError("event_type_counts do not match records")
        for name in ("input_tokens", "output_tokens", "cost_microusd"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_COUNTER
            ):
                raise ValueError(f"{name} must be a bounded non-negative integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "records": [item.to_dict() for item in self.records],
            "event_type_counts": dict(self.event_type_counts),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_microusd": self.cost_microusd,
        }


def _operation_name(event_type: str, operation_kind: str | None) -> str | None:
    if event_type == "operation.planned" and operation_kind == "model":
        return "chat"
    if event_type == "operation.planned" and operation_kind == "capability":
        return "execute_tool"
    if event_type.startswith("run."):
        return "invoke_agent"
    return None


def _usage_value(payload: dict[str, Any], name: str) -> int:
    candidates = (
        _nested(payload, "usage", name),
        _nested(payload, "measured", name),
        payload.get(name),
    )
    for candidate in candidates:
        value = _nonnegative_number(candidate)
        if isinstance(value, int):
            return value
    return 0


def _cost_value(payload: dict[str, Any]) -> int:
    for candidate in (
        _nested(payload, "cost", "microusd"),
        _nested(payload, "measured", "cost_microusd"),
        payload.get("cost_microusd"),
    ):
        value = _nonnegative_number(candidate)
        if isinstance(value, int):
            return value
    return 0


def project_runtime_event(
    event: RuntimeEvent,
    *,
    policy: RuntimeTelemetryPolicy,
) -> RuntimeTelemetryRecord:
    """Project a committed event without copying its arbitrary payload."""
    if not isinstance(event, RuntimeEvent):
        raise TypeError("event must be a RuntimeEvent")
    if not isinstance(policy, RuntimeTelemetryPolicy):
        raise TypeError("policy must be a RuntimeTelemetryPolicy")
    payload = _mapping(event.payload)
    event_type = normalize_runtime_event_type(event.event_type)
    attributes: dict[str, TelemetryScalar] = {
        "agnoclaw.event.sequence": event.sequence,
        "agnoclaw.event.type": event_type,
        "agnoclaw.run.id_hash": policy.identifier_digest(event.run_id, domain="run"),
        "service.name": policy.service_name,
    }
    if policy.include_event_linkage:
        attributes["agnoclaw.event.id_hash"] = policy.identifier_digest(
            event.event_id,
            domain="event",
        )
        if event.attempt_id is not None:
            attributes["agnoclaw.attempt.id_hash"] = policy.identifier_digest(
                event.attempt_id,
                domain="attempt",
            )
    operation_kind = _known_code(payload.get("kind"), _OPERATION_KINDS)
    operation_name = _operation_name(event_type, operation_kind)
    if operation_name is not None:
        attributes["gen_ai.operation.name"] = operation_name
    for source, target, allowed in (
        ("state", "agnoclaw.state", _RUN_STATES | _OPERATION_STATES | _APPROVAL_STATES),
        ("before", "agnoclaw.state.before", _RUN_STATES),
        ("after", "agnoclaw.state.after", _RUN_STATES),
        ("transition", "agnoclaw.transition", _TRANSITIONS),
        ("kind", "agnoclaw.operation.kind", _OPERATION_KINDS),
        ("effect_class", "agnoclaw.effect.class", _EFFECT_CLASSES),
        ("verdict", "agnoclaw.reconciliation.verdict", _RECONCILIATION_VERDICTS),
    ):
        value = _known_code(payload.get(source), allowed)
        if value is not None:
            attributes[target] = value
    for source, target in (
        ("revision", "agnoclaw.revision"),
        ("dispatch_attempt", "agnoclaw.dispatch.attempt"),
        ("child_depth", "agnoclaw.child.depth"),
    ):
        number = _nonnegative_number(payload.get(source))
        if isinstance(number, int):
            attributes[target] = number
    include_measurements = event_type in {
        "operation.settled",
        "run.child.budget.observed",
    }
    input_tokens = _usage_value(payload, "input_tokens") if include_measurements else 0
    output_tokens = _usage_value(payload, "output_tokens") if include_measurements else 0
    cost_microusd = _cost_value(payload) if include_measurements else 0
    if input_tokens:
        attributes["gen_ai.usage.input_tokens"] = input_tokens
    if output_tokens:
        attributes["gen_ai.usage.output_tokens"] = output_tokens
    if cost_microusd:
        attributes["agnoclaw.cost.microusd"] = cost_microusd
    return RuntimeTelemetryRecord(
        event_type=event_type,
        occurred_at=event.occurred_at,
        attributes=tuple(sorted(attributes.items())),
    )


@runtime_checkable
class RuntimeTelemetrySink(Protocol):
    """Async destination for already-safe telemetry batches."""

    async def export(self, batch: RuntimeTelemetryBatch) -> None: ...


class RuntimeTelemetryBatchExporter:
    """Runtime outbox exporter that strips arbitrary event bodies before delegation."""

    def __init__(
        self,
        *,
        sink: RuntimeTelemetrySink,
        policy: RuntimeTelemetryPolicy,
    ) -> None:
        if not isinstance(sink, RuntimeTelemetrySink):
            raise TypeError("sink must implement async export(batch)")
        if not isinstance(policy, RuntimeTelemetryPolicy):
            raise TypeError("policy must be a RuntimeTelemetryPolicy")
        self.sink = sink
        self.policy = policy

    async def export(self, events: tuple[RuntimeEvent, ...]) -> None:
        if not 1 <= len(events) <= 1000:
            raise ValueError("events must contain between 1 and 1000 RuntimeEvent values")
        if any(not isinstance(item, RuntimeEvent) for item in events):
            raise TypeError("events must contain RuntimeEvent values")
        records = tuple(project_runtime_event(item, policy=self.policy) for item in events)
        input_tokens = 0
        output_tokens = 0
        cost_microusd = 0
        for record in records:
            attributes = dict(record.attributes)
            input_tokens += int(attributes.get("gen_ai.usage.input_tokens", 0))
            output_tokens += int(attributes.get("gen_ai.usage.output_tokens", 0))
            cost_microusd += int(attributes.get("agnoclaw.cost.microusd", 0))
        batch = RuntimeTelemetryBatch(
            records=records,
            event_type_counts=tuple(sorted(Counter(item.event_type for item in records).items())),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microusd=cost_microusd,
        )
        await self.sink.export(batch)


class _Counter(Protocol):
    def add(self, amount: int | float, attributes: dict[str, TelemetryScalar]) -> None: ...


class _Meter(Protocol):
    def create_counter(self, name: str, *, unit: str, description: str) -> _Counter: ...


class OpenTelemetryRuntimeSink:
    """Bridge safe batches to an OTel-enabled logger and optional Meter.

    Configure the logger with OpenTelemetry's logging handler and inject an OTel Meter.
    This class deliberately does not configure global providers or an OTLP endpoint.
    """

    def __init__(self, *, logger: logging.Logger, meter: _Meter | None = None) -> None:
        if not isinstance(logger, logging.Logger):
            raise TypeError("logger must be a logging.Logger")
        self._logger = logger
        self._events: _Counter | None = None
        self._tokens: _Counter | None = None
        self._cost: _Counter | None = None
        if meter is not None:
            for method in ("create_counter",):
                if not callable(getattr(meter, method, None)):
                    raise TypeError("meter must provide create_counter")
            self._events = meter.create_counter(
                "agnoclaw.runtime.events",
                unit="{event}",
                description="Committed agnoclaw runtime events exported by type.",
            )
            self._tokens = meter.create_counter(
                "agnoclaw.gen_ai.token.usage",
                unit="{token}",
                description="Provider-reported tokens in committed runtime evidence.",
            )
            self._cost = meter.create_counter(
                "agnoclaw.gen_ai.cost",
                unit="{USD_micro}",
                description="Provider-reported cost in millionths of USD.",
            )

    async def export(self, batch: RuntimeTelemetryBatch) -> None:
        if not isinstance(batch, RuntimeTelemetryBatch):
            raise TypeError("batch must be a RuntimeTelemetryBatch")
        for record in batch.records:
            log_attributes = dict(record.attributes)
            log_attributes["agnoclaw.event.occurred_at"] = record.occurred_at
            log_attributes["agnoclaw.telemetry.schema_version"] = record.schema_version
            self._logger.info(
                "agnoclaw.runtime.event",
                extra=log_attributes,
            )
        if self._events is not None:
            for event_type, count in batch.event_type_counts:
                self._events.add(count, {"agnoclaw.event.type": event_type})
        if self._tokens is not None:
            if batch.input_tokens:
                self._tokens.add(batch.input_tokens, {"gen_ai.token.type": "input"})
            if batch.output_tokens:
                self._tokens.add(batch.output_tokens, {"gen_ai.token.type": "output"})
        if self._cost is not None and batch.cost_microusd:
            self._cost.add(batch.cost_microusd, {"currency": "USD"})


__all__ = [
    "OpenTelemetryRuntimeSink",
    "KNOWN_RUNTIME_EVENT_TYPES",
    "RUNTIME_TELEMETRY_SCHEMA_VERSION",
    "RuntimeTelemetryBatch",
    "RuntimeTelemetryBatchExporter",
    "RuntimeTelemetryPolicy",
    "RuntimeTelemetryRecord",
    "RuntimeTelemetrySink",
    "normalize_runtime_event_type",
    "project_runtime_event",
]
