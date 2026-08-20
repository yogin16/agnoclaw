"""Adversarial contracts for the blocking lifecycle presentation bridge."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from agnoclaw.runtime.presentation import RunPresentationDetached
from agnoclaw.runtime.sync_bridge import SyncLifecycleCoordinator


def test_sync_bridge_detaches_a_slow_consumer_without_blocking_producer() -> None:
    coordinator = SyncLifecycleCoordinator(name="slow-consumer-test")

    async def events() -> AsyncIterator[str]:
        yield "first"
        yield "second"

    async def open_stream() -> tuple[str, AsyncIterator[str]]:
        return "run-1", events()

    try:
        stream = coordinator.stream(open_stream, capacity=1)
        stream._future.result(timeout=2)

        delivered = list(stream)

        assert len(delivered) == 1
        detached = delivered[0]
        assert isinstance(detached, RunPresentationDetached)
        assert detached.run_id == "run-1"
        assert detached.reason_code == "PRESENTATION_SLOW_CONSUMER"
        assert detached.published_events == 1
        assert detached.buffer_capacity == 1
    finally:
        coordinator.stop()


def test_sync_bridge_preserves_terminal_stream_errors() -> None:
    coordinator = SyncLifecycleCoordinator(name="error-test")

    async def events() -> AsyncIterator[str]:
        yield "visible-before-error"
        raise RuntimeError("terminal stream failure")

    async def open_stream() -> tuple[str, AsyncIterator[str]]:
        return "run-2", events()

    try:
        stream = coordinator.stream(open_stream, capacity=8)

        assert next(stream) == "visible-before-error"
        with pytest.raises(RuntimeError, match="terminal stream failure"):
            next(stream)
    finally:
        coordinator.stop()


def test_sync_bridge_stop_joins_its_owned_thread() -> None:
    coordinator = SyncLifecycleCoordinator(name="shutdown-test")

    async def answer() -> int:
        return 42

    assert coordinator.run(answer()) == 42
    thread = coordinator._thread
    assert thread is not None and thread.is_alive()

    coordinator.stop()

    assert not thread.is_alive()
    with pytest.raises(RuntimeError, match="coordinator is closed"):
        coordinator.run(answer())
