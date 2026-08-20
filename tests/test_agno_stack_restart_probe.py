"""Contracts for the real-process AgentHarness/Agno restart probe."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "agno_stack_restart_probe.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agno_stack_restart_probe", PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_agno_stack_restart_probe_requires_process_crash_acknowledgement() -> None:
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


def test_agno_stack_restart_probe_declares_exact_boundaries() -> None:
    module = _module()

    assert module._SCENARIOS == (
        "planned",
        "during_dispatch",
        "after_settlement",
        "factory_digest_mismatch",
    )
    assert module._CRASH_EXIT_CODE > 0


@pytest.mark.integration
def test_agno_stack_restart_probe_kills_and_reopens_full_stack() -> None:
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
    assert report["scope"] == "real-process-agent-harness-agno-stack-restart"
    assert report["operation_kind"] == "model"
    assert report["scenarios"] == 4
    assert report["real_process_crashes"] == 4
    assert report["safe_pre_dispatch_resumes"] == 1
    assert report["reconciliation_blocks"] == 1
    assert report["durable_result_completions"] == 1
    assert report["factory_digest_mismatch_blocks"] == 1
    assert report["provider_calls"] == 3
    assert report["post_restart_provider_calls"] == 1
    assert report["blind_ambiguous_redispatches"] == 0
    assert report["duplicate_provider_calls"] == 0
    assert report["runtime_provider_and_agno_integrity"] is True
    assert report["factory_models_created"] == 13
    assert report["recovery_models_created"] == 5
    assert report["recovery_models_closed"] == 5
    assert report["startup_scan_recoveries"] == 4
    assert [item["crashed_operation_state"] for item in report["outcomes"]] == [
        "planned",
        "dispatching",
        "succeeded",
        "planned",
    ]
    assert [item["recovered_run_state"] for item in report["outcomes"]] == [
        "completed",
        "waiting_for_reconciliation",
        "completed",
        "failed",
    ]
