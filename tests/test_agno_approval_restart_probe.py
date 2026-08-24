"""Contracts for the real-process Agno durable-approval restart probe."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "agno_approval_restart_probe.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agno_approval_restart_probe", PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_agno_approval_probe_requires_process_crash_acknowledgement() -> None:
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


def test_agno_approval_probe_declares_exact_boundary() -> None:
    module = _module()

    assert module._SCENARIOS == ("during_approval_wait",)
    assert module._CRASH_EXIT_CODE > 0


@pytest.mark.integration
def test_agno_approval_probe_kills_approves_and_recovers_once() -> None:
    completed = subprocess.run(
        [sys.executable, str(PROBE), "--allow-process-crash"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=75,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "passed"
    assert report["scope"] == "real-process-agno-durable-approval-restart"
    assert report["operation_kind"] == "model"
    assert report["model_construction"] == "public_agno_model_factory"
    assert report["scenarios"] == 1
    assert report["real_process_crashes"] == 1
    assert report["approval_wait_recoveries"] == 1
    assert report["provider_calls"] == 2
    assert report["post_restart_provider_calls"] == 1
    assert report["approval_requests"] == 1
    assert report["approved_records"] == 1
    assert report["tool_effects"] == 1
    assert report["duplicate_provider_calls"] == 0
    assert report["duplicate_approval_requests"] == 0
    assert report["duplicate_tool_effects"] == 0
    assert report["database_integrity"] is True
    assert report["outcomes"] == [
        {
            "scenario": "during_approval_wait",
            "state_before_decision": "waiting_for_approval",
            "state_after_decision": "running",
            "recovered_run_state": "completed",
            "provider_calls": 2,
            "post_restart_provider_calls": 1,
            "approval_requests": 1,
            "approved_records": 1,
            "tool_effects": 1,
            "tool_checkpoint_before_decision": False,
        }
    ]
