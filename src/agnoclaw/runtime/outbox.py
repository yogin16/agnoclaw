"""Leased at-least-once delivery for authoritative runtime events."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .store import MAX_OUTBOX_DEFER_SECONDS, OutboxItem, RuntimeEvent, RuntimeStore


@runtime_checkable
class RuntimeEventBatchExporter(Protocol):
    """Async destination; consumers deduplicate with ``RuntimeEvent.event_id``."""

    async def export(self, events: tuple[RuntimeEvent, ...]) -> None: ...


@dataclass(frozen=True)
class RuntimeOutboxConfig:
    owner: str
    batch_size: int = 50
    lease_seconds: int = 60
    delivery_timeout_seconds: float = 30
    retry_base_seconds: float = 1
    retry_max_seconds: float = 300
    max_attempts: int = 20
    idle_poll_seconds: float = 1

    def __post_init__(self) -> None:
        if not isinstance(self.owner, str) or not self.owner.strip() or len(self.owner) > 256:
            raise ValueError("owner must be a non-empty string of at most 256 characters")
        if not 1 <= self.batch_size <= 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        if not 1 <= self.lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        if not 0 < self.delivery_timeout_seconds < self.lease_seconds:
            raise ValueError("delivery_timeout_seconds must be positive and below lease_seconds")
        if not 0 <= self.retry_base_seconds <= MAX_OUTBOX_DEFER_SECONDS:
            raise ValueError("retry_base_seconds is outside the supported range")
        if not self.retry_base_seconds <= self.retry_max_seconds <= MAX_OUTBOX_DEFER_SECONDS:
            raise ValueError("retry_max_seconds must be between retry_base_seconds and 86400")
        if not 1 <= self.max_attempts <= 10_000:
            raise ValueError("max_attempts must be between 1 and 10000")
        if not 0 < self.idle_poll_seconds <= 60:
            raise ValueError("idle_poll_seconds must be between 0 and 60")

    def retry_delay(self, attempts: int) -> float:
        exponent = min(max(attempts - 1, 0), 30)
        return min(self.retry_max_seconds, self.retry_base_seconds * (2**exponent))


@dataclass(frozen=True)
class RuntimeOutboxBatchResult:
    leased: int
    delivered: int
    deferred: int
    dead_lettered: int = 0
    first_outbox_id: int | None = None
    last_outbox_id: int | None = None
    failure_code: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.leased > 0 and self.delivered == self.leased


class RuntimeOutboxWorker:
    """Small restart-safe pump over the RuntimeStore transactional outbox."""

    def __init__(
        self,
        *,
        store: RuntimeStore,
        exporter: RuntimeEventBatchExporter,
        config: RuntimeOutboxConfig,
    ) -> None:
        if not isinstance(exporter, RuntimeEventBatchExporter):
            raise TypeError("exporter must implement async export(events)")
        self._store = store
        self._exporter = exporter
        self.config = config

    async def run_once(self) -> RuntimeOutboxBatchResult:
        items = await asyncio.to_thread(
            self._store.lease_outbox,
            owner=self.config.owner,
            limit=self.config.batch_size,
            lease_seconds=self.config.lease_seconds,
        )
        if not items:
            return RuntimeOutboxBatchResult(leased=0, delivered=0, deferred=0)
        first_id = items[0].outbox_id
        last_id = items[-1].outbox_id
        active_items = items
        already_deferred = 0
        if any(item.attempts >= self.config.max_attempts for item in items) and len(items) > 1:
            active_items = items[:1]
            await self._defer(items[1:], delay_seconds=0)
            already_deferred = len(items) - 1
        try:
            async with asyncio.timeout(self.config.delivery_timeout_seconds):
                await self._exporter.export(tuple(item.event for item in active_items))
        except TimeoutError:
            return await self._handle_failure(
                items=items,
                active_items=active_items,
                already_deferred=already_deferred,
                reason_code="export_timeout",
                failure_code="RUNTIME_OUTBOX_EXPORT_TIMEOUT",
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._defer(active_items, delay_seconds=0))
            raise
        except Exception:
            return await self._handle_failure(
                items=items,
                active_items=active_items,
                already_deferred=already_deferred,
                reason_code="export_failed",
                failure_code="RUNTIME_OUTBOX_EXPORT_FAILED",
            )
        for item in active_items:
            await asyncio.to_thread(
                self._store.acknowledge_outbox,
                outbox_id=item.outbox_id,
                lease_token=self._lease_token(item),
            )
        return RuntimeOutboxBatchResult(
            leased=len(items),
            delivered=len(active_items),
            deferred=already_deferred,
            first_outbox_id=first_id,
            last_outbox_id=last_id,
        )

    async def run(self, *, stop: asyncio.Event) -> None:
        """Export until ``stop`` is set; cancellation safely releases live leases."""
        while not stop.is_set():
            result = await self.run_once()
            if result.leased:
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.config.idle_poll_seconds)
            except TimeoutError:
                pass

    async def _defer(
        self,
        items: list[OutboxItem],
        *,
        delay_seconds: float | None = None,
    ) -> None:
        retry_delay = (
            self.config.retry_delay(max(item.attempts for item in items))
            if delay_seconds is None
            else delay_seconds
        )
        for item in items:
            await asyncio.to_thread(
                self._store.defer_outbox,
                outbox_id=item.outbox_id,
                lease_token=self._lease_token(item),
                delay_seconds=retry_delay,
            )

    async def _handle_failure(
        self,
        *,
        items: list[OutboxItem],
        active_items: list[OutboxItem],
        already_deferred: int,
        reason_code: str,
        failure_code: str,
    ) -> RuntimeOutboxBatchResult:
        should_quarantine = active_items[0].attempts >= self.config.max_attempts
        if should_quarantine:
            for item in active_items:
                await asyncio.to_thread(
                    self._store.dead_letter_outbox,
                    outbox_id=item.outbox_id,
                    lease_token=self._lease_token(item),
                    reason_code=reason_code,
                )
            deferred = already_deferred
            dead_lettered = len(active_items)
            result_code = "RUNTIME_OUTBOX_EXPORT_DEAD_LETTERED"
        else:
            await self._defer(active_items)
            deferred = already_deferred + len(active_items)
            dead_lettered = 0
            result_code = failure_code
        return RuntimeOutboxBatchResult(
            leased=len(items),
            delivered=0,
            deferred=deferred,
            dead_lettered=dead_lettered,
            first_outbox_id=items[0].outbox_id,
            last_outbox_id=items[-1].outbox_id,
            failure_code=result_code,
        )

    @staticmethod
    def _lease_token(item: OutboxItem) -> str:
        if item.lease_token is None:
            raise RuntimeError("leased outbox item is missing its lease token")
        return item.lease_token


__all__ = [
    "RuntimeEventBatchExporter",
    "RuntimeOutboxBatchResult",
    "RuntimeOutboxConfig",
    "RuntimeOutboxWorker",
]
