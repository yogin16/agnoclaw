"""Portable detector results; hosts own scheduling and promotion of findings."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from agnoclaw.runtime.context import ExecutionContext

from ._contracts import Record, fail, frozen_json, identifier
from .components import DomainComponent, resolve, timeout
from .extensions import DomainEvent, EventType
from .identity import ContractRef
from .objects import ObjectRef


@dataclass(frozen=True)
class Finding(Record):
    finding_id: str
    target: ObjectRef
    details: Mapping[str, Any]

    def __post_init__(self) -> None:
        identifier(self.finding_id, "finding_id")
        object.__setattr__(self, "details", frozen_json(self.details))


@dataclass(frozen=True)
class WatcherResult(Record):
    """One terminal outcome per assessment: finding, suppression, or error."""

    assessed: int
    findings: tuple[Finding, ...] = ()
    suppressed: int = 0
    errored: int = 0
    source_health: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "source_health", frozen_json(self.source_health))
        if any(type(n) is not int or n < 0 for n in (self.assessed, self.suppressed, self.errored)):
            raise ValueError("watcher counts must be non-negative integers")
        if self.assessed != len(self.findings) + self.suppressed + self.errored:
            raise ValueError("every assessment must have exactly one accounted outcome")
        if len({f.finding_id for f in self.findings}) != len(self.findings):
            raise ValueError("finding IDs must be unique within a result")


@dataclass(frozen=True)
class Watcher(DomainComponent):
    name: str
    event_type: EventType
    detect: Callable[[DomainEvent, ExecutionContext], WatcherResult | Awaitable[WatcherResult]] = (
        field(repr=False)
    )
    implementation_id: str = field(kw_only=True)
    version: str = field(default="1", kw_only=True)
    timeout_seconds: float = field(default=30, kw_only=True)
    max_findings: int = field(default=100, kw_only=True)
    kind: ClassVar[str] = "watcher"

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        identifier(self.implementation_id, "implementation_id")
        timeout(self.timeout_seconds)
        if type(self.max_findings) is not int or self.max_findings < 1:
            raise ValueError("max_findings must be a positive integer")
        if not callable(self.detect):
            raise TypeError("detect must be callable")

    def dependencies(self) -> tuple[object, ...]:
        return (self.event_type,)

    def manifest(self) -> dict[str, Any]:
        return {
            "event": str(self.event_type.ref),
            "implementation_id": self.implementation_id,
            "timeout_seconds": self.timeout_seconds,
            "max_findings": self.max_findings,
        }

    async def run(self, event: DomainEvent, *, context: ExecutionContext) -> WatcherResult:
        if event.tenant_id != context.tenant_id:
            fail("SCOPE_MISMATCH", "Watcher event belongs to another tenant.")
        if (
            event.type_reference != str(self.event_type.ref)
            or event.type_digest != self.event_type.digest
        ):
            fail("EVENT_SCHEMA_CHANGED", "Watcher event contract differs.")
        self.event_type.payload.validate_content(event.payload)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                result = await resolve(self.detect(event, context))
            if not isinstance(result, WatcherResult) or len(result.findings) > self.max_findings:
                raise ValueError("invalid or oversized watcher result")
        except Exception:
            fail("WATCHER_FAILED", "Watcher failed, exceeded bounds, or timed out.")
        for finding in result.findings:
            finding.target.require_scope(context)
        return result
