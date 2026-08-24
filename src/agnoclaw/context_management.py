"""Truthful, scoped context accounting, archival, search, and rehydration.

The model context is a cache, not the source of truth.  This module keeps the
source trajectory in the scoped :class:`~agnoclaw.runtime.ArtifactStore`, gives
every searchable item a stable content-derived identity, and makes provenance
explicit when archived material is rehydrated.  It deliberately does not call a
model and therefore cannot silently summarize, rewrite, or promote evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

from .runtime.artifacts import ArtifactReference, ArtifactScope, ArtifactStore
from .runtime.errors import HarnessError
from .runtime.security import freeze_data, thaw_data

CONTEXT_SCHEMA_VERSION = "1.0"
_ITEM_ID_RE = re.compile(r"^context-item:v1:[0-9a-f]{64}:[0-9a-f]{64}$")
_SEGMENT_ID_RE = re.compile(r"^context-segment:v1:[0-9a-f]{64}:[0-9a-f]{64}$")
_CHECKPOINT_ID_RE = re.compile(r"^context-checkpoint:v1:[0-9a-f]{64}:[0-9a-f]{64}$")
_TERM_RE = re.compile(r"[\w-]+", re.UNICODE)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_identifier(value: str | None, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > 512:
        raise ValueError(f"{field_name} cannot exceed 512 characters")
    return value


def _safe_text(value: Any, *, max_bytes: int) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            )
        except Exception:
            text = str(value)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    clipped = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return f"{clipped}\n[agnoclaw: item clipped from {len(encoded)} bytes]"


class ContextItemKind(StrEnum):
    """Semantic class used by retention and inspection policies."""

    SYSTEM_INSTRUCTION = "system_instruction"
    GOAL = "goal"
    USER_INTENT = "user_intent"
    ASSISTANT_RESPONSE = "assistant_response"
    TOOL_RESULT = "tool_result"
    APPROVAL = "approval"
    DECISION = "decision"
    PLAN = "plan"
    CITATION = "citation"
    TEST_RESULT = "test_result"
    FILE_REFERENCE = "file_reference"
    PROGRESS = "progress"
    OPEN_QUESTION = "open_question"
    ARTIFACT_REFERENCE = "artifact_reference"
    FAILURE = "failure"
    SUMMARY = "summary"
    OTHER = "other"


class ContextSource(StrEnum):
    """Where context supplied to a caller or model was reconstructed from."""

    LIVE = "live"
    SUMMARY = "summary"
    ARTIFACT = "artifact"
    MEMORY = "memory"
    TRAJECTORY = "trajectory"


@dataclass(frozen=True)
class ContextContinuationRecord:
    """Typed, bounded state that must survive a context replacement.

    The free-form ``summary`` remains the model-readable narrative. The other fields
    become individually searchable invariant items with stable provenance, so a
    plausible summary cannot silently erase goals, decisions, approvals, test state,
    files, citations, progress, or unresolved questions.
    """

    summary: str
    goal: str | None = None
    plan: tuple[str, ...] = ()
    progress: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    approvals: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    citations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        summary = _require_continuation_text(self.summary, field_name="summary", max_bytes=65_536)
        object.__setattr__(self, "summary", summary)
        if self.goal is not None:
            object.__setattr__(
                self,
                "goal",
                _require_continuation_text(self.goal, field_name="goal"),
            )
        total = 1 if self.goal is not None else 0
        for field_name in (
            "plan",
            "progress",
            "decisions",
            "approvals",
            "open_questions",
            "tests",
            "files",
            "citations",
        ):
            raw = getattr(self, field_name)
            if isinstance(raw, str) or not isinstance(raw, Sequence):
                raise TypeError(f"{field_name} must be a bounded sequence of strings")
            if len(raw) > 64:
                raise ValueError(f"{field_name} cannot contain more than 64 entries")
            normalized = tuple(
                _require_continuation_text(value, field_name=field_name) for value in raw
            )
            object.__setattr__(self, field_name, normalized)
            total += len(normalized)
        if total == 0:
            raise ValueError("a continuation record requires at least one structured field")
        if total > 256:
            raise ValueError("a continuation record cannot exceed 256 structured entries")

    @property
    def record_id(self) -> str:
        return f"context-continuation:v1:{_sha256(_canonical(self.to_dict()))}"

    @property
    def entry_count(self) -> int:
        return sum(1 for _field_name, _kind, _content, _index in self.entries())

    def entries(self) -> tuple[tuple[str, ContextItemKind, str, int], ...]:
        rows: list[tuple[str, ContextItemKind, str, int]] = []
        if self.goal is not None:
            rows.append(("goal", ContextItemKind.GOAL, self.goal, 0))
        field_kinds = (
            ("plan", ContextItemKind.PLAN),
            ("progress", ContextItemKind.PROGRESS),
            ("decisions", ContextItemKind.DECISION),
            ("approvals", ContextItemKind.APPROVAL),
            ("open_questions", ContextItemKind.OPEN_QUESTION),
            ("tests", ContextItemKind.TEST_RESULT),
            ("files", ContextItemKind.FILE_REFERENCE),
            ("citations", ContextItemKind.CITATION),
        )
        for field_name, kind in field_kinds:
            rows.extend(
                (field_name, kind, content, index)
                for index, content in enumerate(getattr(self, field_name))
            )
        return tuple(rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "summary": self.summary,
            "goal": self.goal,
            "plan": list(self.plan),
            "progress": list(self.progress),
            "decisions": list(self.decisions),
            "approvals": list(self.approvals),
            "open_questions": list(self.open_questions),
            "tests": list(self.tests),
            "files": list(self.files),
            "citations": list(self.citations),
        }


def _require_continuation_text(
    value: Any,
    *,
    field_name: str,
    max_bytes: int = 16_384,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} entries must be non-empty strings")
    encoded = value.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError(f"{field_name} entries cannot exceed {max_bytes} UTF-8 bytes")
    return value.strip()


class ContextBudgetAction(StrEnum):
    """Truthful budget recommendation; it never triggers mutation by itself."""

    NONE = "none"
    PREPARE = "prepare"
    COMPACT = "compact"
    EMERGENCY = "emergency"


@dataclass(frozen=True)
class ContextScope:
    """Exact tenant/user/session namespace for context material."""

    session_id: str
    tenant_id: str | None = None
    user_id: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.session_id, field_name="session_id")
        for name in ("tenant_id", "user_id"):
            value = getattr(self, name)
            if value is not None:
                _require_identifier(value, field_name=name)

    @property
    def digest(self) -> str:
        return _sha256(_canonical(self.to_dict()))

    @property
    def artifact_scope(self) -> ArtifactScope:
        return ArtifactScope(
            run_id=f"context-{self.digest}",
            tenant_id=self.tenant_id,
            user_id=self.user_id,
        )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextScope:
        return cls(
            tenant_id=value.get("tenant_id"),
            user_id=value.get("user_id"),
            session_id=value["session_id"],
        )


class TokenCounter(Protocol):
    """Small provider-neutral token accounting seam."""

    def count(self, text: str) -> int: ...


@dataclass(frozen=True)
class DeterministicTokenCounter:
    """Conservative deterministic fallback when a model tokenizer is unavailable."""

    utf8_bytes_per_token: int = 4

    def __post_init__(self) -> None:
        if self.utf8_bytes_per_token <= 0:
            raise ValueError("utf8_bytes_per_token must be positive")

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, math.ceil(len(text.encode("utf-8")) / self.utf8_bytes_per_token))


@dataclass(frozen=True)
class ContextBudget:
    """One inspectable context-budget measurement with hysteresis thresholds."""

    used_tokens: int
    max_tokens: int
    prepare_at: float = 0.80
    compact_at: float = 0.90
    emergency_at: float = 0.97
    release_below: float = 0.70
    exact: bool = False

    def __post_init__(self) -> None:
        if self.used_tokens < 0 or self.max_tokens <= 0:
            raise ValueError("context token counts require used >= 0 and max > 0")
        if not 0 < self.release_below < self.prepare_at < self.compact_at < self.emergency_at <= 1:
            raise ValueError("context thresholds must be strictly ordered")

    @property
    def utilization(self) -> float:
        return self.used_tokens / self.max_tokens

    @property
    def action(self) -> ContextBudgetAction:
        if self.utilization >= self.emergency_at:
            return ContextBudgetAction.EMERGENCY
        if self.utilization >= self.compact_at:
            return ContextBudgetAction.COMPACT
        if self.utilization >= self.prepare_at:
            return ContextBudgetAction.PREPARE
        return ContextBudgetAction.NONE

    @property
    def over_budget(self) -> bool:
        return self.used_tokens > self.max_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "used_tokens": self.used_tokens,
            "max_tokens": self.max_tokens,
            "utilization": self.utilization,
            "action": self.action.value,
            "over_budget": self.over_budget,
            "exact": self.exact,
            "thresholds": {
                "prepare_at": self.prepare_at,
                "compact_at": self.compact_at,
                "emergency_at": self.emergency_at,
                "release_below": self.release_below,
            },
        }


@dataclass(frozen=True)
class ContextItem:
    """Stable itemized context; content stays in its scoped trajectory artifact."""

    item_id: str
    kind: ContextItemKind
    content: str
    ordinal: int
    token_count: int
    invariant: bool = False
    provenance: Any = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _ITEM_ID_RE.fullmatch(self.item_id):
            raise ValueError("item_id is not a canonical v1 context item ID")
        if self.ordinal < 0 or self.token_count < 0:
            raise ValueError("context ordinal and token_count cannot be negative")
        if not isinstance(self.content, str):
            raise TypeError("context item content must be text")
        object.__setattr__(self, "provenance", freeze_data(self.provenance))

    def expected_id(self, scope: ContextScope) -> str:
        digest = _sha256(
            _canonical(
                {
                    "scope": scope.to_dict(),
                    "kind": self.kind.value,
                    "content": self.content,
                    "ordinal": self.ordinal,
                    "provenance": thaw_data(self.provenance),
                }
            )
        )
        return f"context-item:v1:{scope.digest}:{digest}"

    @classmethod
    def create(
        cls,
        *,
        scope: ContextScope,
        kind: ContextItemKind,
        content: Any,
        ordinal: int,
        counter: TokenCounter | None = None,
        invariant: bool = False,
        provenance: Mapping[str, Any] | None = None,
        max_content_bytes: int = 1_000_000,
    ) -> ContextItem:
        if max_content_bytes <= 0:
            raise ValueError("max_content_bytes must be positive")
        text = _safe_text(content, max_bytes=max_content_bytes)
        provenance_value = dict(provenance or {})
        digest = _sha256(
            _canonical(
                {
                    "scope": scope.to_dict(),
                    "kind": kind.value,
                    "content": text,
                    "ordinal": ordinal,
                    "provenance": provenance_value,
                }
            )
        )
        return cls(
            item_id=f"context-item:v1:{scope.digest}:{digest}",
            kind=kind,
            content=text,
            ordinal=ordinal,
            token_count=(counter or DeterministicTokenCounter()).count(text),
            invariant=invariant,
            provenance=provenance_value,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "kind": self.kind.value,
            "content": self.content,
            "ordinal": self.ordinal,
            "token_count": self.token_count,
            "invariant": self.invariant,
            "provenance": thaw_data(self.provenance),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextItem:
        return cls(
            item_id=value["item_id"],
            kind=ContextItemKind(value["kind"]),
            content=value["content"],
            ordinal=int(value["ordinal"]),
            token_count=int(value["token_count"]),
            invariant=bool(value.get("invariant", False)),
            provenance=value.get("provenance") or {},
        )


@dataclass(frozen=True)
class ContextSegment:
    """Immutable trajectory boundary archived before live context replacement."""

    segment_id: str
    scope: ContextScope
    sequence: int
    items: tuple[ContextItem, ...]
    artifact: ArtifactReference
    total_tokens: int

    def __post_init__(self) -> None:
        if not _SEGMENT_ID_RE.fullmatch(self.segment_id):
            raise ValueError("segment_id is not a canonical v1 context segment ID")
        if self.sequence <= 0:
            raise ValueError("context segment sequence must be positive")
        if self.artifact.scope != self.scope.artifact_scope:
            raise ValueError("context segment artifact scope mismatch")
        if (
            self.artifact.purpose != "context_trajectory"
            or self.artifact.media_type != "application/json"
            or self.artifact.encoding != "utf-8"
        ):
            raise ValueError("context segment requires a JSON trajectory artifact")
        if self.total_tokens != sum(item.token_count for item in self.items):
            raise ValueError("context segment token total is inconsistent")
        if tuple(sorted(item.ordinal for item in self.items)) != tuple(
            item.ordinal for item in self.items
        ):
            raise ValueError("context items must be in ordinal order")
        if any(item.item_id != item.expected_id(self.scope) for item in self.items):
            raise ValueError("context item identity or scope mismatch")
        expected_segment = _sha256(
            _canonical(
                {
                    "scope": self.scope.to_dict(),
                    "sequence": self.sequence,
                    "item_ids": [item.item_id for item in self.items],
                }
            )
        )
        if self.segment_id != f"context-segment:v1:{self.scope.digest}:{expected_segment}":
            raise ValueError("context segment identity does not match its items")

    @property
    def source_tokens(self) -> int:
        """Tokens present in source messages, excluding derived continuation items."""
        total = 0
        for item in self.items:
            provenance = thaw_data(item.provenance)
            continuation = (
                provenance.get("continuation") if isinstance(provenance, Mapping) else None
            )
            if not isinstance(continuation, Mapping):
                total += item.token_count
        return total

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "scope": self.scope.to_dict(),
            "sequence": self.sequence,
            "artifact": self.artifact.to_dict(),
            "total_tokens": self.total_tokens,
            "source_tokens": self.source_tokens,
            "item_ids": [item.item_id for item in self.items],
            "item_count": len(self.items),
        }


@dataclass(frozen=True)
class ArchivedContextSegment:
    """Content-free manifest entry for one archived segment."""

    segment_id: str
    scope: ContextScope
    sequence: int
    artifact: ArtifactReference
    total_tokens: int
    source_tokens: int
    item_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _SEGMENT_ID_RE.fullmatch(self.segment_id):
            raise ValueError("segment_id is not a canonical v1 context segment ID")
        if (
            self.sequence <= 0
            or self.total_tokens < 0
            or self.source_tokens < 0
            or self.source_tokens > self.total_tokens
        ):
            raise ValueError("invalid archived context counters")
        if self.artifact.scope != self.scope.artifact_scope:
            raise ValueError("archived context artifact scope mismatch")
        if (
            self.artifact.purpose != "context_trajectory"
            or self.artifact.media_type != "application/json"
            or self.artifact.encoding != "utf-8"
        ):
            raise ValueError("archived context requires a JSON trajectory artifact")
        if any(not _ITEM_ID_RE.fullmatch(item_id) for item_id in self.item_ids):
            raise ValueError("manifest contains an invalid context item ID")

    @classmethod
    def from_segment(cls, segment: ContextSegment) -> ArchivedContextSegment:
        return cls(
            segment_id=segment.segment_id,
            scope=segment.scope,
            sequence=segment.sequence,
            artifact=segment.artifact,
            total_tokens=segment.total_tokens,
            source_tokens=segment.source_tokens,
            item_ids=tuple(item.item_id for item in segment.items),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "scope": self.scope.to_dict(),
            "sequence": self.sequence,
            "artifact": self.artifact.to_dict(),
            "total_tokens": self.total_tokens,
            "source_tokens": self.source_tokens,
            "item_ids": list(self.item_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ArchivedContextSegment:
        return cls(
            segment_id=value["segment_id"],
            scope=ContextScope.from_dict(value["scope"]),
            sequence=int(value["sequence"]),
            artifact=ArtifactReference.from_dict(dict(value["artifact"])),
            total_tokens=int(value["total_tokens"]),
            source_tokens=int(value.get("source_tokens", value["total_tokens"])),
            item_ids=tuple(value.get("item_ids") or ()),
        )


@dataclass(frozen=True)
class ContextCheckpoint:
    """Evidence that one archived segment replaced a bounded live context."""

    checkpoint_id: str
    scope: ContextScope
    sequence: int
    segment_id: str
    summary: str
    retained_item_ids: tuple[str, ...]
    before_tokens: int
    after_tokens: int

    def __post_init__(self) -> None:
        if not _CHECKPOINT_ID_RE.fullmatch(self.checkpoint_id):
            raise ValueError("checkpoint_id is not a canonical v1 context checkpoint ID")
        if self.sequence <= 0 or self.before_tokens <= 0 or self.after_tokens < 0:
            raise ValueError("invalid context checkpoint counters")
        if self.after_tokens >= self.before_tokens:
            raise ValueError("context checkpoint must reduce live token usage")
        if not self.summary.strip():
            raise ValueError("context checkpoint summary cannot be empty")
        if not _SEGMENT_ID_RE.fullmatch(self.segment_id):
            raise ValueError("context checkpoint segment ID is invalid")
        if any(not _ITEM_ID_RE.fullmatch(value) for value in self.retained_item_ids):
            raise ValueError("context checkpoint retained item ID is invalid")
        if any(value.split(":")[2] != self.scope.digest for value in self.retained_item_ids):
            raise ValueError("context checkpoint retained item scope is invalid")
        expected = ContextCheckpoint._checkpoint_id(
            scope=self.scope,
            sequence=self.sequence,
            segment_id=self.segment_id,
            summary_digest=self.summary_digest,
            retained_item_ids=self.retained_item_ids,
            before_tokens=self.before_tokens,
            after_tokens=self.after_tokens,
        )
        if self.checkpoint_id != expected:
            raise ValueError("context checkpoint identity does not match its evidence")

    @staticmethod
    def _checkpoint_id(
        *,
        scope: ContextScope,
        sequence: int,
        segment_id: str,
        summary_digest: str,
        retained_item_ids: Sequence[str],
        before_tokens: int,
        after_tokens: int,
    ) -> str:
        payload = {
            "scope": scope.to_dict(),
            "sequence": sequence,
            "segment_id": segment_id,
            "summary_digest": summary_digest,
            "retained_item_ids": list(retained_item_ids),
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
        }
        return f"context-checkpoint:v1:{scope.digest}:{_sha256(_canonical(payload))}"

    @classmethod
    def create(
        cls,
        *,
        scope: ContextScope,
        sequence: int,
        segment_id: str,
        summary: str,
        retained_item_ids: Sequence[str],
        before_tokens: int,
        after_tokens: int,
    ) -> ContextCheckpoint:
        summary_digest = f"sha256:{_sha256(summary.encode('utf-8'))}"
        return cls(
            checkpoint_id=cls._checkpoint_id(
                scope=scope,
                sequence=sequence,
                segment_id=segment_id,
                summary_digest=summary_digest,
                retained_item_ids=retained_item_ids,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
            ),
            scope=scope,
            sequence=sequence,
            segment_id=segment_id,
            summary=summary,
            retained_item_ids=tuple(retained_item_ids),
            before_tokens=before_tokens,
            after_tokens=after_tokens,
        )

    @property
    def saved_tokens(self) -> int:
        return self.before_tokens - self.after_tokens

    @property
    def summary_digest(self) -> str:
        return f"sha256:{_sha256(self.summary.encode('utf-8'))}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "scope": self.scope.to_dict(),
            "sequence": self.sequence,
            "segment_id": self.segment_id,
            "summary": self.summary,
            "summary_digest": self.summary_digest,
            "retained_item_ids": list(self.retained_item_ids),
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "saved_tokens": self.saved_tokens,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextCheckpoint:
        return cls(
            checkpoint_id=value["checkpoint_id"],
            scope=ContextScope.from_dict(value["scope"]),
            sequence=int(value["sequence"]),
            segment_id=value["segment_id"],
            summary=value["summary"],
            retained_item_ids=tuple(value.get("retained_item_ids") or ()),
            before_tokens=int(value["before_tokens"]),
            after_tokens=int(value["after_tokens"]),
        )


@dataclass(frozen=True)
class ArchivedContextCheckpoint:
    """Content-free checkpoint evidence persisted in a session manifest."""

    checkpoint_id: str
    scope: ContextScope
    sequence: int
    segment_id: str
    summary_digest: str
    retained_item_ids: tuple[str, ...]
    before_tokens: int
    after_tokens: int

    def __post_init__(self) -> None:
        if not _CHECKPOINT_ID_RE.fullmatch(self.checkpoint_id):
            raise ValueError("archived checkpoint ID is invalid")
        if not _SEGMENT_ID_RE.fullmatch(self.segment_id):
            raise ValueError("archived checkpoint segment ID is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.summary_digest):
            raise ValueError("archived checkpoint summary digest is invalid")
        if self.sequence <= 0 or self.before_tokens <= 0 or self.after_tokens < 0:
            raise ValueError("invalid archived checkpoint counters")
        if self.after_tokens >= self.before_tokens:
            raise ValueError("archived checkpoint must reduce live token usage")
        expected = ContextCheckpoint._checkpoint_id(
            scope=self.scope,
            sequence=self.sequence,
            segment_id=self.segment_id,
            summary_digest=self.summary_digest,
            retained_item_ids=self.retained_item_ids,
            before_tokens=self.before_tokens,
            after_tokens=self.after_tokens,
        )
        if self.checkpoint_id != expected:
            raise ValueError("archived checkpoint identity does not match its evidence")
        if any(not _ITEM_ID_RE.fullmatch(value) for value in self.retained_item_ids):
            raise ValueError("archived checkpoint retained item ID is invalid")
        if any(value.split(":")[2] != self.scope.digest for value in self.retained_item_ids):
            raise ValueError("archived checkpoint retained item scope is invalid")

    @classmethod
    def from_checkpoint(cls, checkpoint: ContextCheckpoint) -> ArchivedContextCheckpoint:
        return cls(
            checkpoint_id=checkpoint.checkpoint_id,
            scope=checkpoint.scope,
            sequence=checkpoint.sequence,
            segment_id=checkpoint.segment_id,
            summary_digest=checkpoint.summary_digest,
            retained_item_ids=checkpoint.retained_item_ids,
            before_tokens=checkpoint.before_tokens,
            after_tokens=checkpoint.after_tokens,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "scope": self.scope.to_dict(),
            "sequence": self.sequence,
            "segment_id": self.segment_id,
            "summary_digest": self.summary_digest,
            "retained_item_ids": list(self.retained_item_ids),
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "saved_tokens": self.before_tokens - self.after_tokens,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ArchivedContextCheckpoint:
        return cls(
            checkpoint_id=value["checkpoint_id"],
            scope=ContextScope.from_dict(value["scope"]),
            sequence=int(value["sequence"]),
            segment_id=value["segment_id"],
            summary_digest=value["summary_digest"],
            retained_item_ids=tuple(value.get("retained_item_ids") or ()),
            before_tokens=int(value["before_tokens"]),
            after_tokens=int(value["after_tokens"]),
        )


@dataclass(frozen=True)
class ContextManifest:
    """Bounded, content-free chain persisted with the owning Agno session."""

    scope: ContextScope
    revision: int = 0
    segments: tuple[ArchivedContextSegment, ...] = ()
    checkpoints: tuple[ArchivedContextCheckpoint, ...] = ()
    schema_version: str = CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CONTEXT_SCHEMA_VERSION:
            raise ValueError(f"unsupported context schema '{self.schema_version}'")
        if self.revision < 0:
            raise ValueError("context manifest revision cannot be negative")
        if self.revision != len(self.segments) or self.revision != len(self.checkpoints):
            raise ValueError("context manifest revision does not match its evidence chain")
        if any(segment.scope != self.scope for segment in self.segments):
            raise ValueError("context manifest segment scope mismatch")
        if any(checkpoint.scope != self.scope for checkpoint in self.checkpoints):
            raise ValueError("context manifest checkpoint scope mismatch")
        sequences = tuple(segment.sequence for segment in self.segments)
        if sequences != tuple(range(1, len(sequences) + 1)):
            raise ValueError("context manifest segment sequence is not contiguous")
        checkpoint_sequences = tuple(checkpoint.sequence for checkpoint in self.checkpoints)
        if checkpoint_sequences != tuple(range(1, len(checkpoint_sequences) + 1)):
            raise ValueError("context manifest checkpoint sequence is not contiguous")
        for segment, checkpoint in zip(self.segments, self.checkpoints, strict=True):
            if checkpoint.segment_id != segment.segment_id:
                raise ValueError("context manifest segment/checkpoint chain is inconsistent")
            if checkpoint.before_tokens != segment.source_tokens:
                raise ValueError("context manifest checkpoint token evidence is inconsistent")
            if not set(checkpoint.retained_item_ids).issubset(segment.item_ids):
                raise ValueError("context manifest retained-item evidence is inconsistent")

    def append(
        self,
        segment: ContextSegment,
        checkpoint: ContextCheckpoint,
        *,
        max_segments: int = 1_000,
    ) -> ContextManifest:
        if segment.scope != self.scope or checkpoint.scope != self.scope:
            raise ContextScopeError(self.scope, actual=segment.scope)
        expected = len(self.segments) + 1
        if segment.sequence != expected or checkpoint.sequence != len(self.checkpoints) + 1:
            raise ContextManifestConflictError(expected=expected)
        if checkpoint.segment_id != segment.segment_id:
            raise ContextManifestConflictError(expected=expected)
        segment_item_ids = {item.item_id for item in segment.items}
        if not set(checkpoint.retained_item_ids).issubset(segment_item_ids):
            raise ContextManifestConflictError(expected=expected)
        if checkpoint.before_tokens != segment.source_tokens:
            raise ContextManifestConflictError(expected=expected)
        if len(self.segments) >= max_segments:
            raise ContextManifestLimitError(max_segments=max_segments)
        return replace(
            self,
            revision=self.revision + 1,
            segments=(*self.segments, ArchivedContextSegment.from_segment(segment)),
            checkpoints=(
                *self.checkpoints,
                ArchivedContextCheckpoint.from_checkpoint(checkpoint),
            ),
        )

    @property
    def artifact_storage_keys(self) -> tuple[str, ...]:
        """Authoritative live keys a host must include in artifact GC input."""
        return tuple(segment.artifact.storage_key for segment in self.segments)

    @property
    def artifact_ids(self) -> tuple[str, ...]:
        """Content addresses owned by this session context manifest."""
        return tuple(segment.artifact.artifact_id for segment in self.segments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scope": self.scope.to_dict(),
            "revision": self.revision,
            "segments": [segment.to_dict() for segment in self.segments],
            "checkpoints": [checkpoint.to_dict() for checkpoint in self.checkpoints],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextManifest:
        return cls(
            schema_version=value.get("schema_version", ""),
            scope=ContextScope.from_dict(value["scope"]),
            revision=int(value.get("revision", 0)),
            segments=tuple(
                ArchivedContextSegment.from_dict(segment) for segment in value.get("segments") or ()
            ),
            checkpoints=tuple(
                ArchivedContextCheckpoint.from_dict(checkpoint)
                for checkpoint in value.get("checkpoints") or ()
            ),
        )


@dataclass(frozen=True)
class ContextSearchHit:
    """One scoped search result with explicit reconstruction provenance."""

    item_id: str
    segment_id: str
    kind: ContextItemKind
    excerpt: str
    score: float
    source: ContextSource
    provenance: Any

    def __post_init__(self) -> None:
        if self.score < 0:
            raise ValueError("context search score cannot be negative")
        object.__setattr__(self, "provenance", freeze_data(self.provenance))


@dataclass(frozen=True)
class ContextRehydration:
    """Exact archived items selected for return or reinsertion into live context."""

    scope: ContextScope
    items: tuple[ContextItem, ...]
    sources: tuple[ContextSource, ...] = (ContextSource.TRAJECTORY, ContextSource.ARTIFACT)
    injected: bool = False

    @property
    def token_count(self) -> int:
        return sum(item.token_count for item in self.items)


class ContextScopeError(HarnessError):
    def __init__(self, expected: ContextScope, *, actual: ContextScope) -> None:
        super().__init__(
            code="CONTEXT_SCOPE_MISMATCH",
            category="context",
            message="Archived context does not belong to the requested identity scope.",
            retryable=False,
            details={"expected_scope": expected.digest, "actual_scope": actual.digest},
        )


class ContextManifestConflictError(HarnessError):
    def __init__(self, *, expected: int) -> None:
        super().__init__(
            code="CONTEXT_MANIFEST_CONFLICT",
            category="context",
            message="The context manifest changed before the segment could be committed.",
            retryable=True,
            details={"expected_sequence": expected},
        )


class ContextManifestLimitError(HarnessError):
    def __init__(self, *, max_segments: int) -> None:
        super().__init__(
            code="CONTEXT_MANIFEST_LIMIT",
            category="context",
            message="The bounded context manifest has reached its segment limit.",
            retryable=False,
            details={"max_segments": max_segments},
        )


class ContextItemNotFoundError(HarnessError):
    def __init__(self, item_ids: Iterable[str]) -> None:
        missing = tuple(sorted(set(item_ids)))
        super().__init__(
            code="CONTEXT_ITEM_NOT_FOUND",
            category="context",
            message="One or more selected context items are not in the scoped archive.",
            retryable=False,
            details={"missing_item_ids": list(missing[:50]), "missing_count": len(missing)},
        )


class ContextQualityError(HarnessError):
    def __init__(self, *, reason: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(
            code="CONTEXT_COMPACTION_QUALITY_FAILED",
            category="context",
            message="Context replacement was refused because its quality gate failed.",
            retryable=False,
            details={"reason": reason, **dict(details or {})},
        )


def _terms(value: str) -> Counter[str]:
    return Counter(term.casefold() for term in _TERM_RE.findall(value))


def _kind_for_role(role: str | None, *, has_error: bool = False) -> ContextItemKind:
    if has_error:
        return ContextItemKind.FAILURE
    normalized = str(role or "").casefold()
    if normalized == "system":
        return ContextItemKind.SYSTEM_INSTRUCTION
    if normalized == "user":
        return ContextItemKind.USER_INTENT
    if normalized == "assistant":
        return ContextItemKind.ASSISTANT_RESPONSE
    if normalized in {"tool", "function"}:
        return ContextItemKind.TOOL_RESULT
    return ContextItemKind.OTHER


def _harness_context_kind(message: Any) -> str | None:
    provider_data = (
        message.get("provider_data")
        if isinstance(message, Mapping)
        else getattr(message, "provider_data", None)
    )
    value = (
        provider_data.get("_agnoclaw_context_kind") if isinstance(provider_data, Mapping) else None
    )
    return value if value in {"checkpoint", "rehydration", "summary", "memory_flush"} else None


def _spilled_output_evidence(value: Any) -> dict[str, Any] | None:
    candidate = value
    if isinstance(candidate, str) and candidate.lstrip().startswith("{"):
        try:
            candidate = json.loads(candidate)
        except (TypeError, ValueError):
            return None
    if not isinstance(candidate, Mapping) or candidate.get("type") != "agnoclaw.spilled_output":
        return None
    artifact = candidate.get("artifact")
    read = candidate.get("read")
    if not isinstance(artifact, Mapping) or not isinstance(read, Mapping):
        return None
    artifact_id = candidate.get("id")
    checksum = artifact.get("checksum")
    if not isinstance(artifact_id, str) or not isinstance(checksum, str):
        return None
    return {
        "artifact_id": artifact_id,
        "checksum": checksum,
        "read_tool": read.get("tool"),
        "rendered_chars": candidate.get("rendered_chars"),
    }


class ArtifactContextArchive:
    """Bounded context archive over an injected scoped ArtifactStore."""

    def __init__(
        self,
        store: ArtifactStore,
        *,
        max_items_per_segment: int = 20_000,
        max_item_bytes: int = 1_000_000,
        max_search_segments: int = 1_000,
        max_rehydrate_tokens: int = 16_000,
        counter: TokenCounter | None = None,
    ) -> None:
        if not 1 <= max_items_per_segment <= 100_000:
            raise ValueError("max_items_per_segment must be between 1 and 100000")
        if max_item_bytes <= 0 or max_search_segments <= 0 or max_rehydrate_tokens <= 0:
            raise ValueError("context archive bounds must be positive")
        self.store = store
        self.max_items_per_segment = max_items_per_segment
        self.max_item_bytes = max_item_bytes
        self.max_search_segments = max_search_segments
        self.max_rehydrate_tokens = max_rehydrate_tokens
        self.counter = counter or DeterministicTokenCounter()

    def items_from_messages(
        self,
        messages: Sequence[Any],
        *,
        scope: ContextScope,
        invariant_user_content: str | None = None,
        continuation: ContextContinuationRecord | None = None,
        continuation_origin: str | None = None,
        continuation_sources: Mapping[tuple[str, int], Mapping[str, Any]] | None = None,
    ) -> tuple[ContextItem, ...]:
        structured_count = continuation.entry_count if continuation is not None else 0
        item_count = len(messages) + structured_count
        if item_count > self.max_items_per_segment:
            raise HarnessError(
                code="CONTEXT_SEGMENT_LIMIT",
                category="context",
                message="The context segment exceeds the configured item bound.",
                retryable=False,
                details={
                    "item_count": item_count,
                    "max_items": self.max_items_per_segment,
                },
            )
        items: list[ContextItem] = []
        last_user = max(
            (
                index
                for index, message in enumerate(messages)
                if str(
                    message.get("role")
                    if isinstance(message, Mapping)
                    else getattr(message, "role", "")
                ).casefold()
                == "user"
                and _harness_context_kind(message) is None
                and (
                    invariant_user_content is None
                    or (
                        message.get("content")
                        if isinstance(message, Mapping)
                        else getattr(message, "content", None)
                    )
                    == invariant_user_content
                )
            ),
            default=-1,
        )
        for ordinal, message in enumerate(messages):
            if isinstance(message, Mapping):
                role = message.get("role")
                content = message.get("content")
                message_id = message.get("id")
                tool_name = message.get("tool_name")
                has_error = bool(message.get("tool_call_error"))
            else:
                role = getattr(message, "role", None)
                content = getattr(message, "content", None)
                message_id = getattr(message, "id", None)
                tool_name = getattr(message, "tool_name", None)
                has_error = bool(getattr(message, "tool_call_error", False))
            harness_context_kind = _harness_context_kind(message)
            spilled_output = _spilled_output_evidence(content)
            normalized_role = str(role or "").casefold()
            kind = (
                ContextItemKind.ARTIFACT_REFERENCE
                if spilled_output is not None
                else (
                    ContextItemKind.SUMMARY
                    if harness_context_kind == "checkpoint"
                    else (
                        ContextItemKind.SUMMARY
                        if harness_context_kind == "summary" and normalized_role == "assistant"
                        else (
                            ContextItemKind.OTHER
                            if harness_context_kind in {"rehydration", "summary"}
                            or (
                                harness_context_kind == "memory_flush"
                                and normalized_role in {"user", "assistant"}
                            )
                            else _kind_for_role(role, has_error=has_error)
                        )
                    )
                )
            )
            items.append(
                ContextItem.create(
                    scope=scope,
                    kind=kind,
                    content=content,
                    ordinal=ordinal,
                    counter=self.counter,
                    invariant=(
                        ordinal == last_user
                        or kind in {ContextItemKind.ARTIFACT_REFERENCE, ContextItemKind.FAILURE}
                    ),
                    provenance={
                        "message_id": str(message_id) if message_id is not None else None,
                        "role": str(role) if role is not None else None,
                        "tool_name": str(tool_name) if tool_name is not None else None,
                        "spilled_output": spilled_output,
                        "harness_context_kind": harness_context_kind,
                    },
                    max_content_bytes=self.max_item_bytes,
                )
            )
        if continuation is not None:
            record_id = continuation.record_id
            for offset, (field_name, kind, content, entry_index) in enumerate(
                continuation.entries(),
                start=len(messages),
            ):
                continuation_provenance: dict[str, Any] = {
                    "record_id": record_id,
                    "field": field_name,
                    "entry_index": entry_index,
                }
                if continuation_origin is not None:
                    continuation_provenance["origin"] = continuation_origin
                source = (continuation_sources or {}).get((field_name, entry_index))
                if source is not None:
                    continuation_provenance["source"] = dict(source)
                items.append(
                    ContextItem.create(
                        scope=scope,
                        kind=kind,
                        content=content,
                        ordinal=offset,
                        counter=self.counter,
                        invariant=True,
                        provenance={"continuation": continuation_provenance},
                        max_content_bytes=self.max_item_bytes,
                    )
                )
        return tuple(items)

    async def archive_messages(
        self,
        messages: Sequence[Any],
        *,
        scope: ContextScope,
        sequence: int,
        trajectory: Mapping[str, Any] | None = None,
        invariant_user_content: str | None = None,
        continuation: ContextContinuationRecord | None = None,
        continuation_origin: str | None = None,
        continuation_sources: Mapping[tuple[str, int], Mapping[str, Any]] | None = None,
    ) -> ContextSegment:
        if sequence <= 0:
            raise ValueError("context segment sequence must be positive")
        items = self.items_from_messages(
            messages,
            scope=scope,
            invariant_user_content=invariant_user_content,
            continuation=continuation,
            continuation_origin=continuation_origin,
            continuation_sources=continuation_sources,
        )
        segment_digest = _sha256(
            _canonical(
                {
                    "scope": scope.to_dict(),
                    "sequence": sequence,
                    "item_ids": [item.item_id for item in items],
                }
            )
        )
        segment_id = f"context-segment:v1:{scope.digest}:{segment_digest}"
        payload = {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "segment_id": segment_id,
            "scope": scope.to_dict(),
            "sequence": sequence,
            "items": [item.to_dict() for item in items],
            "trajectory": dict(trajectory or {}),
        }
        artifact = await self.store.stage_json(
            payload,
            scope=scope.artifact_scope,
            purpose="context_trajectory",
            metadata={
                "context_schema": CONTEXT_SCHEMA_VERSION,
                "segment_id": segment_id,
                "sequence": sequence,
                "item_count": len(items),
            },
        )
        return ContextSegment(
            segment_id=segment_id,
            scope=scope,
            sequence=sequence,
            items=items,
            artifact=artifact,
            total_tokens=sum(item.token_count for item in items),
        )

    async def _load(self, entry: ArchivedContextSegment, *, scope: ContextScope) -> ContextSegment:
        if entry.scope != scope:
            raise ContextScopeError(scope, actual=entry.scope)
        raw = await self.store.load_json(entry.artifact)
        if not isinstance(raw, Mapping):
            raise ContextQualityError(reason="segment_payload_not_mapping")
        try:
            actual_scope = ContextScope.from_dict(raw.get("scope") or {})
        except (KeyError, TypeError, ValueError) as exc:
            raise ContextQualityError(reason="segment_scope_invalid") from exc
        if actual_scope != scope:
            raise ContextScopeError(scope, actual=actual_scope)
        if (
            raw.get("segment_id") != entry.segment_id
            or int(raw.get("sequence", -1)) != entry.sequence
        ):
            raise ContextQualityError(reason="segment_manifest_mismatch")
        try:
            items = tuple(ContextItem.from_dict(value) for value in raw.get("items") or ())
            segment = ContextSegment(
                segment_id=entry.segment_id,
                scope=scope,
                sequence=entry.sequence,
                items=items,
                artifact=entry.artifact,
                total_tokens=sum(item.token_count for item in items),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContextQualityError(reason="segment_evidence_invalid") from exc
        if tuple(item.item_id for item in items) != entry.item_ids:
            raise ContextQualityError(reason="segment_item_manifest_mismatch")
        if segment.total_tokens != entry.total_tokens:
            raise ContextQualityError(reason="segment_token_manifest_mismatch")
        if segment.source_tokens != entry.source_tokens:
            raise ContextQualityError(reason="segment_source_token_manifest_mismatch")
        return segment

    async def load_latest_retained_items(
        self,
        manifest: ContextManifest,
        *,
        scope: ContextScope,
    ) -> tuple[ContextItem, ...]:
        """Load the latest checkpoint's bounded active item set without a search limit."""
        if manifest.scope != scope:
            raise ContextScopeError(scope, actual=manifest.scope)
        if not manifest.checkpoints:
            return ()
        checkpoint = manifest.checkpoints[-1]
        segment = await self._load(manifest.segments[-1], scope=scope)
        retained = frozenset(checkpoint.retained_item_ids)
        items = tuple(item for item in segment.items if item.item_id in retained)
        if {item.item_id for item in items} != retained:
            raise ContextQualityError(reason="checkpoint_retained_items_missing")
        return items

    async def search(
        self,
        manifest: ContextManifest,
        query: str,
        *,
        scope: ContextScope,
        limit: int = 10,
        kinds: Iterable[ContextItemKind] | None = None,
    ) -> tuple[ContextSearchHit, ...]:
        if manifest.scope != scope:
            raise ContextScopeError(scope, actual=manifest.scope)
        if not query.strip():
            raise ValueError("context search query cannot be empty")
        if len(query.encode("utf-8")) > 4_096:
            raise ValueError("context search query cannot exceed 4096 UTF-8 bytes")
        if not 1 <= limit <= 100:
            raise ValueError("context search limit must be between 1 and 100")
        if len(manifest.segments) > self.max_search_segments:
            raise HarnessError(
                code="CONTEXT_SEARCH_BOUND_EXCEEDED",
                category="context",
                message="The context search exceeds the configured segment bound.",
                retryable=False,
                details={
                    "segment_count": len(manifest.segments),
                    "max_segments": self.max_search_segments,
                },
            )
        query_terms = _terms(query)
        allowed = frozenset(kinds) if kinds is not None else None
        hits: list[ContextSearchHit] = []
        seen: set[str] = set()
        seen_continuation_values: set[tuple[str, str, str]] = set()
        for entry in reversed(manifest.segments):
            segment = await self._load(entry, scope=scope)
            for item in segment.items:
                if item.item_id in seen:
                    continue
                seen.add(item.item_id)
                provenance = item.provenance
                continuation = (
                    provenance.get("continuation") if isinstance(provenance, Mapping) else None
                )
                if isinstance(continuation, Mapping):
                    field_name = continuation.get("field")
                    if isinstance(field_name, str):
                        semantic_key = (
                            item.kind.value,
                            field_name,
                            _sha256(item.content.encode("utf-8")),
                        )
                        # Repeated compaction carries active structured state into
                        # a new immutable segment.  Keep the newest searchable
                        # instance rather than allowing identical carried state to
                        # crowd source evidence out of a bounded result set.
                        if semantic_key in seen_continuation_values:
                            continue
                        seen_continuation_values.add(semantic_key)
                if allowed is not None and item.kind not in allowed:
                    continue
                item_terms = _terms(item.content)
                overlap = sum(min(count, item_terms[term]) for term, count in query_terms.items())
                if overlap <= 0:
                    continue
                coverage = overlap / max(1, sum(query_terms.values()))
                density = overlap / max(1, sum(item_terms.values()))
                score = coverage * 0.8 + density * 0.2 + (0.05 if item.invariant else 0.0)
                excerpt = item.content
                if len(excerpt) > 500:
                    excerpt = f"{excerpt[:497]}..."
                hits.append(
                    ContextSearchHit(
                        item_id=item.item_id,
                        segment_id=segment.segment_id,
                        kind=item.kind,
                        excerpt=excerpt,
                        score=score,
                        source=ContextSource.TRAJECTORY,
                        provenance=item.provenance,
                    )
                )
        hits.sort(key=lambda hit: (-hit.score, hit.segment_id, hit.item_id))
        return tuple(hits[:limit])

    async def rehydrate(
        self,
        manifest: ContextManifest,
        item_ids: Iterable[str],
        *,
        scope: ContextScope,
        max_tokens: int | None = None,
    ) -> ContextRehydration:
        if manifest.scope != scope:
            raise ContextScopeError(scope, actual=manifest.scope)
        requested = tuple(dict.fromkeys(item_ids))
        if not requested:
            raise ValueError("at least one context item ID is required")
        if len(requested) > 100:
            raise ValueError("at most 100 context items can be rehydrated at once")
        if any(not _ITEM_ID_RE.fullmatch(value) for value in requested):
            raise ValueError("context item ID is invalid")
        remaining = set(requested)
        found: dict[str, ContextItem] = {}
        for entry in manifest.segments:
            if not remaining.intersection(entry.item_ids):
                continue
            segment = await self._load(entry, scope=scope)
            for item in segment.items:
                if item.item_id in remaining:
                    found[item.item_id] = item
                    remaining.remove(item.item_id)
            if not remaining:
                break
        if remaining:
            raise ContextItemNotFoundError(remaining)
        ordered = tuple(found[item_id] for item_id in requested)
        token_limit = self.max_rehydrate_tokens if max_tokens is None else max_tokens
        if token_limit <= 0:
            raise ValueError("max_tokens must be positive")
        total = sum(item.token_count for item in ordered)
        if total > token_limit:
            raise HarnessError(
                code="CONTEXT_REHYDRATION_BUDGET_EXCEEDED",
                category="context",
                message="Selected archived context exceeds the rehydration token budget.",
                retryable=False,
                details={"token_count": total, "max_tokens": token_limit},
            )
        return ContextRehydration(scope=scope, items=ordered)


__all__ = [
    "CONTEXT_SCHEMA_VERSION",
    "ArchivedContextCheckpoint",
    "ArchivedContextSegment",
    "ArtifactContextArchive",
    "ContextBudget",
    "ContextBudgetAction",
    "ContextCheckpoint",
    "ContextContinuationRecord",
    "ContextItem",
    "ContextItemKind",
    "ContextItemNotFoundError",
    "ContextManifest",
    "ContextManifestConflictError",
    "ContextManifestLimitError",
    "ContextQualityError",
    "ContextRehydration",
    "ContextScope",
    "ContextScopeError",
    "ContextSearchHit",
    "ContextSource",
    "DeterministicTokenCounter",
    "TokenCounter",
]
