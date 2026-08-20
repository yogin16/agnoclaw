"""Pure safety contracts for the destructive-target PostgreSQL restore rehearsal."""

from __future__ import annotations

import argparse
import subprocess

import pytest
from scripts import postgres_backup_restore_probe as probe
from scripts.postgres_backup_restore_probe import _validate_target

SOURCE = "postgresql://postgres:secret@127.0.0.1:55441/agnoclaw_test"
TARGET = "postgresql://postgres:secret@127.0.0.1:55441/agnoclaw_restore_test"


def test_restore_probe_accepts_one_explicit_loopback_test_target() -> None:
    resolved = _validate_target(
        SOURCE,
        TARGET,
        "agnoclaw-postgres-test",
        allow_target_reset=True,
    )

    assert resolved.source_database == "agnoclaw_test"
    assert resolved.target_database == "agnoclaw_restore_test"
    assert resolved.port == 55441


@pytest.mark.parametrize(
    ("source", "target", "container", "allowed"),
    [
        (SOURCE, TARGET, "agnoclaw-postgres-test", False),
        (
            "postgresql://postgres:secret@db.example.com:55441/agnoclaw_test",
            TARGET,
            "agnoclaw-postgres-test",
            True,
        ),
        (
            SOURCE,
            "postgresql://postgres:secret@127.0.0.1:55441/production",
            "agnoclaw-postgres-test",
            True,
        ),
        (SOURCE, SOURCE, "agnoclaw-postgres-test", True),
        (
            SOURCE,
            "postgresql://postgres:secret@127.0.0.1:55441/other_test",
            "agnoclaw-postgres-test",
            True,
        ),
        (
            SOURCE,
            "postgresql://postgres:secret@127.0.0.1:55442/agnoclaw_restore_test",
            "agnoclaw-postgres-test",
            True,
        ),
        (SOURCE, TARGET, "--dangerous", True),
        (
            "https://127.0.0.1:55441/agnoclaw_test",
            TARGET,
            "agnoclaw-postgres-test",
            True,
        ),
    ],
)
def test_restore_probe_refuses_ambiguous_or_broad_targets(
    source: str,
    target: str,
    container: str,
    allowed: bool,
) -> None:
    with pytest.raises(ValueError):
        _validate_target(
            source,
            target,
            container,
            allow_target_reset=allowed,
        )


def test_cleanup_attempts_every_exact_target(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _validate_target(
        SOURCE,
        TARGET,
        "agnoclaw-postgres-test",
        allow_target_reset=True,
    )
    calls: list[tuple[str, ...]] = []

    def fail_each_docker(
        container: str,
        *arguments: str,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del container, timeout
        calls.append(arguments)
        raise RuntimeError("simulated")

    def fail_source(source_dsn: str, run_id: str) -> int:
        calls.append((source_dsn, run_id))
        raise RuntimeError("simulated")

    monkeypatch.setattr(probe, "_docker", fail_each_docker)
    monkeypatch.setattr(probe, "_cleanup_source", fail_source)

    result = probe._cleanup_probe(
        container="agnoclaw-postgres-test",
        target=target,
        dump_path="/tmp/agnoclaw_backup_probe_exact.dump",
        source_dsn=SOURCE,
        run_id="agnoclaw_backup_probe_exact:run",
        timeout=1.0,
    )

    assert [call[0] for call in calls] == ["rm", "dropdb", SOURCE]
    assert not result.dump_removed
    assert not result.target_removed
    assert result.source_rows_cleaned == 0
    assert result.failures == (
        "dump cleanup failed (RuntimeError)",
        "target cleanup failed (RuntimeError)",
        "source marker cleanup failed (RuntimeError)",
    )


def test_cleanup_success_reports_exact_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _validate_target(
        SOURCE,
        TARGET,
        "agnoclaw-postgres-test",
        allow_target_reset=True,
    )
    monkeypatch.setattr(
        probe,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    monkeypatch.setattr(probe, "_cleanup_source", lambda source, run: 1)

    result = probe._cleanup_probe(
        container="agnoclaw-postgres-test",
        target=target,
        dump_path="/tmp/agnoclaw_backup_probe_exact.dump",
        source_dsn=SOURCE,
        run_id="agnoclaw_backup_probe_exact:run",
        timeout=1.0,
    )

    assert result.dump_removed
    assert result.target_removed
    assert result.source_rows_cleaned == 1
    assert result.failures == ()


def test_docker_timeout_is_normalized_without_command_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.TimeoutExpired(["docker", "exec"], timeout=1)

    monkeypatch.setattr(probe.subprocess, "run", timeout)

    with pytest.raises(RuntimeError, match="container command 'pg_dump' failed"):
        probe._docker(
            "agnoclaw-postgres-test",
            "pg_dump",
            "--format=custom",
            timeout=1,
        )


def test_main_preserves_primary_failure_and_attaches_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        probe,
        "_arguments",
        lambda: argparse.Namespace(
            source_dsn=SOURCE,
            target_dsn=TARGET,
            container="agnoclaw-postgres-test",
            allow_target_reset=True,
            timeout_seconds=1.0,
            max_local_rto_seconds=1.0,
        ),
    )
    monkeypatch.setattr(probe, "_verify_container", lambda *args, **kwargs: None)

    def primary_failure(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("primary dump failure")

    monkeypatch.setattr(probe, "_docker", primary_failure)
    monkeypatch.setattr(
        probe,
        "_cleanup_probe",
        lambda **kwargs: probe.CleanupResult(
            dump_removed=False,
            target_removed=True,
            source_rows_cleaned=0,
            failures=("dump cleanup failed (RuntimeError)",),
        ),
    )

    with pytest.raises(RuntimeError, match="primary dump failure") as caught:
        probe.main()

    assert caught.value.__notes__ == ["dump cleanup failed (RuntimeError)"]
