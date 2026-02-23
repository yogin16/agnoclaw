"""
Heartbeat daemon + cron scheduler for agnoclaw.

Inspired by OpenClaw's Gateway CronManager. Supports two scheduling modes:

  1. Heartbeat (interval-based): fires every N minutes, runs in the main agent's
     session. Best for context-aware monitoring. HEARTBEAT_OK suppression.

  2. Cron jobs (expression-based): fires at precise times using standard cron
     expressions. Can run in the main session or an isolated session.

OpenClaw distinction:
  - Heartbeat runs inside the existing agent session (full conversational context)
  - Cron can be isolated (fresh session, clean slate) or main (enqueued as event)

Process persistence:
  Use `agnoclaw heartbeat install-service` to register this as a launchd (macOS)
  or systemd (Linux) user service for always-on operation beyond terminal lifetime.

Usage:
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon, CronJob
    from agnoclaw import HarnessAgent

    agent = HarnessAgent()

    # Simple interval heartbeat
    daemon = HeartbeatDaemon(agent, on_alert=print)
    daemon.start()

    # With a cron job (daily standup at 9am)
    daemon.add_cron_job(CronJob(
        name="daily-standup",
        schedule="0 9 * * 1-5",  # 9am, Mon-Fri
        prompt="Run the daily-standup skill.",
        skill="daily-standup",
        isolated=True,
    ))
    daemon.start()
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
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


@dataclass
class CronJob:
    """
    A scheduled agent task.

    Args:
        name: Unique identifier for this job.
        schedule: Cron expression (e.g. '0 9 * * 1-5') or interval string
                  ('30m', '1h', '6h'). Use '*/5 * * * *' for every 5 minutes.
        prompt: The message to send to the agent when the job fires.
        skill: Optional skill name to activate for this run.
        isolated: If True, runs in a fresh isolated session (clean slate).
                  If False (default), runs in the main agent's session.
        model_id: Optional model override for this job (e.g. 'claude-haiku-4-5-20251001').
        provider: Optional provider override for this job.
        enabled: Set to False to disable without removing.
    """

    name: str
    schedule: str
    prompt: str
    skill: Optional[str] = None
    isolated: bool = False
    model_id: Optional[str] = None
    provider: Optional[str] = None
    enabled: bool = True
    _next_run: Optional[datetime] = field(default=None, repr=False, compare=False)


class HeartbeatDaemon:
    """
    Asyncio-based heartbeat scheduler with optional cron job support.

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
        self._cron_tasks: list[asyncio.Task] = []
        self._running = False
        self._cron_jobs: list[CronJob] = []

    def add_cron_job(self, job: CronJob) -> None:
        """Add a cron job to run alongside the heartbeat."""
        self._cron_jobs.append(job)
        logger.info("Registered cron job '%s' (schedule=%s, isolated=%s)", job.name, job.schedule, job.isolated)

    def start(self) -> None:
        """Start the heartbeat daemon and any registered cron jobs."""
        if self._running:
            logger.warning("Heartbeat daemon already running")
            return
        self._running = True

        # Start main heartbeat loop
        self._task = asyncio.create_task(self._run_heartbeat_loop(), name="agnoclaw-heartbeat")

        # Start each cron job
        for job in self._cron_jobs:
            if job.enabled:
                task = asyncio.create_task(self._run_cron_loop(job), name=f"agnoclaw-cron-{job.name}")
                self._cron_tasks.append(task)

        logger.info(
            "Heartbeat daemon started (interval=%dm, active=%s-%s, cron_jobs=%d)",
            self._config.heartbeat.interval_minutes,
            self._config.heartbeat.active_hours_start,
            self._config.heartbeat.active_hours_end,
            len(self._cron_jobs),
        )

    def stop(self) -> None:
        """Stop the heartbeat daemon and all cron jobs."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        for task in self._cron_tasks:
            if not task.done():
                task.cancel()
        self._cron_tasks.clear()
        logger.info("Heartbeat daemon stopped")

    async def trigger_now(self) -> Optional[str]:
        """
        Manually trigger a heartbeat run immediately.

        Returns:
            The alert message if something needs attention, or None if HEARTBEAT_OK.
        """
        return await self._run_heartbeat()

    async def trigger_cron(self, job_name: str) -> Optional[str]:
        """
        Manually trigger a named cron job immediately.

        Returns:
            The job response, or None if nothing to report.
        """
        for job in self._cron_jobs:
            if job.name == job_name:
                return await self._run_cron_job(job)
        return f"[error] Cron job '{job_name}' not found."

    # ── Heartbeat loop ─────────────────────────────────────────────────────────

    async def _run_heartbeat_loop(self) -> None:
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
        Execute one heartbeat run on the main agent's session.

        Unlike older approach of spawning a fresh Agent, this runs on the
        provided agent to preserve workspace context (matching OpenClaw behavior).

        Returns:
            Alert message if attention needed, None if HEARTBEAT_OK or suppressed.
        """
        if self._workspace.is_empty_heartbeat():
            logger.debug("HEARTBEAT.md is empty — skipping run")
            return None

        heartbeat_content = self._workspace.heartbeat_md() or ""
        prompt = HEARTBEAT_PROMPT
        if heartbeat_content:
            prompt = f"{HEARTBEAT_PROMPT}\n\nYour HEARTBEAT.md:\n{heartbeat_content}"

        try:
            response = await self._agent.arun(prompt)
            content = str(response.content) if response and response.content else ""
            return self._filter_response(content)
        except Exception as e:
            logger.error("Heartbeat run failed: %s", e)
            return None

    # ── Cron job loop ──────────────────────────────────────────────────────────

    async def _run_cron_loop(self, job: CronJob) -> None:
        """Loop for a single cron job — waits for next scheduled time then fires."""
        while self._running and job.enabled:
            sleep_seconds = self._seconds_until_next(job.schedule)
            if sleep_seconds < 0:
                # Interval string parse failed — treat as disabled
                logger.error("Cron job '%s': could not parse schedule '%s'", job.name, job.schedule)
                return

            logger.debug("Cron job '%s': next run in %.0fs", job.name, sleep_seconds)
            await asyncio.sleep(sleep_seconds)

            if not self._running:
                break

            result = await self._run_cron_job(job)
            if result:
                self._on_alert(f"[{job.name}] {result}")

    async def _run_cron_job(self, job: CronJob) -> Optional[str]:
        """Execute a single cron job run."""
        prompt = job.prompt

        try:
            if job.isolated:
                # Isolated: fresh agent session — no prior context
                result = await self._run_isolated(job, prompt)
            else:
                # Main session: run on the shared agent (has workspace + history)
                result = await self._agent.arun(prompt, skill=job.skill)

            content = str(result.content) if result and result.content else ""
            return content if content else None
        except Exception as e:
            logger.error("Cron job '%s' failed: %s", job.name, e)
            return None

    async def _run_isolated(self, job: CronJob, prompt: str) -> object:
        """Run a cron job in a fresh isolated session."""
        from agno.agent import Agent

        cfg = self._config
        model_id = job.model_id or cfg.heartbeat.model or cfg.default_model
        provider = job.provider or cfg.default_provider

        # Build Agno-native "provider:model_id" string
        from agnoclaw.agent import _resolve_model
        model_str = _resolve_model(model_id, provider, cfg)
        isolated_agent = Agent(
            model=model_str,
            instructions=(
                "You are a scheduled task agent. Complete the task and respond concisely. "
                "You do not have access to conversation history."
            ),
        )
        return isolated_agent.run(prompt)

    # ── Schedule parsing ───────────────────────────────────────────────────────

    @staticmethod
    def _seconds_until_next(schedule: str) -> float:
        """
        Calculate seconds until the next scheduled run.

        Supports:
          - Interval strings: '30m', '1h', '6h', '2h30m', '45s'
          - Cron expressions: '0 9 * * 1-5', '*/15 * * * *', '0 0 * * *'

        Returns -1 if parsing fails.
        """
        schedule = schedule.strip()

        # ── Interval string parsing ──────────────────────────────────────────
        # Supports: 30m, 1h, 6h, 2h30m, 45s, 1h30m
        import re
        interval_pattern = re.compile(
            r'^(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$',
            re.IGNORECASE,
        )
        m = interval_pattern.match(schedule)
        if m and any(m.group(k) for k in ("hours", "minutes", "seconds")):
            total = 0
            if m.group("hours"):
                total += int(m.group("hours")) * 3600
            if m.group("minutes"):
                total += int(m.group("minutes")) * 60
            if m.group("seconds"):
                total += int(m.group("seconds"))
            return float(total)

        # ── Cron expression parsing ──────────────────────────────────────────
        # Try croniter if available, else fall back to cronsim
        try:
            from croniter import croniter
            now = datetime.now()
            ci = croniter(schedule, now)
            next_dt = ci.get_next(datetime)
            return max(0.0, (next_dt - now).total_seconds())
        except ImportError:
            pass

        try:
            from cronsim import CronSim
            now = datetime.now()
            sim = CronSim(schedule, now)
            next_dt = next(sim)
            return max(0.0, (next_dt - now).total_seconds())
        except ImportError:
            pass

        logger.warning(
            "No cron library found for expression '%s'. "
            "Install croniter or cronsim: uv add croniter",
            schedule,
        )
        return -1.0

    # ── Active hours ───────────────────────────────────────────────────────────

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

    # ── HEARTBEAT_OK filtering ─────────────────────────────────────────────────

    def _filter_response(self, content: str) -> Optional[str]:
        """Return None if HEARTBEAT_OK and under threshold; else return content."""
        if HEARTBEAT_OK_TOKEN in content:
            if len(content) <= self._config.heartbeat.ok_threshold_chars:
                logger.debug("HEARTBEAT_OK — no action needed")
                return None
            content = content.replace(HEARTBEAT_OK_TOKEN, "").strip()
            if not content:
                return None
        return content if content else None

    @staticmethod
    def _default_alert(message: str) -> None:
        """Default alert handler — print to console."""
        print(f"\n[HEARTBEAT ALERT]\n{message}\n")
