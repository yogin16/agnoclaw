"""
ChatLog widget — scrollable chat display with streaming support.

Uses Textual's RichLog for appending styled content. During streaming,
text chunks are accumulated and rendered as Rich Markdown when the
response completes.
"""

from __future__ import annotations

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.widgets import RichLog


class ChatLog(RichLog):
    """Scrollable chat log with streaming support."""

    DEFAULT_CSS = """
    ChatLog {
        height: 1fr;
        border: solid $surface-lighten-2;
        padding: 0 1;
        scrollbar-size: 1 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(
            highlight=True, markup=True, wrap=True, auto_scroll=True, **kwargs
        )
        self._streaming_text: list[str] = []
        self._is_streaming = False

    def add_user_message(self, text: str) -> None:
        """Display a user message in the chat log."""
        self.write(Text.from_markup("\n[bold blue]You[/bold blue]"))
        self.write(Text(text))

    def start_agent_response(self) -> None:
        """Begin accumulating a streaming agent response."""
        self.write(Text.from_markup("\n[bold green]Agent[/bold green]"))
        self._streaming_text.clear()
        self._is_streaming = True

    def append_chunk(self, text: str) -> None:
        """Append a streaming chunk. Text is accumulated for final render."""
        if self._is_streaming:
            self._streaming_text.append(text)
            # Write raw text during streaming for immediate feedback
            self.write(Text(text), scroll_end=True)

    def finish_agent_response(self, full_text: str = "") -> None:
        """Finalize the agent response — re-render accumulated text as Markdown."""
        self._is_streaming = False
        final = full_text or "".join(self._streaming_text)
        self._streaming_text.clear()

        if not final.strip():
            return

        # Clear the raw streaming lines and replace with rendered markdown
        # For simplicity, we append the rendered version below
        try:
            self.write(Text(""))  # visual separator
            self.write(RichMarkdown(final))
        except Exception:
            # Fallback: just show plain text
            self.write(Text(final))

    def add_tool_indicator(self, tool_name: str, *, done: bool = False) -> None:
        """Show a tool call indicator."""
        if done:
            self.write(
                Text.from_markup(f"  [dim]→ {tool_name} ✓[/dim]"),
                scroll_end=True,
            )
        else:
            self.write(
                Text.from_markup(f"  [dim]→ {tool_name}...[/dim]"),
                scroll_end=True,
            )

    def add_notification(self, text: str, *, style: str = "yellow") -> None:
        """Display an inline notification in the chat log."""
        self.write(
            Text.from_markup(f"\n[{style}]📢 {text}[/{style}]"),
            scroll_end=True,
        )

    def add_error(self, error: str) -> None:
        """Display an error message."""
        self.write(
            Text.from_markup(f"\n[red bold]Error:[/red bold] [red]{error}[/red]"),
            scroll_end=True,
        )

    def clear_log(self) -> None:
        """Clear all content from the chat log."""
        self.clear()
