"""
StatusBar widget — footer showing heartbeat timer, tool count, context info.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Label, Static


class AgnoStatusBar(Static):
    """Footer status bar with heartbeat timer, tool count, and context info."""

    DEFAULT_CSS = """
    AgnoStatusBar {
        dock: bottom;
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    AgnoStatusBar Horizontal {
        height: 1;
        width: 1fr;
    }
    AgnoStatusBar .status-item {
        width: auto;
        padding: 0 2 0 0;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._heartbeat_minutes = 0
        self._tool_count = 0
        self._streaming = False

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Label("● heartbeat: --", id="hb-status", classes="status-item")
            yield Label("│ tools: 0", id="tool-count", classes="status-item")
            yield Label("│ ready", id="agent-status", classes="status-item")

    def update_heartbeat(self, minutes: int) -> None:
        """Update the heartbeat timer display."""
        self._heartbeat_minutes = minutes
        label = self.query_one("#hb-status", Label)
        label.update(f"● heartbeat: {minutes}m")

    def update_tool_count(self, count: int) -> None:
        """Update the tool call counter."""
        self._tool_count = count
        label = self.query_one("#tool-count", Label)
        label.update(f"│ tools: {count}")

    def set_streaming(self, streaming: bool) -> None:
        """Update agent status indicator."""
        self._streaming = streaming
        label = self.query_one("#agent-status", Label)
        if streaming:
            label.update("│ streaming...")
        else:
            label.update("│ ready")
