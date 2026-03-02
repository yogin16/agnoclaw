"""
Modal screens for the agnoclaw TUI.

- SkillPickerScreen: select a skill to activate for the next message
- HelpScreen: display key bindings and slash commands
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView, Static


class SkillPickerScreen(ModalScreen[str]):
    """Modal dialog for selecting a skill."""

    DEFAULT_CSS = """
    SkillPickerScreen {
        align: center middle;
    }
    SkillPickerScreen > VerticalScroll {
        width: 60;
        max-height: 20;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [("escape", "dismiss_screen", "Close")]

    def __init__(self, skills: list[dict], **kwargs) -> None:
        super().__init__(**kwargs)
        self._skills = skills

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("[bold]Select a skill[/bold] (press Escape to cancel)\n")
            items = []
            for s in self._skills:
                invocable = "user" if s.get("user_invocable") else "model"
                items.append(
                    ListItem(
                        Label(f"[cyan]{s['name']}[/cyan]  {s['description']}  [{invocable}]")
                    )
                )
            yield ListView(*items, id="skill-list")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._skills):
            self.dismiss(self._skills[idx]["name"])

    def action_dismiss_screen(self) -> None:
        self.dismiss("")


class HelpScreen(ModalScreen):
    """Modal help screen showing key bindings and slash commands."""

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen > VerticalScroll {
        width: 60;
        max-height: 24;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [("escape", "dismiss_screen", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(
                "[bold]agnoclaw TUI — Help[/bold]\n\n"
                "[bold]Key Bindings[/bold]\n"
                "  Ctrl+Q       Quit\n"
                "  Ctrl+N       Toggle notification panel\n"
                "  Ctrl+S       Open skill picker\n"
                "  Escape       Close modal / cancel\n\n"
                "[bold]Slash Commands[/bold]\n"
                "  /skill NAME  Activate skill for next message\n"
                "  /skills      List available skills\n"
                "  /clear       Clear session context\n"
                "  /compact     Compact session (if supported)\n"
                "  /help        Show this help\n"
                "  /quit        Exit the TUI\n\n"
                "[dim]Press Escape to close.[/dim]"
            )

    def action_dismiss_screen(self) -> None:
        self.dismiss()
