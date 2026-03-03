"""
ComposerAssistBar — inline guidance row beneath the chat composer.

Shows command suggestions, shortcut hints, and queued skill context without
opening modal popups.
"""

from __future__ import annotations

from textual.widgets import Static

COMMAND_HINTS = [
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


class ComposerAssistBar(Static):
    """Bottom inline assist row for suggestions and shortcuts."""

    DEFAULT_CSS = """
    ComposerAssistBar {
        height: 1;
        background: #121216;
        color: #777782;
        padding: 0 1;
        border: none;
    }
    """

    def __init__(self, *, skill_names: list[str] | None = None, **kwargs) -> None:
        super().__init__("enter send · / for commands · ? shortcuts", **kwargs)
        self._skill_names = skill_names or []
        self._queued_skill: str | None = None
        self._show_shortcuts = False

    def set_skill_names(self, skill_names: list[str]) -> None:
        """Update known skill names used by /skill suggestions."""
        self._skill_names = skill_names

    def set_queued_skill(self, skill_name: str | None) -> None:
        """Track the currently queued skill."""
        self._queued_skill = skill_name
        self.update_for_input("")

    def toggle_shortcuts(self) -> None:
        """Toggle expanded shortcut guidance in-place."""
        self._show_shortcuts = not self._show_shortcuts
        self.update_for_input("")

    def update_for_input(self, value: str) -> None:
        """Update assist text based on current composer input."""
        prefix = value.strip()
        if prefix and self._show_shortcuts:
            # Auto-collapse expanded hint mode as soon as user resumes typing.
            self._show_shortcuts = False

        if self._show_shortcuts:
            self.update(
                "shortcuts: enter send · tab complete · ctrl+s skills · "
                "ctrl+n notifications · ctrl+t theme · ctrl+q quit"
            )
            return

        if not prefix:
            base = "enter send · / for commands · ? shortcuts"
            if self._queued_skill:
                base = f"queued skill {self._queued_skill} · {base}"
            self.update(base)
            return

        if prefix.startswith("/"):
            suggestions = [cmd for cmd in COMMAND_HINTS if cmd.startswith(prefix)]
            if prefix.startswith("/skill "):
                skill_prefix = prefix[len("/skill ") :]
                skill_matches = [
                    f"/skill {name}"
                    for name in self._skill_names
                    if name.startswith(skill_prefix)
                ]
                suggestions.extend(skill_matches)
            if suggestions:
                self.update("suggestions: " + "   ".join(suggestions[:4]))
            else:
                self.update("unknown command · /help for command list")
            return

        msg = "enter send · / for commands · ? shortcuts"
        if self._queued_skill:
            msg = f"queued skill {self._queued_skill} · {msg}"
        self.update(msg)
