"""
AgentDriver — async bridge between the Textual app and AgentHarness.

Runs agent.arun(stream=True) as a Textual Worker and posts StreamChunk/StreamDone
messages back to the app. Manages HeartbeatDaemon lifecycle on Textual's asyncio loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .events import (
    HeartbeatAlert,
    HeartbeatTick,
    StreamChunk,
    StreamDone,
    StreamError,
    ToolCallCompleted,
    ToolCallStarted,
)

if TYPE_CHECKING:
    from textual.app import App

    from agnoclaw.agent import AgentHarness

logger = logging.getLogger("agnoclaw.tui.driver")


class AgentDriver:
    """
    Bridges AgentHarness and the Textual app.

    Handles:
    - Streaming agent responses via arun(stream=True)
    - HeartbeatDaemon lifecycle (start/stop)
    - Heartbeat tick counter for StatusBar
    """

    def __init__(self, app: App, agent: AgentHarness) -> None:
        self._app = app
        self._agent = agent
        self._daemon = None
        self._heartbeat_tick_task: asyncio.Task | None = None
        self._minutes_since_heartbeat = 0
        self._streaming = False
        self._tool_count = 0
        self._active_tool_labels: dict[str, str] = {}

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    @property
    def tool_count(self) -> int:
        return self._tool_count

    async def send_message(
        self, text: str, *, skill: str | None = None
    ) -> None:
        """
        Send a user message to the agent and stream the response.

        Posts StreamChunk, ToolCallStarted/Completed, and StreamDone/StreamError
        messages to the app as events arrive.
        """
        self._streaming = True
        accumulated = []
        self._active_tool_labels.clear()

        try:
            response = await self._agent.arun(text, stream=True, skill=skill)

            async for event in response:
                # Extract text content
                content = self._agent._extract_event_content(event)
                if content:
                    accumulated.append(content)
                    self._app.post_message(StreamChunk(content))

                # Map tool call events
                event_type = self._agent._map_agno_event_type(event)
                if event_type == "tool.call.started":
                    summary = self._agent._stream_event_summary(event)
                    tool_name = str(summary.get("tool_name") or getattr(event, "tool_name", "tool"))
                    tool_call_id = summary.get("tool_call_id")
                    display_name = self._agent._format_tool_invocation_label(
                        tool_name,
                        summary.get("arguments"),
                    )
                    if tool_call_id:
                        self._active_tool_labels[str(tool_call_id)] = display_name
                    self._tool_count += 1
                    self._app.post_message(
                        ToolCallStarted(tool_name, display_name=display_name)
                    )
                elif event_type == "tool.call.completed":
                    summary = self._agent._stream_event_summary(event)
                    tool_name = str(summary.get("tool_name") or getattr(event, "tool_name", "tool"))
                    tool_call_id = summary.get("tool_call_id")
                    display_name = (
                        self._active_tool_labels.pop(str(tool_call_id))
                        if tool_call_id and str(tool_call_id) in self._active_tool_labels
                        else self._agent._format_tool_invocation_label(
                            tool_name,
                            summary.get("arguments"),
                        )
                    )
                    self._app.post_message(
                        ToolCallCompleted(tool_name, display_name=display_name)
                    )

            self._app.post_message(StreamDone("".join(accumulated)))

        except Exception as e:
            logger.exception("Agent streaming error")
            self._app.post_message(StreamError(str(e)))
        finally:
            self._streaming = False
            self._active_tool_labels.clear()

    def start_heartbeat(self) -> None:
        """Start HeartbeatDaemon on Textual's asyncio loop."""
        from agnoclaw.config import get_config

        cfg = get_config()
        if not cfg.heartbeat.enabled:
            logger.debug("Heartbeat disabled in config")
            return

        if self._agent.workspace.is_empty_heartbeat():
            logger.debug("HEARTBEAT.md empty — skipping heartbeat")
            return

        from agnoclaw.heartbeat import HeartbeatDaemon

        def on_alert(msg: str) -> None:
            self._minutes_since_heartbeat = 0
            self._app.post_message(HeartbeatAlert(msg))

        self._daemon = HeartbeatDaemon(self._agent, on_alert=on_alert, config=cfg)
        self._daemon.start()

        # Start tick counter
        self._heartbeat_tick_task = asyncio.create_task(
            self._heartbeat_ticker(), name="agnoclaw-hb-tick"
        )

        logger.info(
            "Heartbeat started (interval=%dm)", cfg.heartbeat.interval_minutes
        )

    def stop_heartbeat(self) -> None:
        """Stop HeartbeatDaemon and tick counter."""
        if self._daemon:
            self._daemon.stop()
            self._daemon = None
        if self._heartbeat_tick_task and not self._heartbeat_tick_task.done():
            self._heartbeat_tick_task.cancel()
            self._heartbeat_tick_task = None

    async def _heartbeat_ticker(self) -> None:
        """Post HeartbeatTick every minute for the status bar."""
        while True:
            try:
                await asyncio.sleep(60)
                self._minutes_since_heartbeat += 1
                self._app.post_message(
                    HeartbeatTick(self._minutes_since_heartbeat)
                )
            except asyncio.CancelledError:
                break
