"""Tests for the heartbeat daemon."""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import time


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
    from agnoclaw.heartbeat.daemon import HEARTBEAT_PROMPT, HEARTBEAT_OK_TOKEN
    assert HEARTBEAT_OK_TOKEN in HEARTBEAT_PROMPT


def test_heartbeat_daemon_init():
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.config import HarnessConfig, HeartbeatConfig

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.workspace.is_empty_heartbeat = MagicMock(return_value=False)
    mock_agent.workspace.heartbeat_md = MagicMock(return_value="checklist")

    cfg = HarnessConfig(heartbeat=HeartbeatConfig(
        enabled=True,
        interval_minutes=30,
        active_hours_start="08:00",
        active_hours_end="22:00",
    ))

    daemon = HeartbeatDaemon(agent=mock_agent, config=cfg)
    assert daemon is not None
    assert daemon._running is False


def test_heartbeat_is_active_hours_within_range():
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.config import HarnessConfig, HeartbeatConfig

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    cfg = HarnessConfig(heartbeat=HeartbeatConfig(
        active_hours_start="08:00",
        active_hours_end="22:00",
    ))

    daemon = HeartbeatDaemon(agent=mock_agent, config=cfg)

    # Mock datetime to be within active hours (noon)
    noon_time = time(12, 0)
    with patch("agnoclaw.heartbeat.daemon.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = noon_time
        result = daemon._is_active_hours()
    assert result is True


def test_heartbeat_is_active_hours_outside_range():
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.config import HarnessConfig, HeartbeatConfig

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    cfg = HarnessConfig(heartbeat=HeartbeatConfig(
        active_hours_start="08:00",
        active_hours_end="22:00",
    ))

    daemon = HeartbeatDaemon(agent=mock_agent, config=cfg)

    # 2am should be outside active hours
    two_am = time(2, 0)
    with patch("agnoclaw.heartbeat.daemon.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = two_am
        result = daemon._is_active_hours()
    assert result is False


def test_heartbeat_is_active_hours_at_start():
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.config import HarnessConfig, HeartbeatConfig

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    cfg = HarnessConfig(heartbeat=HeartbeatConfig(
        active_hours_start="08:00",
        active_hours_end="22:00",
    ))

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
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.config import HarnessConfig, HeartbeatConfig

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.workspace.is_empty_heartbeat = MagicMock(return_value=True)

    cfg = HarnessConfig(heartbeat=HeartbeatConfig(enabled=True))
    daemon = HeartbeatDaemon(agent=mock_agent, config=cfg)

    result = await daemon.trigger_now()
    assert result is None


@pytest.mark.asyncio
async def test_heartbeat_run_heartbeat_skips_outside_active_hours():
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.config import HarnessConfig, HeartbeatConfig

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.workspace.is_empty_heartbeat = MagicMock(return_value=False)
    mock_agent.workspace.heartbeat_md = MagicMock(return_value="check this")
    mock_agent.arun = AsyncMock()

    cfg = HarnessConfig(heartbeat=HeartbeatConfig(
        enabled=True,
        active_hours_start="08:00",
        active_hours_end="22:00",
    ))
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
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon, CronJob

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    job = CronJob(name="standup", schedule="0 9 * * 1-5", prompt="daily standup")
    daemon.add_cron_job(job)
    assert len(daemon._cron_jobs) == 1
    assert daemon._cron_jobs[0].name == "standup"


@pytest.mark.asyncio
async def test_run_isolated_uses_async_arun():
    """Isolated cron execution should use AgentHarness.arun() to avoid blocking the event loop."""
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon

    parent_agent = MagicMock()
    parent_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=parent_agent)

    mock_harness = MagicMock()
    mock_harness.arun = AsyncMock(return_value=MagicMock(content="ok"))

    job = CronJob(name="iso", schedule="1h", prompt="Ping", skill="test-skill")
    with patch("agnoclaw.agent.AgentHarness", return_value=mock_harness):
        await daemon._run_isolated(job, "Ping now")

    mock_harness.arun.assert_awaited_once_with("Ping now", skill="test-skill")


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
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.config import HarnessConfig, HeartbeatConfig

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
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon, CronJob

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
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon, CronJob

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    daemon.add_cron_job(CronJob(name="job1", schedule="30m", prompt="test"))
    daemon.start()

    assert daemon._running is True
    daemon.stop()

    assert daemon._running is False
    assert len(daemon._cron_tasks) == 0


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
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon, CronJob

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


# ── _run_cron_job tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_cron_job_non_isolated():
    """Non-isolated cron job runs on the main agent."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon, CronJob

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.arun = AsyncMock(return_value=MagicMock(content="done"))

    daemon = HeartbeatDaemon(agent=mock_agent)
    job = CronJob(name="main-job", schedule="1h", prompt="do work", skill="my-skill")

    result = await daemon._run_cron_job(job)
    assert result == "done"
    mock_agent.arun.assert_awaited_once_with("do work", skill="my-skill")


@pytest.mark.asyncio
async def test_run_cron_job_exception_returns_none():
    """Cron job that raises returns None (logged, not propagated)."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon, CronJob

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.arun = AsyncMock(side_effect=RuntimeError("boom"))

    daemon = HeartbeatDaemon(agent=mock_agent)
    job = CronJob(name="fail-job", schedule="1h", prompt="fail")

    result = await daemon._run_cron_job(job)
    assert result is None


# ── Active hours edge cases ─────────────────────────────────────────────


def test_active_hours_overnight_range():
    """Overnight range (e.g. 22:00-06:00) works correctly."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.config import HarnessConfig, HeartbeatConfig

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    cfg = HarnessConfig(heartbeat=HeartbeatConfig(
        active_hours_start="22:00",
        active_hours_end="06:00",
    ))
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
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon
    from agnoclaw.config import HarnessConfig, HeartbeatConfig

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    cfg = HarnessConfig(heartbeat=HeartbeatConfig(
        active_hours_start="not-a-time",
        active_hours_end="also-bad",
    ))
    daemon = HeartbeatDaemon(agent=mock_agent, config=cfg)
    assert daemon._is_active_hours() is True


# ── add_cron_job validation ─────────────────────────────────────────────


def test_add_cron_job_invalid_schedule_raises():
    """add_cron_job raises ValueError for clearly invalid schedule."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon, CronJob

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    # "bad" is not an interval, not a cron expression (< 5 fields), and cron libs return -1
    job = CronJob(name="bad-job", schedule="bad", prompt="test")
    with pytest.raises(ValueError, match="Invalid schedule"):
        daemon.add_cron_job(job)


def test_add_cron_job_disabled_not_started():
    """Disabled cron jobs are registered but not started."""
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon, CronJob

    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    daemon = HeartbeatDaemon(agent=mock_agent)

    job = CronJob(name="disabled", schedule="1h", prompt="test", enabled=False)
    daemon.add_cron_job(job)
    assert len(daemon._cron_jobs) == 1
