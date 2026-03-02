"""
Async REPL for agnoclaw — replaces blocking click.prompt() with prompt_toolkit
async prompt. HeartbeatDaemon runs in-process on the same asyncio loop, enabling
proactive notifications that print above the prompt while the user is typing.

Usage:
    from agnoclaw.cli.async_repl import AsyncREPL

    agent = AgentHarness(...)
    repl = AsyncREPL(agent, enable_heartbeat=True)
    asyncio.run(repl.run())
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout
    from rich.console import Console
    from rich.panel import Panel
except ImportError as e:
    raise ImportError(
        "CLI dependencies not installed. Install with: pip install agnoclaw[cli]"
    ) from e

if TYPE_CHECKING:
    from agnoclaw.agent import AgentHarness

logger = logging.getLogger("agnoclaw.cli.async_repl")


class AsyncREPL:
    """
    Async REPL with prompt_toolkit, supporting in-process heartbeat notifications.

    Features:
    - Non-blocking prompt via prompt_toolkit.PromptSession.prompt_async()
    - HeartbeatDaemon runs as asyncio tasks on the same loop
    - Notifications print above the prompt line via patch_stdout()
    - Slash command support (/skill, /skills, /clear, /help, /quit)
    """

    def __init__(
        self,
        agent: AgentHarness,
        *,
        enable_heartbeat: bool = True,
        debug: bool = False,
    ):
        self._agent = agent
        self._enable_heartbeat = enable_heartbeat
        self._debug = debug
        self._console = Console()
        self._session = PromptSession()
        self._notification_queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued_skill: str | None = None
        self._daemon = None

    async def run(self) -> None:
        """Main REPL loop — runs until /quit or Ctrl+C."""
        if self._enable_heartbeat:
            self._start_heartbeat()

        # Background task to print notifications above prompt
        notif_task = asyncio.create_task(
            self._notification_printer(), name="agnoclaw-notif-printer"
        )

        self._console.print(
            Panel(
                f"[bold cyan]agnoclaw[/bold cyan] — async session\n"
                f"Workspace: [dim]{self._agent.workspace.path}[/dim]\n"
                f"Type [bold]/quit[/bold] or [bold]Ctrl+C[/bold] to exit. "
                f"[bold]/skill <name>[/bold] to activate a skill. "
                f"[bold]/clear[/bold] to reset session."
                + (
                    "\n[dim]Heartbeat: active[/dim]"
                    if self._enable_heartbeat and self._daemon
                    else ""
                ),
                border_style="cyan",
            )
        )

        try:
            with patch_stdout():
                while True:
                    try:
                        user_input = await self._session.prompt_async("\n[you] > ")
                    except (EOFError, KeyboardInterrupt):
                        self._console.print("\n[dim]Goodbye.[/dim]")
                        break

                    if not user_input.strip():
                        continue

                    # Handle slash commands
                    if user_input.strip().startswith("/"):
                        if user_input.strip() in ("/quit", "/exit", "/q"):
                            self._console.print("[dim]Goodbye.[/dim]")
                            break

                        from agnoclaw.cli.main import _handle_slash_command

                        handled, self._queued_skill = _handle_slash_command(
                            user_input.strip(), self._agent, self._queued_skill
                        )
                        if handled:
                            continue

                    # Extract skill activation
                    active_skill = None
                    if "--skill" in user_input:
                        parts = user_input.split("--skill", 1)
                        user_input = parts[0].strip()
                        active_skill = (
                            parts[1].strip().split()[0] if parts[1].strip() else None
                        )
                    elif self._queued_skill:
                        active_skill = self._queued_skill
                        self._queued_skill = None

                    await self._stream_response(user_input, skill=active_skill)
        finally:
            notif_task.cancel()
            if self._daemon:
                self._daemon.stop()

    async def _stream_response(
        self, message: str, *, skill: str | None = None
    ) -> None:
        """Stream agent response token-by-token."""
        self._console.print("\n[bold green][agent][/bold green]")

        try:
            response = await self._agent.arun(message, stream=True, skill=skill)

            # Stream events
            async for event in response:
                content = self._agent._extract_event_content(event)
                if content:
                    print(content, end="", flush=True)

                # Show tool call indicators
                event_type = self._agent._map_agno_event_type(event)
                if event_type == "tool.call.started":
                    tool_name = getattr(event, "tool_name", "tool")
                    self._console.print(
                        f"\n  [dim]→ {tool_name}...[/dim]", end=""
                    )
                elif event_type == "tool.call.completed":
                    self._console.print(" [dim]done[/dim]")

            print()  # newline after stream
        except KeyboardInterrupt:
            self._console.print("\n[dim](interrupted)[/dim]")
        except Exception as e:
            self._console.print(f"\n[red][error][/red] {e}")
            if self._debug:
                import traceback

                traceback.print_exc()

    def _start_heartbeat(self) -> None:
        """Start HeartbeatDaemon on the current asyncio loop."""
        from agnoclaw.config import get_config

        cfg = get_config()
        if not cfg.heartbeat.enabled:
            logger.debug("Heartbeat disabled in config — skipping")
            return

        if self._agent.workspace.is_empty_heartbeat():
            logger.debug("HEARTBEAT.md empty — skipping heartbeat")
            return

        from agnoclaw.heartbeat import HeartbeatDaemon

        def on_alert(msg: str) -> None:
            """Push alert to notification queue for async printing."""
            self._notification_queue.put_nowait(msg)

        self._daemon = HeartbeatDaemon(self._agent, on_alert=on_alert, config=cfg)
        self._daemon.start()
        logger.info(
            "Heartbeat started (interval=%dm)",
            cfg.heartbeat.interval_minutes,
        )

    async def _notification_printer(self) -> None:
        """Background task: pulls from queue, prints above prompt via patch_stdout."""
        while True:
            try:
                msg = await self._notification_queue.get()
                self._console.print(
                    Panel(
                        msg,
                        title="[yellow]Heartbeat Alert[/yellow]",
                        border_style="yellow",
                    )
                )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Notification printer error")
