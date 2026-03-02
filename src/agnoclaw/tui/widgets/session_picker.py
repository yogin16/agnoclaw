"""
SessionPickerScreen — modal for browsing and resuming past sessions.

Reads session metadata from the SQLite/Postgres storage backend.
"""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView, Static

logger = logging.getLogger("agnoclaw.tui.session_picker")


class SessionPickerScreen(ModalScreen[str]):
    """Modal for selecting a past session to resume."""

    DEFAULT_CSS = """
    SessionPickerScreen {
        align: center middle;
    }
    SessionPickerScreen > VerticalScroll {
        width: 70;
        max-height: 22;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [("escape", "dismiss_screen", "Close")]

    def __init__(
        self,
        sessions: list[dict] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._sessions = sessions or []

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("[bold]Select a session[/bold] (press Escape to cancel)\n")

            if not self._sessions:
                yield Static("[dim]No past sessions found.[/dim]")
            else:
                items = []
                for s in self._sessions:
                    session_id = s.get("session_id", "unknown")
                    created = s.get("created_at", "")
                    summary = s.get("summary", "")[:60]
                    runs = s.get("run_count", 0)
                    label_text = (
                        f"[cyan]{session_id[:12]}[/cyan]  "
                        f"[dim]{created}[/dim]  "
                        f"runs: {runs}"
                    )
                    if summary:
                        label_text += f"  {summary}"
                    items.append(ListItem(Label(label_text)))
                yield ListView(*items, id="session-list")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._sessions):
            self.dismiss(self._sessions[idx].get("session_id", ""))

    def action_dismiss_screen(self) -> None:
        self.dismiss("")
