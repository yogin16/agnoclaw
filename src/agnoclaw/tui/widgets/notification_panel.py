"""
NotificationPanel — right sidebar for heartbeat/cron alerts.

Displays timestamped alerts that scroll independently of the main chat log.
"""

from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.widgets import RichLog


class NotificationPanel(RichLog):
    """Right sidebar for heartbeat and cron notifications."""

    DEFAULT_CSS = """
    NotificationPanel {
        height: 1fr;
        border: none;
        background: #111114;
        padding: 0 1;
        scrollbar-size: 1 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(
            highlight=True, markup=True, wrap=True, auto_scroll=True, **kwargs
        )
        self._count = 0

    def on_mount(self) -> None:
        self.write(Text.from_markup("[bold]Notifications[/bold]"))
        self.write(Text.from_markup("[dim]Heartbeat and cron alerts appear here.[/dim]\n"))

    def add_heartbeat_alert(self, text: str) -> None:
        """Add a heartbeat alert with timestamp."""
        self._count += 1
        ts = datetime.now().strftime("%H:%M")
        self.write(
            Text.from_markup(f"[yellow bold][HB][/yellow bold] [dim]{ts}[/dim]"),
            scroll_end=True,
        )
        self.write(Text(text[:200]), scroll_end=True)  # Truncate long alerts
        self.write(Text(""), scroll_end=True)

    def add_cron_result(self, job_name: str, text: str) -> None:
        """Add a cron job result with timestamp."""
        self._count += 1
        ts = datetime.now().strftime("%H:%M")
        self.write(
            Text.from_markup(
                f"[cyan bold][{job_name}][/cyan bold] [dim]{ts}[/dim]"
            ),
            scroll_end=True,
        )
        self.write(Text(text[:200]), scroll_end=True)
        self.write(Text(""), scroll_end=True)

    def add_system_note(self, note: str, *, style: str = "cyan") -> None:
        """Add a generic system-level note."""
        self._count += 1
        ts = datetime.now().strftime("%H:%M")
        self.write(
            Text.from_markup(f"[{style}]{note}[/{style}] [dim]{ts}[/dim]"),
            scroll_end=True,
        )

    @property
    def alert_count(self) -> int:
        return self._count

    def clear_notifications(self) -> None:
        """Clear all notifications."""
        self._count = 0
        self.clear()
        self.on_mount()
