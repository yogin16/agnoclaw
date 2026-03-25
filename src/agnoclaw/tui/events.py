"""
Custom Textual Messages for the agnoclaw TUI.

These messages flow from AgentDriver (background workers) to the App and widgets,
enabling reactive UI updates for streaming, tool calls, and heartbeat alerts.
"""

from __future__ import annotations

from textual.message import Message

# ── Streaming ─────────────────────────────────────────────────────────────────


class StreamChunk(Message):
    """A chunk of text from the agent's streaming response."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class StreamDone(Message):
    """Agent finished streaming its response."""

    def __init__(self, full_text: str = "") -> None:
        super().__init__()
        self.full_text = full_text


class StreamError(Message):
    """An error occurred during agent response."""

    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error


# ── Tool calls ────────────────────────────────────────────────────────────────


class ToolCallStarted(Message):
    """A tool call has begun."""

    def __init__(self, tool_name: str, display_name: str | None = None) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.display_name = display_name or tool_name


class ToolCallCompleted(Message):
    """A tool call has finished."""

    def __init__(self, tool_name: str, display_name: str | None = None) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.display_name = display_name or tool_name


# ── Heartbeat ─────────────────────────────────────────────────────────────────


class HeartbeatAlert(Message):
    """Heartbeat detected something that needs attention."""

    def __init__(self, alert_text: str) -> None:
        super().__init__()
        self.alert_text = alert_text


class HeartbeatTick(Message):
    """Periodic tick with minutes since last heartbeat run."""

    def __init__(self, minutes: int) -> None:
        super().__init__()
        self.minutes = minutes


# ── Cron ──────────────────────────────────────────────────────────────────────


class CronResult(Message):
    """A cron job produced output."""

    def __init__(self, job_name: str, text: str) -> None:
        super().__init__()
        self.job_name = job_name
        self.text = text


# ── User input ────────────────────────────────────────────────────────────────


class UserSubmitted(Message):
    """User submitted a message from the input bar."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text
