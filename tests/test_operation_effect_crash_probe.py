"""Contracts for the real-process capability-effect crash probe."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "operation_effect_crash_probe.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("operation_effect_crash_probe", PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_effect_crash_probe_requires_explicit_process_crash_acknowledgement() -> None:
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


def test_effect_crash_probe_validates_bounds_and_effect_recovery_classes() -> None:
    module = _module()

    for effect in module.EffectClass:
        intent = module._intent("run-1", effect)
        assert intent.kind is module.OperationKind.CAPABILITY
        assert intent.effect_class is effect
        assert bool(intent.idempotency_key) is (effect is module.EffectClass.IDEMPOTENT)

    args = module._parser().parse_args(["--allow-process-crash", "--iterations", "101"])
    with pytest.raises(module.ProbeConfigurationError, match="between 1 and 100"):
        module._validate_args(args)


@pytest.mark.integration
def test_effect_crash_probe_runs_real_process_matrix() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "--allow-process-crash",
            "--iterations",
            "1",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["operation_kind"] == "capability"
    assert report == {
        "blind_ambiguous_redispatches": 0,
        "duplicate_external_effects": 0,
        "external_effect_commits": 4,
        "iterations": 1,
        "operation_kind": "capability",
        "provider_delivery_attempts": 8,
        "real_process_crashes": 8,
        "reconciliation_blocks": 4,
        "runtime_and_provider_integrity": True,
        "safe_retries": 4,
        "scenarios": 8,
        "scope": "real-process-operation-gateway-capability-effect-crash",
        "status": "passed",
    }
