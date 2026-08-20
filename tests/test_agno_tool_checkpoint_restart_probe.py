"""Contracts for the real-process Agno tool-checkpoint restart probe."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "agno_tool_checkpoint_restart_probe.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agno_tool_checkpoint_restart_probe", PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_agno_tool_checkpoint_probe_requires_process_crash_acknowledgement() -> None:
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


def test_agno_tool_checkpoint_probe_declares_exact_boundaries() -> None:
    module = _module()

    assert module._SCENARIOS == (
        "after_tool_checkpoint",
        "during_second_provider",
        "after_second_settlement",
    )
    assert module._CRASH_EXIT_CODE > 0


@pytest.mark.integration
def test_agno_tool_checkpoint_probe_kills_and_recovers_full_matrix() -> None:
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
    assert report["scope"] == "real-process-agno-tool-checkpoint-restart"
    assert report["operation_kind"] == "model"
    assert report["model_construction"] == "public_agno_model_factory"
    assert report["scenarios"] == 3
    assert report["real_process_crashes"] == 3
    assert report["checkpoint_continuations"] == 2
    assert report["reconciliation_blocks"] == 1
    assert report["provider_calls"] == 6
    assert report["post_restart_provider_calls"] == 1
    assert report["tool_effects"] == 3
    assert report["duplicate_provider_calls"] == 0
    assert report["duplicate_tool_effects"] == 0
    assert report["database_integrity"] is True
    assert [item["second_provider_before_recovery"] for item in report["outcomes"]] == [
        "absent",
        "dispatching",
        "succeeded",
    ]
    assert [item["recovered_run_state"] for item in report["outcomes"]] == [
        "completed",
        "waiting_for_reconciliation",
        "completed",
    ]
