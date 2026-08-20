"""Persistent event-loop ownership for blocking lifecycle convenience calls."""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from typing import Any

from .presentation import RunPresentationDetached

_END = object()


class SyncLifecycleStream(Iterator[Any]):
    """Thread-safe sync view over one lifecycle-owned async presentation."""

    def __init__(
        self,
        coordinator: SyncLifecycleCoordinator,
        opener: Callable[[], Awaitable[tuple[str | None, AsyncIterator[Any]]]],
        *,
        capacity: int,
    ) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            raise ValueError("sync lifecycle stream capacity must be a positive integer")
        self._coordinator = coordinator
        self._capacity = capacity
        # Reserve one slot for the terminal marker so a completed producer can
        # never block the coordinator loop behind a stalled sync consumer.
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=capacity + 1)
        self._error: BaseException | None = None
        self._closed = False
        self._state_lock = threading.Lock()
        self._future = coordinator.submit(self._pump(opener))

    async def _pump(
        self,
        opener: Callable[[], Awaitable[tuple[str | None, AsyncIterator[Any]]]],
    ) -> None:
        stream: AsyncIterator[Any] | None = None
        run_id: str | None = None
        published = 0
        try:
            run_id, stream = await opener()
            async for event in stream:
                if self._queue.qsize() >= self._capacity:
                    while True:
                        try:
                            self._queue.get_nowait()
                        except queue.Empty:
                            break
                    self._queue.put_nowait(
                        RunPresentationDetached(
                            run_id=run_id,
                            reason_code="PRESENTATION_SLOW_CONSUMER",
                            published_events=published,
                            buffer_capacity=self._capacity,
                        )
                    )
                    break
                self._queue.put_nowait(event)
                published += 1
        except asyncio.CancelledError:
            # Closing a presentation is observation loss, not run cancellation.
            pass
        except BaseException as exc:
            self._error = exc
        finally:
            if stream is not None:
                closer = getattr(stream, "aclose", None)
                if callable(closer):
                    try:
                        await closer()
                    except (asyncio.CancelledError, RuntimeError):
                        pass
            self._queue.put_nowait(_END)

    def __iter__(self) -> SyncLifecycleStream:
        return self

    def __next__(self) -> Any:
        with self._state_lock:
            if self._closed:
                raise StopIteration
        item = self._queue.get()
        if item is _END:
            with self._state_lock:
                self._closed = True
            if self._error is not None:
                raise self._error
            raise StopIteration
        return item

    def close(self) -> None:
        """Detach this observer without cancelling its logical lifecycle run."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._future.cancel()

    def __del__(self) -> None:  # pragma: no cover - best-effort observer cleanup
        try:
            self.close()
        except Exception:
            pass


class SyncLifecycleCoordinator:
    """One reusable background asyncio loop owned by a blocking harness facade."""

    def __init__(self, *, name: str) -> None:
        self._name = name
        self._ready = threading.Event()
        self._state_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._state_lock:
            self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("sync lifecycle coordinator is closed")
            thread = self._thread
            if thread is None:
                thread = threading.Thread(
                    target=self._serve,
                    name=f"agnoclaw-sync:{self._name}",
                    daemon=True,
                )
                self._thread = thread
                thread.start()
        self._ready.wait()
        loop = self._loop
        if loop is None:  # pragma: no cover - ready event invariant
            raise RuntimeError("sync lifecycle coordinator failed to start")
        return loop

    def in_coordinator_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def submit(
        self,
        awaitable: Coroutine[Any, Any, Any],
    ) -> concurrent.futures.Future[Any]:
        """Schedule an awaitable on the owned loop from any other thread."""
        try:
            loop = self._ensure_started()
            return asyncio.run_coroutine_threadsafe(awaitable, loop)
        except BaseException:
            closer = getattr(awaitable, "close", None)
            if callable(closer):
                closer()
            raise

    def run(
        self,
        awaitable: Coroutine[Any, Any, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        return self.submit(awaitable).result(timeout=timeout)

    def stream(
        self,
        opener: Callable[[], Awaitable[tuple[str | None, AsyncIterator[Any]]]],
        *,
        capacity: int = 256,
    ) -> SyncLifecycleStream:
        return SyncLifecycleStream(self, opener, capacity=capacity)

    def stop(self) -> None:
        """Stop and join the idle coordinator after harness shutdown completes."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)


__all__ = ["SyncLifecycleCoordinator", "SyncLifecycleStream"]
