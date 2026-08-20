"""Pure safety contracts for the live etcd authority probe."""

from __future__ import annotations

import argparse

import pytest
from scripts import etcd_writer_authority_probe as probe


def _args(**overrides: object) -> probe.ProbeArguments:
    values: dict[str, object] = {
        "allow_container_create": True,
        "image": probe.ETCD_IMAGE,
        "timeout_seconds": 60.0,
        "lease_ttl_seconds": 3,
    }
    values.update(overrides)
    return probe.ProbeArguments(**values)  # type: ignore[arg-type]


def test_live_etcd_probe_requires_exact_bounded_owned_scope() -> None:
    probe._validate_arguments(_args())
    for invalid in (
        _args(allow_container_create=False),
        _args(image="quay.io/coreos/etcd:v3.6.14"),
        _args(timeout_seconds=9),
        _args(lease_ttl_seconds=2),
    ):
        with pytest.raises(ValueError):
            probe._validate_arguments(invalid)


def test_arguments_normalize_to_typed_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe.argparse.ArgumentParser,
        "parse_args",
        lambda self: argparse.Namespace(
            allow_container_create=True,
            image=probe.ETCD_IMAGE,
            timeout=90,
            lease_ttl=5,
        ),
    )
    assert probe._arguments() == probe.ProbeArguments(
        allow_container_create=True,
        image=probe.ETCD_IMAGE,
        timeout_seconds=90.0,
        lease_ttl_seconds=5,
    )


def test_main_preserves_primary_failure_and_cleanup_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "_arguments", lambda: _args())
    monkeypatch.setattr(
        probe,
        "_probe",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("probe failure")),
    )
    calls = 0

    def fail_cleanup(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("exact cleanup failed")

    monkeypatch.setattr(probe, "_docker", fail_cleanup)

    with pytest.raises(RuntimeError, match="probe failure") as failure:
        probe.main()

    assert calls == 1
    assert "exact cleanup failed" in " ".join(failure.value.__notes__)
