"""Content-minimized projection from observer events to the durable trajectory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from .checkpoints import execution_context_value
from .errors import HarnessError
from .events import HarnessEvent
from .store import RuntimeEventInput

TRAJECTORY_PROJECTION_SCHEMA_VERSION = "1.0"
_MAX_DEPTH = 8
_MAX_NODES = 256
_MAX_COLLECTION_ITEMS = 64
_MAX_PAYLOAD_BYTES = 32_768
_MAX_CLEAR_TEXT = 256
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SAFE_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_CLEAR_TEXT_FIELDS = frozenset(
    {
        "action",
        "artifact_id",
        "capability",
        "category",
        "checkpoint",
        "checksum",
        "code",
        "effect_class",
        "event",
        "event_type",
        "hook",
        "kind",
        "model",
        "name",
        "operation_id",
        "operation_target",
        "phase",
        "policy_version",
        "provider",
        "provider_id",
        "reason_code",
        "server",
        "skill",
        "source",
        "source_event",
        "state",
        "status",
        "tool",
        "tool_name",
        "transport",
    }
)


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _text_descriptor(value: str) -> dict[str, Any]:
    encoded = value.encode("utf-8")
    return {
        "$digest": _sha256(encoded),
        "$type": "text",
        "$bytes": len(encoded),
        "$chars": len(value),
    }


def _opaque_descriptor(value: Any) -> dict[str, Any]:
    return {
        "$opaque": True,
        "$type": f"{value.__class__.__module__}.{value.__class__.__qualname__}",
    }


@dataclass
class _ProjectionBudget:
    nodes: int = 0
    omitted: int = 0

    def enter(self) -> bool:
        self.nodes += 1
        if self.nodes > _MAX_NODES:
            self.omitted += 1
            return False
        return True


def _field_name(value: Any) -> str:
    if isinstance(value, str) and _SAFE_FIELD_RE.fullmatch(value):
        return value
    encoded = str(value).encode("utf-8", errors="replace")
    return f"field_{hashlib.sha256(encoded).hexdigest()[:16]}"


def _project_value(
    value: Any,
    *,
    field_name: str | None,
    depth: int,
    budget: _ProjectionBudget,
) -> Any:
    if not budget.enter():
        return {"$omitted": True, "$reason": "node_budget"}
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"$invalid": "non_finite_number"}
    if isinstance(value, str):
        if (
            field_name in _CLEAR_TEXT_FIELDS
            and len(value) <= _MAX_CLEAR_TEXT
            and _SAFE_IDENTIFIER_RE.fullmatch(value)
        ):
            return value
        return _text_descriptor(value)
    if depth >= _MAX_DEPTH:
        budget.omitted += 1
        return {"$omitted": True, "$reason": "depth_budget"}
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        items = list(value.items())
        for key, item in items[:_MAX_COLLECTION_ITEMS]:
            normalized_key = _field_name(key)
            projected[normalized_key] = _project_value(
                item,
                field_name=normalized_key,
                depth=depth + 1,
                budget=budget,
            )
        omitted = len(items) - min(len(items), _MAX_COLLECTION_ITEMS)
        if omitted:
            budget.omitted += omitted
            projected["$omitted_items"] = omitted
        return projected
    if isinstance(value, (list, tuple)):
        projected_items = [
            _project_value(
                item,
                field_name=field_name,
                depth=depth + 1,
                budget=budget,
            )
            for item in value[:_MAX_COLLECTION_ITEMS]
        ]
        omitted = len(value) - min(len(value), _MAX_COLLECTION_ITEMS)
        if omitted:
            budget.omitted += omitted
            projected_items.append({"$omitted_items": omitted})
        return projected_items
    return _opaque_descriptor(value)


def _context_digest(event: HarnessEvent) -> str:
    canonical = json.dumps(
        execution_context_value(event.context),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _sha256(canonical)


def _trace_identifier(value: str | None) -> str | dict[str, Any] | None:
    if value is None:
        return None
    if len(value) <= _MAX_CLEAR_TEXT and _SAFE_IDENTIFIER_RE.fullmatch(value):
        return value
    return _text_descriptor(value)


def project_harness_event(
    event: HarnessEvent,
    *,
    attempt_id: str,
) -> RuntimeEventInput:
    """Build a bounded ledger event without persisting raw model/user content."""
    budget = _ProjectionBudget()
    data = _project_value(
        event.payload,
        field_name="payload",
        depth=0,
        budget=budget,
    )
    payload: dict[str, Any] = {
        "projection_schema_version": TRAJECTORY_PROJECTION_SCHEMA_VERSION,
        "source_event_version": event.event_version,
        "context_digest": _context_digest(event),
        "request_id": _trace_identifier(event.context.request_id),
        "trace_id": _trace_identifier(event.context.trace_id),
        "data": data,
        "projection_nodes": budget.nodes,
        "omitted_items": budget.omitted,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        payload = {
            "projection_schema_version": TRAJECTORY_PROJECTION_SCHEMA_VERSION,
            "source_event_version": event.event_version,
            "context_digest": _context_digest(event),
            "request_id": _trace_identifier(event.context.request_id),
            "trace_id": _trace_identifier(event.context.trace_id),
            "data": {
                "$omitted": True,
                "$reason": "payload_budget",
                "$projected_digest": _sha256(encoded),
                "$projected_bytes": len(encoded),
            },
            "projection_nodes": budget.nodes,
            "omitted_items": budget.omitted,
        }
    return RuntimeEventInput(
        event_id=event.event_id,
        run_id=event.run_id,
        event_type=f"trajectory.{event.event_type}",
        occurred_at=event.occurred_at,
        attempt_id=attempt_id,
        payload=payload,
    )


def _runtime_projection(harness: Any, event: HarnessEvent) -> tuple[Any, RuntimeEventInput] | None:
    active_run_id = harness._active_runtime_run_id.get()
    if active_run_id is None or active_run_id != event.run_id:
        return None
    append = getattr(harness._get_runtime_store(), "append_runtime_event", None)
    if not callable(append):
        raise HarnessError(
            code="RUNTIME_STORE_EVENT_PROJECTION_REQUIRED",
            category="configuration",
            message=(
                "Lifecycle execution requires a RuntimeStore implementing "
                "authoritative event projection."
            ),
            retryable=False,
            details={"event_type": event.event_type},
        )
    proposed = project_harness_event(
        event,
        attempt_id=f"{event.run_id}:attempt:1",
    )
    return append, proposed


def project_runtime_event_sync(harness: Any, event: HarnessEvent) -> bool:
    """Commit a lifecycle observer signal before notifying compatibility sinks."""
    projection = _runtime_projection(harness, event)
    if projection is None:
        return False
    append, proposed = projection
    append(proposed, owner=harness._runtime_owner(event.context))
    return True


async def project_runtime_event_async(harness: Any, event: HarnessEvent) -> bool:
    """Asynchronously commit a lifecycle observer signal through a sync store."""
    projection = _runtime_projection(harness, event)
    if projection is None:
        return False
    append, proposed = projection
    await asyncio.to_thread(
        append,
        proposed,
        owner=harness._runtime_owner(event.context),
    )
    return True


__all__ = [
    "TRAJECTORY_PROJECTION_SCHEMA_VERSION",
    "project_harness_event",
    "project_runtime_event_async",
    "project_runtime_event_sync",
]
