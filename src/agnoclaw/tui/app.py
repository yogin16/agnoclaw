"""
AgnoClawApp — main Textual application for agnoclaw TUI.

Single-process architecture: Textual's asyncio event loop hosts the TUI,
HeartbeatDaemon, and agent calls. No threads, no IPC.

Layout:
┌──────────────────────────────────┬──────────────┐
│                                  │ NOTIFICATIONS│
│  ChatLog (RichLog)               │ [HB] alerts  │
│  streaming + tool indicators     │ [cron] jobs  │
├──────────────────────────────────┴──────────────┤
│ working... status line                            │  StatusBar
│ > prompt input                             /skill│  InputBar
│ enter send · / commands · ? shortcuts             │  AssistBar
├─────────────────────────────────────────────────┤
│ empty spacer (only when chat is empty)          │
└─────────────────────────────────────────────────┘
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from .driver import AgentDriver
from .events import (
    CronResult,
    HeartbeatAlert,
    HeartbeatTick,
    SessionCompactionDone,
    SessionCompactionError,
    StreamChunk,
    StreamDone,
    StreamError,
    ToolCallCompleted,
    ToolCallStarted,
    UserSubmitted,
)
from .screens import SkillPickerScreen
from .widgets import (
    AgnoStatusBar,
    ChatLog,
    ComposerAssistBar,
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
    AgnoClawApp {
        background: #0d0d10;
    }
    #layout {
        height: 1fr;
    }
    #main-area {
        height: 1fr;
        min-height: 8;
    }
    AgnoClawApp.empty-chat #main-area {
        height: auto;
        min-height: 5;
    }
    #notif-panel {
        height: 1fr;
        width: 34;
        min-width: 24;
    }
    #composer-wrap {
        height: auto;
        margin-top: 1;
        width: 1fr;
    }
    #empty-spacer {
        height: 1fr;
        display: none;
    }
    AgnoClawApp.empty-chat #empty-spacer {
        display: block;
    }

    /* Visual theme variants (borderless) */
    AgnoClawApp.theme-ocean InputBar:focus {
        background: #1b2229;
    }
    AgnoClawApp.theme-ocean AgnoStatusBar {
        color: #8ecae6;
    }

    AgnoClawApp.theme-sunset InputBar:focus {
        background: #2a211b;
    }
    AgnoClawApp.theme-sunset AgnoStatusBar {
        color: #f4a261;
    }

    AgnoClawApp.theme-mono InputBar:focus {
        background: #212121;
    }
    AgnoClawApp.theme-mono AgnoStatusBar {
        color: #bdbdbd;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("ctrl+n", "toggle_notifications", "Notifications", show=True),
        Binding("ctrl+s", "open_skill_picker", "Skill Picker", show=True),
        Binding("ctrl+t", "cycle_theme", "Theme", show=True),
        Binding("ctrl+l", "toggle_log_viewer", "Debug Log", show=False),
        Binding("f1", "open_help", "Help", show=False),
        Binding("?", "toggle_shortcuts", "Shortcuts", show=True),
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
        self._notifications_visible = False
        self._notifications_unread = 0
        self._compaction_running = False
        self._ui_themes = ("ocean", "sunset", "mono")
        self._active_ui_theme = "ocean"
        self._working_active = False
        self._working_title = ""
        self._working_started_at = 0.0
        self._spinner_frames = ("working   ", "working.  ", "working.. ", "working...")
        self._spinner_idx = 0
        self._startup_context_shown = False

    def compose(self) -> ComposeResult:
        # Get skill names for tab completion
        skill_names = []
        if hasattr(self._agent, "skills"):
            try:
                skill_names = [s["name"] for s in self._agent.skills.list_skills()]
            except Exception:
                pass

        with Vertical(id="layout"):
            with Horizontal(id="main-area"):
                yield ChatLog(id="chat-log")
                yield NotificationPanel(id="notif-panel")
            yield LogViewer(id="log-viewer")
            with Vertical(id="composer-wrap"):
                yield AgnoStatusBar(id="status-bar")
                yield InputBar(skill_names=skill_names, id="input-bar")
                yield ComposerAssistBar(skill_names=skill_names, id="assist-bar")
            yield Static("", id="empty-spacer")

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

        self._apply_ui_theme(self._active_ui_theme, announce=False)
        self.add_class("empty-chat")

        status = self.query_one("#status-bar", AgnoStatusBar)
        assist = self.query_one("#assist-bar", ComposerAssistBar)
        status.set_theme_name(self._active_ui_theme)
        status.set_queued_skill(self._queued_skill)
        assist.set_queued_skill(self._queued_skill)
        assist.update_for_input("")

        # Keep notifications hidden when heartbeat isn't active.
        self._notifications_visible = False
        notif_panel = self.query_one("#notif-panel", NotificationPanel)
        notif_panel.display = self._notifications_visible

        heartbeat_enabled = cfg.heartbeat.enabled and not self._agent.workspace.is_empty_heartbeat()
        status.set_heartbeat_enabled(heartbeat_enabled)
        status.set_notifications_unread(0)

        self._agent_driver.start_heartbeat()
        self.set_interval(0.16, self._tick_working_indicator, pause=False)
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

        # Keep input usable while model is working; don't drop typed text.
        if self._agent_driver.is_streaming or self._compaction_running:
            chat.add_notification("Agent is still working. Wait for completion.", style="yellow")
            input_bar.value = text
            input_bar.focus()
            return

        if not self._startup_context_shown:
            model = (
                getattr(self._agent, "model_name", "")
                or getattr(self._agent, "_resolved_model", "")
                or ""
            )
            chat.add_startup_note(
                model=model,
                session_id=getattr(self._agent, "session_id", None),
            )
            self._startup_context_shown = True

        self.remove_class("empty-chat")
        chat.add_user_message(text)
        chat.start_agent_response()
        status.set_streaming(True)
        self._set_working_state(True, self._infer_working_title(text))

        # Determine skill
        skill = self._queued_skill
        self._queued_skill = None
        self._sync_skill_context()

        # Run agent in background worker
        self.run_worker(
            self._agent_driver.send_message(text, skill=skill),
            name="agent-response",
            exclusive=True,
        )

    def _handle_slash_command(self, text: str) -> None:
        """Process slash commands."""
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        normalized_args = args.strip().lower()

        if cmd in ("/quit", "/exit", "/q"):
            self.exit()
            return

        if cmd in ("/help", "/?"):
            self.action_toggle_shortcuts()
            return

        chat = self.query_one("#chat-log", ChatLog)

        if cmd == "/clear":
            new_session = None
            chat.clear_log()
            self.add_class("empty-chat")
            self._startup_context_shown = False
            self._queued_skill = None
            self._sync_skill_context()
            if hasattr(self._agent, "clear_session_context"):
                new_session = self._agent.clear_session_context()
            if new_session:
                chat.add_notification(
                    f"Session context cleared. New session: {new_session}.",
                    style="dim",
                )
            else:
                chat.add_notification("Session context cleared.", style="dim")
            notif = self.query_one("#notif-panel", NotificationPanel)
            notif.add_system_note("Session context cleared", style="dim")
            return

        if cmd == "/skill":
            if normalized_args in ("list", "ls"):
                self._show_skill_list()
            elif not args:
                self.action_open_skill_picker()
            else:
                skill_name = args.strip().split()[0]
                if hasattr(self._agent, "skills"):
                    names = {s["name"] for s in self._agent.skills.list_skills()}
                    if skill_name in names:
                        self._queued_skill = skill_name
                        self._sync_skill_context()
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

        if cmd == "/skills":
            self._show_skill_list()
            return

        if cmd == "/compact":
            self._start_session_compaction()
            return

        if cmd in ("/notifications", "/notif", "/n"):
            self.action_toggle_notifications()
            return

        if cmd in ("/log", "/logs"):
            self.action_toggle_log_viewer()
            return

        if cmd == "/theme":
            self._handle_theme_command(normalized_args)
            return

        chat.add_notification(f"Unknown command: {cmd}", style="red")

    def _handle_theme_command(self, normalized_args: str) -> None:
        """Handle /theme command variants."""
        chat = self.query_one("#chat-log", ChatLog)
        if not normalized_args or normalized_args in ("list", "ls"):
            chat.add_notification(
                "Themes: ocean, sunset, mono (use /theme NAME or /theme next).",
                style="cyan",
            )
            return

        if normalized_args in ("next", "cycle"):
            self.action_cycle_theme()
            return

        if normalized_args in self._ui_themes:
            self._apply_ui_theme(normalized_args)
            return

        chat.add_notification(f"Unknown theme: {normalized_args}", style="red")

    def _show_skill_list(self) -> None:
        """Render available skills in chat notifications."""
        chat = self.query_one("#chat-log", ChatLog)
        if hasattr(self._agent, "skills"):
            skills = self._agent.skills.list_skills()
            if skills:
                lines = [f"  {s['name']}: {s['description']}" for s in skills]
                chat.add_notification("Available skills:\n" + "\n".join(lines), style="cyan")
            else:
                chat.add_notification("No skills found.", style="dim")

    def _infer_working_title(self, text: str) -> str:
        """Create contextual working title from user prompt."""
        compact = " ".join(text.strip().split())
        if not compact:
            return "thinking"
        words = compact.split()
        preview = " ".join(words[:6])
        if len(words) > 6:
            preview += "..."
        return f"thinking about {preview}"

    def _sync_skill_context(self) -> None:
        """Sync queued skill state on visible status UI."""
        status = self.query_one("#status-bar", AgnoStatusBar)
        assist = self.query_one("#assist-bar", ComposerAssistBar)
        status.set_queued_skill(self._queued_skill)
        assist.set_queued_skill(self._queued_skill)

    def _set_working_state(self, active: bool, title: str = "") -> None:
        """Enable/disable animated working status."""
        self._working_active = active
        if active and title:
            self._working_title = title
            if self._working_started_at <= 0:
                self._working_started_at = perf_counter()
        if not active:
            self._working_started_at = 0.0
            self._working_title = ""
            status = self.query_one("#status-bar", AgnoStatusBar)
            status.set_agent_status("ready")
            return
        self._spinner_idx = 0
        self._render_working_status()

    def _render_working_status(self) -> None:
        """Render the current working status with spinner frame."""
        if not self._working_active:
            return
        frame = self._spinner_frames[self._spinner_idx % len(self._spinner_frames)]
        elapsed_s = int(max(0.0, perf_counter() - self._working_started_at))
        status_text = f"{frame} {elapsed_s}s"
        status = self.query_one("#status-bar", AgnoStatusBar)
        status.set_agent_status(status_text)

    def _tick_working_indicator(self) -> None:
        """Periodic spinner update while working."""
        if not self._working_active:
            return
        self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner_frames)
        self._render_working_status()

    def _apply_ui_theme(self, theme_name: str, *, announce: bool = True) -> None:
        """Apply one of the app's UI theme variants."""
        if theme_name not in self._ui_themes:
            return

        for name in self._ui_themes:
            self.remove_class(f"theme-{name}")
        self.add_class(f"theme-{theme_name}")
        self._active_ui_theme = theme_name

        status = self.query_one("#status-bar", AgnoStatusBar)
        status.set_theme_name(theme_name)

        if announce:
            chat = self.query_one("#chat-log", ChatLog)
            notif = self.query_one("#notif-panel", NotificationPanel)
            chat.add_notification(f"Theme switched to: {theme_name}", style="cyan")
            notif.add_system_note(f"Theme: {theme_name}", style="cyan")

    def _start_session_compaction(self) -> None:
        """Run session compaction in a background worker."""
        chat = self.query_one("#chat-log", ChatLog)
        input_bar = self.query_one("#input-bar", InputBar)
        status = self.query_one("#status-bar", AgnoStatusBar)

        if self._compaction_running:
            chat.add_notification("Session compaction is already running.", style="yellow")
            return

        if not hasattr(self._agent, "compact_session"):
            chat.add_notification("Session compaction is not supported.", style="yellow")
            return

        self._compaction_running = True
        input_bar.set_disabled(True)
        status.set_compacting(True)
        self._set_working_state(True, "compacting session")
        chat.add_notification("Compacting session...", style="cyan")
        self.run_worker(
            self._compact_session_worker(),
            name="session-compaction",
            exclusive=True,
        )

    async def _compact_session_worker(self) -> None:
        """Background worker for session compaction."""
        try:
            await self._agent.compact_session()
            self.post_message(SessionCompactionDone())
        except Exception as exc:
            logger.exception("Session compaction failed")
            self.post_message(SessionCompactionError(str(exc)))

    # ── Stream event handlers ─────────────────────────────────────────────────

    def on_stream_chunk(self, event: StreamChunk) -> None:
        chat = self.query_one("#chat-log", ChatLog)
        chat.append_chunk(event.text)
        if self._working_active and self._working_title.startswith("thinking"):
            self._working_title = "writing response"

    def on_stream_done(self, event: StreamDone) -> None:
        chat = self.query_one("#chat-log", ChatLog)
        input_bar = self.query_one("#input-bar", InputBar)
        status = self.query_one("#status-bar", AgnoStatusBar)

        chat.finish_agent_response(event.full_text)
        status.set_streaming(False)
        status.update_tool_count(self._agent_driver.tool_count)
        self._set_working_state(False)
        input_bar.focus()
        log = self.query_one("#log-viewer", LogViewer)
        log.log_event("stream.done", f"{len(event.full_text)} chars")

    def on_stream_error(self, event: StreamError) -> None:
        chat = self.query_one("#chat-log", ChatLog)
        input_bar = self.query_one("#input-bar", InputBar)
        status = self.query_one("#status-bar", AgnoStatusBar)

        chat.add_error(event.error)
        elapsed_s = int(max(0.0, perf_counter() - self._working_started_at))
        chat.finish_working(success=False, elapsed_s=elapsed_s)
        status.set_streaming(False)
        self._set_working_state(False)
        input_bar.focus()
        log = self.query_one("#log-viewer", LogViewer)
        log.log_error(event.error)

    def on_session_compaction_done(self, event: SessionCompactionDone) -> None:
        """Handle successful session compaction."""
        del event
        self._compaction_running = False
        chat = self.query_one("#chat-log", ChatLog)
        input_bar = self.query_one("#input-bar", InputBar)
        status = self.query_one("#status-bar", AgnoStatusBar)
        log = self.query_one("#log-viewer", LogViewer)

        input_bar.set_disabled(False)
        status.set_compacting(False)
        self._set_working_state(False)
        chat.add_notification("Session compaction complete.", style="green")
        log.log_event("session.compact.done")

    def on_session_compaction_error(self, event: SessionCompactionError) -> None:
        """Handle failed session compaction."""
        self._compaction_running = False
        chat = self.query_one("#chat-log", ChatLog)
        input_bar = self.query_one("#input-bar", InputBar)
        status = self.query_one("#status-bar", AgnoStatusBar)
        log = self.query_one("#log-viewer", LogViewer)

        input_bar.set_disabled(False)
        status.set_compacting(False)
        elapsed_s = int(max(0.0, perf_counter() - self._working_started_at))
        chat.finish_working(success=False, elapsed_s=elapsed_s)
        self._set_working_state(False)
        chat.add_error(f"Session compaction failed: {event.error}")
        log.log_error(event.error)

    # ── Tool call indicators ──────────────────────────────────────────────────

    def on_tool_call_started(self, event: ToolCallStarted) -> None:
        chat = self.query_one("#chat-log", ChatLog)
        chat.start_tool_trace(event.tool_name)
        status = self.query_one("#status-bar", AgnoStatusBar)
        status.update_tool_count(self._agent_driver.tool_count)
        self._set_working_state(True, f"running tool {event.tool_name}")
        log = self.query_one("#log-viewer", LogViewer)
        log.log_tool_call(event.tool_name, started=True)

    def on_tool_call_completed(self, event: ToolCallCompleted) -> None:
        chat = self.query_one("#chat-log", ChatLog)
        chat.finish_tool_trace(event.tool_name)
        if self._working_active:
            self._working_title = "thinking after tools"
        log = self.query_one("#log-viewer", LogViewer)
        log.log_tool_call(event.tool_name, started=False)

    # ── Heartbeat events ──────────────────────────────────────────────────────

    def on_heartbeat_alert(self, event: HeartbeatAlert) -> None:
        notif = self.query_one("#notif-panel", NotificationPanel)
        notif.add_heartbeat_alert(event.alert_text)
        self._notifications_unread += 1
        status = self.query_one("#status-bar", AgnoStatusBar)
        status.set_notifications_unread(self._notifications_unread)

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
        self._notifications_unread += 1
        status = self.query_one("#status-bar", AgnoStatusBar)
        status.set_notifications_unread(self._notifications_unread)
        chat = self.query_one("#chat-log", ChatLog)
        chat.add_notification(f"Cron {event.job_name}: {event.text[:100]}", style="cyan")

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_toggle_notifications(self) -> None:
        """Toggle the notification panel visibility."""
        panel = self.query_one("#notif-panel", NotificationPanel)
        self._notifications_visible = not self._notifications_visible
        panel.display = self._notifications_visible
        if self._notifications_visible:
            self._notifications_unread = 0
            status = self.query_one("#status-bar", AgnoStatusBar)
            status.set_notifications_unread(0)
        chat = self.query_one("#chat-log", ChatLog)
        state = "shown" if self._notifications_visible else "hidden"
        chat.add_notification(f"Notifications panel {state}.", style="dim")

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
                self._sync_skill_context()
                chat = self.query_one("#chat-log", ChatLog)
                chat.add_notification(f"Skill queued: {skill_name}", style="green")

        self.push_screen(SkillPickerScreen(skills), on_skill_selected)

    def action_cycle_theme(self) -> None:
        """Cycle through built-in UI themes."""
        idx = self._ui_themes.index(self._active_ui_theme)
        next_idx = (idx + 1) % len(self._ui_themes)
        self._apply_ui_theme(self._ui_themes[next_idx])

    def action_open_help(self) -> None:
        """Legacy help action now toggles inline shortcut guidance."""
        self.action_toggle_shortcuts()

    def action_toggle_shortcuts(self) -> None:
        """Toggle inline shortcuts row beneath the composer."""
        assist = self.query_one("#assist-bar", ComposerAssistBar)
        assist.toggle_shortcuts()

    def update_composer_assist(self, value: str) -> None:
        """Update inline assist row from current input value."""
        assist = self.query_one("#assist-bar", ComposerAssistBar)
        assist.update_for_input(value)

    def action_toggle_log_viewer(self) -> None:
        """Toggle the debug log viewer panel."""
        log = self.query_one("#log-viewer", LogViewer)
        log.toggle_visible()

    def action_quit(self) -> None:
        """Clean up and exit."""
        self._agent_driver.stop_heartbeat()
        self.exit()
