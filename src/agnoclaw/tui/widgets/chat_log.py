"""
ChatLog widget — scrollable chat display with streaming support.

Uses a VerticalScroll container with Static children. During streaming,
a single Static widget is updated in-place with accumulated text (live
tokens). On completion, the same widget is re-rendered as Markdown.
"""

from __future__ import annotations

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static


class ChatLog(VerticalScroll):
    """Scrollable chat log with live streaming and Markdown rendering."""

    DEFAULT_CSS = """
    ChatLog {
        height: 1fr;
        border: solid $surface-lighten-2;
        padding: 0 1;
        scrollbar-size: 1 1;
    }
    ChatLog .user-label {
        color: $text;
        text-style: bold;
        background: $primary-darken-3;
        padding: 0 1;
        margin: 1 0 0 0;
    }
    ChatLog .agent-label {
        color: $success;
        text-style: bold;
        margin: 1 0 0 0;
    }
    ChatLog .message-text {
        padding: 0 1;
    }
    ChatLog .streaming-text {
        padding: 0 1;
    }
    ChatLog .tool-indicator {
        color: $text-muted;
        padding: 0 2;
    }
    ChatLog .notification {
        padding: 0 1;
        margin: 1 0 0 0;
    }
    ChatLog .error-text {
        color: $error;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._streaming_text: list[str] = []
        self._is_streaming = False
        self._streaming_widget: Static | None = None
        self._msg_counter = 0

    def _next_id(self, prefix: str) -> str:
        self._msg_counter += 1
        return f"{prefix}-{self._msg_counter}"

    def add_user_message(self, text: str) -> None:
        """Display a user message in the chat log."""
        self.mount(Static("You", classes="user-label", id=self._next_id("ul")))
        self.mount(Static(text, classes="message-text", id=self._next_id("um")))
        self.scroll_end(animate=False)

    def start_agent_response(self) -> None:
        """Mount the agent label and a streaming widget."""
        self.mount(Static("Agent", classes="agent-label", id=self._next_id("al")))
        self._streaming_widget = Static(
            "...", classes="streaming-text", id=self._next_id("st")
        )
        self.mount(self._streaming_widget)
        self._streaming_text.clear()
        self._is_streaming = True
        self.scroll_end(animate=False)

    def append_chunk(self, text: str) -> None:
        """Append a streaming chunk — updates the streaming widget in-place."""
        if self._is_streaming and self._streaming_widget is not None:
            self._streaming_text.append(text)
            # Update the widget with all accumulated text so far
            self._streaming_widget.update("".join(self._streaming_text))
            self.scroll_end(animate=False)

    def finish_agent_response(self, full_text: str = "") -> None:
        """Re-render the streaming widget as Markdown."""
        self._is_streaming = False
        final = full_text or "".join(self._streaming_text)
        self._streaming_text.clear()

        if self._streaming_widget is not None:
            if final.strip():
                try:
                    self._streaming_widget.update(RichMarkdown(final))
                except Exception:
                    self._streaming_widget.update(final)
            else:
                self._streaming_widget.update("")
            self._streaming_widget = None
        self.scroll_end(animate=False)

    def add_tool_indicator(self, tool_name: str, *, done: bool = False) -> None:
        """Show a tool call indicator."""
        mark = "✓" if done else "..."
        self.mount(
            Static(
                Text.from_markup(f"→ {tool_name} {mark}"),
                classes="tool-indicator",
                id=self._next_id("ti"),
            )
        )
        self.scroll_end(animate=False)

    def add_notification(self, text: str, *, style: str = "yellow") -> None:
        """Display an inline notification in the chat log."""
        self.mount(
            Static(
                Text.from_markup(f"[{style}]{text}[/{style}]"),
                classes="notification",
                id=self._next_id("nt"),
            )
        )
        self.scroll_end(animate=False)

    def add_error(self, error: str) -> None:
        """Display an error message."""
        self.mount(
            Static(
                Text.from_markup(f"[bold]Error:[/bold] {error}"),
                classes="error-text",
                id=self._next_id("er"),
            )
        )
        self.scroll_end(animate=False)

    def clear_log(self) -> None:
        """Clear all content from the chat log."""
        self.query("Static").remove()
        self._streaming_widget = None
        self._streaming_text.clear()
        self._is_streaming = False
