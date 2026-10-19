"""Bounded, sequential domain context composition over existing token accounting."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from agnoclaw.context_management import DeterministicTokenCounter, TokenCounter
from agnoclaw.runtime.context import ExecutionContext

from ._contracts import Record, aware, fail, identifier
from .objects import ObjectRef


@dataclass(frozen=True)
class ContextContribution(Record):
    source: ObjectRef
    content: str
    observed_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        aware(self.observed_at)
        aware(self.expires_at)
        if self.observed_at >= self.expires_at:
            raise ValueError("context expiry must follow observation")
        if not isinstance(self.content, str) or len(self.content.encode("utf-8")) > 1_000_000:
            raise ValueError("context contribution exceeds the 1 MB text budget")


@dataclass(frozen=True)
class ContextContributor:
    name: str
    load: Callable[[ExecutionContext, ObjectRef], Awaitable[ContextContribution | None]] = field(
        repr=False,
        compare=False,
    )
    required: bool = False
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        identifier(self.name, "name")
        if not callable(self.load):
            raise TypeError("context contributor loader must be callable")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("contributor timeout must be positive and finite")


@dataclass(frozen=True)
class ContextBundle(Record):
    text: str
    token_count: int
    included: tuple[str, ...]
    omitted: tuple[tuple[str, str], ...]


async def assemble_context(
    contributors: Sequence[ContextContributor],
    *,
    target: ObjectRef,
    context: ExecutionContext,
    now: datetime,
    max_tokens: int,
    counter: TokenCounter | None = None,
) -> ContextBundle:
    """JSON-encode untrusted source data; do not insert this as trusted system policy.

    Required contributors go first; optional contributors retain caller priority order.
    Sequential loading is deliberate: host adapters often share one database session.
    Budget includes the entire rendered envelope. Required omissions fail explicitly.
    """
    target.require_scope(context)
    aware(now)
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ValueError("context token budget must be a positive integer")
    names = [provider.name for provider in contributors]
    if len(names) != len(set(names)):
        raise ValueError("context contributor names must be unique")
    counter = counter or DeterministicTokenCounter()
    content: list[dict] = []
    included: list[str] = []
    omitted: list[tuple[str, str]] = []
    text = "[]"
    if counter.count(text) > max_tokens:
        fail("CONTEXT_BUDGET", "Context envelope exceeds the token budget.")
    for provider in sorted(contributors, key=lambda item: not item.required):
        reason = "missing"
        item = None
        try:
            async with asyncio.timeout(provider.timeout_seconds):
                item = await provider.load(context, target)
        except Exception:
            reason = "error"
        if item is not None:
            # Scope violations are never downgraded to optional omissions.
            item.source.require_scope(context)
            if not item.observed_at <= now < item.expires_at:
                reason = "stale"
            else:
                candidate = content + [{"name": provider.name, "data": item.to_dict()}]
                candidate_text = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
                if counter.count(candidate_text) <= max_tokens:
                    content, text = candidate, candidate_text
                    included.append(provider.name)
                    continue
                reason = "budget"
        if provider.required:
            fail("CONTEXT_REQUIRED", f"Required context contributor unavailable ({reason}).")
        omitted.append((provider.name, reason))
    return ContextBundle(text, counter.count(text), tuple(included), tuple(omitted))
