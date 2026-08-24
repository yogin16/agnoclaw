"""Pure authority contracts for the synchronous PostgreSQL loss probe."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import postgres_synchronous_failover_probe as probe


def _args(**overrides: object) -> probe.SynchronousProbeArguments:
    values: dict[str, object] = {
        "allow_topology_create": True,
        "image": probe.base.POSTGRES_IMAGE,
        "timeout_seconds": 90.0,
        "outage_timeout_seconds": 1.0,
        "blocked_observation_seconds": 0.5,
    }
    values.update(overrides)
    return probe.SynchronousProbeArguments(**values)  # type: ignore[arg-type]


def test_synchronous_probe_requires_exact_topology_and_bounded_observation() -> None:
    probe._validate_arguments(_args())

    for invalid in (
        _args(allow_topology_create=False),
        _args(image="postgres:latest"),
        _args(blocked_observation_seconds=0.09),
        _args(blocked_observation_seconds=5.1),
        _args(timeout_seconds=10, blocked_observation_seconds=10),
    ):
        with pytest.raises(ValueError):
            probe._validate_arguments(invalid)


def test_script_help_loads_exact_sibling_in_python_isolated_mode(
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
    assert "--blocked-observation" in result.stdout


def test_standby_name_is_exact_identifier() -> None:
    assert probe._standby_name("0123456789abcdef") == "standby_0123456789abcdef"
    assert probe._STANDBY_NAME_RE.fullmatch("primary_0123456789abcdef")

    for invalid in ("", "0123", "A" * 16, "0" * 17, "0' OR true --"):
        with pytest.raises(ValueError):
            probe._standby_name(invalid)


def test_remote_apply_configuration_rejects_untrusted_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        probe.base,
        "_connect",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not connect")),
    )

    with pytest.raises(ValueError, match="synchronous-replication grammar"):
        probe._configure_remote_apply("unused", "standby_x'); DROP DATABASE prod; --")


def test_remote_apply_configuration_uses_exact_validated_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class Cursor:
        def execute(self, sql: str) -> Cursor:
            statements.append(sql)
            return self

        def fetchone(self) -> dict[str, bool]:
            return {"reloaded": True}

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def execute(self, sql: str) -> Cursor:
            return Cursor().execute(sql)

    monkeypatch.setattr(probe.base, "_connect", lambda *args, **kwargs: Connection())

    probe._configure_remote_apply("test-dsn", "standby_0123456789abcdef")

    assert statements == [
        "ALTER SYSTEM SET synchronous_standby_names TO "
        "'FIRST 1 (standby_0123456789abcdef)'",
        "ALTER SYSTEM SET synchronous_commit TO 'remote_apply'",
        "SELECT pg_reload_conf() AS reloaded",
    ]


def test_network_state_requires_owned_names_and_valid_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = "agnoclaw-pg-failover-0123456789abcdef-standby"
    network = "agnoclaw-pg-failover-0123456789abcdef-net"
    monkeypatch.setattr(
        probe.base,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            json.dumps({network: {"IPAddress": "172.20.0.3"}}),
            "",
        ),
    )

    assert probe._network_connected(container, network, timeout=1)
    with pytest.raises(ValueError):
        probe._network_connected("postgres-production", network, timeout=1)
    with pytest.raises(ValueError):
        probe._network_connected(container, "shared-network", timeout=1)

    monkeypatch.setattr(
        probe.base,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "[]", ""),
    )
    with pytest.raises(RuntimeError, match="invalid container-network shape"):
        probe._network_connected(container, network, timeout=1)


def test_synchronous_status_requires_exact_named_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = iter(
        (
            {"count": 1},
            {
                "application_name": "standby_0123456789abcdef",
                "state": "streaming",
                "sync_state": "sync",
                "sent_lsn": "0/20",
                "write_lsn": "0/20",
                "flush_lsn": "0/20",
                "replay_lsn": "0/20",
            },
        )
    )
    monkeypatch.setattr(probe.base, "_query", lambda *args, **kwargs: next(rows))

    status = probe._synchronous_status("unused", "standby_0123456789abcdef")

    assert status is not None
    assert status["state"] == "streaming"
    assert status["sync_state"] == "sync"


def test_replication_sender_termination_is_exact_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    def query(_dsn: str, sql: str, parameters: tuple[object, ...]) -> dict[str, object]:
        calls.append((sql, parameters))
        return {"matched": 1, "terminated": True}

    monkeypatch.setattr(probe.base, "_query", query)
    standby_name = "standby_0123456789abcdef"
    probe._terminate_exact_replication_sender("unused", standby_name)

    assert calls[0][1] == (standby_name,)
    assert "pg_terminate_backend(pid)" in calls[0][0]
    assert "application_name = %s" in calls[0][0]

    with pytest.raises(ValueError, match="replication-sender grammar"):
        probe._terminate_exact_replication_sender("unused", "standby-production")

    monkeypatch.setattr(
        probe.base,
        "_query",
        lambda *args, **kwargs: {"matched": 2, "terminated": True},
    )
    with pytest.raises(RuntimeError, match="exact synchronous replication sender"):
        probe._terminate_exact_replication_sender("unused", standby_name)


def test_arguments_normalize_to_typed_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe.argparse.ArgumentParser,
        "parse_args",
        lambda self: argparse.Namespace(
            allow_topology_create=True,
            image=probe.base.POSTGRES_IMAGE,
            timeout=120,
            outage_timeout=2,
            blocked_observation=0.75,
        ),
    )

    assert probe._arguments() == probe.SynchronousProbeArguments(
        allow_topology_create=True,
        image=probe.base.POSTGRES_IMAGE,
        timeout_seconds=120.0,
        outage_timeout_seconds=2.0,
        blocked_observation_seconds=0.75,
    )


def test_main_preserves_primary_failure_and_attaches_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "_arguments", lambda: _args())
    monkeypatch.setattr(
        probe,
        "_probe",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("primary failure")),
    )
    monkeypatch.setattr(
        probe.base,
        "_cleanup_topology",
        lambda *args, **kwargs: probe.base.CleanupResult(
            resources_removed=(),
            failures=("container cleanup failed (exact; RuntimeError)",),
        ),
    )

    with pytest.raises(RuntimeError, match="primary failure") as caught:
        probe.main()

    assert caught.value.__notes__ == ["container cleanup failed (exact; RuntimeError)"]
