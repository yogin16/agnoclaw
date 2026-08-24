"""Fail closed when a new operation kind lacks a real-process crash gate."""

from __future__ import annotations

from pathlib import Path

from agnoclaw.runtime import OperationKind

ROOT = Path(__file__).resolve().parents[1]

_CERTIFIED_PROBES = {
    OperationKind.MODEL: (
        "agno_stack_restart_probe",
        "agno_tool_checkpoint_restart_probe",
        "agno_approval_restart_probe",
    ),
    OperationKind.CAPABILITY: ("operation_effect_crash_probe",),
}


def test_every_operation_kind_has_an_asserted_real_process_probe() -> None:
    """Adding an operation kind must add an integration oracle in the same change."""
    assert set(_CERTIFIED_PROBES) == set(OperationKind)

    for kind, probes in _CERTIFIED_PROBES.items():
        assert probes
        for probe in probes:
            script = (ROOT / "scripts" / f"{probe}.py").read_text(encoding="utf-8")
            contract = (ROOT / "tests" / f"test_{probe}.py").read_text(encoding="utf-8")
            assert f'"operation_kind": OperationKind.{kind.name}.value' in script
            assert f'assert report["operation_kind"] == "{kind.value}"' in contract
            assert "@pytest.mark.integration" in contract
