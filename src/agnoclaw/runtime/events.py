"""Harness event envelope and event sink contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Awaitable, Protocol, runtime_checkable
from uuid import uuid4

from .context import ExecutionContext

EVENT_VERSION = "0.2"


class EventSinkMode(str, Enum):
    BEST_EFFORT = "best_effort"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True)
class HarnessEvent:
    """Structured runtime event emitted by the harness."""

    event_id: str
    event_type: str
    event_version: str
    occurred_at: str
    run_id: str
    context: ExecutionContext
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_version": self.event_version,
            "occurred_at": self.occurred_at,
            "run_id": self.run_id,
            "context": {
                "tenant_id": self.context.tenant_id,
                "org_id": self.context.org_id,
                "team_id": self.context.team_id,
                "workspace_id": self.context.workspace_id,
                "session_id": self.context.session_id,
                "user_id": self.context.user_id,
                "request_id": self.context.request_id,
                "trace_id": self.context.trace_id,
                "roles": list(self.context.roles),
                "scopes": list(self.context.scopes),
                "metadata": self.context.metadata,
            },
            "payload": self.payload,
        }


@runtime_checkable
class EventSink(Protocol):
    """Event sink protocol. Implementations may be sync or async."""

    def emit(self, event: HarnessEvent) -> None | Awaitable[None]:
        ...


class NullEventSink:
    """No-op event sink."""

    def emit(self, event: HarnessEvent) -> None:
        del event


class InMemoryEventSink:
    """Useful for testing and local instrumentation."""

    def __init__(self) -> None:
        self.events: list[HarnessEvent] = []

    def emit(self, event: HarnessEvent) -> None:
        self.events.append(event)


def build_event(
    *,
    event_type: str,
    run_id: str,
    context: ExecutionContext,
    payload: dict[str, Any] | None = None,
    event_version: str = EVENT_VERSION,
) -> HarnessEvent:
    """Create a normalized event envelope."""
    return HarnessEvent(
        event_id=f"evt_{uuid4().hex}",
        event_type=event_type,
        event_version=event_version,
        occurred_at=datetime.now(UTC).isoformat(),
        run_id=run_id,
        context=context,
        payload=payload or {},
    )

