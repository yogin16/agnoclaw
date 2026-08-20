"""Interactive TUI routing contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agnoclaw.config import RuntimeProfile
from agnoclaw.tui.driver import AgentDriver
from agnoclaw.tui.events import StreamChunk, StreamDone


@pytest.mark.asyncio
async def test_tui_driver_streams_durable_run_and_uses_authoritative_final_result():
    raw = SimpleNamespace(event="RunContent", content="live")
    handle = MagicMock(run_id="run_tui")
    handle.wait = AsyncMock(return_value=SimpleNamespace(content="final"))

    async def start(_message, **kwargs):
        presentation = kwargs.pop("_presentation")
        presentation.publish(raw)
        presentation.finish()
        assert kwargs == {"skill": "review"}
        return handle

    app = MagicMock()
    agent = MagicMock(profile=RuntimeProfile.DURABLE)
    agent.start = AsyncMock(side_effect=start)
    agent.arun = AsyncMock()
    agent._extract_event_content.return_value = "live"
    agent._map_agno_event_type.return_value = None

    await AgentDriver(app, agent).send_message("work", skill="review")

    messages = [call.args[0] for call in app.post_message.call_args_list]
    assert any(isinstance(message, StreamChunk) and message.text == "live" for message in messages)
    assert any(
        isinstance(message, StreamDone) and message.full_text == "final" for message in messages
    )
    handle.wait.assert_awaited_once_with()
    agent.arun.assert_not_awaited()


@pytest.mark.asyncio
async def test_tui_worker_cancellation_cancels_durable_run_explicitly():
    handle = MagicMock(run_id="run_cancel")
    handle.cancel = AsyncMock()
    started = asyncio.Event()

    async def start(_message, **_kwargs):
        started.set()
        return handle

    app = MagicMock()
    agent = MagicMock(profile=RuntimeProfile.DURABLE)
    agent.start = AsyncMock(side_effect=start)
    driver = AgentDriver(app, agent)

    task = asyncio.create_task(driver.send_message("work"))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    handle.cancel.assert_awaited_once_with()
