"""Cancellation-safe bounded concurrency and exact per-session execution lanes."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .errors import HarnessError


async def drain_thread_call(
    call: Callable[[], Any],
) -> tuple[Any, bool, BaseException | None]:
    """Drain a thread-backed call and report cancellation without losing its outcome."""

    def captured() -> tuple[bool, Any]:
        try:
            return True, call()
        except BaseException as exc:
            return False, exc

    task = asyncio.create_task(asyncio.to_thread(captured))
    cancelled = False
    while True:
        try:
            succeeded, value = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
    if succeeded:
        return value, cancelled, None
    return None, cancelled, value


class RuntimeClosePolicy(StrEnum):
    """Ownership decision for live lifecycle runs during harness shutdown."""

    DRAIN = "drain"
    DETACH = "detach"
    CANCEL = "cancel"

    @classmethod
    def parse(cls, value: str | RuntimeClosePolicy) -> RuntimeClosePolicy:
        try:
            return cls(value)
        except ValueError as exc:
            choices = ", ".join(item.value for item in cls)
            raise ValueError(f"runtime close policy must be one of: {choices}") from exc


class RuntimeAdmissionOverloadedError(HarnessError):
    """Raised when bounded lifecycle admission cannot accept more work."""

    def __init__(
        self,
        *,
        reason: str,
        retry_after_seconds: float,
        max_waiting: int,
        queue_limit: int,
    ) -> None:
        super().__init__(
            code="RUNTIME_ADMISSION_OVERLOADED",
            category="runtime_admission",
            message="Lifecycle admission is saturated; retry after backoff.",
            retryable=True,
            details={
                "reason": reason,
                "retry_after_seconds": retry_after_seconds,
                "max_waiting": max_waiting,
                "queue_limit": queue_limit,
            },
        )


@dataclass
class _Lane:
    lock: asyncio.Lock
    references: int = 0


@dataclass
class _TenantWaiter:
    tenant_key: str
    lane_key: str
    future: asyncio.Future[None]
    state: str = "queued"


class AsyncSessionLanes:
    """Bound and fairly admit work while serializing each exact session.

    Ready sessions are admitted in tenant round-robin order. The bound and fairness
    contract is process-local; store-issued leases remain the cross-process ownership
    authority.
    """

    def __init__(
        self,
        *,
        max_concurrency: int = 16,
        max_waiting: int = 1024,
        max_waiting_per_tenant: int | None = None,
        max_waiting_per_session: int | None = None,
        admission_timeout_seconds: float | None = 30.0,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if max_waiting <= 0:
            raise ValueError("max_waiting must be positive")
        if max_waiting_per_tenant is None:
            max_waiting_per_tenant = min(256, max_waiting)
        if max_waiting_per_session is None:
            max_waiting_per_session = min(32, max_waiting_per_tenant)
        if not 0 < max_waiting_per_tenant <= max_waiting:
            raise ValueError("max_waiting_per_tenant must be between 1 and max_waiting")
        if not 0 < max_waiting_per_session <= max_waiting_per_tenant:
            raise ValueError(
                "max_waiting_per_session must be between 1 and max_waiting_per_tenant"
            )
        if admission_timeout_seconds is not None and admission_timeout_seconds <= 0:
            raise ValueError("admission_timeout_seconds must be positive or None")
        self.max_concurrency = max_concurrency
        self.max_waiting = max_waiting
        self.max_waiting_per_tenant = max_waiting_per_tenant
        self.max_waiting_per_session = max_waiting_per_session
        self.admission_timeout_seconds = admission_timeout_seconds
        self._registry_lock = asyncio.Lock()
        self._lanes: dict[str, _Lane] = {}
        self._capacity_lock = asyncio.Lock()
        self._tenant_queues: dict[str, deque[_TenantWaiter]] = {}
        self._tenant_order: deque[str] = deque()
        self._active_by_tenant: dict[str, int] = {}
        self._waiting_by_tenant: dict[str, int] = {}
        self._waiting_by_lane: dict[str, int] = {}
        self._active = 0
        self._waiting = 0
        self._peak_active = 0
        self._peak_waiting = 0
        self._admitted = 0
        self._rejected = 0
        self._cancelled = 0
        self._timed_out = 0
        self._fair_queue_admissions = 0

    @staticmethod
    def key(
        *,
        tenant_id: str | None,
        session_id: str | None,
        run_id: str,
    ) -> str:
        """Build a collision-safe internal lane key without trusting delimiters."""
        if session_id is None:
            return f"run:{len(run_id)}:{run_id}"
        tenant = tenant_id or ""
        return f"session:{len(tenant)}:{tenant}:{len(session_id)}:{session_id}"

    async def _reference(self, lane_key: str) -> _Lane:
        async with self._registry_lock:
            lane = self._lanes.get(lane_key)
            if lane is None:
                lane = _Lane(lock=asyncio.Lock())
                self._lanes[lane_key] = lane
            lane.references += 1
            return lane

    async def _dereference(self, lane_key: str, lane: _Lane) -> None:
        async with self._registry_lock:
            lane.references -= 1
            if lane.references == 0 and not lane.lock.locked():
                self._lanes.pop(lane_key, None)

    @staticmethod
    def _tenant_key(tenant_id: str | None) -> str:
        tenant = tenant_id or ""
        return f"tenant:{len(tenant)}:{tenant}"

    def _overloaded(self, *, reason: str, queue_limit: int) -> RuntimeAdmissionOverloadedError:
        return RuntimeAdmissionOverloadedError(
            reason=reason,
            retry_after_seconds=self.admission_timeout_seconds or 1.0,
            max_waiting=self.max_waiting,
            queue_limit=queue_limit,
        )

    async def _reserve_waiter(self, *, tenant_key: str, lane_key: str) -> None:
        async with self._capacity_lock:
            rejection: tuple[str, int] | None = None
            if self._waiting_by_lane.get(lane_key, 0) >= self.max_waiting_per_session:
                rejection = ("session_queue_full", self.max_waiting_per_session)
            elif (
                self._waiting_by_tenant.get(tenant_key, 0)
                >= self.max_waiting_per_tenant
            ):
                rejection = ("tenant_queue_full", self.max_waiting_per_tenant)
            elif self._waiting >= self.max_waiting:
                rejection = ("queue_full", self.max_waiting)
            if rejection is not None:
                self._rejected += 1
                reason, queue_limit = rejection
                raise self._overloaded(reason=reason, queue_limit=queue_limit)
            self._waiting += 1
            self._waiting_by_tenant[tenant_key] = (
                self._waiting_by_tenant.get(tenant_key, 0) + 1
            )
            self._waiting_by_lane[lane_key] = self._waiting_by_lane.get(lane_key, 0) + 1
            self._peak_waiting = max(self._peak_waiting, self._waiting)

    def _consume_waiting_locked(self, *, tenant_key: str, lane_key: str) -> None:
        self._waiting -= 1
        tenant_remaining = self._waiting_by_tenant[tenant_key] - 1
        lane_remaining = self._waiting_by_lane[lane_key] - 1
        if tenant_remaining:
            self._waiting_by_tenant[tenant_key] = tenant_remaining
        else:
            self._waiting_by_tenant.pop(tenant_key)
        if lane_remaining:
            self._waiting_by_lane[lane_key] = lane_remaining
        else:
            self._waiting_by_lane.pop(lane_key)
        if self._waiting < 0:  # pragma: no cover - internal accounting invariant
            raise AssertionError("runtime admission waiting count became negative")

    async def _drop_reservation(
        self,
        *,
        tenant_key: str,
        lane_key: str,
        reason: str,
    ) -> None:
        async with self._capacity_lock:
            self._consume_waiting_locked(tenant_key=tenant_key, lane_key=lane_key)
            if reason == "timed_out":
                self._timed_out += 1
            elif reason == "cancelled":
                self._cancelled += 1

    def _activate_locked(self, tenant_key: str) -> None:
        self._active += 1
        self._active_by_tenant[tenant_key] = self._active_by_tenant.get(tenant_key, 0) + 1
        self._peak_active = max(self._peak_active, self._active)
        self._admitted += 1

    def _deactivate_locked(self, tenant_key: str) -> None:
        self._active -= 1
        remaining = self._active_by_tenant[tenant_key] - 1
        if remaining:
            self._active_by_tenant[tenant_key] = remaining
        else:
            self._active_by_tenant.pop(tenant_key)

    def _grant_waiters_locked(self) -> None:
        while self._active < self.max_concurrency and self._tenant_order:
            tenant_key = self._tenant_order.popleft()
            queue = self._tenant_queues[tenant_key]
            waiter = queue.popleft()
            if queue:
                self._tenant_order.append(tenant_key)
            else:
                self._tenant_queues.pop(tenant_key)
            if waiter.state != "queued":  # pragma: no cover - removed while locked
                continue
            waiter.state = "granted"
            self._consume_waiting_locked(
                tenant_key=waiter.tenant_key,
                lane_key=waiter.lane_key,
            )
            self._activate_locked(tenant_key)
            self._fair_queue_admissions += 1
            waiter.future.set_result(None)

    def _remove_waiter_locked(self, waiter: _TenantWaiter) -> None:
        queue = self._tenant_queues.get(waiter.tenant_key)
        if queue is None:  # pragma: no cover - state/queue invariant
            raise AssertionError("queued runtime admission waiter is missing")
        queue.remove(waiter)
        if not queue:
            self._tenant_queues.pop(waiter.tenant_key)
            self._tenant_order.remove(waiter.tenant_key)

    async def _abandon_waiter(self, waiter: _TenantWaiter, *, reason: str) -> None:
        async with self._capacity_lock:
            if waiter.state == "queued":
                self._remove_waiter_locked(waiter)
                waiter.state = reason
                self._consume_waiting_locked(
                    tenant_key=waiter.tenant_key,
                    lane_key=waiter.lane_key,
                )
            elif waiter.state == "granted":
                waiter.state = reason
                self._deactivate_locked(waiter.tenant_key)
                self._grant_waiters_locked()
            else:  # pragma: no cover - one task abandons one waiter once
                return
            if reason == "timed_out":
                self._timed_out += 1
            elif reason == "cancelled":
                self._cancelled += 1

    async def _acquire_capacity(
        self,
        tenant_key: str,
        lane_key: str,
        *,
        timeout_seconds: float | None,
    ) -> None:
        if timeout_seconds is not None and timeout_seconds <= 0:
            await self._drop_reservation(
                tenant_key=tenant_key,
                lane_key=lane_key,
                reason="timed_out",
            )
            raise self._overloaded(
                reason="admission_timeout",
                queue_limit=self.max_waiting,
            )
        waiter: _TenantWaiter | None = None
        async with self._capacity_lock:
            if self._active < self.max_concurrency and not self._tenant_order:
                self._consume_waiting_locked(tenant_key=tenant_key, lane_key=lane_key)
                self._activate_locked(tenant_key)
                return
            waiter = _TenantWaiter(
                tenant_key=tenant_key,
                lane_key=lane_key,
                future=asyncio.get_running_loop().create_future(),
            )
            queue = self._tenant_queues.get(tenant_key)
            if queue is None:
                queue = deque()
                self._tenant_queues[tenant_key] = queue
                self._tenant_order.append(tenant_key)
            queue.append(waiter)
            self._grant_waiters_locked()
        try:
            await asyncio.wait_for(asyncio.shield(waiter.future), timeout=timeout_seconds)
        except TimeoutError:
            await self._abandon_waiter(waiter, reason="timed_out")
            raise self._overloaded(
                reason="admission_timeout",
                queue_limit=self.max_waiting,
            ) from None
        except asyncio.CancelledError:
            await self._abandon_waiter(waiter, reason="cancelled")
            raise

    async def _release_capacity(self, tenant_key: str) -> None:
        async with self._capacity_lock:
            self._deactivate_locked(tenant_key)
            self._grant_waiters_locked()

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    @asynccontextmanager
    async def hold(
        self,
        *,
        tenant_id: str | None,
        session_id: str | None,
        run_id: str,
    ) -> AsyncIterator[None]:
        """Hold global capacity and the exact session lane for one execution."""
        lane_key = self.key(
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=run_id,
        )
        tenant_key = self._tenant_key(tenant_id)
        await self._reserve_waiter(tenant_key=tenant_key, lane_key=lane_key)
        reservation_held = True
        lane: _Lane | None = None
        deadline = (
            None
            if self.admission_timeout_seconds is None
            else time.monotonic() + self.admission_timeout_seconds
        )
        capacity_acquired = False
        lane_acquired = False
        try:
            lane = await self._reference(lane_key)
            try:
                await asyncio.wait_for(
                    lane.lock.acquire(),
                    timeout=self._remaining(deadline),
                )
            except TimeoutError:
                await self._drop_reservation(
                    tenant_key=tenant_key,
                    lane_key=lane_key,
                    reason="timed_out",
                )
                reservation_held = False
                raise self._overloaded(
                    reason="admission_timeout",
                    queue_limit=self.max_waiting,
                ) from None
            except asyncio.CancelledError:
                await self._drop_reservation(
                    tenant_key=tenant_key,
                    lane_key=lane_key,
                    reason="cancelled",
                )
                reservation_held = False
                raise
            lane_acquired = True
            # Capacity acquisition owns and either consumes or releases the reservation.
            reservation_held = False
            await self._acquire_capacity(
                tenant_key,
                lane_key,
                timeout_seconds=self._remaining(deadline),
            )
            capacity_acquired = True
            yield
        finally:
            if reservation_held:
                await self._drop_reservation(
                    tenant_key=tenant_key,
                    lane_key=lane_key,
                    reason="cancelled",
                )
            if capacity_acquired:
                await self._release_capacity(tenant_key)
            if lane_acquired and lane is not None:
                lane.lock.release()
            if lane is not None:
                await self._dereference(lane_key, lane)

    @property
    def active(self) -> int:
        return self._active

    @property
    def peak_active(self) -> int:
        return self._peak_active

    @property
    def lane_count(self) -> int:
        return len(self._lanes)

    @property
    def admission_stats(self) -> dict[str, int | float | None]:
        """Return a content-free snapshot suitable for metrics and health output."""
        return {
            "max_concurrency": self.max_concurrency,
            "max_waiting": self.max_waiting,
            "max_waiting_per_tenant": self.max_waiting_per_tenant,
            "max_waiting_per_session": self.max_waiting_per_session,
            "admission_timeout_seconds": self.admission_timeout_seconds,
            "active": self._active,
            "waiting": self._waiting,
            "active_tenants": len(self._active_by_tenant),
            "waiting_tenants": len(self._waiting_by_tenant),
            "waiting_sessions": len(self._waiting_by_lane),
            "queued_tenants": len(self._tenant_queues),
            "peak_active": self._peak_active,
            "peak_waiting": self._peak_waiting,
            "admitted": self._admitted,
            "rejected": self._rejected,
            "cancelled": self._cancelled,
            "timed_out": self._timed_out,
            "fair_queue_admissions": self._fair_queue_admissions,
            "lane_count": len(self._lanes),
        }


__all__ = [
    "AsyncSessionLanes",
    "RuntimeAdmissionOverloadedError",
    "RuntimeClosePolicy",
    "drain_thread_call",
]
