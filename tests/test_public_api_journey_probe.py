"""Executable source-side public API journey contracts."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import agnoclaw

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "public_api_journey_probe.py"


def test_journey_uses_only_the_top_level_agnoclaw_public_api() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name != "agnoclaw" for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("agnoclaw"):
            assert node.module == "agnoclaw"
            imported.update(alias.name for alias in node.names)
    assert imported
    assert imported <= set(agnoclaw.__all__)


def test_public_api_journey_completes_with_content_free_evidence() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    report = json.loads(result.stdout)

    assert report["schema_version"] == "1.0"
    assert report["quick"] == {
        "model_invocations": 1,
        "owned_models_closed": 2,
        "profile": "quick",
        "terminal": True,
    }
    assert report["durable_and_learning"] == {
        "learning_effects": 1,
        "learning_state": "promoted",
        "logical_runs": 2,
        "model_invocations": 2,
        "owned_models_closed": 5,
        "profile": "durable",
        "reopened_completed_runs": 2,
    }
    assert report["migration"] == {
        "personal_rows": 1,
        "phases": ["applied", "verified", "cutover", "rolled_back"],
        "rollback_removed_target": True,
    }
    assert report["provider_network_calls"] == 0
    assert report["production_certification"] is False
    assert report["cleanup"] == "complete"
    assert "public-api-journey-ready" not in result.stdout
    assert "Retry only verified safe reads" not in result.stdout
    assert "Ada" not in result.stdout


def test_public_api_journey_refuses_a_nonempty_operator_root(tmp_path: Path) -> None:
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert json.loads(result.stderr) == {
        "error": "JourneyConfigurationError",
        "message": "--root must reference an empty directory",
        "schema_version": "1.0",
    }
    assert sentinel.read_text(encoding="utf-8") == "keep"
