"""Durable, owner-scoped maintenance for ambiguous learning effects.

The worker only observes external state and commits verified reconciliation evidence.
It never replays a promotion or rollback.  A database-authoritative lease, monotonic
fence, and durable scan cursor make competing processes and restarts safe.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .learning_candidates import (
    LearningOwner,
    LearningReconciliationWorkerLease,
    LearningReconciliationWorkerLeaseError,
    ReconciliationCursor,
)
from .learning_reconciliation import (
    LearningReconciliationCoordinator,
    ReconciliationBatchOutcome,
)
from .runtime.errors import HarnessError


@runtime_checkable
class LearningReconciliationLeaseStore(Protocol):
    """Optional ledger capability required only by the durable worker."""

    def claim_reconciliation_worker(
        self,
        *,
        owner: LearningOwner,
        worker_id: str,
        lease_seconds: int,
    ) -> LearningReconciliationWorkerLease | None: ...

    def checkpoint_reconciliation_worker(
        self,
        lease: LearningReconciliationWorkerLease,
        *,
        cursor: ReconciliationCursor | None,
        lease_seconds: int,
    ) -> LearningReconciliationWorkerLease: ...

    def release_reconciliation_worker(
        self,
        lease: LearningReconciliationWorkerLease,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class LearningReconciliationWorkerConfig:
    """Bounded maintenance-loop configuration.

    One-second leases are supported for deterministic failure tests. Production
    deployments should normally use the 30-second default.
    """

    worker_id: str
    lease_seconds: int = 30
    poll_interval_seconds: float = 5.0
    page_limit: int = 100
    max_concurrency: int = 4
    heartbeat_interval_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.worker_id, str) or not self.worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        if len(self.worker_id) > 512:
            raise ValueError("worker_id cannot exceed 512 characters")
        if (
            isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, int)
            or not 1 <= self.lease_seconds <= 3600
        ):
            raise ValueError("lease_seconds must be an integer between 1 and 3600")
        if (
            isinstance(self.poll_interval_seconds, bool)
            or not isinstance(self.poll_interval_seconds, (int, float))
            or not 0.01 <= self.poll_interval_seconds <= 300
        ):
            raise ValueError("poll_interval_seconds must be between 0.01 and 300")
        if (
            isinstance(self.page_limit, bool)
            or not isinstance(self.page_limit, int)
            or not 1 <= self.page_limit <= 1000
        ):
            raise ValueError("page_limit must be an integer between 1 and 1000")
        if (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or not 1 <= self.max_concurrency <= 32
        ):
            raise ValueError("max_concurrency must be an integer between 1 and 32")
        if self.heartbeat_interval_seconds is not None and (
            isinstance(self.heartbeat_interval_seconds, bool)
            or not isinstance(self.heartbeat_interval_seconds, (int, float))
            or not 0.01 <= self.heartbeat_interval_seconds < self.lease_seconds
        ):
            raise ValueError(
                "heartbeat_interval_seconds must be at least 0.01 and shorter than the lease"
            )

    @property
    def heartbeat_seconds(self) -> float:
        if self.heartbeat_interval_seconds is not None:
            return self.heartbeat_interval_seconds
        return max(0.05, self.lease_seconds / 3)


@dataclass(frozen=True, slots=True)
class LearningReconciliationWorkerStats:
    """Content-free maintenance counters safe for operator output."""

    owner_digest: str
    worker_id: str
    claims: int = 0
    pages: int = 0
    items: int = 0
    reconciled: int = 0
    deferred: int = 0
    stale: int = 0
    rejected: int = 0
    failed: int = 0
    lease_losses: int = 0
    store_failures: int = 0

    def to_dict(self) -> dict[str, str | int]:
        return {
            "owner_digest": self.owner_digest,
            "worker_id": self.worker_id,
            "claims": self.claims,
            "pages": self.pages,
            "items": self.items,
            "reconciled": self.reconciled,
            "deferred": self.deferred,
            "stale": self.stale,
            "rejected": self.rejected,
            "failed": self.failed,
            "lease_losses": self.lease_losses,
            "store_failures": self.store_failures,
        }


@dataclass(slots=True)
class _WorkerCounters:
    claims: int = 0
    pages: int = 0
    items: int = 0
    reconciled: int = 0
    deferred: int = 0
    stale: int = 0
    rejected: int = 0
    failed: int = 0
    lease_losses: int = 0
    store_failures: int = 0

    def add(self, batch: ReconciliationBatchOutcome) -> None:
        self.pages += 1
        self.items += len(batch.items)
        for item in batch.items:
            attribute = item.status.value
            setattr(self, attribute, getattr(self, attribute) + 1)


class LearningReconciliationWorker:
    """Run bounded reconciliation sweeps under an exact-owner durable lease."""

    def __init__(
        self,
        coordinator: LearningReconciliationCoordinator,
        *,
        owner: LearningOwner,
        config: LearningReconciliationWorkerConfig,
    ) -> None:
        if not isinstance(coordinator, LearningReconciliationCoordinator):
            raise TypeError("coordinator must be a LearningReconciliationCoordinator")
        if not isinstance(owner, LearningOwner):
            raise TypeError("owner must be a LearningOwner")
        if not isinstance(config, LearningReconciliationWorkerConfig):
            raise TypeError("config must be a LearningReconciliationWorkerConfig")
        store = coordinator.gateway.ledger
        if not isinstance(store, LearningReconciliationLeaseStore):
            raise TypeError(
                "the learning ledger must implement LearningReconciliationLeaseStore"
            )
        self.coordinator = coordinator
        self.owner = owner
        self.config = config
        self.store = store

    def _stats(self, counters: _WorkerCounters) -> LearningReconciliationWorkerStats:
        return LearningReconciliationWorkerStats(
            owner_digest=self.owner.digest,
            worker_id=self.config.worker_id,
            claims=counters.claims,
            pages=counters.pages,
            items=counters.items,
            reconciled=counters.reconciled,
            deferred=counters.deferred,
            stale=counters.stale,
            rejected=counters.rejected,
            failed=counters.failed,
            lease_losses=counters.lease_losses,
            store_failures=counters.store_failures,
        )

    async def _claim(self) -> LearningReconciliationWorkerLease | None:
        return await asyncio.to_thread(
            self.store.claim_reconciliation_worker,
            owner=self.owner,
            worker_id=self.config.worker_id,
            lease_seconds=self.config.lease_seconds,
        )

    async def _checkpoint(
        self,
        lease: LearningReconciliationWorkerLease,
        cursor: ReconciliationCursor | None,
    ) -> LearningReconciliationWorkerLease:
        return await asyncio.to_thread(
            self.store.checkpoint_reconciliation_worker,
            lease,
            cursor=cursor,
            lease_seconds=self.config.lease_seconds,
        )

    async def _release(self, lease: LearningReconciliationWorkerLease) -> None:
        await asyncio.to_thread(self.store.release_reconciliation_worker, lease)

    @staticmethod
    def _retryable_store_failure(error: BaseException) -> bool:
        return isinstance(error, HarnessError) and error.retryable

    async def _release_for_continuous_run(
        self,
        lease: LearningReconciliationWorkerLease,
        counters: _WorkerCounters,
    ) -> None:
        try:
            await self._release(lease)
        except HarnessError as exc:
            if not exc.retryable:
                raise
            counters.store_failures += 1

    async def _run_page(
        self,
        lease: LearningReconciliationWorkerLease,
    ) -> tuple[LearningReconciliationWorkerLease, ReconciliationBatchOutcome]:
        task = asyncio.create_task(
            self.coordinator.run_page(
                owner=self.owner,
                limit=self.config.page_limit,
                cursor=lease.cursor,
            )
        )
        try:
            while True:
                done, _ = await asyncio.wait(
                    {task},
                    timeout=self.config.heartbeat_seconds,
                )
                if task in done:
                    return lease, task.result()
                # Renew without advancing: observation may be slow, and only the
                # completed page's cursor is safe to durably checkpoint.
                lease = await self._checkpoint(lease, lease.cursor)
        except BaseException:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise

    async def _wait_or_stop(self, stop: asyncio.Event) -> bool:
        if stop.is_set():
            return True
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=self.config.poll_interval_seconds,
            )
        except TimeoutError:
            return stop.is_set()
        return True

    async def run_once(self) -> LearningReconciliationWorkerStats:
        """Attempt one leased page; return zero claims when another worker owns it."""
        counters = _WorkerCounters()
        lease = await self._claim()
        if lease is None:
            return self._stats(counters)
        counters.claims = 1
        current_lease = lease
        try:
            try:
                current_lease, batch = await self._run_page(current_lease)
                counters.add(batch)
                current_lease = await self._checkpoint(
                    current_lease,
                    batch.next_cursor,
                )
            except LearningReconciliationWorkerLeaseError:
                counters.lease_losses += 1
        finally:
            await self._release(current_lease)
        return self._stats(counters)

    async def run(self, stop: asyncio.Event) -> LearningReconciliationWorkerStats:
        """Run until ``stop`` is set, recovering normally after lease loss."""
        if not isinstance(stop, asyncio.Event):
            raise TypeError("stop must be an asyncio.Event")
        counters = _WorkerCounters()
        while not stop.is_set():
            try:
                lease = await self._claim()
            except HarnessError as exc:
                if not exc.retryable:
                    raise
                counters.store_failures += 1
                await self._wait_or_stop(stop)
                continue
            if lease is None:
                await self._wait_or_stop(stop)
                continue
            counters.claims += 1
            current_lease = lease
            try:
                while not stop.is_set():
                    try:
                        current_lease, batch = await self._run_page(current_lease)
                        counters.add(batch)
                        current_lease = await self._checkpoint(
                            current_lease,
                            batch.next_cursor,
                        )
                    except LearningReconciliationWorkerLeaseError:
                        counters.lease_losses += 1
                        break
                    except HarnessError as exc:
                        if not exc.retryable:
                            raise
                        counters.store_failures += 1
                        break
                    if batch.next_cursor is None:
                        break
            finally:
                await self._release_for_continuous_run(current_lease, counters)
            await self._wait_or_stop(stop)
        return self._stats(counters)


__all__ = [
    "LearningReconciliationLeaseStore",
    "LearningReconciliationWorker",
    "LearningReconciliationWorkerConfig",
    "LearningReconciliationWorkerStats",
]
