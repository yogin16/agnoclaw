"""Versioned JSON boundary shared by lifecycle HTTP servers and remote clients."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import HarnessError
from .lifecycle import RunSnapshot, RunState
from .output_segments import RunOutputSegment
from .security import thaw_data
from .store import RuntimeEvent, decode_event_cursor

LIFECYCLE_PROTOCOL_VERSION = "1.0"
MAX_LIFECYCLE_REQUEST_BYTES = 1_048_576
MAX_LIFECYCLE_MESSAGE_BYTES = 1_000_000
MAX_LIFECYCLE_METADATA_BYTES = 65_536
MAX_LIFECYCLE_EVENT_PAGE_SIZE = 100
MAX_LIFECYCLE_OUTPUT_PAGE_SIZE = 50


class LifecycleProtocolError(HarnessError):
    """The peer returned or supplied an invalid lifecycle protocol envelope."""

    def __init__(self, message: str = "The lifecycle protocol envelope is invalid.") -> None:
        super().__init__(
            code="LIFECYCLE_PROTOCOL_INVALID",
            category="protocol",
            message=message,
            retryable=False,
        )


def lifecycle_envelope(kind: str, **payload: Any) -> dict[str, Any]:
    return {
        "protocol_version": LIFECYCLE_PROTOCOL_VERSION,
        "kind": kind,
        **payload,
    }


def require_lifecycle_envelope(value: Any, *, kind: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LifecycleProtocolError()
    if value.get("protocol_version") != LIFECYCLE_PROTOCOL_VERSION:
        raise LifecycleProtocolError("The lifecycle protocol version is unsupported.")
    if value.get("kind") != kind:
        raise LifecycleProtocolError("The lifecycle protocol response kind is invalid.")
    return value


def snapshot_to_wire(snapshot: RunSnapshot) -> dict[str, Any]:
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


def snapshot_from_wire(value: Any) -> RunSnapshot:
    if not isinstance(value, Mapping):
        raise LifecycleProtocolError("The lifecycle run snapshot is invalid.")
    try:
        schema_version = _strict_string(value["schema_version"])
        if schema_version != "1.0":
            raise ValueError
        metadata = value.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise TypeError
        return RunSnapshot(
            schema_version=schema_version,
            run_id=_strict_string(value["run_id"]),
            state=RunState(_strict_string(value["state"])),
            revision=_strict_int(value["revision"]),
            tenant_id=_optional_string(value.get("tenant_id")),
            user_id=_optional_string(value.get("user_id")),
            session_id=_optional_string(value.get("session_id")),
            parent_run_id=_optional_string(value.get("parent_run_id")),
            root_run_id=_optional_string(value.get("root_run_id")),
            child_depth=_strict_int(value.get("child_depth", 0)),
            created_at=_strict_string(value["created_at"]),
            updated_at=_strict_string(value["updated_at"]),
            steering_open=_strict_bool(value["steering_open"]),
            pending_request_id=_optional_string(value.get("pending_request_id")),
            last_transition_id=_optional_string(value.get("last_transition_id")),
            last_reason_code=_optional_string(value.get("last_reason_code")),
            metadata=metadata,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LifecycleProtocolError("The lifecycle run snapshot is invalid.") from exc


def event_from_wire(value: Any, *, run_id: str) -> RuntimeEvent:
    if not isinstance(value, Mapping) or value.get("run_id") != run_id:
        raise LifecycleProtocolError("The lifecycle event is invalid for this run.")
    try:
        schema_version = _strict_string(value["schema_version"])
        if schema_version != "1.0":
            raise ValueError
        return RuntimeEvent(
            schema_version=schema_version,
            event_id=_strict_string(value["event_id"]),
            run_id=_strict_string(value["run_id"]),
            sequence=_strict_int(value["sequence"]),
            event_type=_strict_string(value["event_type"]),
            occurred_at=_strict_string(value["occurred_at"]),
            attempt_id=_optional_string(value.get("attempt_id")),
            payload=value.get("payload") or {},
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LifecycleProtocolError("The lifecycle event is invalid.") from exc


def output_segment_from_wire(value: Any, *, run_id: str) -> RunOutputSegment:
    if not isinstance(value, Mapping) or value.get("run_id") != run_id:
        raise LifecycleProtocolError("The lifecycle output segment is invalid for this run.")
    try:
        segment = RunOutputSegment(
            schema_version=_strict_string(value["schema_version"]),
            run_id=_strict_string(value["run_id"]),
            segment_sequence=_strict_int(value["segment_sequence"]),
            event_sequence=_strict_int(value["event_sequence"]),
            cursor=_strict_string(value["cursor"]),
            content=_strict_string(value["content"]),
            delta_count=_strict_int(value["delta_count"]),
            artifact_id=_strict_string(value["artifact_id"]),
            occurred_at=_strict_string(value["occurred_at"]),
        )
        if decode_event_cursor(segment.cursor, run_id=run_id) != segment.event_sequence:
            raise ValueError
        return segment
    except (HarnessError, KeyError, TypeError, ValueError) as exc:
        raise LifecycleProtocolError("The lifecycle output segment is invalid.") from exc


def harness_error_to_wire(error: HarnessError) -> dict[str, Any]:
    return {
        "code": error.code,
        "category": error.category,
        "message": error.message,
        "retryable": error.retryable,
        "details": thaw_data(error.details) if error.details is not None else None,
    }


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise TypeError
    return value


def _strict_string(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError
    return value


def _strict_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError
    return value


def _strict_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise TypeError
    return value


__all__ = [
    "LIFECYCLE_PROTOCOL_VERSION",
    "MAX_LIFECYCLE_EVENT_PAGE_SIZE",
    "MAX_LIFECYCLE_OUTPUT_PAGE_SIZE",
    "MAX_LIFECYCLE_MESSAGE_BYTES",
    "MAX_LIFECYCLE_METADATA_BYTES",
    "MAX_LIFECYCLE_REQUEST_BYTES",
    "LifecycleProtocolError",
    "event_from_wire",
    "harness_error_to_wire",
    "lifecycle_envelope",
    "output_segment_from_wire",
    "require_lifecycle_envelope",
    "snapshot_from_wire",
    "snapshot_to_wire",
]
