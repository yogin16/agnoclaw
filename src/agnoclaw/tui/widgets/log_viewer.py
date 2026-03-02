"""
LogViewer widget — debug output panel for development.

Shows internal events, tool call details, and EventSink events.
Toggled via Ctrl+L in the TUI.
"""

from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.widgets import RichLog


class LogViewer(RichLog):
    """Debug log viewer panel."""

    DEFAULT_CSS = """
    LogViewer {
        height: 12;
        border: solid $surface-lighten-2;
        padding: 0 1;
        scrollbar-size: 1 1;
        display: none;
    }
    LogViewer.-visible {
        display: block;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(
            highlight=True, markup=True, wrap=True, auto_scroll=True, **kwargs
        )

    def on_mount(self) -> None:
        self.write(Text.from_markup("[bold dim]Debug Log[/bold dim]"))

    def log_event(self, event_type: str, detail: str = "") -> None:
        """Add a timestamped log entry."""
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        color = self._color_for(event_type)
        msg = f"[dim]{ts}[/dim] [{color}]{event_type}[/{color}]"
        if detail:
            msg += f" [dim]{detail[:120]}[/dim]"
        self.write(Text.from_markup(msg), scroll_end=True)

    def log_tool_call(self, tool_name: str, *, started: bool = True) -> None:
        """Log a tool call start/complete."""
        if started:
            self.log_event("tool.start", tool_name)
        else:
            self.log_event("tool.done", tool_name)

    def log_error(self, error: str) -> None:
        """Log an error."""
        self.log_event("error", error)

    def toggle_visible(self) -> None:
        """Toggle visibility."""
        self.toggle_class("-visible")

    @staticmethod
    def _color_for(event_type: str) -> str:
        if "error" in event_type:
            return "red"
        if "tool" in event_type:
            return "yellow"
        if "heartbeat" in event_type:
            return "magenta"
        return "cyan"
