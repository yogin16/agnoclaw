"""
Cookbook 10: Scheduler Persistence & CLI Management

Demonstrates:
- JsonSchedulerBackend — persistent JSON-backed schedule storage
- SchedulerJob — defining jobs with cron/interval schedules
- SchedulerRunRecord — tracking run history
- CRUD operations: upsert, list, get, delete, enable/disable
- CLI equivalent: `agnoclaw schedule` subcommands

Run: uv run python cookbook/10_scheduler_persistence.py
"""

import tempfile
from datetime import datetime

from agnoclaw.runtime import (
    JsonSchedulerBackend,
    SchedulerJob,
)


def main():
    # ── Create a persistent backend backed by a temp JSON file ─────────────
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        f.write("{}")
        db_path = f.name

    backend = JsonSchedulerBackend(db_path)
    print(f"Backend initialized at: {db_path}\n")

    # ── Schedule jobs ──────────────────────────────────────────────────────
    daily = SchedulerJob(
        name="daily-report",
        schedule="0 8 * * *",  # cron: every day at 8am
        prompt="Generate the daily status report from recent commits",
        skill="report",
        model_id="anthropic:claude-sonnet-4-6",
        metadata={"team": "platform", "priority": "high"},
    )
    hourly = SchedulerJob(
        name="health-check",
        schedule="60m",  # simple interval: every 60 minutes
        prompt="Check system health and alert on anomalies",
        enabled=True,
    )
    disabled_job = SchedulerJob(
        name="stale-cleanup",
        schedule="24h",
        prompt="Clean up stale workspace files",
        enabled=False,  # created but not active
    )

    backend.upsert_job(daily)
    backend.upsert_job(hourly)
    backend.upsert_job(disabled_job)
    print("Jobs created:\n")

    # ── List jobs ──────────────────────────────────────────────────────────
    all_jobs = backend.list_jobs()
    print(f"Total jobs: {len(all_jobs)}")
    for j in all_jobs:
        print(f"  {j.name:20s} schedule={j.schedule:10s} enabled={j.enabled}")

    active_jobs = backend.list_jobs(enabled=True)
    print(f"\nActive jobs: {len(active_jobs)}")

    # ── Get job by name ────────────────────────────────────────────────────
    job = backend.get_job("daily-report")
    assert job is not None
    print(f"\nFetched: {job.name} — prompt: {job.prompt[:60]}...")

    # ── Enable/disable ─────────────────────────────────────────────────────
    backend.set_job_enabled("stale-cleanup", enabled=True)
    job2 = backend.get_job("stale-cleanup")
    assert job2 is not None and job2.enabled
    print(f"Enabled stale-cleanup: {job2.enabled}")

    # ── Record run history ─────────────────────────────────────────────────
    record = backend.record_run_start(
        "daily-report",
        metadata={"trigger": "manual", "timestamp": datetime.utcnow().isoformat()},
    )
    print(f"\nRun started: {record.run_id} @ {record.started_at}")

    backend.record_run_finish(
        record.run_id,
        status="completed",
        output="Report generated successfully\n- 12 commits processed\n- 0 failures",
        metadata={"duration_ms": 3450},
    )

    # ── List run history ───────────────────────────────────────────────────
    runs = backend.list_runs(job_name="daily-report")
    print(f"\nRun history for daily-report ({len(runs)} runs):")
    for r in runs:
        print(f"  [{r.status}] started={r.started_at} finished={r.finished_at}")

    # ── Delete a job ───────────────────────────────────────────────────────
    backend.delete_job("stale-cleanup")
    assert backend.get_job("stale-cleanup") is None
    print(f"\nDeleted 'stale-cleanup'. Remaining jobs: {len(backend.list_jobs())}")

    # ── CLI equivalent ─────────────────────────────────────────────────────
    print("\n---")
    print("CLI equivalent commands:")
    print("  agnoclaw schedule add daily-report \\")
    print("    --schedule '0 8 * * *' --prompt 'Generate report' --skill report")
    print("  agnoclaw schedule list")
    print("  agnoclaw schedule show daily-report")
    print("  agnoclaw schedule enable stale-cleanup")
    print("  agnoclaw schedule disable stale-cleanup")
    print("  agnoclaw schedule trigger daily-report")
    print("  agnoclaw schedule runs daily-report")
    print("  agnoclaw schedule remove stale-cleanup")


if __name__ == "__main__":
    main()
