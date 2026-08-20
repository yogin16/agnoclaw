"""Contracts for the real-process learning-reconciliation worker restart probe."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "learning_reconciliation_restart_probe.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "learning_reconciliation_restart_probe",
        PROBE,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_learning_reconciliation_probe_requires_crash_acknowledgement() -> None:
    completed = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 2
    assert "--allow-process-crash is required" in completed.stderr


def test_learning_reconciliation_probe_declares_bounded_lease() -> None:
    module = _module()

    assert module._CRASH_EXIT_CODE > 0
    assert module._LEASE_SECONDS == 3


@pytest.mark.integration
def test_learning_reconciliation_probe_crashes_reclaims_and_reconciles() -> None:
    completed = subprocess.run(
        [sys.executable, str(PROBE), "--allow-process-crash"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report == {
        "active_lease_steals": 0,
        "cleanup": "complete",
        "database_integrity": True,
        "duplicate_reconciliations": 0,
        "external_observations": 1,
        "final_candidate_state": "qualified",
        "lease_fence_after": 2,
        "lease_fence_before": 1,
        "promotion_redispatches": 0,
        "real_process_crashes": 1,
        "reconciled_candidates": 1,
        "released_recovery_lease": True,
        "scope": "real-process-learning-reconciliation-worker-restart",
        "status": "passed",
    }
