"""Pure safety contracts for PostgreSQL rewind/rejoin role rotation."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import postgres_role_rotation_probe as probe


def _args(**overrides: object) -> probe.RotationProbeArguments:
    values: dict[str, object] = {
        "allow_topology_create": True,
        "image": probe.base.POSTGRES_IMAGE,
        "timeout_seconds": 90.0,
        "outage_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return probe.RotationProbeArguments(**values)  # type: ignore[arg-type]


def test_rotation_probe_reuses_bounded_topology_authority() -> None:
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


def test_replication_and_helper_names_are_exact() -> None:
    names = probe.base._resource_names("0123456789abcdef")
    assert probe._replication_name("primary", names.probe_id) == "primary_0123456789abcdef"
    assert probe._replication_name("standby", names.probe_id) == "standby_0123456789abcdef"
    assert probe._rewind_helper_name(names, "a").endswith("-rewind-a")
    assert probe._rewind_helper_name(names, "b").endswith("-rewind-b")

    for role in ("writer", "", "primary;rm"):
        with pytest.raises(ValueError):
            probe._replication_name(role, names.probe_id)
    with pytest.raises(ValueError):
        probe._rewind_helper_name(names, "c")


def test_wait_streaming_requires_one_exact_named_row(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = iter(
        (
            {"count": 1},
            {
                "application_name": "primary_0123456789abcdef",
                "state": "streaming",
                "sync_state": "async",
                "sent_lsn": "0/20",
                "write_lsn": "0/20",
                "flush_lsn": "0/20",
                "replay_lsn": "0/20",
            },
        )
    )
    monkeypatch.setattr(probe.base, "_query", lambda *args, **kwargs: next(rows))
    monkeypatch.setattr(
        probe.base,
        "_wait_for",
        lambda _description, predicate, **kwargs: predicate(),
    )

    status = probe._wait_streaming(
        "unused",
        "primary_0123456789abcdef",
        timeout=1,
    )
    assert status["state"] == "streaming"

    with pytest.raises(ValueError):
        probe._wait_streaming("unused", "production", timeout=1)


def test_rewind_rejects_unowned_or_running_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    names = probe.base._resource_names("0123456789abcdef")
    owned = probe.base.OwnedResources(
        containers=[names.primary, names.standby],
        volumes=[names.primary_volume, names.standby_volume],
        networks=[names.network],
    )
    monkeypatch.setattr(probe.base, "_container_running", lambda *args, **kwargs: True)

    with pytest.raises(RuntimeError, match="running target"):
        probe._rewind_target(
            names=names,
            owned=owned,
            helper_suffix="a",
            target_container=names.primary,
            target_volume=names.primary_volume,
            source_container=names.standby,
            source_alias="standby",
            replication_name="primary_0123456789abcdef",
            image="postgres@sha256:" + ("a" * 64),
            password="test-only",
            require_clean_shutdown=False,
            timeout=1,
        )

    with pytest.raises(ValueError, match="owned probe volume"):
        probe._rewind_target(
            names=names,
            owned=owned,
            helper_suffix="a",
            target_container=names.primary,
            target_volume="production-data",
            source_container=names.standby,
            source_alias="standby",
            replication_name="primary_0123456789abcdef",
            image="postgres@sha256:" + ("a" * 64),
            password="test-only",
            require_clean_shutdown=False,
            timeout=1,
        )


def test_rewind_command_is_fsynced_recovery_conf_and_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = probe.base._resource_names("0123456789abcdef")
    owned = probe.base.OwnedResources(
        containers=[names.primary, names.standby],
        volumes=[names.primary_volume, names.standby_volume],
        networks=[names.network],
    )
    created: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        probe.base,
        "_container_running",
        lambda container, **kwargs: container == names.standby,
    )

    def create_resource(
        owned_resources: object,
        kind: str,
        name: str,
        *arguments: str,
        timeout: float,
    ) -> None:
        del owned_resources, kind, name, timeout
        created.append(arguments)

    monkeypatch.setattr(probe.base, "_create_resource", create_resource)
    monkeypatch.setattr(
        probe.base,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            "",
            "",
        ),
    )

    result = probe._rewind_target(
        names=names,
        owned=owned,
        helper_suffix="a",
        target_container=names.primary,
        target_volume=names.primary_volume,
        source_container=names.standby,
        source_alias="standby",
        replication_name="primary_0123456789abcdef",
        image="postgres@sha256:" + ("a" * 64),
        password="test-only-secret",
        require_clean_shutdown=True,
        timeout=10,
    )

    arguments = created[0]
    script = arguments[-1]
    assert result.required_clean_shutdown is True
    assert "--write-recovery-conf" not in script
    assert "touch \"$PGDATA/standby.signal\"" in script
    assert "sed -i '/^[[:space:]]*primary_conninfo" in script
    assert "primary_conninfo = '%s'" in script
    assert "--no-ensure-shutdown" in script
    assert "--no-sync" not in script
    source_conninfo = next(
        value for value in arguments if value.startswith("SOURCE_CONNINFO=")
    )
    assert "password=" not in source_conninfo
    assert "REWIND_PASSWORD=test-only-secret" in arguments
    assert "unset REWIND_PASSWORD" in script
    assert "PGPASSFILE=/tmp/rewind.pgpass" in script
    assert "chown postgres:postgres /tmp/rewind.pgpass" in script


def test_checkpoint_lsn_is_forced_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    statements: list[str] = []

    class Cursor:
        def __init__(self, row: dict[str, str] | None = None) -> None:
            self.row = row

        def fetchone(self) -> dict[str, str] | None:
            return self.row

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def execute(self, sql: str) -> Cursor:
            statements.append(sql)
            return Cursor({"lsn": "0/30"} if sql.startswith("SELECT") else None)

    monkeypatch.setattr(probe.base, "_connect", lambda *args, **kwargs: Connection())
    assert probe._checkpoint_lsn("unused") == "0/30"
    assert statements == [
        "CHECKPOINT",
        "SELECT pg_current_wal_flush_lsn()::text AS lsn",
    ]


def test_arguments_normalize_to_typed_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe.argparse.ArgumentParser,
        "parse_args",
        lambda self: argparse.Namespace(
            allow_topology_create=True,
            image=probe.base.POSTGRES_IMAGE,
            timeout=120,
            outage_timeout=2,
        ),
    )
    assert probe._arguments() == probe.RotationProbeArguments(
        allow_topology_create=True,
        image=probe.base.POSTGRES_IMAGE,
        timeout_seconds=120.0,
        outage_timeout_seconds=2.0,
    )


def test_main_preserves_primary_failure_and_cleanup_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "_arguments", lambda: _args())
    monkeypatch.setattr(
        probe,
        "_probe",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("rotation failure")),
    )
    monkeypatch.setattr(
        probe.base,
        "_cleanup_topology",
        lambda *args, **kwargs: probe.base.CleanupResult(
            resources_removed=(),
            failures=("rewind helper cleanup failed",),
        ),
    )

    with pytest.raises(RuntimeError, match="rotation failure") as caught:
        probe.main()
    assert caught.value.__notes__ == ["rewind helper cleanup failed"]
