"""Bounded live presentation delivery for first-party interactive clients.

Presentation events are deliberately non-authoritative. Durable state, operation
settlement, terminal results, and minimized trajectory events remain in RuntimeStore.
This attachment only carries live Agno events to a client connected to the process
currently executing the run.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from .hooks import RunResultEnvelope
from .output_segments import DurableOutputSegmentWriter
from .store import RunOwner


def validate_output_persistence(
    enabled: bool,
    artifact_store: Any,
    blueprint: dict[str, Any],
    options: dict[str, Any],
) -> None:
    if not isinstance(enabled, bool):
        raise TypeError("persist_output must be a boolean")
    if enabled and artifact_store is None:
        from .errors import HarnessError

        raise HarnessError(
            code="RUN_OUTPUT_ARTIFACT_STORE_REQUIRED",
            category="configuration",
            message="Persisted run output requires an artifact store.",
            retryable=False,
        )
    structured = any(
        source.get(key) is not None
        for source in (blueprint, options)
        for key in ("output_schema", "parser_model", "output_model")
    ) or any(
        bool(source.get(key))
        for source in (blueprint, options)
        for key in ("structured_outputs", "use_json_mode")
    )
    if enabled and structured:
        from .errors import HarnessError

        raise HarnessError(
            code="RUN_OUTPUT_TEXT_REQUIRED",
            category="configuration",
            message="Persisted segmented output currently requires plain text output.",
            retryable=False,
        )

if TYPE_CHECKING:
    from ..agent import AgentHarness


@dataclass(frozen=True)
class RunPresentationDetached:
    """Signal that a live display detached while the logical run continues."""

    run_id: str | None
    reason_code: str
    published_events: int
    buffer_capacity: int


class RunPresentationPublisher(Protocol):
    """Private producer contract owned by one lifecycle worker."""

    def publish(self, event: Any) -> bool: ...

    def finish(self) -> None: ...


_FINISHED = object()


class LiveRunPresentation:
    """Single-consumer bounded queue that never backpressures model execution."""

    def __init__(self, *, capacity: int = 256) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            raise ValueError("presentation capacity must be a positive integer")
        self._capacity = capacity
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=capacity)
        self._run_id: str | None = None
        self._closed = False
        self._consumer_started = False
        self._published_events = 0

    def bind(self, run_id: str | None) -> None:
        """Attach logical identity before the worker can publish its first event."""
        if run_id is not None:
            self._run_id = run_id

    def _clear(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def detach(self, *, reason_code: str) -> None:
        """Detach this display without cancelling or blocking the underlying run."""
        if self._closed:
            return
        self._closed = True
        self._clear()
        self._queue.put_nowait(
            RunPresentationDetached(
                run_id=self._run_id,
                reason_code=reason_code,
                published_events=self._published_events,
                buffer_capacity=self._capacity,
            )
        )

    def publish(self, event: Any) -> bool:
        """Publish without waiting; a slow consumer is detached, never backpressure."""
        if self._closed:
            return False
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.detach(reason_code="PRESENTATION_SLOW_CONSUMER")
            return False
        self._published_events += 1
        return True

    def finish(self) -> None:
        """Close normal delivery, converting a full terminal buffer into detachment."""
        if self._closed:
            return
        try:
            self._queue.put_nowait(_FINISHED)
        except asyncio.QueueFull:
            self.detach(reason_code="PRESENTATION_SLOW_CONSUMER")
            return
        self._closed = True

    async def events(self) -> AsyncIterator[Any]:
        """Yield live events once; consumer closure detaches but never cancels the run."""
        if self._consumer_started:
            raise RuntimeError("live run presentation supports one consumer")
        self._consumer_started = True
        try:
            while True:
                if self._closed and self._queue.empty():
                    return
                event = await self._queue.get()
                if event is _FINISHED:
                    return
                yield event
        finally:
            if not self._closed:
                self.detach(reason_code="PRESENTATION_CONSUMER_CLOSED")


async def execute_presented_model_request(
    harness: AgentHarness,
    *,
    run_id: str,
    message: str,
    request: dict[str, Any],
) -> Any:
    """Execute a model operation, optionally feeding its non-authoritative display."""
    presentation = request.get("presentation")
    execution_kwargs = dict(request["kwargs"])
    persist_output = execution_kwargs.pop("persist_output", False)
    if presentation is None and not persist_output:
        return await harness.arun(
            message,
            context=request["context"],
            **execution_kwargs,
        )

    artifact_store = harness._artifact_store
    context = request["context"]
    writer = (
        DurableOutputSegmentWriter(
            run_id=run_id,
            attempt_id=f"{run_id}:attempt:1",
            owner=RunOwner(tenant_id=context.tenant_id, user_id=context.user_id),
            store=harness._get_runtime_store(),
            artifact_store=artifact_store,
        )
        if artifact_store is not None and (persist_output or presentation is not None)
        else None
    )
    try:
        stream = await harness.arun(
            message,
            stream=True,
            stream_events=True,
            context=request["context"],
            **execution_kwargs,
        )
        if not hasattr(stream, "__aiter__"):
            return stream

        chunks: list[str] = []
        async for event in stream:
            if presentation is not None:
                presentation.publish(event)
            content = harness._extract_event_content(event)
            if content:
                chunks.append(content)
                if writer is not None:
                    await writer.add(content)
        return RunResultEnvelope(
            run_id=run_id,
            content="".join(chunks),
            raw_output=None,
            metadata={},
        )
    finally:
        try:
            if writer is not None:
                await writer.finish()
        finally:
            if presentation is not None:
                presentation.finish()


__all__ = [
    "LiveRunPresentation",
    "RunPresentationDetached",
    "RunPresentationPublisher",
    "execute_presented_model_request",
    "validate_output_persistence",
]
