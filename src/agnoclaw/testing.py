"""Deterministic fault and effect controls for host-side harness tests.

This module is intentionally opt-in: importing :mod:`agnoclaw` does not import test
support or add runtime dependencies. The controls drive real public stores and
``OperationGateway`` calls; they are not alternate in-memory implementations.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections import Counter
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TypeVar

T = TypeVar("T")
EffectDispatch = Callable[[], T | Awaitable[T]]


class InjectedRuntimeCrash(RuntimeError):
    """Content-free exception raised at one exact persistence checkpoint."""

    def __init__(self, *, stage: str, occurrence: int):
        self.stage = stage
        self.occurrence = occurrence
        super().__init__(f"injected runtime crash at {stage} occurrence {occurrence}")


class StoreFaultScript:
    """Thread-safe one-shot fault injector for RuntimeStore checkpoint tests."""

    def __init__(self, stage: str, *, occurrence: int = 1):
        if not isinstance(stage, str) or not stage.strip():
            raise ValueError("stage must be a non-empty string")
        if occurrence < 1:
            raise ValueError("occurrence must be positive")
        self.stage = stage
        self.occurrence = occurrence
        self._counts: Counter[str] = Counter()
        self._triggered = False
        self._lock = threading.Lock()

    def __call__(self, stage: str) -> None:
        with self._lock:
            self._counts[stage] += 1
            count = self._counts[stage]
            if stage != self.stage or count != self.occurrence or self._triggered:
                return
            self._triggered = True
        raise InjectedRuntimeCrash(stage=stage, occurrence=count)

    @property
    def triggered(self) -> bool:
        with self._lock:
            return self._triggered

    def hits(self, stage: str | None = None) -> int:
        with self._lock:
            if stage is None:
                return sum(self._counts.values())
            return self._counts[stage]

    def assert_triggered(self) -> None:
        if not self.triggered:
            raise AssertionError(
                f"fault stage {self.stage!r} occurrence {self.occurrence} was not reached"
            )


class StoreBarrierTimeoutError(TimeoutError):
    """Content-free failure raised when a deterministic store barrier is not released."""

    def __init__(self, *, stage: str, occurrence: int):
        self.stage = stage
        self.occurrence = occurrence
        super().__init__(f"store barrier timed out at {stage} occurrence {occurrence}")


class StoreBarrierScript:
    """Pause one exact persistence checkpoint until its test explicitly releases it.

    Runtime-store callbacks execute on database worker threads, so this controller uses
    only ``threading`` primitives. Its finite timeout turns a malformed race script into
    a prompt test failure instead of a hung suite.
    """

    def __init__(
        self,
        stage: str,
        *,
        occurrence: int = 1,
        timeout: float = 5.0,
    ) -> None:
        if not isinstance(stage, str) or not stage.strip():
            raise ValueError("stage must be a non-empty string")
        if occurrence < 1:
            raise ValueError("occurrence must be positive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.stage = stage
        self.occurrence = occurrence
        self.timeout = timeout
        self._counts: Counter[str] = Counter()
        self._reached = False
        self._lock = threading.Lock()
        self._arrival = threading.Event()
        self._release = threading.Event()

    def __call__(self, stage: str) -> None:
        with self._lock:
            self._counts[stage] += 1
            count = self._counts[stage]
            if stage != self.stage or count != self.occurrence or self._reached:
                return
            self._reached = True
        self._arrival.set()
        if not self._release.wait(self.timeout):
            raise StoreBarrierTimeoutError(stage=stage, occurrence=count)

    @property
    def reached(self) -> bool:
        with self._lock:
            return self._reached

    def hits(self, stage: str | None = None) -> int:
        with self._lock:
            if stage is None:
                return sum(self._counts.values())
            return self._counts[stage]

    def wait(self, *, timeout: float = 2.0) -> None:
        """Wait synchronously for the selected store checkpoint to enter the barrier."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if not self._arrival.wait(timeout):
            raise AssertionError(
                f"store barrier {self.stage!r} occurrence {self.occurrence} was not reached"
            )

    def release(self) -> None:
        """Release the selected database transaction exactly once."""
        self._release.set()

    def assert_reached(self) -> None:
        if not self.reached:
            raise AssertionError(
                f"store barrier {self.stage!r} occurrence {self.occurrence} was not reached"
            )


class EffectBoundary(StrEnum):
    """Externally meaningful boundaries exposed by ``OperationGateway`` composition."""

    PRE_DISPATCH = "pre_dispatch"
    BEFORE_EFFECT = "before_effect"
    AFTER_EFFECT = "after_effect"


class DeterministicEffectDriver:
    """Manually advance one operation through its external-effect boundaries.

    Pass :meth:`pre_dispatch` to ``OperationGateway.execute(pre_dispatch=...)`` and
    wrap the external callable with :meth:`wrap_effect`. Tests can then observe the
    authoritative store and choose whether completion, cancellation, lease loss, or a
    simulated process crash wins at each boundary.
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._arrivals: list[EffectBoundary] = []
        self._released = 0
        self._owner_loop: asyncio.AbstractEventLoop | None = None

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._owner_loop is None:
            self._owner_loop = loop
        elif self._owner_loop is not loop:
            raise RuntimeError("a deterministic effect driver belongs to one event loop")

    async def _checkpoint(self, boundary: EffectBoundary) -> None:
        self._bind_loop()
        async with self._condition:
            index = len(self._arrivals)
            self._arrivals.append(boundary)
            self._condition.notify_all()
            await self._condition.wait_for(lambda: self._released > index)

    async def pre_dispatch(self) -> None:
        """Pause at the gateway's final known-no-effect checkpoint."""
        await self._checkpoint(EffectBoundary.PRE_DISPATCH)

    def wrap_effect(self, dispatch: EffectDispatch[T]) -> Callable[[], Awaitable[T]]:
        """Wrap one sync or async external effect with before/after manual gates."""
        if not callable(dispatch):
            raise TypeError("dispatch must be callable")

        async def driven() -> T:
            await self._checkpoint(EffectBoundary.BEFORE_EFFECT)
            value = dispatch()
            resolved = await value if inspect.isawaitable(value) else value
            await self._checkpoint(EffectBoundary.AFTER_EFFECT)
            return resolved

        return driven

    async def wait_for(
        self,
        boundary: EffectBoundary,
        *,
        occurrence: int = 1,
        timeout: float = 2.0,
    ) -> int:
        """Wait until a boundary occurrence arrives and return its script index."""
        self._bind_loop()
        boundary = EffectBoundary(boundary)
        if occurrence < 1:
            raise ValueError("occurrence must be positive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        def position() -> int | None:
            matches = [
                index for index, item in enumerate(self._arrivals) if item is boundary
            ]
            return matches[occurrence - 1] if len(matches) >= occurrence else None

        async with asyncio.timeout(timeout):
            async with self._condition:
                await self._condition.wait_for(lambda: position() is not None)
                index = position()
        assert index is not None
        return index

    async def advance(
        self,
        boundary: EffectBoundary,
        *,
        occurrence: int = 1,
        timeout: float = 2.0,
    ) -> None:
        """Release the next arrived boundary, enforcing exact script order."""
        index = await self.wait_for(
            boundary,
            occurrence=occurrence,
            timeout=timeout,
        )
        async with self._condition:
            if index != self._released:
                expected = self._arrivals[self._released]
                raise RuntimeError(
                    f"cannot release {boundary.value}; next boundary is {expected.value}"
                )
            self._released += 1
            self._condition.notify_all()

    @property
    def history(self) -> tuple[EffectBoundary, ...]:
        """Return the immutable boundary arrival history for assertions."""
        return tuple(self._arrivals)


__all__ = [
    "DeterministicEffectDriver",
    "EffectBoundary",
    "EffectDispatch",
    "InjectedRuntimeCrash",
    "StoreBarrierScript",
    "StoreBarrierTimeoutError",
    "StoreFaultScript",
]
