"""Bounded artifact-backed output segments for durable run presentation replay."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .artifacts import ArtifactScope, ArtifactStore
from .errors import HarnessError
from .security import thaw_data
from .store import RunOwner, RuntimeEvent, RuntimeEventInput, RuntimeStore, encode_event_cursor

OUTPUT_SEGMENT_SCHEMA_VERSION = "1.0"
OUTPUT_SEGMENT_EVENT_TYPE = "run.output.segment"
OUTPUT_SEGMENT_ARTIFACT_PURPOSE = "run.output.segment"
MAX_OUTPUT_SEGMENT_CHARS = 8_192
MAX_OUTPUT_SEGMENT_DELTAS = 32


class RunOutputSegmentError(HarnessError):
    """A persisted segment failed its typed reference/content contract."""

    def __init__(self, *, run_id: str, reason: str) -> None:
        super().__init__(
            code="RUN_OUTPUT_SEGMENT_INVALID",
            category="artifact",
            message="A persisted run output segment is invalid.",
            retryable=False,
            details={"run_id": run_id, "reason": reason},
        )


@dataclass(frozen=True)
class RunOutputSegment:
    """One ordered provider-text segment reconstructed from authorized artifacts."""

    run_id: str
    segment_sequence: int
    event_sequence: int
    cursor: str
    content: str
    delta_count: int
    artifact_id: str
    occurred_at: str
    schema_version: str = OUTPUT_SEGMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OUTPUT_SEGMENT_SCHEMA_VERSION:
            raise ValueError("unsupported run output segment schema")
        if not self.run_id or not self.artifact_id or not self.occurred_at:
            raise ValueError("run output segment identifiers must be non-empty")
        if self.segment_sequence <= 0 or self.event_sequence <= 0 or self.delta_count <= 0:
            raise ValueError("run output segment sequences and delta count must be positive")
        if self.delta_count > MAX_OUTPUT_SEGMENT_DELTAS:
            raise ValueError("run output segment delta count exceeds its bound")
        if not isinstance(self.content, str) or not self.content:
            raise ValueError("run output segment content must be non-empty text")
        if len(self.content) > MAX_OUTPUT_SEGMENT_CHARS:
            raise ValueError("run output segment content exceeds its bound")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "segment_sequence": self.segment_sequence,
            "event_sequence": self.event_sequence,
            "cursor": self.cursor,
            "content": self.content,
            "delta_count": self.delta_count,
            "artifact_id": self.artifact_id,
            "occurred_at": self.occurred_at,
        }


class DurableOutputSegmentWriter:
    """Batch provider text into bounded artifacts and content-free runtime events."""

    def __init__(
        self,
        *,
        run_id: str,
        attempt_id: str,
        owner: RunOwner,
        store: RuntimeStore,
        artifact_store: ArtifactStore,
        max_chars: int = MAX_OUTPUT_SEGMENT_CHARS,
        max_deltas: int = MAX_OUTPUT_SEGMENT_DELTAS,
    ) -> None:
        if not 1 <= max_chars <= MAX_OUTPUT_SEGMENT_CHARS:
            raise ValueError(f"max_chars must be between 1 and {MAX_OUTPUT_SEGMENT_CHARS}")
        if not 1 <= max_deltas <= MAX_OUTPUT_SEGMENT_DELTAS:
            raise ValueError(f"max_deltas must be between 1 and {MAX_OUTPUT_SEGMENT_DELTAS}")
        self._run_id = run_id
        self._attempt_id = attempt_id
        self._owner = owner
        self._store = store
        self._artifact_store = artifact_store
        self._max_chars = max_chars
        self._max_deltas = max_deltas
        self._parts: list[str] = []
        self._chars = 0
        self._deltas = 0
        self._sequence = 0

    async def add(self, content: str) -> None:
        """Add one provider delta, flushing at deterministic count/size boundaries."""
        if not isinstance(content, str):
            raise TypeError("run output delta must be text")
        remaining = content
        while remaining:
            available = self._max_chars - self._chars
            piece, remaining = remaining[:available], remaining[available:]
            self._parts.append(piece)
            self._chars += len(piece)
            self._deltas += 1
            if self._chars == self._max_chars or self._deltas == self._max_deltas:
                await self.flush()

    async def flush(self) -> None:
        """Stage and atomically authorize the current segment."""
        if not self._parts:
            return
        content = "".join(self._parts)
        delta_count = self._deltas
        segment_sequence = self._sequence + 1
        scope = ArtifactScope(
            run_id=self._run_id,
            tenant_id=self._owner.tenant_id,
            user_id=self._owner.user_id,
        )
        reference = await self._artifact_store.stage_json(
            {
                "schema_version": OUTPUT_SEGMENT_SCHEMA_VERSION,
                "run_id": self._run_id,
                "segment_sequence": segment_sequence,
                "content": content,
                "delta_count": delta_count,
            },
            scope=scope,
            purpose=OUTPUT_SEGMENT_ARTIFACT_PURPOSE,
            metadata={"segment_sequence": segment_sequence},
        )
        occurred_at = datetime.now(UTC).isoformat()
        proposed = RuntimeEventInput(
            event_id=f"evt_output_{uuid4().hex}",
            run_id=self._run_id,
            event_type=OUTPUT_SEGMENT_EVENT_TYPE,
            occurred_at=occurred_at,
            attempt_id=self._attempt_id,
            payload={
                "schema_version": OUTPUT_SEGMENT_SCHEMA_VERSION,
                "segment_sequence": segment_sequence,
                "artifact_id": reference.artifact_id,
                "content_chars": len(content),
                "delta_count": delta_count,
            },
        )
        await asyncio.to_thread(
            self._store.append_runtime_event,
            proposed,
            owner=self._owner,
            artifact_reference=reference,
        )
        self._parts.clear()
        self._chars = 0
        self._deltas = 0
        self._sequence = segment_sequence

    async def finish(self) -> None:
        await self.flush()


async def load_run_output_segment(
    event: RuntimeEvent,
    *,
    store: RuntimeStore,
    artifact_store: ArtifactStore,
    owner: RunOwner | None,
) -> RunOutputSegment:
    """Authorize, load, and cross-check one minimized segment event."""
    if event.event_type != OUTPUT_SEGMENT_EVENT_TYPE:
        raise RunOutputSegmentError(run_id=event.run_id, reason="event_type")
    payload = thaw_data(event.payload)
    if not isinstance(payload, Mapping):
        raise RunOutputSegmentError(run_id=event.run_id, reason="event_payload")
    try:
        schema_version = payload["schema_version"]
        segment_sequence = payload["segment_sequence"]
        artifact_id = payload["artifact_id"]
        content_chars = payload["content_chars"]
        delta_count = payload["delta_count"]
        if schema_version != OUTPUT_SEGMENT_SCHEMA_VERSION:
            raise ValueError
        if not isinstance(segment_sequence, int) or isinstance(segment_sequence, bool):
            raise TypeError
        if not isinstance(artifact_id, str) or not artifact_id:
            raise TypeError
        if not isinstance(content_chars, int) or isinstance(content_chars, bool):
            raise TypeError
        if not isinstance(delta_count, int) or isinstance(delta_count, bool):
            raise TypeError
    except (KeyError, TypeError, ValueError) as exc:
        raise RunOutputSegmentError(run_id=event.run_id, reason="event_payload") from exc
    reference = await asyncio.to_thread(store.get_artifact, artifact_id, owner=owner)
    if reference.purpose != OUTPUT_SEGMENT_ARTIFACT_PURPOSE:
        raise RunOutputSegmentError(run_id=event.run_id, reason="artifact_purpose")
    value = await artifact_store.load_json(reference)
    if not isinstance(value, Mapping):
        raise RunOutputSegmentError(run_id=event.run_id, reason="artifact_payload")
    expected = {
        "schema_version": OUTPUT_SEGMENT_SCHEMA_VERSION,
        "run_id": event.run_id,
        "segment_sequence": segment_sequence,
        "delta_count": delta_count,
    }
    if any(value.get(key) != item for key, item in expected.items()):
        raise RunOutputSegmentError(run_id=event.run_id, reason="artifact_binding")
    content = value.get("content")
    if not isinstance(content, str) or len(content) != content_chars:
        raise RunOutputSegmentError(run_id=event.run_id, reason="content_length")
    try:
        return RunOutputSegment(
            run_id=event.run_id,
            segment_sequence=segment_sequence,
            event_sequence=event.sequence,
            cursor=encode_event_cursor(run_id=event.run_id, sequence=event.sequence),
            content=content,
            delta_count=delta_count,
            artifact_id=artifact_id,
            occurred_at=event.occurred_at,
        )
    except ValueError as exc:
        raise RunOutputSegmentError(run_id=event.run_id, reason="segment_bounds") from exc


__all__ = [
    "DurableOutputSegmentWriter",
    "MAX_OUTPUT_SEGMENT_CHARS",
    "MAX_OUTPUT_SEGMENT_DELTAS",
    "OUTPUT_SEGMENT_ARTIFACT_PURPOSE",
    "OUTPUT_SEGMENT_EVENT_TYPE",
    "OUTPUT_SEGMENT_SCHEMA_VERSION",
    "RunOutputSegment",
    "RunOutputSegmentError",
    "load_run_output_segment",
]
