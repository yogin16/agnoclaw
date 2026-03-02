"""
InputBar widget — text input with slash-command tab completion.

Disables during streaming to prevent overlapping agent calls.
Emits UserSubmitted messages on Enter.
"""

from __future__ import annotations

from textual.suggester import SuggestFromList
from textual.widgets import Input

from agnoclaw.tui.events import UserSubmitted

# Base slash commands available in TUI
BASE_SLASH_COMMANDS = [
    "/skill",
    "/skills",
    "/clear",
    "/help",
    "/quit",
    "/compact",
]


class InputBar(Input):
    """Chat input with slash-command tab completion."""

    DEFAULT_CSS = """
    InputBar {
        dock: bottom;
        height: 3;
        border: solid $surface-lighten-2;
        padding: 0 1;
    }
    InputBar:focus {
        border: solid $accent;
    }
    InputBar.-disabled {
        opacity: 0.5;
    }
    """

    def __init__(
        self,
        *,
        skill_names: list[str] | None = None,
        **kwargs,
    ) -> None:
        completions = list(BASE_SLASH_COMMANDS)
        if skill_names:
            completions.extend(f"/skill {name}" for name in skill_names)

        super().__init__(
            placeholder="Type a message or /command...",
            suggester=SuggestFromList(completions, case_sensitive=False),
            **kwargs,
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Emit UserSubmitted and clear the input."""
        text = event.value.strip()
        if text:
            self.post_message(UserSubmitted(text))
            self.value = ""

    def set_disabled(self, disabled: bool) -> None:
        """Enable/disable input during streaming."""
        self.disabled = disabled
        if disabled:
            self.add_class("-disabled")
            self.placeholder = "Agent is responding..."
        else:
            self.remove_class("-disabled")
            self.placeholder = "Type a message or /command..."
            self.focus()

    def update_completions(self, skill_names: list[str]) -> None:
        """Update slash-command completions with current skill names."""
        completions = list(BASE_SLASH_COMMANDS)
        completions.extend(f"/skill {name}" for name in skill_names)
        self.suggester = SuggestFromList(completions, case_sensitive=False)
