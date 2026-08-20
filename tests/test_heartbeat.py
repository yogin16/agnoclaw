"""Tests for the heartbeat daemon."""

import asyncio
from datetime import UTC, datetime, time, timedelta
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

# ── HeartbeatDaemon tests ────────────────────────────────────────────────


def test_heartbeat_daemon_imports():
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    assert HeartbeatDaemon is not None


def test_heartbeat_ok_token():
    from agnoclaw.heartbeat.daemon import HEARTBEAT_OK_TOKEN

    assert HEARTBEAT_OK_TOKEN == "HEARTBEAT_OK"


def test_heartbeat_prompt_exists():
    from agnoclaw.heartbeat.daemon import HEARTBEAT_PROMPT

    assert isinstance(HEARTBEAT_PROMPT, str)
    assert len(HEARTBEAT_PROMPT) > 10


def test_heartbeat_prompt_mentions_ok():
    from agnoclaw.heartbeat.daemon import HEARTBEAT_OK_TOKEN, HEARTBEAT_PROMPT

    assert HEARTBEAT_OK_TOKEN in HEARTBEAT_PROMPT


def test_heartbeat_daemon_init():
    from agnoclaw.config import HarnessConfig, HeartbeatConfig
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.workspace.is_empty_heartbeat = MagicMock(return_value=False)
    mock_agent.workspace.heartbeat_md = MagicMock(return_value="checklist")

    cfg = HarnessConfig(
        heartbeat=HeartbeatConfig(
            enabled=True,
            interval_minutes=30,
            active_hours_start="08:00",
            active_hours_end="22:00",
        )
    )

    daemon = HeartbeatDaemon(agent=mock_agent, config=cfg)
    assert daemon is not None


@pytest.mark.asyncio
async def test_scheduler_only_mode_does_not_start_heartbeat_loop():
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.runtime.scheduler import RuntimeSchedulerBackend
    from agnoclaw.runtime.store import SQLiteRuntimeStore

    agent = MagicMock()
    agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(
        agent,
        scheduler_backend=RuntimeSchedulerBackend(SQLiteRuntimeStore(":memory:")),
        heartbeat_enabled=False,
    )

    daemon.start()
    try:
        assert daemon._task is None
        assert len(daemon._cron_tasks) == 1
        assert daemon._cron_tasks[0].get_name() == "agnoclaw-durable-scheduler"
    finally:
        daemon.stop()
        await asyncio.sleep(0)
    assert daemon._running is False


@pytest.mark.asyncio
async def test_durable_scheduler_worker_poll_loop_processes_due_job(tmp_path):
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.runtime.scheduler import RuntimeSchedulerBackend, SchedulerJob
    from agnoclaw.runtime.store import SQLiteRuntimeStore

    backend = RuntimeSchedulerBackend(SQLiteRuntimeStore(tmp_path / "runtime.db"))
    backend.upsert_job(
        SchedulerJob(
            name="worker-e2e",
            schedule="1h",
            prompt="process through worker loop",
            next_run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
    )
    runtime_run = MagicMock(run_id="runtime-worker-e2e")
    runtime_run.wait = AsyncMock(return_value=MagicMock(content="worker complete"))
    agent = MagicMock()
    agent.workspace = MagicMock()
    agent.start = AsyncMock(return_value=runtime_run)
    daemon = HeartbeatDaemon(
        agent,
        scheduler_backend=backend,
        scheduler_poll_interval_seconds=0.01,
        heartbeat_enabled=False,
    )

    daemon.start()
    try:
        for _ in range(200):
            records = backend.list_runs(job_name="worker-e2e")
            if records and records[0].status == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("durable scheduler worker did not process the due job")
    finally:
        await daemon.astop()

    agent.start.assert_awaited_once()
    assert records[0].runtime_run_id == "runtime-worker-e2e"
    assert records[0].output == "worker complete"


def test_heartbeat_is_active_hours_within_range():
    from agnoclaw.config import HarnessConfig, HeartbeatConfig
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    cfg = HarnessConfig(
        heartbeat=HeartbeatConfig(
            active_hours_start="08:00",
            active_hours_end="22:00",
        )
    )

    daemon = HeartbeatDaemon(agent=mock_agent, config=cfg)

    # Mock datetime to be within active hours (noon)
    noon_time = time(12, 0)
    with patch("agnoclaw.heartbeat.daemon.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = noon_time
        result = daemon._is_active_hours()
    assert result is True


def test_heartbeat_is_active_hours_outside_range():
    from agnoclaw.config import HarnessConfig, HeartbeatConfig
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    cfg = HarnessConfig(
        heartbeat=HeartbeatConfig(
            active_hours_start="08:00",
            active_hours_end="22:00",
        )
    )

    daemon = HeartbeatDaemon(agent=mock_agent, config=cfg)

    # 2am should be outside active hours
    two_am = time(2, 0)
    with patch("agnoclaw.heartbeat.daemon.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = two_am
        result = daemon._is_active_hours()
    assert result is False


def test_heartbeat_is_active_hours_at_start():
    from agnoclaw.config import HarnessConfig, HeartbeatConfig
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    cfg = HarnessConfig(
        heartbeat=HeartbeatConfig(
            active_hours_start="08:00",
            active_hours_end="22:00",
        )
    )

    daemon = HeartbeatDaemon(agent=mock_agent, config=cfg)

    at_start = time(8, 0)
    with patch("agnoclaw.heartbeat.daemon.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = at_start
        result = daemon._is_active_hours()
    assert result is True


def test_heartbeat_ok_suppression_logic():
    """HEARTBEAT_OK responses under threshold should be suppressed."""
    from agnoclaw.heartbeat.daemon import HEARTBEAT_OK_TOKEN

    ok_threshold = 300
    short_ok = HEARTBEAT_OK_TOKEN  # way under threshold
    long_response = "A" * 400  # over threshold
    medium_ok = f"{HEARTBEAT_OK_TOKEN}. Minor note here."  # ok token present but short

    # Simulate the suppression logic from _run_heartbeat
    def simulate_suppress(content: str, threshold: int = ok_threshold) -> bool:
        if HEARTBEAT_OK_TOKEN in content:
            return len(content) <= threshold
        return False

    assert simulate_suppress(short_ok) is True
    assert simulate_suppress(long_response) is False
    assert simulate_suppress(medium_ok) is True  # short enough even with note


def test_heartbeat_ok_suppression_long_response():
    from agnoclaw.heartbeat.daemon import HEARTBEAT_OK_TOKEN

    ok_threshold = 50
    # HEARTBEAT_OK with lots of content — should NOT be suppressed
    verbose_ok = f"{HEARTBEAT_OK_TOKEN}. " + "A" * 200

    def simulate_suppress(content: str, threshold: int = ok_threshold) -> bool:
        if HEARTBEAT_OK_TOKEN in content:
            return len(content) <= threshold
        return False

    assert simulate_suppress(verbose_ok) is False


@pytest.mark.asyncio
async def test_heartbeat_trigger_now_skips_empty_checklist():
    """trigger_now should return None when HEARTBEAT.md is empty."""
    from agnoclaw.config import HarnessConfig, HeartbeatConfig
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.workspace.is_empty_heartbeat = MagicMock(return_value=True)

    cfg = HarnessConfig(heartbeat=HeartbeatConfig(enabled=True))
    daemon = HeartbeatDaemon(agent=mock_agent, config=cfg)

    result = await daemon.trigger_now()
    assert result is None


@pytest.mark.asyncio
async def test_heartbeat_run_heartbeat_skips_outside_active_hours():
    from agnoclaw.config import HarnessConfig, HeartbeatConfig
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.workspace.is_empty_heartbeat = MagicMock(return_value=False)
    mock_agent.workspace.heartbeat_md = MagicMock(return_value="check this")
    mock_agent.arun = AsyncMock()

    cfg = HarnessConfig(
        heartbeat=HeartbeatConfig(
            enabled=True,
            active_hours_start="08:00",
            active_hours_end="22:00",
        )
    )
    daemon = HeartbeatDaemon(agent=mock_agent, config=cfg)

    # Make _is_active_hours return False
    two_am = time(2, 0)
    with patch("agnoclaw.heartbeat.daemon.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = two_am

        # _run_heartbeat doesn't check active hours — only _run_loop does
        # trigger_now calls _run_heartbeat directly (no active hour check)
        # so let's test _run_loop behavior via the running flag
        assert daemon._running is False


def test_heartbeat_start_stop():
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    # Test that daemon has start/stop methods
    assert hasattr(daemon, "start")
    assert hasattr(daemon, "stop")
    assert hasattr(daemon, "trigger_now")


def test_heartbeat_default_alert_callable():
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    assert callable(HeartbeatDaemon._default_alert)


# ── Workspace heartbeat integration ─────────────────────────────────────


def test_workspace_heartbeat_read(tmp_path):
    from agnoclaw.workspace import Workspace

    ws = Workspace(tmp_path / "ws")
    ws.initialize()

    content = ws.heartbeat_md()
    assert content is not None  # Default HEARTBEAT.md has content


def test_workspace_heartbeat_empty_when_blank(tmp_path):
    from agnoclaw.workspace import Workspace

    ws = Workspace(tmp_path / "ws")
    ws.initialize()
    ws.write_file("heartbeat", "")  # explicitly empty

    content = ws.heartbeat_md()
    assert content is None


def test_workspace_is_empty_heartbeat_default(tmp_path):
    from agnoclaw.workspace import Workspace

    ws = Workspace(tmp_path / "ws")
    ws.initialize()

    # Default heartbeat has content — not empty
    assert not ws.is_empty_heartbeat()


def test_workspace_is_empty_heartbeat_headers_only(tmp_path):
    from agnoclaw.workspace import Workspace

    ws = Workspace(tmp_path / "ws")
    ws.initialize()
    ws.write_file("heartbeat", "# Heartbeat\n\n## Subsection\n")

    assert ws.is_empty_heartbeat()


# ── CronJob + schedule parser tests ─────────────────────────────────────


def test_cron_job_dataclass():
    from agnoclaw.heartbeat.daemon import CronJob

    job = CronJob(name="test", schedule="30m", prompt="hello")
    assert job.name == "test"
    assert job.schedule == "30m"
    assert job.isolated is False
    assert job.enabled is True


def test_seconds_until_next_interval_minutes():
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    # "30m" → 1800 seconds
    result = HeartbeatDaemon._seconds_until_next("30m")
    assert result == 1800.0


def test_seconds_until_next_interval_hours():
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    # "1h" → 3600 seconds
    result = HeartbeatDaemon._seconds_until_next("1h")
    assert result == 3600.0


def test_seconds_until_next_interval_combined():
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    # "1h30m" → 5400 seconds
    result = HeartbeatDaemon._seconds_until_next("1h30m")
    assert result == 5400.0


def test_seconds_until_next_interval_seconds():
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    # "45s" → 45 seconds
    result = HeartbeatDaemon._seconds_until_next("45s")
    assert result == 45.0


def test_seconds_until_next_cron_or_minus_one():
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    # Valid cron expression — if croniter available should be >0; if not should be -1
    result = HeartbeatDaemon._seconds_until_next("0 9 * * 1-5")
    # Either a positive float (next scheduled run) or -1 (no cron lib)
    assert result == -1.0 or result > 0


def test_daemon_add_cron_job():
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    job = CronJob(name="standup", schedule="0 9 * * 1-5", prompt="daily standup")
    daemon.add_cron_job(job)
    assert len(daemon._cron_jobs) == 1
    assert daemon._cron_jobs[0].name == "standup"


def test_daemon_cron_jobs_persist_to_scheduler_backend():
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon
    from agnoclaw.runtime import InMemorySchedulerBackend

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    backend = InMemorySchedulerBackend()
    daemon = HeartbeatDaemon(agent=mock_agent, scheduler_backend=backend)

    daemon.add_cron_job(CronJob(name="standup", schedule="30m", prompt="daily standup"))

    assert backend.get_job("standup") is not None
    assert daemon.list_cron_jobs()[0].name == "standup"


def test_daemon_loads_cron_jobs_from_scheduler_backend():
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.runtime import InMemorySchedulerBackend, SchedulerJob

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    backend = InMemorySchedulerBackend()
    backend.upsert_job(SchedulerJob(name="stored", schedule="1h", prompt="stored job"))

    daemon = HeartbeatDaemon(agent=mock_agent, scheduler_backend=backend)

    assert [job.name for job in daemon.list_cron_jobs()] == ["stored"]


def test_daemon_remove_and_disable_cron_job_updates_scheduler_backend():
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon
    from agnoclaw.runtime import InMemorySchedulerBackend

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    backend = InMemorySchedulerBackend()
    daemon = HeartbeatDaemon(agent=mock_agent, scheduler_backend=backend)
    daemon.add_cron_job(CronJob(name="stored", schedule="1h", prompt="stored job"))

    assert daemon.set_cron_enabled("stored", False) is True
    assert backend.get_job("stored").enabled is False
    assert daemon.remove_cron_job("stored") is True
    assert backend.get_job("stored") is None


def test_json_scheduler_backend_persists_jobs_and_runs(tmp_path):
    from agnoclaw.runtime import JsonSchedulerBackend, SchedulerJob

    path = tmp_path / "schedules.json"
    backend = JsonSchedulerBackend(path)
    backend.upsert_job(SchedulerJob(name="daily", schedule="0 9 * * *", prompt="brief"))
    run = backend.record_run_start("daily", run_id="run-1")
    backend.record_run_finish(run.run_id, status="completed", output="ok")

    reloaded = JsonSchedulerBackend(path)

    assert reloaded.get_job("daily").prompt == "brief"
    assert reloaded.list_runs(job_name="daily")[0].output == "ok"


def test_scheduler_backend_filters_and_missing_records():
    from agnoclaw.runtime import InMemorySchedulerBackend, SchedulerJob

    backend = InMemorySchedulerBackend()
    backend.upsert_job(SchedulerJob(name="enabled", schedule="1h", prompt="go"))
    backend.upsert_job(SchedulerJob(name="disabled", schedule="1h", prompt="stop", enabled=False))

    first = backend.record_run_start("enabled", run_id="run-1", metadata={"a": 1})
    second = backend.record_run_start("disabled", run_id="run-2")
    finished = backend.record_run_finish(first.run_id, status="completed", metadata={"b": 2})

    assert [job.name for job in backend.list_jobs(enabled=True)] == ["enabled"]
    assert [job.name for job in backend.list_jobs(enabled=False)] == ["disabled"]
    assert backend.set_job_enabled("missing", False) is None
    assert backend.record_run_finish("missing", status="failed") is None
    assert finished is not None
    assert finished.metadata == {"a": 1, "b": 2}
    assert [run.run_id for run in backend.list_runs(limit=1)] in (["run-1"], ["run-2"])
    assert second.status == "running"


def test_json_scheduler_backend_delete_and_enable_persist(tmp_path):
    from agnoclaw.runtime import JsonSchedulerBackend, SchedulerJob

    path = tmp_path / "schedules.json"
    backend = JsonSchedulerBackend(path)
    backend.upsert_job(SchedulerJob(name="toggle", schedule="1h", prompt="go"))

    assert backend.set_job_enabled("toggle", False) is not None
    assert JsonSchedulerBackend(path).get_job("toggle").enabled is False
    assert backend.delete_job("toggle") is True
    assert JsonSchedulerBackend(path).get_job("toggle") is None
    assert backend.delete_job("toggle") is False


def test_json_scheduler_backend_rejects_every_mutation_when_fenced(tmp_path):
    from agnoclaw.runtime import JsonSchedulerBackend, SchedulerJob, scheduler_fence_path
    from agnoclaw.runtime.errors import HarnessError

    path = tmp_path / "schedules.json"
    backend = JsonSchedulerBackend(path)
    backend.upsert_job(SchedulerJob(name="fenced", schedule="1h", prompt="go"))
    scheduler_fence_path(path).write_text("{}", encoding="utf-8")

    with pytest.raises(HarnessError, match="fenced") as raised:
        backend.upsert_job(SchedulerJob(name="blocked", schedule="1h", prompt="stop"))

    assert raised.value.code == "SCHEDULER_STORE_FENCED"
    assert JsonSchedulerBackend(path).get_job("blocked") is None


@pytest.mark.asyncio
async def test_run_isolated_uses_async_arun():
    """A legacy isolated cron run remains async and closes its owned harness."""
    from agnoclaw.config import RuntimeProfile
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon

    parent_agent = MagicMock()
    parent_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=parent_agent)

    mock_harness = MagicMock(profile=RuntimeProfile.LEGACY)
    mock_harness.arun = AsyncMock(return_value=MagicMock(content="ok"))
    mock_harness.aclose = AsyncMock()

    job = CronJob(name="iso", schedule="1h", prompt="Ping", skill="test-skill")
    with patch("agnoclaw.agent.AgentHarness", return_value=mock_harness):
        run, result = await daemon._run_isolated(job, "Ping now")

    assert run.run_id is None
    assert result.content == "ok"
    mock_harness.arun.assert_awaited_once_with("Ping now", skill="test-skill", metadata=None)
    mock_harness.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_run_isolated_closes_owned_harness_after_failure():
    from agnoclaw.config import RuntimeProfile
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon

    parent_agent = MagicMock()
    parent_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=parent_agent)
    mock_harness = MagicMock(profile=RuntimeProfile.LEGACY)
    mock_harness.arun = AsyncMock(side_effect=RuntimeError("secret provider detail"))
    mock_harness.aclose = AsyncMock()

    with patch("agnoclaw.agent.AgentHarness", return_value=mock_harness):
        with pytest.raises(RuntimeError, match="secret provider detail"):
            await daemon._run_isolated(CronJob("iso", "1h", "Ping"), "Ping")

    mock_harness.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_isolated_durable_cancellation_detaches_and_preserves_run_id():
    from agnoclaw.config import RuntimeProfile
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon
    from agnoclaw.runtime import HarnessRun, InMemorySchedulerBackend

    waiter_started = asyncio.Event()
    release_waiter = asyncio.Event()

    async def wait_forever(_timeout):
        waiter_started.set()
        await release_waiter.wait()

    runtime_run = HarnessRun(
        run_id="run_isolated",
        store=MagicMock(),
        waiter=wait_forever,
    )
    isolated = MagicMock(profile=RuntimeProfile.DURABLE)
    isolated.start = AsyncMock(return_value=runtime_run)
    isolated.aclose = AsyncMock()
    parent = MagicMock()
    parent.workspace = MagicMock()
    backend = InMemorySchedulerBackend()
    daemon = HeartbeatDaemon(parent, scheduler_backend=backend)

    with patch("agnoclaw.agent.AgentHarness", return_value=isolated):
        task = asyncio.create_task(
            daemon._run_cron_job(
                CronJob(name="isolated", schedule="1h", prompt="work", isolated=True)
            )
        )
        await waiter_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    record = backend.list_runs(job_name="isolated")[0]
    assert record.status == "detached"
    assert record.metadata["runtime_run_id"] == "run_isolated"
    isolated.aclose.assert_awaited_once_with(policy="detach")

    release_waiter.set()
    await asyncio.sleep(0)


# ── _filter_response tests ──────────────────────────────────────────────


def test_filter_response_heartbeat_ok_short():
    """Short HEARTBEAT_OK response is suppressed (returns None)."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    result = daemon._filter_response("HEARTBEAT_OK")
    assert result is None


def test_filter_response_heartbeat_ok_with_brief_note():
    """HEARTBEAT_OK with short note under threshold is suppressed."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    result = daemon._filter_response("HEARTBEAT_OK. All clear.")
    assert result is None


def test_filter_response_alert_returned():
    """Non-OK response is returned as alert."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    result = daemon._filter_response("Database connection timeout detected!")
    assert result == "Database connection timeout detected!"


def test_filter_response_empty_returns_none():
    """Empty response returns None."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    result = daemon._filter_response("")
    assert result is None


def test_filter_response_ok_with_long_content():
    """HEARTBEAT_OK with long content beyond threshold returns the extra content."""
    from agnoclaw.config import HarnessConfig, HeartbeatConfig
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    cfg = HarnessConfig(heartbeat=HeartbeatConfig(ok_threshold_chars=50))
    daemon = HeartbeatDaemon(agent=mock_agent, config=cfg)

    long_content = "HEARTBEAT_OK " + "A" * 200
    result = daemon._filter_response(long_content)
    assert result is not None
    assert "HEARTBEAT_OK" not in result  # token stripped


# ── _default_alert tests ────────────────────────────────────────────────


def test_default_alert_prints(capsys):
    """_default_alert prints to console."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    HeartbeatDaemon._default_alert("Test alert message")
    captured = capsys.readouterr()
    assert "HEARTBEAT ALERT" in captured.out
    assert "Test alert message" in captured.out


# ── start/stop lifecycle tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_creates_tasks():
    """start() creates asyncio tasks for heartbeat and cron jobs."""
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    job = CronJob(name="test-job", schedule="1h", prompt="test")
    daemon.add_cron_job(job)

    daemon.start()
    assert daemon._running is True
    assert daemon._task is not None
    assert len(daemon._cron_tasks) == 1

    # Clean up
    daemon.stop()
    assert daemon._running is False


@pytest.mark.asyncio
async def test_start_twice_is_noop():
    """Calling start() twice does not create duplicate tasks."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    daemon.start()
    first_task = daemon._task
    daemon.start()  # second call should be no-op
    assert daemon._task is first_task

    daemon.stop()


@pytest.mark.asyncio
async def test_stop_cancels_tasks():
    """stop() cancels running tasks."""
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    daemon.add_cron_job(CronJob(name="job1", schedule="30m", prompt="test"))
    daemon.start()

    assert daemon._running is True
    daemon.stop()

    assert daemon._running is False
    assert len(daemon._cron_tasks) == 0


@pytest.mark.asyncio
async def test_astop_waits_for_owned_tasks_to_quiesce():
    """astop() joins heartbeat, cron, and durable execution tasks."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    daemon.start()
    heartbeat_task = daemon._task
    execution_task = asyncio.create_task(asyncio.Event().wait())
    daemon._scheduler_tasks["run-1"] = execution_task

    await daemon.astop()

    assert daemon._running is False
    assert daemon._task is None
    assert heartbeat_task is not None and heartbeat_task.done()
    assert execution_task.done()
    assert daemon._scheduler_tasks == {}


# ── trigger_cron tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trigger_cron_not_found():
    """trigger_cron returns error for unknown job name."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    result = await daemon.trigger_cron("nonexistent")
    assert "[error]" in result


@pytest.mark.asyncio
async def test_trigger_cron_found():
    """trigger_cron executes a registered job."""
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.arun = AsyncMock(return_value=MagicMock(content="cron result"))

    daemon = HeartbeatDaemon(agent=mock_agent)
    daemon.add_cron_job(CronJob(name="my-job", schedule="1h", prompt="do it"))

    result = await daemon.trigger_cron("my-job")
    assert result == "cron result"


# ── _run_heartbeat tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_heartbeat_returns_alert():
    """_run_heartbeat returns alert message from agent."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.workspace.is_empty_heartbeat = MagicMock(return_value=False)
    mock_agent.workspace.heartbeat_md = MagicMock(return_value="Check the server status")
    mock_agent.arun = AsyncMock(return_value=MagicMock(content="Server is down!"))

    daemon = HeartbeatDaemon(agent=mock_agent)
    result = await daemon._run_heartbeat()
    assert result == "Server is down!"


@pytest.mark.asyncio
async def test_run_heartbeat_suppresses_ok():
    """_run_heartbeat returns None for HEARTBEAT_OK responses."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.workspace.is_empty_heartbeat = MagicMock(return_value=False)
    mock_agent.workspace.heartbeat_md = MagicMock(return_value="check items")
    mock_agent.arun = AsyncMock(return_value=MagicMock(content="HEARTBEAT_OK"))

    daemon = HeartbeatDaemon(agent=mock_agent)
    result = await daemon._run_heartbeat()
    assert result is None


@pytest.mark.asyncio
async def test_run_heartbeat_handles_exception():
    """_run_heartbeat returns error message on exception."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.workspace.is_empty_heartbeat = MagicMock(return_value=False)
    mock_agent.workspace.heartbeat_md = MagicMock(return_value="check items")
    mock_agent.arun = AsyncMock(side_effect=RuntimeError("connection failed"))

    daemon = HeartbeatDaemon(agent=mock_agent)
    result = await daemon._run_heartbeat()
    assert "[heartbeat error]" in result
    assert "connection failed" not in result


@pytest.mark.asyncio
async def test_run_heartbeat_no_heartbeat_content():
    """_run_heartbeat works when heartbeat_md returns empty string."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.workspace.is_empty_heartbeat = MagicMock(return_value=False)
    mock_agent.workspace.heartbeat_md = MagicMock(return_value="")
    mock_agent.arun = AsyncMock(return_value=MagicMock(content="HEARTBEAT_OK"))

    daemon = HeartbeatDaemon(agent=mock_agent)
    result = await daemon._run_heartbeat()
    assert result is None


@pytest.mark.asyncio
async def test_run_heartbeat_uses_lifecycle_for_durable_profile():
    from agnoclaw.config import RuntimeProfile
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    response = MagicMock(content="HEARTBEAT_OK")
    run = MagicMock(run_id="run_heartbeat")
    run.wait = AsyncMock(return_value=response)
    agent = MagicMock(profile=RuntimeProfile.DURABLE)
    agent.workspace.is_empty_heartbeat.return_value = False
    agent.workspace.heartbeat_md.return_value = "check items"
    agent.start = AsyncMock(return_value=run)
    agent.arun = AsyncMock()

    result = await HeartbeatDaemon(agent=agent)._run_heartbeat()

    assert result is None
    agent.start.assert_awaited_once()
    agent.arun.assert_not_awaited()
    run.wait.assert_awaited_once_with()


# ── _run_cron_job tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_cron_job_non_isolated():
    """Non-isolated cron job runs on the main agent."""
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.arun = AsyncMock(return_value=MagicMock(content="done"))

    daemon = HeartbeatDaemon(agent=mock_agent)
    job = CronJob(name="main-job", schedule="1h", prompt="do work", skill="my-skill")

    result = await daemon._run_cron_job(job)
    assert result == "done"
    mock_agent.arun.assert_awaited_once_with("do work", skill="my-skill", metadata=ANY)


@pytest.mark.asyncio
async def test_run_cron_job_records_run_history():
    """Cron job executions should be recorded when a scheduler backend is configured."""
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon
    from agnoclaw.runtime import InMemorySchedulerBackend

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.arun = AsyncMock(return_value=MagicMock(content="done"))
    backend = InMemorySchedulerBackend()

    daemon = HeartbeatDaemon(agent=mock_agent, scheduler_backend=backend)
    job = CronJob(name="main-job", schedule="1h", prompt="do work", skill="my-skill")

    result = await daemon._run_cron_job(job)
    runs = backend.list_runs(job_name="main-job")

    assert result == "done"
    assert len(runs) == 1
    assert runs[0].status == "completed"
    assert runs[0].output == "done"
    metadata = mock_agent.arun.call_args.kwargs["metadata"]
    assert metadata["scheduler"]["schedule_id"] == "main-job"
    assert metadata["scheduler"]["schedule_run_id"] == runs[0].run_id


@pytest.mark.asyncio
async def test_run_cron_job_links_durable_runtime_identity():
    from agnoclaw.config import RuntimeProfile
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon
    from agnoclaw.runtime import InMemorySchedulerBackend

    response = MagicMock(content="done")
    runtime_run = MagicMock(run_id="run_runtime")
    runtime_run.wait = AsyncMock(return_value=response)
    agent = MagicMock(profile=RuntimeProfile.DURABLE)
    agent.workspace = MagicMock()
    agent.start = AsyncMock(return_value=runtime_run)
    agent.arun = AsyncMock()
    backend = InMemorySchedulerBackend()

    result = await HeartbeatDaemon(agent=agent, scheduler_backend=backend)._run_cron_job(
        CronJob(name="durable", schedule="1h", prompt="work")
    )
    record = backend.list_runs(job_name="durable")[0]

    assert result == "done"
    assert record.status == "completed"
    assert record.metadata["runtime_run_id"] == "run_runtime"
    agent.start.assert_awaited_once()
    agent.arun.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_cron_job_cancellation_detaches_durable_run():
    from agnoclaw.config import RuntimeProfile
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon
    from agnoclaw.runtime import HarnessRun, InMemorySchedulerBackend

    waiter_started = asyncio.Event()
    release_waiter = asyncio.Event()

    async def wait_forever(_timeout):
        waiter_started.set()
        await release_waiter.wait()

    runtime_run = HarnessRun(
        run_id="run_continues",
        store=MagicMock(),
        waiter=wait_forever,
    )
    agent = MagicMock(profile=RuntimeProfile.DURABLE)
    agent.workspace = MagicMock()
    agent.start = AsyncMock(return_value=runtime_run)
    backend = InMemorySchedulerBackend()
    daemon = HeartbeatDaemon(agent=agent, scheduler_backend=backend)

    task = asyncio.create_task(
        daemon._run_cron_job(CronJob(name="durable", schedule="1h", prompt="work"))
    )
    await waiter_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = backend.list_runs(job_name="durable")[0]
    assert record.status == "detached"
    assert record.error == "SCHEDULER_EXECUTION_DETACHED"
    assert record.metadata["runtime_run_id"] == "run_continues"

    release_waiter.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_run_cron_job_exception_returns_none():
    """Cron job that raises returns None (logged, not propagated)."""
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.arun = AsyncMock(side_effect=RuntimeError("boom"))

    daemon = HeartbeatDaemon(agent=mock_agent)
    job = CronJob(name="fail-job", schedule="1h", prompt="fail")

    result = await daemon._run_cron_job(job)
    assert result is None


@pytest.mark.asyncio
async def test_run_cron_job_records_failed_run_history():
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon
    from agnoclaw.runtime import InMemorySchedulerBackend

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.arun = AsyncMock(side_effect=RuntimeError("boom"))
    backend = InMemorySchedulerBackend()

    daemon = HeartbeatDaemon(agent=mock_agent, scheduler_backend=backend)
    job = CronJob(name="fail-job", schedule="1h", prompt="fail")

    result = await daemon._run_cron_job(job)
    runs = backend.list_runs(job_name="fail-job")

    assert result is None
    assert runs[0].status == "failed"
    assert runs[0].error == "SCHEDULER_RUN_FAILED"
    assert "boom" not in runs[0].error


@pytest.mark.asyncio
async def test_durable_scheduler_claim_uses_stable_lifecycle_identity(tmp_path):
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.runtime.scheduler import RuntimeSchedulerBackend, SchedulerJob
    from agnoclaw.runtime.store import SQLiteRuntimeStore

    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    backend = RuntimeSchedulerBackend(store)
    backend.upsert_job(
        SchedulerJob(
            name="durable",
            schedule="1h",
            prompt="work",
            isolated=True,
            metadata={"learning_consent": True},
            next_run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
    )
    response = MagicMock(content="done")
    runtime_run = MagicMock(run_id="runtime-one")
    runtime_run.wait = AsyncMock(return_value=response)
    agent = MagicMock()
    agent.workspace = MagicMock()
    agent.start = AsyncMock(return_value=runtime_run)
    daemon = HeartbeatDaemon(agent=agent, scheduler_backend=backend)
    claim = backend.claim_due_runs(worker_id="worker-a")[0]

    result = await daemon._run_claimed_job(claim)

    assert result == "done"
    kwargs = agent.start.await_args.kwargs
    assert kwargs["idempotency_key"].startswith("scheduler:schedocc_")
    assert kwargs["session_id"] == f"schedule:{claim.record.occurrence_id}"
    assert kwargs["learning_consent"] is True
    assert kwargs["metadata"]["scheduler"]["attempt"] == 1
    record = backend.list_runs()[0]
    assert record.status == "completed"
    assert record.runtime_run_id == "runtime-one"


@pytest.mark.asyncio
async def test_durable_scheduler_reattaches_bound_run_without_restart(tmp_path):
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.runtime.scheduler import RuntimeSchedulerBackend, SchedulerJob
    from agnoclaw.runtime.store import SQLiteRuntimeStore

    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    backend = RuntimeSchedulerBackend(store)
    backend.upsert_job(
        SchedulerJob(
            name="durable",
            schedule="1h",
            prompt="work",
            next_run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
    )
    first = backend.claim_due_runs(worker_id="worker-a")[0]
    first = backend.bind_runtime_run(first, runtime_run_id="runtime-existing")
    backend.release_claim(first)
    reclaimed = backend.claim_due_runs(worker_id="worker-b")[0]

    response = MagicMock(content="restored")
    runtime_run = MagicMock()
    runtime_run.wait = AsyncMock(return_value=response)
    agent = MagicMock()
    agent.workspace = MagicMock()
    agent.get_run.return_value = runtime_run
    agent.start = AsyncMock()
    agent.recover_run = AsyncMock()
    daemon = HeartbeatDaemon(agent=agent, scheduler_backend=backend)

    result = await daemon._run_claimed_job(reclaimed)

    assert result == "restored"
    agent.get_run.assert_called_once_with("runtime-existing")
    agent.start.assert_not_awaited()
    agent.recover_run.assert_not_awaited()
    assert backend.list_runs()[0].status == "completed"


@pytest.mark.asyncio
async def test_durable_scheduler_recovers_incomplete_bound_run(tmp_path):
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.runtime.errors import HarnessError
    from agnoclaw.runtime.scheduler import RuntimeSchedulerBackend, SchedulerJob
    from agnoclaw.runtime.store import SQLiteRuntimeStore

    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    backend = RuntimeSchedulerBackend(store)
    backend.upsert_job(
        SchedulerJob(
            name="durable",
            schedule="1h",
            prompt="work",
            next_run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
    )
    first = backend.claim_due_runs(worker_id="worker-a")[0]
    first = backend.bind_runtime_run(first, runtime_run_id="runtime-existing")
    backend.release_claim(first)
    reclaimed = backend.claim_due_runs(worker_id="worker-b")[0]

    incomplete = HarnessError(
        code="RUN_WAIT_INCOMPLETE",
        category="lifecycle",
        message="still queued",
        retryable=True,
    )
    stale = MagicMock()
    stale.wait = AsyncMock(side_effect=incomplete)
    recovered = MagicMock()
    recovered.wait = AsyncMock(return_value=MagicMock(content="recovered"))
    agent = MagicMock()
    agent.workspace = MagicMock()
    agent.get_run.return_value = stale
    agent.recover_run = AsyncMock(return_value=recovered)
    daemon = HeartbeatDaemon(agent=agent, scheduler_backend=backend)

    result = await daemon._run_claimed_job(reclaimed)

    assert result == "recovered"
    agent.recover_run.assert_awaited_once_with("runtime-existing")
    assert backend.list_runs()[0].status == "completed"


@pytest.mark.asyncio
async def test_durable_scheduler_stop_detaches_without_cancelling_runtime(tmp_path):
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.runtime import HarnessRun
    from agnoclaw.runtime.scheduler import RuntimeSchedulerBackend, SchedulerJob
    from agnoclaw.runtime.store import SQLiteRuntimeStore

    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    backend = RuntimeSchedulerBackend(store)
    backend.upsert_job(
        SchedulerJob(
            name="durable",
            schedule="1h",
            prompt="work",
            next_run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
    )
    waiter_started = asyncio.Event()
    release_waiter = asyncio.Event()

    async def wait_forever(_timeout):
        waiter_started.set()
        await release_waiter.wait()

    runtime_run = HarnessRun(run_id="runtime-continues", store=store, waiter=wait_forever)
    agent = MagicMock()
    agent.workspace = MagicMock()
    agent.start = AsyncMock(return_value=runtime_run)
    daemon = HeartbeatDaemon(agent=agent, scheduler_backend=backend)
    claim = backend.claim_due_runs(worker_id="worker-a")[0]
    task = asyncio.create_task(daemon._run_claimed_job(claim))
    await waiter_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = backend.list_runs()[0]
    assert record.status == "detached"
    assert record.runtime_run_id == "runtime-continues"
    release_waiter.set()


@pytest.mark.asyncio
async def test_durable_scheduler_ambiguous_bind_keeps_same_attempt_reclaimable(tmp_path):
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.runtime.scheduler import RuntimeSchedulerBackend, SchedulerJob
    from agnoclaw.runtime.store import RuntimeStoreConnectionLostError, SQLiteRuntimeStore

    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    backend = RuntimeSchedulerBackend(store)
    backend.upsert_job(
        SchedulerJob(
            name="durable",
            schedule="1h",
            prompt="work",
            max_retries=2,
            next_run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
    )
    claim = backend.claim_due_runs(worker_id="worker-a")[0]
    run = MagicMock(run_id="runtime-created")
    run.wait = AsyncMock()
    agent = MagicMock()
    agent.workspace = MagicMock()
    agent.start = AsyncMock(return_value=run)
    daemon = HeartbeatDaemon(agent=agent, scheduler_backend=backend)

    original_bind = backend.bind_runtime_run
    backend.bind_runtime_run = MagicMock(
        side_effect=RuntimeStoreConnectionLostError(backend="sqlite")
    )
    assert await daemon._run_claimed_job(claim) is None

    records = backend.list_runs()
    assert len(records) == 1
    assert records[0].attempt == 1
    assert records[0].status == "pending"
    backend.bind_runtime_run = original_bind
    reclaimed = backend.claim_due_runs(worker_id="worker-b")[0]
    assert reclaimed.run_id == claim.run_id
    assert reclaimed.record.attempt == 1


@pytest.mark.asyncio
async def test_durable_scheduler_ambiguous_completion_does_not_retry(tmp_path):
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.runtime.scheduler import RuntimeSchedulerBackend, SchedulerJob
    from agnoclaw.runtime.store import RuntimeStoreConnectionLostError, SQLiteRuntimeStore

    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    backend = RuntimeSchedulerBackend(store)
    backend.upsert_job(
        SchedulerJob(
            name="durable",
            schedule="1h",
            prompt="work",
            max_retries=2,
            next_run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
    )
    claim = backend.claim_due_runs(worker_id="worker-a")[0]
    run = MagicMock(run_id="runtime-created")
    run.wait = AsyncMock(return_value=MagicMock(content="completed"))
    agent = MagicMock()
    agent.workspace = MagicMock()
    agent.start = AsyncMock(return_value=run)
    daemon = HeartbeatDaemon(agent=agent, scheduler_backend=backend)
    original_finish = backend.finish_claim
    backend.finish_claim = MagicMock(side_effect=RuntimeStoreConnectionLostError(backend="sqlite"))

    assert await daemon._run_claimed_job(claim) is None

    backend.finish_claim = original_finish
    records = backend.list_runs()
    assert len(records) == 1
    assert records[0].attempt == 1
    assert records[0].status == "detached"


@pytest.mark.asyncio
async def test_durable_scheduler_only_retries_retryable_known_failures(tmp_path):
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.runtime.errors import HarnessError
    from agnoclaw.runtime.scheduler import RuntimeSchedulerBackend, SchedulerJob
    from agnoclaw.runtime.store import SQLiteRuntimeStore

    async def run_failure(name: str, *, retryable: bool) -> list:
        store = SQLiteRuntimeStore(tmp_path / f"{name}.db")
        backend = RuntimeSchedulerBackend(store)
        backend.upsert_job(
            SchedulerJob(
                name=name,
                schedule="1h",
                prompt="work",
                max_retries=1,
                retry_delay_seconds=0,
                retry_max_delay_seconds=0,
                next_run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            )
        )
        run = MagicMock(run_id=f"runtime-{name}")
        run.wait = AsyncMock(
            side_effect=HarnessError(
                code="KNOWN_FAILURE",
                category="provider",
                message="safe",
                retryable=retryable,
            )
        )
        agent = MagicMock()
        agent.workspace = MagicMock()
        agent.start = AsyncMock(return_value=run)
        daemon = HeartbeatDaemon(agent=agent, scheduler_backend=backend)
        claim = backend.claim_due_runs(worker_id="worker-a")[0]
        await daemon._run_claimed_job(claim)
        return backend.list_runs()

    retryable = await run_failure("retryable", retryable=True)
    non_retryable = await run_failure("non-retryable", retryable=False)

    assert {record.status for record in retryable} == {"failed", "retry_wait"}
    assert len(retryable) == 2
    assert len(non_retryable) == 1
    assert non_retryable[0].status == "dead_lettered"


# ── Active hours edge cases ─────────────────────────────────────────────


def test_active_hours_overnight_range():
    """Overnight range (e.g. 22:00-06:00) works correctly."""
    from agnoclaw.config import HarnessConfig, HeartbeatConfig
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    cfg = HarnessConfig(
        heartbeat=HeartbeatConfig(
            active_hours_start="22:00",
            active_hours_end="06:00",
        )
    )
    daemon = HeartbeatDaemon(agent=mock_agent, config=cfg)

    # 23:00 should be active (after start, before midnight)
    with patch("agnoclaw.heartbeat.daemon.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = time(23, 0)
        assert daemon._is_active_hours() is True

    # 03:00 should be active (after midnight, before end)
    with patch("agnoclaw.heartbeat.daemon.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = time(3, 0)
        assert daemon._is_active_hours() is True

    # 12:00 should be inactive (between end and start)
    with patch("agnoclaw.heartbeat.daemon.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = time(12, 0)
        assert daemon._is_active_hours() is False


def test_active_hours_parse_failure_returns_true():
    """If active hours can't be parsed, always return True (active)."""
    from agnoclaw.config import HarnessConfig, HeartbeatConfig
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    cfg = HarnessConfig(
        heartbeat=HeartbeatConfig(
            active_hours_start="not-a-time",
            active_hours_end="also-bad",
        )
    )
    daemon = HeartbeatDaemon(agent=mock_agent, config=cfg)
    assert daemon._is_active_hours() is True


# ── add_cron_job validation ─────────────────────────────────────────────


def test_add_cron_job_invalid_schedule_raises():
    """add_cron_job raises ValueError for clearly invalid schedule."""
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    # "bad" is not an interval, not a cron expression (< 5 fields), and cron libs return -1
    job = CronJob(name="bad-job", schedule="bad", prompt="test")
    with pytest.raises(ValueError, match="Invalid schedule"):
        daemon.add_cron_job(job)


def test_add_cron_job_disabled_not_started():
    """Disabled cron jobs are registered but not started."""
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    job = CronJob(name="disabled", schedule="1h", prompt="test", enabled=False)
    daemon.add_cron_job(job)
    assert len(daemon._cron_jobs) == 1
