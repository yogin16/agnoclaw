"""
StatusBar widget — footer showing heartbeat timer, tool count, context info.
"""

from __future__ import annotations

from textual.widgets import Static


class AgnoStatusBar(Static):
    """Footer status bar with heartbeat timer, tool count, and context info."""

    DEFAULT_CSS = """
    AgnoStatusBar {
        height: 1;
        background: #15151a;
        color: #76767f;
        padding: 0 1;
        border: none;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("ready", **kwargs)
        self._heartbeat_minutes = 0
        self._heartbeat_enabled = False
        self._tool_count = 0
        self._streaming = False
        self._notifications_unread = 0
        self._queued_skill: str | None = None
        self._theme_name = "ocean"
        self._agent_status_text = "ready"

    def update_heartbeat(self, minutes: int) -> None:
        """Update the heartbeat timer display."""
        self._heartbeat_minutes = minutes
        self._heartbeat_enabled = True
        self._refresh_line()

    def update_tool_count(self, count: int) -> None:
        """Update the tool call counter."""
        self._tool_count = count
        self._refresh_line()

    def set_heartbeat_enabled(self, enabled: bool) -> None:
        """Explicitly mark heartbeat as enabled/disabled."""
        self._heartbeat_enabled = enabled
        if not enabled:
            self._heartbeat_minutes = 0
        self._refresh_line()

    def set_notifications_unread(self, count: int) -> None:
        """Track unread alert count from heartbeat/cron."""
        self._notifications_unread = max(0, count)
        self._refresh_line()

    def set_streaming(self, streaming: bool) -> None:
        """Update agent status indicator."""
        self._streaming = streaming
        if streaming:
            self.set_agent_status("streaming...")
        else:
            self.set_agent_status("ready")

    def set_compacting(self, compacting: bool) -> None:
        """Update agent status indicator for session compaction."""
        if compacting:
            self.set_agent_status("compacting...")
        elif self._streaming:
            self.set_agent_status("streaming...")
        else:
            self.set_agent_status("ready")

    def set_agent_status(self, status_text: str) -> None:
        """Set arbitrary agent status text."""
        self._agent_status_text = status_text
        self._refresh_line()

    def set_queued_skill(self, skill_name: str | None) -> None:
        """Display queued skill name."""
        self._queued_skill = skill_name
        self._refresh_line()

    def set_theme_name(self, theme_name: str) -> None:
        """Track current UI theme name (shown only when useful)."""
        self._theme_name = theme_name
        self._refresh_line()

    def _refresh_line(self) -> None:
        parts = [self._agent_status_text]
        if self._queued_skill:
            parts.append(f"skill {self._queued_skill}")
        if self._tool_count > 0 or self._streaming:
            parts.append(f"tools {self._tool_count}")
        if self._heartbeat_enabled:
            hb_text = f"{self._heartbeat_minutes}m" if self._heartbeat_minutes else "now"
            parts.append(f"heartbeat {hb_text}")
        if self._notifications_unread:
            parts.append(f"alerts {self._notifications_unread}")
        line = "  ·  ".join(parts)
        self.update(line)
