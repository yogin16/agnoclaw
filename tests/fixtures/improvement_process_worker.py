"""Owned subprocess fixture for improvement-process protocol tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from agnoclaw import EvaluationRollout, run_process_evaluation_worker


def _handler(mode: str):
    def execute(case):
        if mode == "error":
            raise ValueError("sensitive child failure must not cross the protocol")
        delta = 0.1 if mode == "candidate" else 0.0
        return EvaluationRollout(
            output={
                "quality": float(case.payload["baseline_quality"]) + delta,
                "pid": os.getpid(),
                "cwd": os.getcwd(),
                "explicit_environment": os.environ.get("AGNOCLAW_PROCESS_EXPLICIT"),
                "parent_secret_present": "AGNOCLAW_PROCESS_PARENT_SECRET" in os.environ,
            },
            tokens=2,
            cost_usd=0.02,
        )

    return execute


def main() -> int:
    mode = sys.argv[1]
    if mode in {"success", "baseline", "candidate", "error"}:
        return run_process_evaluation_worker(_handler(mode))
    if mode == "oversize":
        sys.stdout.buffer.write(b"x" * (256 * 1024))
        sys.stdout.buffer.flush()
        return 0
    if mode == "invalid":
        sys.stdout.write("private response body that must not enter evidence")
        sys.stdout.flush()
        return 0
    if mode == "crash":
        sys.stderr.write("api-key=must-not-enter-parent-errors")
        sys.stderr.flush()
        return 7
    if mode == "hang":
        Path(sys.argv[2]).write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(60)
        return 0
    if mode == "sleep":
        time.sleep(60)
        return 0
    if mode == "spawn":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
        )
        Path(sys.argv[2]).write_text(
            f"{os.getpid()}\n{child.pid}\n",
            encoding="utf-8",
        )
        time.sleep(60)
        return 0
    raise ValueError("unknown fixture mode")


if __name__ == "__main__":
    raise SystemExit(main())
