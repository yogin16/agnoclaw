"""
ChatLog widget — scrollable chat display with streaming support.

Uses a VerticalScroll container with Static children. During streaming,
a single Static widget is updated in-place with accumulated text (live
tokens). On completion, the same widget is re-rendered as Markdown.
"""

from __future__ import annotations

from time import perf_counter

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static


class ChatLog(VerticalScroll):
    """Scrollable chat log with live streaming and Markdown rendering."""

    DEFAULT_CSS = """
    ChatLog {
        height: auto;
        max-height: 1fr;
        min-height: 8;
        border: none;
        background: #0d0d10;
        padding: 0 0 0 0;
        scrollbar-size: 1 1;
    }
    ChatLog .startup-note {
        color: #6f6f79;
        margin: 1 0 1 0;
        width: 1fr;
    }
    ChatLog .user-message {
        background: #1a1a22;
        color: #e7e7ef;
        padding: 0 1;
        margin: 1 0 0 0;
        width: 1fr;
    }
    ChatLog .agent-message {
        padding: 0 0 1 0;
        margin: 1 0 0 0;
        width: 1fr;
    }
    ChatLog .streaming-text {
        color: #e0e0e8;
    }
    ChatLog .tool-trace-running {
        color: #7b7b85;
        padding: 0 0 0 2;
    }
    ChatLog .tool-trace-done {
        color: #87a980;
        padding: 0 0 0 2;
    }
    ChatLog .working-line {
        color: #8e8e98;
        text-style: italic;
        padding: 0 0 0 2;
        margin: 0 0 1 0;
    }
    ChatLog .notification {
        color: #7d7d87;
        padding: 0 0 0 2;
        margin: 1 0 0 0;
    }
    ChatLog .error-text {
        color: #d88989;
        padding: 0 0 0 2;
        margin: 1 0 0 0;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._streaming_text: list[str] = []
        self._is_streaming = False
        self._streaming_widget: Static | None = None
        self._msg_counter = 0
        self._tool_trace_seq = 1
        self._pending_tool_traces: dict[str, list[tuple[int, float, Static]]] = {}
        self._working_widget: Static | None = None

    def _next_id(self, prefix: str) -> str:
        self._msg_counter += 1
        return f"{prefix}-{self._msg_counter}"

    def _is_near_bottom(self) -> bool:
        """Return True when scroll is near the end, for non-jumpy auto-scroll."""
        max_y = float(getattr(self, "max_scroll_y", 0))
        current_y = float(getattr(self, "scroll_y", 0))
        return max_y - current_y <= 2

    def _maybe_scroll_end(self, *, was_near_bottom: bool) -> None:
        """Auto-follow output only when user was already at bottom."""
        if was_near_bottom:
            self.scroll_end(animate=False)

    def add_user_message(self, text: str) -> None:
        """Display a user message in the chat log."""
        follow = self._is_near_bottom()
        self.mount(Static(text, classes="user-message", id=self._next_id("um")))
        self._maybe_scroll_end(was_near_bottom=follow)

    def start_agent_response(self) -> None:
        """Mount the agent label and a streaming widget."""
        follow = self._is_near_bottom()
        self._streaming_widget = Static(
            "...", classes="agent-message streaming-text", id=self._next_id("st")
        )
        self.mount(self._streaming_widget)
        self._streaming_text.clear()
        self._is_streaming = True
        self._maybe_scroll_end(was_near_bottom=follow)

    def append_chunk(self, text: str) -> None:
        """Append a streaming chunk — updates the streaming widget in-place."""
        if self._is_streaming and self._streaming_widget is not None:
            follow = self._is_near_bottom()
            self._streaming_text.append(text)
            # Update the widget with all accumulated text so far
            self._streaming_widget.update("".join(self._streaming_text))
            self._maybe_scroll_end(was_near_bottom=follow)

    def finish_agent_response(self, full_text: str = "") -> None:
        """Re-render the streaming widget as Markdown."""
        follow = self._is_near_bottom()
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
        self._maybe_scroll_end(was_near_bottom=follow)

    def add_tool_indicator(self, tool_name: str, *, done: bool = False) -> None:
        """Legacy compatibility wrapper around tool trace methods."""
        if done:
            self.finish_tool_trace(tool_name)
        else:
            self.start_tool_trace(tool_name)

    def start_tool_trace(self, tool_name: str) -> None:
        """Add an inline tool trace row directly in chat."""
        follow = self._is_near_bottom()
        trace_id = self._tool_trace_seq
        self._tool_trace_seq += 1
        widget = Static(
            Text.from_markup(
                f"[dim]• running tool #{trace_id}[/dim] [bold]{tool_name}[/bold]"
            ),
            classes="tool-trace-running",
            id=self._next_id("tr"),
        )
        self._pending_tool_traces.setdefault(tool_name, []).append(
            (trace_id, perf_counter(), widget)
        )
        self.mount(widget)
        self._maybe_scroll_end(was_near_bottom=follow)

    def start_working(self, title: str) -> None:
        """Start or replace a single inline working line."""
        follow = self._is_near_bottom()
        if self._working_widget is None:
            self._working_widget = Static("", classes="working-line", id=self._next_id("wk"))
            self.mount(self._working_widget)
        self._working_widget.update(
            Text.from_markup(f"[dim]• working (0s) · {title}[/dim]")
        )
        self._maybe_scroll_end(was_near_bottom=follow)

    def update_working(self, title: str, elapsed_s: int) -> None:
        """Update inline working progress."""
        if self._working_widget is None:
            self.start_working(title)
            return
        self._working_widget.update(
            Text.from_markup(f"[dim]• working ({elapsed_s}s · esc to interrupt) · {title}[/dim]")
        )

    def finish_working(self, *, success: bool, elapsed_s: int) -> None:
        """Finalize inline working line."""
        if self._working_widget is None:
            return

        if success:
            # Keep the transcript clean: don't add a "completed in Xs" line for every turn.
            self._working_widget.remove()
            self._working_widget = None
            return

        self._working_widget.update(
            Text.from_markup(f"[red]• failed in {elapsed_s}s[/red]")
        )
        self._working_widget = None

    def finish_tool_trace(self, tool_name: str) -> None:
        """Complete the oldest pending inline trace for this tool."""
        queue = self._pending_tool_traces.get(tool_name)
        if not queue:
            # Fallback when start event wasn't observed.
            follow = self._is_near_bottom()
            self.mount(
                Static(
                    Text.from_markup(
                        f"[green]• ran[/green] [bold]{tool_name}[/bold]"
                    ),
                    classes="tool-trace-done",
                    id=self._next_id("trf"),
                )
            )
            self._maybe_scroll_end(was_near_bottom=follow)
            return

        follow = self._is_near_bottom()
        trace_id, started_at, widget = queue.pop(0)
        if not queue:
            self._pending_tool_traces.pop(tool_name, None)

        elapsed_ms = int((perf_counter() - started_at) * 1000)
        widget.update(
            Text.from_markup(
                f"[dim]• ran tool #{trace_id}[/dim] [bold]{tool_name}[/bold] "
                f"[dim]({elapsed_ms}ms)[/dim]"
            )
        )
        widget.remove_class("tool-trace-running")
        widget.add_class("tool-trace-done")
        self._maybe_scroll_end(was_near_bottom=follow)

    def add_notification(self, text: str, *, style: str = "yellow") -> None:
        """Display an inline notification in the chat log."""
        follow = self._is_near_bottom()
        self.mount(
            Static(
                Text.from_markup(f"[{style}]{text}[/{style}]"),
                classes="notification",
                id=self._next_id("nt"),
            )
        )
        self._maybe_scroll_end(was_near_bottom=follow)

    def add_welcome_banner(self, *, model: str = "", session_id: str | None = None) -> None:
        """Backward-compat alias for compact startup note."""
        self.add_startup_note(model=model, session_id=session_id)

    def add_startup_note(self, *, model: str = "", session_id: str | None = None) -> None:
        """Render a single-line context note with key shortcuts."""
        follow = self._is_near_bottom()
        short_session = (session_id or "")[:12] if session_id else "ephemeral"
        model_text = model or "default model"
        self.mount(
            Static(
                Text.from_markup(
                    f"[dim]agnoclaw ready  ·  session {short_session}  ·  "
                    f"model {model_text}  ·  ? shortcuts[/dim]"
                ),
                classes="startup-note",
                id=self._next_id("sn"),
            )
        )
        self._maybe_scroll_end(was_near_bottom=follow)

    def add_error(self, error: str) -> None:
        """Display an error message."""
        follow = self._is_near_bottom()
        self.mount(
            Static(
                Text.from_markup(f"[bold]Error:[/bold] {error}"),
                classes="error-text",
                id=self._next_id("er"),
            )
        )
        self._maybe_scroll_end(was_near_bottom=follow)

    def clear_log(self) -> None:
        """Clear all content from the chat log."""
        self.query("Static").remove()
        self._streaming_widget = None
        self._working_widget = None
        self._streaming_text.clear()
        self._is_streaming = False
        self._pending_tool_traces.clear()
