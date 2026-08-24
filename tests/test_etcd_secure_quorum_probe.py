"""Pure safety contracts for the secure three-member etcd authority probe."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import etcd_secure_quorum_probe as probe


def _args(**overrides: object) -> probe.ProbeArguments:
    values: dict[str, object] = {
        "allow_topology_create": True,
        "image": probe.ETCD_IMAGE,
        "timeout_seconds": 90.0,
        "lease_ttl_seconds": 15,
    }
    values.update(overrides)
    return probe.ProbeArguments(**values)  # type: ignore[arg-type]


def _certificates(tmp_path: Path, names: probe.ResourceNames) -> probe.CertificatePaths:
    identities = (
        *(f"{member}-{purpose}" for member in names.members for purpose in ("client", "peer")),
        "root",
        "gateway-root",
        "gateway-controller",
        "gateway-reader",
        "gateway-denied",
    )
    return probe.CertificatePaths(
        ca=tmp_path / "ca.crt",
        certificates={identity: tmp_path / f"{identity}.crt" for identity in identities},
        keys={identity: tmp_path / f"{identity}.key" for identity in identities},
        member_mounts={member: tmp_path / member for member in names.members},
    )


def test_secure_probe_requires_exact_bounded_owned_scope() -> None:
    probe._validate_arguments(_args())
    for invalid in (
        _args(allow_topology_create=False),
        _args(image="quay.io/coreos/etcd:v3.6.14"),
        _args(image="evil.example/etcd@sha256:" + ("a" * 64)),
        _args(timeout_seconds=19),
        _args(lease_ttl_seconds=9),
    ):
        with pytest.raises(ValueError):
            probe._validate_arguments(invalid)


def test_resource_names_are_exact_and_reject_unowned_ids() -> None:
    names = probe._resource_names("0123456789abcdef")
    assert names.network == "agnoclaw-etcd-secure-0123456789abcdef-net"
    assert names.members == (
        "agnoclaw-etcd-secure-0123456789abcdef-m0",
        "agnoclaw-etcd-secure-0123456789abcdef-m1",
        "agnoclaw-etcd-secure-0123456789abcdef-m2",
    )
    for invalid in ("", "0123", "A" * 16, "0" * 17, "../../production"):
        with pytest.raises(ValueError):
            probe._resource_names(invalid)


def test_member_arguments_pin_three_member_mtls_and_safety_flags(tmp_path: Path) -> None:
    names = probe._resource_names("0123456789abcdef")
    certificates = _certificates(tmp_path, names)
    arguments = probe._member_arguments(
        names,
        certificates,
        member=names.members[1],
    )

    joined = " ".join(arguments)
    assert "--client-cert-auth=true" in joined
    assert "--peer-client-cert-auth=true" in joined
    assert f"/certs/{names.members[1]}-client.crt" in joined
    assert f"/certs/{names.members[1]}-peer.crt" in joined
    assert "--tls-min-version TLS1.2" in joined
    assert "--strict-reconfig-check=true" in joined
    assert "--pre-vote=true" in joined
    assert all(f"{member}=https://{member}:2380" in joined for member in names.members)
    with pytest.raises(ValueError):
        probe._member_arguments(
            names,
            certificates,
            member="production-etcd",
        )


def test_cleanup_attempts_every_exact_resource_and_preserves_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = probe._resource_names("0123456789abcdef")
    calls: list[tuple[str, ...]] = []

    def fake_docker(*arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append(arguments)
        if names.members[1] in arguments:
            raise RuntimeError("member cleanup failed")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(probe, "_docker", fake_docker)
    result = probe._cleanup(
        probe.OwnedResources(
            containers=[*names.members, "production-etcd"],
            networks=[names.network, "shared-network"],
        ),
        timeout=10,
    )

    assert len(calls) == 4
    assert set(result.resources_removed) == {names.members[0], names.members[2], names.network}
    assert any("outside the owned namespace" in failure for failure in result.failures)
    assert any("member cleanup failed" in failure for failure in result.failures)


def test_arguments_normalize_to_typed_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe.argparse.ArgumentParser,
        "parse_args",
        lambda self: argparse.Namespace(
            allow_topology_create=True,
            image=probe.ETCD_IMAGE,
            timeout=120,
            lease_ttl=20,
        ),
    )
    assert probe._arguments() == probe.ProbeArguments(
        allow_topology_create=True,
        image=probe.ETCD_IMAGE,
        timeout_seconds=120.0,
        lease_ttl_seconds=20,
    )


def test_script_help_runs_in_python_isolated_mode(tmp_path: Path) -> None:
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
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("probe failure")),
    )
    monkeypatch.setattr(
        probe,
        "_cleanup",
        lambda *args, **kwargs: probe.CleanupResult((), ("exact cleanup failed",)),
    )

    with pytest.raises(RuntimeError, match="probe failure") as failure:
        probe.main()

    assert "exact cleanup failed" in " ".join(failure.value.__notes__)
