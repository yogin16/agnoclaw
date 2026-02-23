"""
Heartbeat daemon — periodic agent check-ins.

Inspired by OpenClaw's heartbeat system. Runs the agent on a schedule,
asking it to check HEARTBEAT.md and surface anything that needs attention.

Protocol:
  - Every N minutes (default: 30), send the agent a heartbeat prompt
  - Agent reads HEARTBEAT.md and decides if anything needs attention
  - If nothing needs attention: agent replies with text containing HEARTBEAT_OK
  - HEARTBEAT_OK responses under ok_threshold_chars are silently suppressed
  - Responses with real content are delivered via the configured callback
  - Active hours restrict when heartbeats fire (e.g. 08:00-22:00)

Usage:
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw import HarnessAgent

    agent = HarnessAgent()
    daemon = HeartbeatDaemon(agent, on_alert=print)
    daemon.start()
    # runs in background until daemon.stop()
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from typing import Callable, Optional

from agnoclaw.config import HarnessConfig, get_config
from agnoclaw.workspace import Workspace

logger = logging.getLogger("agnoclaw.heartbeat")

HEARTBEAT_PROMPT = """Read HEARTBEAT.md in your workspace if it exists.
Check each item in the checklist and determine if anything needs immediate attention.
If nothing needs attention, reply with HEARTBEAT_OK (and nothing else, or very brief context).
If something does need attention, describe it clearly so the user can act."""

HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK"


class HeartbeatDaemon:
    """
    Asyncio-based heartbeat scheduler.

    Args:
        agent: The HarnessAgent to run heartbeats on.
        on_alert: Callback called with the alert message when something needs attention.
                  Signature: on_alert(message: str) -> None
        config: HarnessConfig. Loaded from env/TOML if not provided.
        workspace: Workspace instance. Shared with the agent if not provided.
    """

    def __init__(
        self,
        agent,
        on_alert: Optional[Callable[[str], None]] = None,
        config: Optional[HarnessConfig] = None,
        workspace: Optional[Workspace] = None,
    ):
        self._agent = agent
        self._on_alert = on_alert or self._default_alert
        self._config = config or get_config()
        self._workspace = workspace or (agent.workspace if hasattr(agent, "workspace") else Workspace())
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self) -> None:
        """Start the heartbeat daemon (creates an asyncio task)."""
        if self._running:
            logger.warning("Heartbeat daemon already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="agnoclaw-heartbeat")
        logger.info(
            "Heartbeat daemon started (interval=%dm, active=%s-%s)",
            self._config.heartbeat.interval_minutes,
            self._config.heartbeat.active_hours_start,
            self._config.heartbeat.active_hours_end,
        )

    def stop(self) -> None:
        """Stop the heartbeat daemon."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Heartbeat daemon stopped")

    async def trigger_now(self) -> Optional[str]:
        """
        Manually trigger a heartbeat run immediately.

        Returns:
            The alert message if something needs attention, or None if HEARTBEAT_OK.
        """
        return await self._run_heartbeat()

    async def _run_loop(self) -> None:
        """Main heartbeat loop — sleeps between runs."""
        interval_seconds = self._config.heartbeat.interval_minutes * 60

        while self._running:
            if self._is_active_hours():
                result = await self._run_heartbeat()
                if result:
                    self._on_alert(result)
            else:
                logger.debug("Outside active hours — skipping heartbeat")

            await asyncio.sleep(interval_seconds)

    async def _run_heartbeat(self) -> Optional[str]:
        """
        Execute one heartbeat run.

        Returns:
            Alert message if attention needed, None if HEARTBEAT_OK or suppressed.
        """
        # Skip if HEARTBEAT.md is empty / no actionable content
        if self._workspace.is_empty_heartbeat():
            logger.debug("HEARTBEAT.md is empty — skipping run")
            return None

        heartbeat_content = self._workspace.heartbeat_md() or ""
        prompt = HEARTBEAT_PROMPT
        if heartbeat_content:
            prompt = f"{HEARTBEAT_PROMPT}\n\nYour HEARTBEAT.md:\n{heartbeat_content}"

        try:
            # Use cheaper model for heartbeat if configured
            heartbeat_model = self._config.heartbeat.model
            agent_model = getattr(self._agent, "_model", None)

            # Temporarily use cheaper model for heartbeat if different from main
            if heartbeat_model and hasattr(agent_model, "id") and agent_model.id != heartbeat_model:
                # Create a lightweight agent for the heartbeat check
                from agno.agent import Agent
                from agno.models.anthropic import Claude

                hb_agent = Agent(
                    model=Claude(id=heartbeat_model),
                    instructions="You are a heartbeat monitor. Check the checklist and report any issues.",
                )
                response = hb_agent.run(prompt)
            else:
                response = await self._agent.arun(prompt)

            content = str(response.content) if response and response.content else ""

            # Check for HEARTBEAT_OK suppression
            if HEARTBEAT_OK_TOKEN in content:
                if len(content) <= self._config.heartbeat.ok_threshold_chars:
                    logger.debug("HEARTBEAT_OK — no action needed")
                    return None
                # HEARTBEAT_OK present but response is long — something needs attention
                # Strip the token and surface the rest
                content = content.replace(HEARTBEAT_OK_TOKEN, "").strip()
                if not content:
                    return None

            return content if content else None

        except Exception as e:
            logger.error("Heartbeat run failed: %s", e)
            return None

    def _is_active_hours(self) -> bool:
        """Return True if current time is within the configured active hours."""
        now = datetime.now().time()

        try:
            start_h, start_m = map(int, self._config.heartbeat.active_hours_start.split(":"))
            end_h, end_m = map(int, self._config.heartbeat.active_hours_end.split(":"))
            start = time(start_h, start_m)
            end = time(end_h, end_m)
        except (ValueError, AttributeError):
            return True  # If parsing fails, always active

        if start <= end:
            return start <= now <= end
        else:
            # Overnight range (e.g. 22:00 - 06:00)
            return now >= start or now <= end

    @staticmethod
    def _default_alert(message: str) -> None:
        """Default alert handler — print to console."""
        print(f"\n[HEARTBEAT ALERT]\n{message}\n")
