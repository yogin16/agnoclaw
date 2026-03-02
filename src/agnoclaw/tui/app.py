"""
AgnoClawApp — main Textual application for agnoclaw TUI.

Single-process architecture: Textual's asyncio event loop hosts the TUI,
HeartbeatDaemon, and agent calls. No threads, no IPC.

Layout:
┌─────────────────────────────────────────────────┐
│ agnoclaw · claude-sonnet-4-6 · session:abc      │  HeaderBar
├──────────────────────────────────┬──────────────┤
│                                  │ NOTIFICATIONS│
│  ChatLog (RichLog)               │ [HB] alerts  │
│  streaming + tool indicators     │ [cron] jobs  │
├──────────────────────────────────┴──────────────┤
│ > prompt input                             /skill│  InputBar
├─────────────────────────────────────────────────┤
│ ● heartbeat: 28m │ tools: 6 │ ready             │  StatusBar
└─────────────────────────────────────────────────┘
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal

from .driver import AgentDriver
from .events import (
    CronResult,
    HeartbeatAlert,
    HeartbeatTick,
    StreamChunk,
    StreamDone,
    StreamError,
    ToolCallCompleted,
    ToolCallStarted,
    UserSubmitted,
)
from .screens import HelpScreen, SkillPickerScreen
from .widgets import (
    AgnoStatusBar,
    ChatLog,
    HeaderBar,
    InputBar,
    NotificationPanel,
)
from .widgets.log_viewer import LogViewer

if TYPE_CHECKING:
    from agnoclaw.agent import AgentHarness

logger = logging.getLogger("agnoclaw.tui.app")


class AgnoClawApp(App):
    """Textual TUI for agnoclaw — personal assistant mode."""

    TITLE = "agnoclaw"
    CSS = """
    #main-area {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("ctrl+n", "toggle_notifications", "Notifications", show=True),
        Binding("ctrl+s", "open_skill_picker", "Skill Picker", show=True),
        Binding("ctrl+l", "toggle_log_viewer", "Debug Log", show=False),
    ]

    def __init__(
        self,
        *,
        agent: AgentHarness,
        debug: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._agent = agent
        self._debug = debug
        self._agent_driver = AgentDriver(self, agent)
        self._queued_skill: str | None = None
        self._notifications_visible = True

    def compose(self) -> ComposeResult:
        # Determine display values
        model = getattr(self._agent, "_resolved_model", "") or ""
        session_id = getattr(self._agent, "session_id", None)

        # Get skill names for tab completion
        skill_names = []
        if hasattr(self._agent, "skills"):
            try:
                skill_names = [s["name"] for s in self._agent.skills.list_skills()]
            except Exception:
                pass

        yield HeaderBar(model=model, session_id=session_id)
        with Horizontal(id="main-area"):
            yield ChatLog(id="chat-log")
            yield NotificationPanel(id="notif-panel")
        yield LogViewer(id="log-viewer")
        yield InputBar(skill_names=skill_names, id="input-bar")
        yield AgnoStatusBar(id="status-bar")

    def on_mount(self) -> None:
        """Start heartbeat, apply theme, and focus input."""
        # Apply theme from config
        from agnoclaw.config import get_config

        cfg = get_config()
        if cfg.theme and cfg.theme != "textual-dark":
            try:
                self.theme = cfg.theme
            except Exception:
                pass  # Fall back to default theme

        self._agent_driver.start_heartbeat()
        self.query_one("#input-bar", InputBar).focus()

    # ── User input handling ───────────────────────────────────────────────────

    def on_user_submitted(self, event: UserSubmitted) -> None:
        """Handle user message or slash command."""
        text = event.text

        # Slash commands
        if text.startswith("/"):
            self._handle_slash_command(text)
            return

        # Regular message
        chat = self.query_one("#chat-log", ChatLog)
        input_bar = self.query_one("#input-bar", InputBar)
        status = self.query_one("#status-bar", AgnoStatusBar)

        chat.add_user_message(text)
        chat.start_agent_response()
        input_bar.set_disabled(True)
        status.set_streaming(True)

        # Determine skill
        skill = self._queued_skill
        self._queued_skill = None

        # Run agent in background worker
        self.run_worker(
            self._agent_driver.send_message(text, skill=skill),
            name="agent-response",
            exclusive=True,
        )

    def _handle_slash_command(self, text: str) -> None:
        """Process slash commands."""
        chat = self.query_one("#chat-log", ChatLog)
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd in ("/quit", "/exit", "/q"):
            self.exit()
            return

        if cmd == "/help":
            self.push_screen(HelpScreen())
            return

        if cmd == "/clear":
            chat.clear_log()
            if hasattr(self._agent, "clear_session_context"):
                self._agent.clear_session_context()
            chat.add_notification("Session context cleared.", style="dim")
            return

        if cmd == "/skill":
            if not args:
                self.action_open_skill_picker()
            else:
                skill_name = args.strip().split()[0]
                if hasattr(self._agent, "skills"):
                    names = {s["name"] for s in self._agent.skills.list_skills()}
                    if skill_name in names:
                        self._queued_skill = skill_name
                        chat.add_notification(
                            f"Skill queued: {skill_name}", style="green"
                        )
                    else:
                        chat.add_notification(
                            f"Skill not found: {skill_name}", style="red"
                        )
                else:
                    chat.add_notification("No skills available.", style="yellow")
            return

        if cmd in ("/skills", "/skill list"):
            if hasattr(self._agent, "skills"):
                skills = self._agent.skills.list_skills()
                if skills:
                    lines = [f"  {s['name']}: {s['description']}" for s in skills]
                    chat.add_notification(
                        "Available skills:\n" + "\n".join(lines), style="cyan"
                    )
                else:
                    chat.add_notification("No skills found.", style="dim")
            return

        if cmd == "/compact":
            chat.add_notification("Session compaction not yet implemented.", style="yellow")
            return

        chat.add_notification(f"Unknown command: {cmd}", style="red")

    # ── Stream event handlers ─────────────────────────────────────────────────

    def on_stream_chunk(self, event: StreamChunk) -> None:
        chat = self.query_one("#chat-log", ChatLog)
        chat.append_chunk(event.text)

    def on_stream_done(self, event: StreamDone) -> None:
        chat = self.query_one("#chat-log", ChatLog)
        input_bar = self.query_one("#input-bar", InputBar)
        status = self.query_one("#status-bar", AgnoStatusBar)

        chat.finish_agent_response(event.full_text)
        input_bar.set_disabled(False)
        status.set_streaming(False)
        status.update_tool_count(self._agent_driver.tool_count)

    def on_stream_error(self, event: StreamError) -> None:
        chat = self.query_one("#chat-log", ChatLog)
        input_bar = self.query_one("#input-bar", InputBar)
        status = self.query_one("#status-bar", AgnoStatusBar)

        chat.add_error(event.error)
        input_bar.set_disabled(False)
        status.set_streaming(False)

    # ── Tool call indicators ──────────────────────────────────────────────────

    def on_tool_call_started(self, event: ToolCallStarted) -> None:
        chat = self.query_one("#chat-log", ChatLog)
        chat.add_tool_indicator(event.tool_name, done=False)
        log = self.query_one("#log-viewer", LogViewer)
        log.log_tool_call(event.tool_name, started=True)

    def on_tool_call_completed(self, event: ToolCallCompleted) -> None:
        chat = self.query_one("#chat-log", ChatLog)
        chat.add_tool_indicator(event.tool_name, done=True)
        log = self.query_one("#log-viewer", LogViewer)
        log.log_tool_call(event.tool_name, started=False)

    # ── Heartbeat events ──────────────────────────────────────────────────────

    def on_heartbeat_alert(self, event: HeartbeatAlert) -> None:
        notif = self.query_one("#notif-panel", NotificationPanel)
        notif.add_heartbeat_alert(event.alert_text)

        # Also flash in chat log
        chat = self.query_one("#chat-log", ChatLog)
        chat.add_notification(f"Heartbeat: {event.alert_text[:100]}")

        # Bell notification
        self.bell()

    def on_heartbeat_tick(self, event: HeartbeatTick) -> None:
        status = self.query_one("#status-bar", AgnoStatusBar)
        status.update_heartbeat(event.minutes)

    # ── Cron events ───────────────────────────────────────────────────────────

    def on_cron_result(self, event: CronResult) -> None:
        notif = self.query_one("#notif-panel", NotificationPanel)
        notif.add_cron_result(event.job_name, event.text)

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_toggle_notifications(self) -> None:
        """Toggle the notification panel visibility."""
        panel = self.query_one("#notif-panel", NotificationPanel)
        self._notifications_visible = not self._notifications_visible
        panel.display = self._notifications_visible

    def action_open_skill_picker(self) -> None:
        """Open the skill picker modal."""
        if not hasattr(self._agent, "skills"):
            return

        skills = self._agent.skills.list_skills()
        if not skills:
            chat = self.query_one("#chat-log", ChatLog)
            chat.add_notification("No skills available.", style="yellow")
            return

        def on_skill_selected(skill_name: str) -> None:
            if skill_name:
                self._queued_skill = skill_name
                chat = self.query_one("#chat-log", ChatLog)
                chat.add_notification(f"Skill queued: {skill_name}", style="green")

        self.push_screen(SkillPickerScreen(skills), on_skill_selected)

    def action_toggle_log_viewer(self) -> None:
        """Toggle the debug log viewer panel."""
        log = self.query_one("#log-viewer", LogViewer)
        log.toggle_visible()

    def action_quit(self) -> None:
        """Clean up and exit."""
        self._agent_driver.stop_heartbeat()
        self.exit()
