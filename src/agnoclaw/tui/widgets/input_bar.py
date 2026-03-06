"""
InputBar widget — text input with slash-command tab completion.

Disables during streaming to prevent overlapping agent calls.
Emits UserSubmitted messages on Enter.
"""

from __future__ import annotations

from textual.events import Key
from textual.suggester import SuggestFromList
from textual.widgets import Input

from agnoclaw.tui.events import UserSubmitted

# Base slash commands available in TUI
BASE_SLASH_COMMANDS = [
    "/skill",
    "/skills",
    "/clear",
    "/help",
    "/?",
    "/quit",
    "/compact",
    "/notifications",
    "/log",
    "/theme",
]


class InputBar(Input):
    """Chat input with slash-command tab completion."""

    DEFAULT_CSS = """
    InputBar {
        height: 2;
        width: 1fr;
        border: none !important;
        padding: 0 0;
        background: #1a1a1f;
        color: #f0f0f4;
    }
    InputBar:focus {
        border: none !important;
        background: #23232a;
    }
    InputBar.-disabled {
        opacity: 0.82;
    }
    InputBar > .input--placeholder,
    InputBar > .input--suggestion {
        color: #8a8a95;
    }
    InputBar > .input--cursor {
        background: #e7e7ee;
        color: #121216;
    }
    InputBar.-textual-compact {
        border: none !important;
        height: 2;
        width: 1fr;
        padding: 0 0;
        background: #1a1a1f;
    }
    InputBar.-textual-compact:focus {
        border: none !important;
        background: #23232a;
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
            placeholder="Ask anything...  (? for shortcuts)",
            suggester=SuggestFromList(completions, case_sensitive=False),
            **kwargs,
        )
        # Compact mode gives a true single-line composer (no tall shell).
        self.add_class("-textual-compact")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Emit UserSubmitted and clear the input."""
        text = event.value.strip()
        if text in {"?", "/?"}:
            self.app.action_toggle_shortcuts()
            self.value = ""
            if hasattr(self.app, "update_composer_assist"):
                self.app.update_composer_assist("")
            return
        if text:
            self.post_message(UserSubmitted(text))
            self.value = ""
            if hasattr(self.app, "update_composer_assist"):
                self.app.update_composer_assist("")

    def on_key(self, event: Key) -> None:
        """Toggle shortcuts quickly when pressing ? on an empty prompt."""
        if not self.value and event.key in {"?", "question_mark"}:
            self.app.action_toggle_shortcuts()
            event.prevent_default()
            event.stop()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Push current input text into the assist row."""
        if hasattr(self.app, "update_composer_assist"):
            self.app.update_composer_assist(event.value)

    def set_disabled(self, disabled: bool) -> None:
        """Enable/disable input during streaming."""
        self.disabled = disabled
        if disabled:
            self.add_class("-disabled")
            self.placeholder = "Working..."
        else:
            self.remove_class("-disabled")
            self.placeholder = "Ask anything...  (? for shortcuts)"
            self.focus()
        if hasattr(self.app, "update_composer_assist"):
            self.app.update_composer_assist(self.value)

    def update_completions(self, skill_names: list[str]) -> None:
        """Update slash-command completions with current skill names."""
        completions = list(BASE_SLASH_COMMANDS)
        completions.extend(f"/skill {name}" for name in skill_names)
        self.suggester = SuggestFromList(completions, case_sensitive=False)
