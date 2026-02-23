"""
Cron jobs example — scheduled agent tasks beyond heartbeat.

Demonstrates agnoclaw's CronJob system, inspired by OpenClaw's CronManager.

OpenClaw has two distinct schedulers inside the same Gateway daemon:
  1. Heartbeat  — interval-based (30m default), main session, HEARTBEAT_OK suppression
  2. Cron jobs  — expression-based OR interval string, main OR isolated session

Neither uses OS-level cron. Both live inside the same asyncio event loop.

Run:
    uv run python examples/cron_jobs.py

No API key needed — uses Ollama (qwen3:0.6b) by default.
Set AGNOCLAW_TEST_PROVIDER=anthropic for cloud inference.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

PROVIDER = os.environ.get("AGNOCLAW_TEST_PROVIDER", "ollama")
MODEL = os.environ.get(
    "AGNOCLAW_TEST_MODEL",
    "qwen3:0.6b" if PROVIDER == "ollama" else "claude-haiku-4-5-20251001",
)


def _check_ollama() -> bool:
    """Check if Ollama is running."""
    if PROVIDER != "ollama":
        return True
    try:
        import httpx
        r = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


# ── Part 1: Interval-string scheduling ────────────────────────────────────────

def demo_interval_parsing():
    """
    Show that HeartbeatDaemon._seconds_until_next() understands interval strings.
    No API calls needed — pure schedule parsing.
    """
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon

    print("=== Schedule Parsing ===")
    cases = [
        ("30m",    "30 minutes   → 1800s"),
        ("1h",     "1 hour       → 3600s"),
        ("2h30m",  "2.5 hours    → 9000s"),
        ("45s",    "45 seconds   →   45s"),
        ("6h",     "6 hours      → 21600s"),
    ]

    for schedule, label in cases:
        seconds = HeartbeatDaemon._seconds_until_next(schedule)
        print(f"  '{schedule}' ({label}) = {seconds:.0f}s")

    # Cron expression (requires 'croniter': uv add croniter)
    cron_seconds = HeartbeatDaemon._seconds_until_next("0 9 * * 1-5")
    if cron_seconds > 0:
        hours = cron_seconds / 3600
        print(f"  '0 9 * * 1-5' (9am Mon-Fri) = {hours:.1f}h from now")
    else:
        print("  '0 9 * * 1-5' = (install croniter for cron expression support)")
    print()


# ── Part 2: CronJob dataclass ──────────────────────────────────────────────────

def demo_cron_job_api():
    """Show the CronJob API without running anything."""
    from agnoclaw.heartbeat.daemon import CronJob

    print("=== CronJob Dataclass ===")

    # Interval-based, main session
    disk_check = CronJob(
        name="disk-check",
        schedule="1h",
        prompt="Check if disk usage exceeds 80%%. Alert if so, HEARTBEAT_OK otherwise.",
    )
    print(f"  {disk_check.name}: schedule='{disk_check.schedule}' isolated={disk_check.isolated}")

    # Cron expression, isolated session, skill-activated
    standup = CronJob(
        name="daily-standup",
        schedule="0 9 * * 1-5",   # 9am Monday-Friday
        prompt="Generate today's standup from recent git history.",
        skill="daily-standup",
        isolated=True,             # fresh session — no conversation bleed
    )
    print(f"  {standup.name}: schedule='{standup.schedule}' isolated={standup.isolated} skill={standup.skill}")

    # One-shot at specific time (demonstrates override model)
    weekly_review = CronJob(
        name="weekly-review",
        schedule="0 17 * * 5",    # 5pm Friday
        prompt="Summarize this week's git commits and open issues.",
        isolated=True,
        model_id="claude-haiku-4-5-20251001",  # cheap model for this job
    )
    print(f"  {weekly_review.name}: schedule='{weekly_review.schedule}' model_id={weekly_review.model_id}")
    print()


# ── Part 3: Daemon with jobs (live run) ───────────────────────────────────────

async def demo_live_daemon(tmp_workspace: Path):
    """
    Start a daemon with two cron jobs, trigger them manually to show execution.
    Runs with a 5-second loop for demo purposes.
    """
    from agnoclaw import AgentHarness
    from agnoclaw.heartbeat.daemon import HeartbeatDaemon, CronJob

    print("=== Live Daemon Demo ===")

    # Write a minimal HEARTBEAT.md so the heartbeat doesn't skip
    heartbeat_file = tmp_workspace / "HEARTBEAT.md"
    heartbeat_file.write_text(
        "# Heartbeat\n\n- Check that Python is available\n\nReply HEARTBEAT_OK if OK.\n"
    )

    agent = AgentHarness(
        provider=PROVIDER,
        model_id=MODEL,
        workspace_dir=tmp_workspace,
        session_id="cron-demo",
    )

    alerts = []

    def on_alert(msg: str):
        alerts.append(msg)
        print(f"\n  [ALERT] {msg[:120]}...")

    daemon = HeartbeatDaemon(agent, on_alert=on_alert)

    # Job 1: interval string, main session
    daemon.add_cron_job(CronJob(
        name="health-check",
        schedule="5m",       # would fire every 5 min in production
        prompt="Reply with exactly: HEALTH_OK",
    ))

    # Job 2: interval string, isolated session
    daemon.add_cron_job(CronJob(
        name="isolated-task",
        schedule="10m",      # every 10 min
        prompt="Reply with exactly: ISOLATED_OK",
        isolated=True,
    ))

    print(f"  Registered {len(daemon._cron_jobs)} cron jobs")

    # Manually trigger each job (don't wait for schedule)
    print("  Triggering 'health-check' manually...")
    result = await daemon.trigger_cron("health-check")
    print(f"  Result: {str(result)[:80] if result else 'None'}")

    print("  Triggering 'isolated-task' manually...")
    result = await daemon.trigger_cron("isolated-task")
    print(f"  Result: {str(result)[:80] if result else 'None'}")

    # Also trigger the heartbeat
    print("  Triggering heartbeat manually...")
    result = await daemon.trigger_now()
    print(f"  Heartbeat: {'HEARTBEAT_OK' if result is None else result[:80]}")
    print()


# ── Part 4: Service install (show command, don't execute) ─────────────────────

def demo_service_install():
    """Show the service install command (informational only)."""
    import platform
    import shutil

    os_name = platform.system()
    print("=== Service Install (persistent daemon) ===")
    print(f"  OS: {os_name}")

    if os_name == "Darwin":
        print("  Install (macOS launchd LaunchAgent):")
        print("    agnoclaw heartbeat install-service --interval 30")
        print("  Uninstall:")
        print("    agnoclaw heartbeat install-service --uninstall")
        print("  Logs: ~/.agnoclaw/logs/heartbeat.log")
        print("  Effect: starts on login, survives terminal close")
    elif os_name == "Linux":
        print("  Install (Linux systemd user service):")
        print("    agnoclaw heartbeat install-service --interval 30")
        print("  Status:")
        print("    systemctl --user status agnoclaw-heartbeat")
        print("  Uninstall:")
        print("    agnoclaw heartbeat install-service --uninstall")
    else:
        print(f"  Not supported on {os_name}.")
        print("  Run 'agnoclaw heartbeat start' inside tmux/screen for persistence.")
    print()


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    import tempfile

    print("agnoclaw Cron Jobs Demo")
    print("=" * 50)
    print()

    # Part 1 — no API needed
    demo_interval_parsing()

    # Part 2 — no API needed
    demo_cron_job_api()

    # Part 3 — live run with Ollama/cloud
    if _check_ollama() or PROVIDER != "ollama":
        with tempfile.TemporaryDirectory() as tmp:
            await demo_live_daemon(Path(tmp))
    else:
        print("=== Live Daemon Demo ===")
        print("  (Skipped: Ollama not running. Start with: ollama serve)")
        print()

    # Part 4 — informational only
    demo_service_install()

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
