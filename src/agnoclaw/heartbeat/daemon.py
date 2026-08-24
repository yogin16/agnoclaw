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
    from agnoclaw import AgentHarness

    agent = AgentHarness()

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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from agnoclaw.config import HarnessConfig, get_config
from agnoclaw.runtime.scheduler import (
    DurableSchedulerBackend,
    SchedulerBackend,
    SchedulerJob,
    SchedulerLeaseLostError,
    SchedulerRunClaim,
    is_durable_scheduler_backend,
    scheduler_idempotency_key,
)
from agnoclaw.runtime.store import RuntimeStoreConnectionLostError
from agnoclaw.workspace import Workspace

if TYPE_CHECKING:
    from agnoclaw.agent import AgentHarness

logger = logging.getLogger("agnoclaw.heartbeat")

HEARTBEAT_PROMPT = """Read HEARTBEAT.md in your workspace if it exists.
Check each item in the checklist and determine if anything needs immediate attention.
If nothing needs attention, reply with HEARTBEAT_OK (and nothing else, or very brief context).
If something does need attention, describe it clearly so the user can act."""

HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK"


class _IsolatedRunDetached(asyncio.CancelledError):
    """Carry lifecycle identity when scheduler supervision is cancelled."""

    def __init__(self, run_id: str) -> None:
        super().__init__("isolated lifecycle run detached from scheduler supervision")
        self.run_id = run_id


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
        model_id: Optional compatibility-backend model override. Durable workers reject
                  per-job overrides and use one immutable worker model.
        provider: Optional compatibility-backend provider override.
        enabled: Set to False to disable without removing.
    """

    name: str
    schedule: str
    prompt: str
    skill: str | None = None
    isolated: bool = False
    model_id: str | None = None
    provider: str | None = None
    enabled: bool = True
    timezone: str = "UTC"
    max_retries: int = 0
    retry_delay_seconds: int = 30
    retry_backoff_multiplier: float = 2.0
    retry_max_delay_seconds: int = 3_600
    retry_jitter_seconds: int = 0
    jitter_seconds: int = 0
    misfire_policy: str = "fire_once"
    misfire_grace_seconds: int = 300
    concurrency_key: str | None = None
    overlap_policy: str = "queue"
    learning_consent: bool = False
    _next_run: datetime | None = field(default=None, repr=False, compare=False)

    def to_scheduler_job(self) -> SchedulerJob:
        return SchedulerJob(
            name=self.name,
            schedule=self.schedule,
            prompt=self.prompt,
            skill=self.skill,
            isolated=self.isolated,
            model_id=self.model_id,
            provider=self.provider,
            enabled=self.enabled,
            timezone=self.timezone,
            max_retries=self.max_retries,
            retry_delay_seconds=self.retry_delay_seconds,
            retry_backoff_multiplier=self.retry_backoff_multiplier,
            retry_max_delay_seconds=self.retry_max_delay_seconds,
            retry_jitter_seconds=self.retry_jitter_seconds,
            jitter_seconds=self.jitter_seconds,
            misfire_policy=self.misfire_policy,
            misfire_grace_seconds=self.misfire_grace_seconds,
            concurrency_key=self.concurrency_key,
            overlap_policy=self.overlap_policy,
            metadata={"learning_consent": self.learning_consent},
        )

    @classmethod
    def from_scheduler_job(cls, job: SchedulerJob) -> CronJob:
        return cls(
            name=job.name,
            schedule=job.schedule,
            prompt=job.prompt,
            skill=job.skill,
            isolated=job.isolated,
            model_id=job.model_id,
            provider=job.provider,
            enabled=job.enabled,
            timezone=job.timezone,
            max_retries=job.max_retries,
            retry_delay_seconds=job.retry_delay_seconds,
            retry_backoff_multiplier=job.retry_backoff_multiplier,
            retry_max_delay_seconds=job.retry_max_delay_seconds,
            retry_jitter_seconds=job.retry_jitter_seconds,
            jitter_seconds=job.jitter_seconds,
            misfire_policy=job.misfire_policy,
            misfire_grace_seconds=job.misfire_grace_seconds,
            concurrency_key=job.concurrency_key,
            overlap_policy=job.overlap_policy,
            learning_consent=job.metadata.get("learning_consent") is True,
        )


class HeartbeatDaemon:
    """
    Asyncio-based heartbeat scheduler with optional cron job support.

    Args:
        agent: The AgentHarness to run heartbeats on.
        on_alert: Callback called with the alert message when something needs attention.
                  Signature: on_alert(message: str) -> None
        config: HarnessConfig. Loaded from env/TOML if not provided.
        workspace: Workspace instance. Shared with the agent if not provided.
    """

    def __init__(
        self,
        agent: AgentHarness,
        on_alert: Callable[[str], None] | None = None,
        config: HarnessConfig | None = None,
        workspace: Workspace | None = None,
        scheduler_backend: SchedulerBackend | DurableSchedulerBackend | None = None,
        scheduler_poll_interval_seconds: float = 1.0,
        scheduler_claim_limit: int = 10,
        heartbeat_enabled: bool = True,
    ):
        self._agent = agent
        self._on_alert = on_alert or self._default_alert
        self._config = config or get_config()
        self._workspace = workspace or (
            agent.workspace if hasattr(agent, "workspace") else Workspace()
        )
        self._task: asyncio.Task | None = None
        self._cron_tasks: list[asyncio.Task] = []
        self._running = False
        self._cron_jobs: list[CronJob] = []
        self._scheduler_backend = scheduler_backend
        if scheduler_poll_interval_seconds <= 0:
            raise ValueError("scheduler_poll_interval_seconds must be positive")
        if not 1 <= scheduler_claim_limit <= 100:
            raise ValueError("scheduler_claim_limit must be between 1 and 100")
        self._scheduler_poll_interval_seconds = scheduler_poll_interval_seconds
        self._scheduler_claim_limit = scheduler_claim_limit
        self._heartbeat_enabled = heartbeat_enabled
        self._scheduler_worker_id = f"scheduler_{uuid4().hex}"
        self._scheduler_tasks: dict[str, asyncio.Task[Any]] = {}
        if self._scheduler_backend is not None:
            self._cron_jobs.extend(
                CronJob.from_scheduler_job(job) for job in self._scheduler_backend.list_jobs()
            )

    def add_cron_job(self, job: CronJob) -> None:
        """
        Add a cron job to run alongside the heartbeat.

        Validates the schedule expression before registering. If a cron library
        (croniter/cronsim) is not installed, cron expressions are accepted
        optimistically and validated at runtime.
        """
        # Validate schedule — only reject if it's truly malformed
        test_delay = self._seconds_until_next(job.schedule)
        if test_delay < 0:
            # _seconds_until_next returns -1 only when no cron library is found.
            # If the schedule looks like a cron expression (5 fields), accept it
            # optimistically — it will fail at runtime with a clear error.
            parts = job.schedule.strip().split()
            if len(parts) < 5:
                raise ValueError(
                    f"Invalid schedule '{job.schedule}' for cron job '{job.name}'. "
                    f"Use a cron expression ('0 9 * * 1-5') or interval string ('30m', '1h')."
                )
            logger.warning(
                "Cron job '%s': no cron library available to validate schedule '%s'. "
                "Install croniter: uv add croniter",
                job.name,
                job.schedule,
            )
        self._upsert_cron_job(job)
        if self._scheduler_backend is not None:
            self._scheduler_backend.upsert_job(job.to_scheduler_job())
        logger.info(
            "Registered cron job '%s' (schedule=%s, isolated=%s)",
            job.name,
            job.schedule,
            job.isolated,
        )

    def list_cron_jobs(self, *, enabled: bool | None = None) -> list[CronJob]:
        """List registered cron jobs from memory or the configured scheduler backend."""
        jobs = list(self._cron_jobs)
        if enabled is not None:
            jobs = [job for job in jobs if job.enabled is enabled]
        return sorted(jobs, key=lambda job: job.name)

    def remove_cron_job(self, name: str) -> bool:
        """Remove a cron job by name."""
        before = len(self._cron_jobs)
        self._cron_jobs = [job for job in self._cron_jobs if job.name != name]
        removed = len(self._cron_jobs) < before
        if self._scheduler_backend is not None:
            removed = self._scheduler_backend.delete_job(name) or removed
        return removed

    def set_cron_enabled(self, name: str, enabled: bool) -> bool:
        """Enable or disable a cron job by name."""
        updated = False
        for index, job in enumerate(self._cron_jobs):
            if job.name != name:
                continue
            self._cron_jobs[index] = CronJob(
                name=job.name,
                schedule=job.schedule,
                prompt=job.prompt,
                skill=job.skill,
                isolated=job.isolated,
                model_id=job.model_id,
                provider=job.provider,
                enabled=enabled,
                timezone=job.timezone,
                max_retries=job.max_retries,
                retry_delay_seconds=job.retry_delay_seconds,
                retry_backoff_multiplier=job.retry_backoff_multiplier,
                retry_max_delay_seconds=job.retry_max_delay_seconds,
                retry_jitter_seconds=job.retry_jitter_seconds,
                jitter_seconds=job.jitter_seconds,
                misfire_policy=job.misfire_policy,
                misfire_grace_seconds=job.misfire_grace_seconds,
                concurrency_key=job.concurrency_key,
                overlap_policy=job.overlap_policy,
                learning_consent=job.learning_consent,
            )
            updated = True
            break
        if self._scheduler_backend is not None:
            backend_job = self._scheduler_backend.set_job_enabled(name, enabled)
            updated = backend_job is not None or updated
        return updated

    def _upsert_cron_job(self, job: CronJob) -> None:
        self._cron_jobs = [existing for existing in self._cron_jobs if existing.name != job.name]
        self._cron_jobs.append(job)

    def start(self) -> None:
        """Start the heartbeat daemon and any registered cron jobs."""
        if self._running:
            logger.warning("Heartbeat daemon already running")
            return
        self._running = True

        if self._heartbeat_enabled:
            self._task = asyncio.create_task(
                self._run_heartbeat_loop(),
                name="agnoclaw-heartbeat",
            )

        if self._durable_scheduler() is not None:
            self._cron_tasks.append(
                asyncio.create_task(
                    self._run_durable_scheduler_loop(),
                    name="agnoclaw-durable-scheduler",
                )
            )
        else:
            for job in self._cron_jobs:
                if job.enabled:
                    task = asyncio.create_task(
                        self._run_cron_loop(job),
                        name=f"agnoclaw-cron-{job.name}",
                    )
                    self._cron_tasks.append(task)

        logger.info(
            "Heartbeat daemon started (interval=%dm, active=%s-%s, cron_jobs=%d)",
            self._config.heartbeat.interval_minutes if self._heartbeat_enabled else 0,
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
        for task in self._scheduler_tasks.values():
            if not task.done():
                task.cancel()
        self._scheduler_tasks.clear()
        logger.info("Heartbeat daemon stopped")

    async def astop(self) -> None:
        """Stop the daemon and wait until every owned task has quiesced.

        ``stop()`` remains the synchronous cancellation signal for embedded callers.
        Process and service owners should await this method before closing the agent
        or its runtime store so in-flight scheduler cleanup cannot race resource
        teardown.
        """
        tasks = [
            task
            for task in [self._task, *self._cron_tasks, *self._scheduler_tasks.values()]
            if task is not None and task is not asyncio.current_task()
        ]
        self.stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None

    async def run_forever(self) -> None:
        """
        Start the daemon and block until stopped (via stop() or KeyboardInterrupt).

        Convenience method for CLI and scripts. Equivalent to::

            daemon.start()
            await daemon.wait()
        """
        self.start()
        try:
            while self._running:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self.astop()

    async def trigger_now(self) -> str | None:
        """
        Manually trigger a heartbeat run immediately.

        Returns:
            The alert message if something needs attention, or None if HEARTBEAT_OK.
        """
        return await self._run_heartbeat()

    async def trigger_cron(self, job_name: str) -> str | None:
        """
        Manually trigger a named cron job immediately.

        Returns:
            The job response, or None if nothing to report.
        """
        for job in self._cron_jobs:
            if job.name == job_name:
                durable = self._durable_scheduler()
                if durable is not None:
                    claim = await asyncio.to_thread(
                        durable.claim_now,
                        job_name,
                        worker_id=self._scheduler_worker_id,
                        lease_seconds=self._scheduler_lease_seconds,
                    )
                    if claim is None:
                        return f"[busy] Cron job '{job_name}' already has an active run."
                    return await self._run_claimed_job(claim)
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

    async def _run_heartbeat(self) -> str | None:
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
            from agnoclaw.runtime.first_party import first_party_run

            run = await first_party_run(self._agent, prompt)
            response = await run.wait()
            response_content = getattr(response, "content", None)
            content = str(response_content) if response_content else ""
            return self._filter_response(content)
        except Exception as e:
            code = getattr(e, "code", "HEARTBEAT_RUN_FAILED")
            logger.error("Heartbeat run failed (code=%s, type=%s)", code, type(e).__name__)
            return f"[heartbeat error] {code}"

    # ── Cron job loop ──────────────────────────────────────────────────────────

    @property
    def _scheduler_lease_seconds(self) -> int:
        return max(3, int(getattr(self._config, "runtime_lease_seconds", 30)))

    def _durable_scheduler(self) -> DurableSchedulerBackend | None:
        backend = self._scheduler_backend
        if backend is None or not is_durable_scheduler_backend(backend):
            return None
        return backend  # type: ignore[return-value]

    async def _run_durable_scheduler_loop(self) -> None:
        """Poll database-authoritative due work and supervise a bounded task set."""
        while self._running:
            try:
                backend = self._durable_scheduler()
                if backend is None:  # pragma: no cover - constructor path is stable
                    return
                self._scheduler_tasks = {
                    run_id: task
                    for run_id, task in self._scheduler_tasks.items()
                    if not task.done()
                }
                capacity = max(0, self._scheduler_claim_limit - len(self._scheduler_tasks))
                if capacity:
                    claims = await asyncio.to_thread(
                        backend.claim_due_runs,
                        worker_id=self._scheduler_worker_id,
                        limit=capacity,
                        lease_seconds=self._scheduler_lease_seconds,
                    )
                    for claim in claims:
                        task = asyncio.create_task(
                            self._run_claimed_job(claim),
                            name=f"agnoclaw-schedule:{claim.run_id}",
                        )
                        self._scheduler_tasks[claim.run_id] = task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Durable scheduler poll failed (code=%s, type=%s)",
                    getattr(exc, "code", "SCHEDULER_POLL_FAILED"),
                    type(exc).__name__,
                )
            await asyncio.sleep(self._scheduler_poll_interval_seconds)

    async def _wait_or_recover_bound_run(self, runtime_run_id: str) -> Any:
        """Observe a bound run, then use the lifecycle's fenced recovery path."""
        run = self._agent.get_run(runtime_run_id)
        interval = min(5.0, self._scheduler_poll_interval_seconds)
        while True:
            try:
                return await run.wait(timeout=interval)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                pass
            except Exception as exc:
                error_code = getattr(exc, "code", None)
                if error_code == "RUN_RECONCILIATION_REQUIRED":
                    raise
                if error_code != "RUN_WAIT_INCOMPLETE":
                    raise
            try:
                run = await self._agent.recover_run(runtime_run_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if getattr(exc, "code", None) != "RUNTIME_LEASE_UNAVAILABLE":
                    raise
                await asyncio.sleep(interval)

    async def _renew_scheduler_claim(
        self,
        backend: DurableSchedulerBackend,
        claim: SchedulerRunClaim,
        execution: asyncio.Task[Any],
    ) -> None:
        current = claim
        interval = min(
            float(getattr(self._config, "runtime_lease_renew_interval_seconds", 10.0)),
            self._scheduler_lease_seconds / 3,
        )
        try:
            while True:
                await asyncio.sleep(interval)
                current = await asyncio.to_thread(
                    backend.renew_claim,
                    current,
                    lease_seconds=self._scheduler_lease_seconds,
                )
        except asyncio.CancelledError:
            raise
        except BaseException:
            execution.cancel()

    @staticmethod
    async def _release_scheduler_claim_safely(
        backend: DurableSchedulerBackend,
        claim: SchedulerRunClaim,
    ) -> None:
        try:
            await asyncio.to_thread(backend.release_claim, claim)
        except (SchedulerLeaseLostError, RuntimeStoreConnectionLostError):
            pass

    async def _run_claimed_job(self, claim: SchedulerRunClaim) -> str | None:
        backend = self._durable_scheduler()
        if backend is None:  # pragma: no cover - only invoked from the durable path
            return None
        execution = asyncio.current_task()
        if execution is None:  # pragma: no cover - asyncio always owns this coroutine
            raise RuntimeError("durable scheduler execution requires an asyncio task")
        heartbeat = asyncio.create_task(
            self._renew_scheduler_claim(backend, claim, execution),
            name=f"agnoclaw-schedule-lease:{claim.run_id}",
        )
        current = claim
        lifecycle_admitted = current.record.runtime_run_id is not None
        lifecycle_settled = False
        try:
            if current.record.runtime_run_id:
                result = await self._wait_or_recover_bound_run(
                    current.record.runtime_run_id
                )
            else:
                job = CronJob.from_scheduler_job(current.job)
                metadata = self._scheduler_metadata(
                    job,
                    schedule_run_id=current.run_id,
                    scheduled_at=current.record.scheduled_at,
                    attempt=current.record.attempt,
                    fence_token=current.fence_token,
                )
                run = await self._agent.start(
                    job.prompt,
                    idempotency_key=scheduler_idempotency_key(current.record),
                    session_id=(
                        f"schedule:{current.record.occurrence_id}" if job.isolated else None
                    ),
                    skill=job.skill,
                    metadata=metadata,
                    learning_consent=job.learning_consent,
                )
                if run.run_id is None:  # pragma: no cover - AgentHarness.start invariant
                    raise RuntimeError("durable scheduler received a run without identity")
                lifecycle_admitted = True
                current = await asyncio.to_thread(
                    backend.bind_runtime_run,
                    current,
                    runtime_run_id=run.run_id,
                )
                result = await run.wait()
            lifecycle_settled = True
            content = str(getattr(result, "content", result) or "")
            await asyncio.to_thread(
                backend.finish_claim,
                current,
                status="completed",
                output=content,
            )
            return content or None
        except asyncio.CancelledError:
            await self._release_scheduler_claim_safely(backend, current)
            raise
        except Exception as exc:
            error_code = getattr(exc, "code", "SCHEDULER_RUN_FAILED")
            if isinstance(exc, RuntimeStoreConnectionLostError) or (
                lifecycle_admitted
                and (
                    lifecycle_settled
                    or isinstance(exc, SchedulerLeaseLostError)
                    or error_code in {
                        "RUN_RECONCILIATION_REQUIRED",
                        "RUN_WAIT_INCOMPLETE",
                    }
                )
            ):
                await self._release_scheduler_claim_safely(backend, current)
                return None
            try:
                await asyncio.to_thread(
                    backend.finish_claim,
                    current,
                    status=("failed" if getattr(exc, "retryable", False) else "dead_lettered"),
                    error=str(error_code),
                )
            except (SchedulerLeaseLostError, RuntimeStoreConnectionLostError):
                pass
            return None
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _scheduler_metadata(
        job: CronJob,
        *,
        schedule_run_id: str | None,
        scheduled_at: str | None = None,
        attempt: int | None = None,
        fence_token: int | None = None,
    ) -> dict[str, Any]:
        return {
            "scheduler": {
                "schedule_id": job.name,
                "schedule_run_id": schedule_run_id,
                "schedule_name": job.name,
                "scheduled_at": scheduled_at,
                "attempt": attempt,
                "fence_token": fence_token,
            }
        }

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

    async def _run_cron_job(self, job: CronJob) -> str | None:
        """Execute a single cron job run."""
        from agnoclaw.runtime.first_party import first_party_run

        prompt = job.prompt
        schedule_run = None
        runtime_run_id: str | None = None
        legacy_backend = (
            cast(SchedulerBackend, self._scheduler_backend)
            if self._scheduler_backend is not None
            and self._durable_scheduler() is None
            else None
        )
        if legacy_backend is not None:
            schedule_run = legacy_backend.record_run_start(
                job.name,
                metadata={"schedule": job.schedule, "isolated": job.isolated},
            )
        metadata = self._scheduler_metadata(
            job,
            schedule_run_id=schedule_run.run_id if schedule_run else None,
        )

        try:
            if job.isolated:
                # Isolated: fresh agent session — no prior context
                run, result = await self._run_isolated(job, prompt, metadata=metadata)
                runtime_run_id = run.run_id
            else:
                # Main session: run on the shared agent (has workspace + history)
                run_options: dict[str, Any] = {
                    "skill": job.skill,
                    "metadata": metadata,
                }
                if job.learning_consent:
                    run_options["learning_consent"] = True
                run = await first_party_run(
                    self._agent,
                    prompt,
                    **run_options,
                )
                runtime_run_id = run.run_id
                result = await run.wait()

            result_content = getattr(result, "content", None)
            content = str(result_content) if result_content else ""
            if schedule_run is not None and legacy_backend is not None:
                legacy_backend.record_run_finish(
                    schedule_run.run_id,
                    status="completed",
                    output=content,
                    metadata={"runtime_run_id": runtime_run_id} if runtime_run_id else None,
                )
            return content if content else None
        except asyncio.CancelledError as exc:
            runtime_run_id = getattr(exc, "run_id", runtime_run_id)
            if schedule_run is not None and legacy_backend is not None:
                legacy_backend.record_run_finish(
                    schedule_run.run_id,
                    status="detached" if runtime_run_id else "cancelled",
                    error="SCHEDULER_EXECUTION_DETACHED" if runtime_run_id else "RUN_CANCELLED",
                    metadata={"runtime_run_id": runtime_run_id} if runtime_run_id else None,
                )
            raise
        except Exception as e:
            code = getattr(e, "code", "SCHEDULER_RUN_FAILED")
            logger.error(
                "Cron job '%s' failed (code=%s, type=%s)",
                job.name,
                code,
                type(e).__name__,
            )
            if schedule_run is not None and legacy_backend is not None:
                legacy_backend.record_run_finish(
                    schedule_run.run_id,
                    status="failed",
                    error=str(code),
                    metadata={"runtime_run_id": runtime_run_id} if runtime_run_id else None,
                )
            return None

    async def _run_isolated(
        self,
        job: CronJob,
        prompt: str,
        *,
        metadata: dict | None = None,
    ) -> tuple[Any, Any]:
        """Run a cron job in a fresh owned harness and close it after settlement."""
        from agnoclaw.agent import AgentHarness
        from agnoclaw.runtime.first_party import first_party_run

        cfg = self._config
        model_id = job.model_id or cfg.heartbeat.model or cfg.default_model
        provider = job.provider or cfg.default_provider

        # Build an isolated AgentHarness (not raw Agent) so skills work
        from agnoclaw.agent import _resolve_model

        model_str = _resolve_model(model_id, provider, cfg)
        isolated = AgentHarness(
            model=model_str,
            instructions=(
                "You are a scheduled task agent. Complete the task and respond concisely. "
                "You do not have access to conversation history."
            ),
            config=cfg,
        )
        run = None
        closed = False
        try:
            run_options: dict[str, Any] = {
                "skill": job.skill,
                "metadata": metadata,
            }
            if job.learning_consent:
                run_options["learning_consent"] = True
            run = await first_party_run(
                isolated,
                prompt,
                **run_options,
            )
            result = await run.wait()
            return run, result
        except asyncio.CancelledError:
            if run is not None and run.run_id is not None:
                await isolated.aclose(policy="detach")
                closed = True
                raise _IsolatedRunDetached(run.run_id) from None
            await isolated.aclose(policy="cancel")
            closed = True
            raise
        finally:
            if not closed:
                await isolated.aclose()

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
            r"^(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$",
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
        except ImportError:
            pass
        else:
            now = datetime.now()
            try:
                ci = croniter(schedule, now)
                next_dt = ci.get_next(datetime)
            except ValueError:
                return -1.0
            return max(0.0, (next_dt - now).total_seconds())

        try:
            from cronsim import CronSim
        except ImportError:
            pass
        else:
            now = datetime.now()
            try:
                sim = CronSim(schedule, now)
                next_dt = next(sim)
            except (StopIteration, ValueError):
                return -1.0
            return max(0.0, (next_dt - now).total_seconds())

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

    def _filter_response(self, content: str) -> str | None:
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
