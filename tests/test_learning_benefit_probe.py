"""Contracts for the opt-in Agno Learned Knowledge benefit probe."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from agnoclaw import EvaluationRollout, EvaluationSlice

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "learning_benefit_probe.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("learning_benefit_probe", PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_learning_benefit_probe_requires_explicit_live_model_acknowledgement() -> None:
    completed = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 2
    assert "--allow-live-model is required" in completed.stderr


def test_learning_benefit_probe_freezes_balanced_synthetic_slices() -> None:
    module = _module()
    cases = module._cases()

    assert len(cases) == 6
    assert [case.slice for case in cases].count(EvaluationSlice.HELD_IN) == 2
    assert [case.slice for case in cases].count(EvaluationSlice.HELD_OUT) == 2
    assert [case.slice for case in cases].count(EvaluationSlice.TRANSFER) == 2
    assert len({case.payload_digest for case in cases}) == len(cases)
    assert all("expected_token" not in module._input_builder(case) for case in cases)


def test_learning_benefit_probe_verifier_is_objective_and_rejects_cross_token_leakage() -> None:
    module = _module()
    case = module._cases()[0]
    expected = case.to_dict()["payload"]["expected_token"]

    correct = module._verifier(case, EvaluationRollout(output=expected))
    leaked = module._verifier(
        case,
        EvaluationRollout(output=f"{expected} QUARTZ-284"),
    )
    unknown = module._verifier(case, EvaluationRollout(output="UNKNOWN"))

    assert correct.to_dict() == {
        "quality": 1.0,
        "safety": 1.0,
        "safety_passed": True,
        "privacy_passed": True,
        "objective": True,
    }
    assert leaked.quality == 0
    assert leaked.safety_passed is False
    assert leaked.privacy_passed is False
    assert unknown.quality == 0
    assert unknown.safety_passed is True


def test_learning_benefit_probe_rejects_remote_egress_and_nonempty_evidence_dir(
    tmp_path: Path,
) -> None:
    module = _module()

    assert module._is_loopback_host("http://127.0.0.1:11434") is True
    assert module._is_loopback_host("http://[::1]:11434") is True
    assert module._is_loopback_host("https://models.example.test") is False
    with pytest.raises(module.ProbeConfigurationError, match="without credentials"):
        module._is_loopback_host("http://user:secret@127.0.0.1:11434")

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "owned-by-operator").write_text("preserve", encoding="utf-8")
    with pytest.raises(module.ProbeConfigurationError, match="must not already contain"):
        module._evidence_context(evidence)
    assert (evidence / "owned-by-operator").read_text(encoding="utf-8") == "preserve"


def test_learning_benefit_probe_resolves_only_an_exact_or_latest_model_tag() -> None:
    module = _module()
    digest = "sha256:" + "a" * 64
    inventory = {"nomic-embed-text:latest": digest, "qwen3:0.6b": digest}

    assert module._resolve_model(inventory, "nomic-embed-text") == (
        "nomic-embed-text:latest",
        digest,
    )
    assert module._resolve_model(inventory, "qwen3:0.6b") == ("qwen3:0.6b", digest)
    with pytest.raises(module.ProbeConfigurationError, match="missing required model"):
        module._resolve_model(inventory, "not-installed")
