"""Pure destructive-safety contracts for the owned PostgreSQL failover probe."""

from __future__ import annotations

import argparse
import subprocess

import pytest
from scripts import postgres_failover_probe as probe


def _args(**overrides: object) -> probe.ProbeArguments:
    values: dict[str, object] = {
        "allow_topology_create": True,
        "image": probe.POSTGRES_IMAGE,
        "timeout_seconds": 60.0,
        "outage_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return probe.ProbeArguments(**values)  # type: ignore[arg-type]


def test_failover_probe_requires_exact_image_and_explicit_destructive_authority() -> None:
    probe._validate_arguments(_args())

    invalid = (
        _args(allow_topology_create=False),
        _args(image="postgres:latest"),
        _args(image="postgres@sha256:" + ("A" * 64)),
        _args(timeout_seconds=9),
        _args(timeout_seconds=301),
        _args(outage_timeout_seconds=0),
        _args(outage_timeout_seconds=11),
    )
    for arguments in invalid:
        with pytest.raises(ValueError):
            probe._validate_arguments(arguments)


def test_resource_names_are_exact_and_unambiguous() -> None:
    names = probe._resource_names("0123456789abcdef")

    assert names.primary == "agnoclaw-pg-failover-0123456789abcdef-primary"
    assert names.standby == "agnoclaw-pg-failover-0123456789abcdef-standby"
    assert names.backup == "agnoclaw-pg-failover-0123456789abcdef-backup"
    assert names.network == "agnoclaw-pg-failover-0123456789abcdef-net"
    assert names.primary_volume.endswith("-data-a")
    assert names.standby_volume.endswith("-data-b")

    for invalid in ("", "ABCDEF0123456789", "0123", "0" * 17, "../0123456789abc"):
        with pytest.raises(ValueError):
            probe._resource_names(invalid)


def test_create_resource_records_authority_only_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = probe.OwnedResources()
    exact = "agnoclaw-pg-failover-0123456789abcdef-net"
    monkeypatch.setattr(
        probe,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )

    probe._create_resource(
        owned,
        "networks",
        exact,
        "network",
        "create",
        exact,
        timeout=1,
    )

    assert owned.networks == [exact]
    with pytest.raises(ValueError):
        probe._create_resource(
            owned,
            "networks",
            "shared-production-network",
            "network",
            "create",
            "shared-production-network",
            timeout=1,
        )

    def fail(*args: str, **kwargs: float) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise RuntimeError("simulated")

    monkeypatch.setattr(probe, "_docker", fail)
    with pytest.raises(RuntimeError):
        probe._create_resource(
            owned,
            "volumes",
            "agnoclaw-pg-failover-0123456789abcdef-data-a",
            "volume",
            "create",
            "agnoclaw-pg-failover-0123456789abcdef-data-a",
            timeout=1,
        )
    assert owned.volumes == []


def test_topology_initializes_rewind_safe_checksums_and_runtime_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[str, tuple[str, ...]]] = []
    ports = iter((55431, 55432))

    def create_resource(
        owned: probe.OwnedResources,
        kind: str,
        name: str,
        *arguments: str,
        timeout: float,
    ) -> None:
        del timeout
        created.append((name, arguments))
        getattr(owned, kind).append(name)

    monkeypatch.setattr(probe, "_create_resource", create_resource)
    monkeypatch.setattr(
        probe,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    monkeypatch.setattr(probe, "_network_subnet", lambda *args, **kwargs: "172.20.0.0/16")
    monkeypatch.setattr(probe, "_published_loopback_port", lambda *args, **kwargs: next(ports))
    monkeypatch.setattr(probe, "_wait_for", lambda *args, **kwargs: True)

    names = probe._resource_names("0123456789abcdef")
    probe._create_topology(
        names,
        image="postgres@sha256:" + ("a" * 64),
        password="test-only-password",
        timeout=10,
        owned=probe.OwnedResources(),
    )

    primary_arguments = next(arguments for name, arguments in created if name == names.primary)
    assert "POSTGRES_INITDB_ARGS=--data-checksums" in primary_arguments
    assert "PGPASSWORD=test-only-password" in primary_arguments


def test_container_inspection_failure_is_not_misreported_as_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        probe,
        "_docker",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("inspect failed")),
    )

    with pytest.raises(RuntimeError, match="inspect failed"):
        probe._container_running(
            "agnoclaw-pg-failover-0123456789abcdef-primary",
            timeout=1,
        )


def test_network_must_be_a_private_ipv4_subnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    network = "agnoclaw-pg-failover-0123456789abcdef-net"
    monkeypatch.setattr(
        probe,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "172.20.0.0/16\n", ""),
    )
    assert probe._network_subnet(network, timeout=1) == "172.20.0.0/16"

    monkeypatch.setattr(
        probe,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "0.0.0.0/0\n", ""),
    )
    with pytest.raises(RuntimeError, match="private IPv4"):
        probe._network_subnet(network, timeout=1)


def test_image_identity_requires_a_postgres_content_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "postgres@sha256:" + ("a" * 64)
    monkeypatch.setattr(
        probe,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, digest + "\n", ""),
    )
    assert probe._image_identity(probe.POSTGRES_IMAGE, timeout=1) == digest

    monkeypatch.setattr(
        probe,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "postgres:latest\n", ""),
    )
    with pytest.raises(RuntimeError, match="content digest"):
        probe._image_identity(probe.POSTGRES_IMAGE, timeout=1)


def test_resolve_image_pulls_before_returning_immutable_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    digest = "postgres@sha256:" + ("b" * 64)

    def docker(*arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append(arguments)
        output = digest + "\n" if arguments[:2] == ("image", "inspect") else ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr(probe, "_docker", docker)

    assert probe._resolve_image(probe.POSTGRES_IMAGE, timeout=1) == digest
    assert calls == [
        ("image", "pull", probe.POSTGRES_IMAGE),
        ("image", "inspect", "--format", "{{index .RepoDigests 0}}", probe.POSTGRES_IMAGE),
    ]


def test_resolve_image_accepts_only_an_exact_cached_digest_without_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "postgres@sha256:" + ("d" * 64)
    calls: list[tuple[str, ...]] = []

    def docker(*arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, digest + "\n", "")

    monkeypatch.setattr(probe, "_docker", docker)
    assert probe._resolve_image(digest, timeout=1) == digest
    assert calls == [
        ("image", "inspect", "--format", "{{index .RepoDigests 0}}", digest),
    ]

    monkeypatch.setattr(
        probe,
        "_image_identity",
        lambda *args, **kwargs: "postgres@sha256:" + ("e" * 64),
    )
    with pytest.raises(RuntimeError, match="did not match the requested digest"):
        probe._resolve_image(digest, timeout=1)


def test_resolve_image_retries_transient_pull_failure_within_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    delays: list[float] = []
    digest = "postgres@sha256:" + ("c" * 64)

    def docker(*arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
        assert 0 < timeout <= 10
        calls.append(arguments)
        if arguments[:2] == ("image", "pull") and len(calls) == 1:
            raise RuntimeError("Docker action 'image' failed")
        output = digest + "\n" if arguments[:2] == ("image", "inspect") else ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr(probe, "_docker", docker)
    monkeypatch.setattr(probe.time, "sleep", delays.append)

    assert probe._resolve_image(probe.POSTGRES_IMAGE, timeout=10) == digest
    assert calls == [
        ("image", "pull", probe.POSTGRES_IMAGE),
        ("image", "pull", probe.POSTGRES_IMAGE),
        ("image", "inspect", "--format", "{{index .RepoDigests 0}}", probe.POSTGRES_IMAGE),
    ]
    assert delays == [0.5]


def test_docker_error_drops_sensitive_subprocess_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.CalledProcessError(
            1,
            ["docker", "create", "--env", "PGPASSWORD=do-not-leak"],
            stderr="PGPASSWORD=do-not-leak",
        )

    monkeypatch.setattr(probe.subprocess, "run", fail)

    with pytest.raises(RuntimeError, match="Docker action 'create' failed") as caught:
        probe._docker("create", "--env", "PGPASSWORD=do-not-leak", timeout=1)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "do-not-leak" not in str(caught.value)


def test_cleanup_attempts_all_owned_resources_in_safe_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = probe.OwnedResources(
        containers=[
            "agnoclaw-pg-failover-0123456789abcdef-primary",
            "agnoclaw-pg-failover-0123456789abcdef-backup",
            "agnoclaw-pg-failover-0123456789abcdef-standby",
        ],
        volumes=[
            "agnoclaw-pg-failover-0123456789abcdef-data-a",
            "agnoclaw-pg-failover-0123456789abcdef-data-b",
        ],
        networks=["agnoclaw-pg-failover-0123456789abcdef-net"],
    )
    calls: list[tuple[str, ...]] = []

    def docker(*arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(probe, "_docker", docker)
    result = probe._cleanup_topology(owned, timeout=1)

    assert calls == [
        ("rm", "--force", owned.containers[2]),
        ("rm", "--force", owned.containers[1]),
        ("rm", "--force", owned.containers[0]),
        ("volume", "rm", owned.volumes[1]),
        ("volume", "rm", owned.volumes[0]),
        ("network", "rm", owned.networks[0]),
    ]
    assert result.failures == ()
    assert len(result.resources_removed) == 6


def test_cleanup_reports_every_failure_without_masking_later_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = probe.OwnedResources(
        containers=["agnoclaw-pg-failover-0123456789abcdef-primary"],
        volumes=["agnoclaw-pg-failover-0123456789abcdef-data-a"],
        networks=["agnoclaw-pg-failover-0123456789abcdef-net"],
    )
    calls: list[tuple[str, ...]] = []

    def fail(*arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append(arguments)
        raise RuntimeError("simulated")

    monkeypatch.setattr(probe, "_docker", fail)
    result = probe._cleanup_topology(owned, timeout=1)

    assert len(calls) == 3
    assert len(result.failures) == 3
    assert result.resources_removed == ()


def test_safe_promotion_requires_fence_and_replay_catchup() -> None:
    probe._require_safe_promotion(
        primary_running=False,
        replay_lsn="0/20",
        acknowledged_lsn="0/20",
    )
    probe._require_safe_promotion(
        primary_running=False,
        replay_lsn="1/0",
        acknowledged_lsn="0/FFFFFFFF",
    )

    with pytest.raises(RuntimeError, match="old primary is fenced"):
        probe._require_safe_promotion(
            primary_running=True,
            replay_lsn="0/20",
            acknowledged_lsn="0/20",
        )
    with pytest.raises(RuntimeError, match="acknowledged WAL is replayed"):
        probe._require_safe_promotion(
            primary_running=False,
            replay_lsn="0/1F",
            acknowledged_lsn="0/20",
        )


@pytest.mark.parametrize(
    ("lsn", "expected"),
    [("0/0", 0), ("0/10", 16), ("1/0", 1 << 32), ("A/FF", (10 << 32) + 255)],
)
def test_lsn_ordering(lsn: str, expected: int) -> None:
    assert probe._lsn_to_int(lsn) == expected


def test_multi_host_dsn_requires_read_write_routing() -> None:
    dsn = probe._multi_host_dsn(
        primary_port=55441,
        standby_port=55442,
        password="test_only",
        application_name="agnoclaw-failover-test",
    )

    assert "host=127.0.0.1,127.0.0.1" in dsn
    assert "port=55441,55442" in dsn
    assert "target_session_attrs=read-write" in dsn
    assert "connect_timeout=1" in dsn


def test_main_preserves_primary_failure_and_attaches_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "_arguments", lambda: _args())
    monkeypatch.setattr(
        probe,
        "_resource_names",
        lambda value: probe.TopologyNames(
            probe_id=value,
            primary="agnoclaw-pg-failover-0123456789abcdef-primary",
            standby="agnoclaw-pg-failover-0123456789abcdef-standby",
            backup="agnoclaw-pg-failover-0123456789abcdef-backup",
            network="agnoclaw-pg-failover-0123456789abcdef-net",
            primary_volume="agnoclaw-pg-failover-0123456789abcdef-data-a",
            standby_volume="agnoclaw-pg-failover-0123456789abcdef-data-b",
        ),
    )
    monkeypatch.setattr(
        probe,
        "_probe",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("primary failure")),
    )
    monkeypatch.setattr(
        probe,
        "_cleanup_topology",
        lambda *args, **kwargs: probe.CleanupResult(
            resources_removed=(),
            failures=("volume cleanup failed (exact; RuntimeError)",),
        ),
    )

    with pytest.raises(RuntimeError, match="primary failure") as caught:
        probe.main()

    assert caught.value.__notes__ == ["volume cleanup failed (exact; RuntimeError)"]


def test_arguments_normalize_to_typed_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe.argparse.ArgumentParser,
        "parse_args",
        lambda self: argparse.Namespace(
            allow_topology_create=True,
            image=probe.POSTGRES_IMAGE,
            timeout=45,
            outage_timeout=2,
        ),
    )

    assert probe._arguments() == probe.ProbeArguments(
        allow_topology_create=True,
        image=probe.POSTGRES_IMAGE,
        timeout_seconds=45.0,
        outage_timeout_seconds=2.0,
    )


def test_resolve_image_pulls_an_uncached_pinned_digest_then_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh runner has no cache: a pinned digest pulls once, then verifies."""
    digest = "postgres@sha256:" + ("f" * 64)
    calls: list[tuple[str, ...]] = []

    def docker(*arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append(arguments)
        if arguments[:2] == ("image", "inspect") and len(calls) == 1:
            raise RuntimeError("Docker action 'image' failed")
        output = digest + "\n" if arguments[:2] == ("image", "inspect") else ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr(probe, "_docker", docker)

    assert probe._resolve_image(digest, timeout=1) == digest
    assert calls == [
        ("image", "inspect", "--format", "{{index .RepoDigests 0}}", digest),
        ("image", "pull", digest),
        ("image", "inspect", "--format", "{{index .RepoDigests 0}}", digest),
    ]
