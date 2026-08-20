"""Deterministic long-run context continuity certification contracts."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "long_run_continuity_probe.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("long_run_continuity_probe", PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_long_run_probe_requires_a_real_100_turn_minimum() -> None:
    completed = subprocess.run(
        [sys.executable, str(PROBE), "--turns", "99"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 2
    assert "--turns must be at least 100" in completed.stderr


def test_long_run_live_provider_requires_explicit_acknowledgement() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "--provider",
            "ollama",
            "--turns",
            "100",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 2
    assert "--allow-live-model is required" in completed.stderr


def test_long_run_probe_validates_restart_boundaries_and_evidence_ownership(
    tmp_path: Path,
) -> None:
    module = _module()

    assert module._parse_restart_turns("20,80", turns=100) == (20, 80)
    with pytest.raises(module.ProbeConfigurationError, match="unique and increasing"):
        module._parse_restart_turns("80,20", turns=100)
    with pytest.raises(module.ProbeConfigurationError, match="smaller than --turns"):
        module._parse_restart_turns("100", turns=100)

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    sentinel = evidence / "operator-owned"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(module.ProbeConfigurationError, match="must not already contain"):
        module._evidence_context(evidence)
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_long_run_probe_tool_model_uses_real_agno_tool_call_shape_exactly_once() -> None:
    module = _module()
    marker = module._tool_marker(7, label="head")
    tracker = module.ContinuityToolTracker({7: marker})
    model = module.ContinuityModel(
        id="continuity-model-test",
        tool_turns=frozenset({7}),
    )

    first = model.invoke(
        messages=[type("Message", (), {"role": "user", "content": module._prompt(7, markers={})})()]
    )
    assert first.tool_calls == [
        {
            "id": "continuity-tool-call-007",
            "type": "function",
            "function": {
                "name": "continuity_probe_fact",
                "arguments": '{"turn":7}',
            },
        }
    ]
    assert tracker.continuity_probe_fact(7).endswith(f"Durable fact: {marker}.")
    with pytest.raises(AssertionError, match="duplicated"):
        tracker.continuity_probe_fact(7)

    second = model.invoke(messages=[type("Message", (), {"role": "tool", "content": marker})()])
    assert second.content == "Synthetic tool-bearing step acknowledged."


@pytest.mark.integration
def test_long_run_probe_exercises_compaction_reopen_and_rehydration() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "--turns",
            "100",
            "--restart-turns",
            "30,70",
            "--max-context-tokens",
            "1800",
            "--tool-every",
            "10",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "passed"
    assert report["protocol_version"] == "1.1"
    assert report["scope"] == "deterministic-local-agno-sqlite-context-continuity"
    assert report["turns"] == 100
    assert report["scheduled_restarts"] == 2
    assert report["verified_reopens"] == 3
    assert report["compactions"] >= 3
    assert report["automatic_compactions"] == report["compactions"] - 1
    assert report["retrieval_checks"] == 3
    assert report["rehydrated_items"] == 3
    assert report["rehydration_persisted_exactly_once"] is True
    assert report["manifest_content_free"] is True
    assert report["artifact_integrity_checks"] == report["compactions"]
    assert report["checkpoint_sequence_contiguous"] is True
    assert report["cross_process_lock"] == "local-file-rw-v1"
    assert report["live_context_bounded"] is True
    assert report["final_context_tokens"] < report["max_context_tokens"]
    assert report["external_model_calls"] == 0
    assert report["external_model_calls_observed_minimum"] == 0
    assert report["provider"] == "deterministic"
    assert report["provider_host_class"] == "none"
    assert report["model_digest"].startswith("sha256:")
    assert report["model_configuration_digest"].startswith("sha256:")
    assert report["deterministic_model_calls"] >= 111
    assert report["tool_mode"] == "agno-native-deterministic"
    assert report["planned_tool_turns"] == 11
    assert report["tool_calls"] == report["planned_tool_turns"]
    assert report["tool_results_retrieved"] == 3
    assert report["tool_results_rehydrated"] == 3
    assert report["tool_results_exactly_once"] is True
    assert report["evidence_retained"] is False
    assert "continuity-head" not in completed.stdout
    assert "continuity-middle" not in completed.stdout
    assert "continuity-tail" not in completed.stdout
    assert "tool-continuity" not in completed.stdout


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("AGNOCLAW_RUN_LIVE_CONTINUITY") != "1",
    reason="set AGNOCLAW_RUN_LIVE_CONTINUITY=1 with a loopback Ollama daemon",
)
def test_long_run_probe_exercises_live_ollama_tools_and_compaction() -> None:
    completed = subprocess.run(
        [
            "uv",
            "run",
            "--isolated",
            "--extra",
            "local",
            "python",
            "-W",
            "error::ResourceWarning",
            str(PROBE),
            "--turns",
            "100",
            "--restart-turns",
            "30,70",
            "--max-context-tokens",
            "1800",
            "--tool-every",
            "10",
            "--provider",
            "ollama",
            "--model",
            os.environ.get("AGNOCLAW_LIVE_CONTINUITY_MODEL", "qwen2.5:7b"),
            "--allow-live-model",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=1_800,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "passed"
    assert report["scope"] == "live-ollama-agno-sqlite-context-continuity"
    assert report["provider"] == "ollama"
    assert report["provider_host_class"] == "loopback"
    assert report["turns"] == 100
    assert report["tool_calls"] == report["planned_tool_turns"] == 11
    assert report["tool_results_exactly_once"] is True
    assert report["verified_reopens"] == 3
    assert report["compactions"] >= 3
    assert report["external_model_calls"] is None
    assert report["external_model_calls_observed_minimum"] == 111
    assert report["final_context_tokens"] < report["max_context_tokens"]
