"""Real-process SQLite crash/recovery certification probe."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "sqlite_runtime_crash_probe.py"


def test_sqlite_runtime_crash_probe_requires_explicit_acknowledgement() -> None:
    completed = subprocess.run(
        [sys.executable, str(PROBE), "--iterations", "1"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 2
    assert "--allow-process-crash is required" in completed.stderr


def test_sqlite_runtime_crash_probe_kills_and_recovers_real_processes() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "--iterations",
            "2",
            "--allow-process-crash",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "passed"
    assert report["iterations"] == 2
    assert report["crash_count"] == 10
    assert report["scenarios"] == [
        "create",
        "transition",
        "operation_prepare",
        "operation_dispatch",
        "operation_settle",
    ]
    assert len(report["outcomes"]) == 5
    assert all(item["event_count"] >= 1 for item in report["outcomes"])
