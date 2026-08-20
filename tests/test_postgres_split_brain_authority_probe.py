"""Pure safety contracts for the PostgreSQL split-brain authority probe."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import postgres_split_brain_authority_probe as probe


def _args(**overrides: object) -> probe.SplitBrainProbeArguments:
    values: dict[str, object] = {
        "allow_topology_create": True,
        "image": probe.base.POSTGRES_IMAGE,
        "etcd_image": probe.etcd.ETCD_IMAGE,
        "timeout_seconds": 90.0,
        "outage_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return probe.SplitBrainProbeArguments(**values)  # type: ignore[arg-type]


def test_split_brain_probe_reuses_exact_bounded_topology_authority() -> None:
    probe._validate_arguments(_args())
    probe._validate_arguments(_args(image="postgres@sha256:" + ("a" * 64)))

    for invalid in (
        _args(allow_topology_create=False),
        _args(image="postgres:latest"),
        _args(timeout_seconds=9),
        _args(outage_timeout_seconds=0),
    ):
        with pytest.raises(ValueError):
            probe._validate_arguments(invalid)


def test_arguments_normalize_to_typed_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe.argparse.ArgumentParser,
        "parse_args",
        lambda self: argparse.Namespace(
            allow_topology_create=True,
            image=probe.base.POSTGRES_IMAGE,
            etcd_image=probe.etcd.ETCD_IMAGE,
            timeout=120,
            outage_timeout=2,
        ),
    )
    assert probe._arguments() == probe.SplitBrainProbeArguments(
        allow_topology_create=True,
        image=probe.base.POSTGRES_IMAGE,
        etcd_image=probe.etcd.ETCD_IMAGE,
        timeout_seconds=120.0,
        outage_timeout_seconds=2.0,
    )


def test_script_help_loads_exact_siblings_in_python_isolated_mode(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(Path(probe.__file__).resolve()), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "--allow-topology-create" in result.stdout


def test_main_preserves_primary_failure_and_cleanup_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "_arguments", lambda: _args())
    monkeypatch.setattr(
        probe,
        "_probe",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("split failure")),
    )
    monkeypatch.setattr(
        probe.base,
        "_cleanup_topology",
        lambda *args, **kwargs: probe.base.CleanupResult(
            resources_removed=(),
            failures=("exact cleanup failed",),
        ),
    )

    with pytest.raises(RuntimeError, match="split failure") as failure:
        probe.main()

    assert "exact cleanup failed" in " ".join(failure.value.__notes__)
