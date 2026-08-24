"""Opt-in live Ollama proof for Agno Learned Knowledge outcome benefit."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests._ollama import ollama_available

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "learning_benefit_probe.py"

pytestmark = [pytest.mark.integration, pytest.mark.live_model]


def test_agno_learned_knowledge_beats_no_learning_control() -> None:
    pytest.importorskip("ollama")
    pytest.importorskip("lancedb")
    if not ollama_available():
        pytest.skip("Ollama is unavailable")

    completed = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::ResourceWarning",
            str(PROBE),
            "--allow-live-model",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert completed.returncode == 0, (
        f"stderr:\n{completed.stderr}\nstdout:\n{completed.stdout}"
    )
    report = json.loads(completed.stdout)
    assert report["status"] == "passed"
    assert report["gate_qualified"] is True
    assert report["cross_slice_benefit"] is True
    assert report["execution_order_balanced"] is True
    assert report["case_count"] == 6
    assert report["rollout_count"] == 12
    assert report["evidence_retained"] is False
    assert set(report["paired_statistics"]) == {"held_in", "held_out", "transfer"}
    assert all(
        statistic["mean_delta"] > 0
        for statistic in report["paired_statistics"].values()
    )
