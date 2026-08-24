"""Pure safety and failure-preservation contracts for the PostgreSQL outage probe."""

from __future__ import annotations

import argparse
import subprocess

import pytest
from scripts import postgres_restart_probe as probe

DSN = "postgresql://postgres:secret@127.0.0.1:55442/agnoclaw_test"


def test_restart_probe_accepts_one_exact_loopback_test_target() -> None:
    target = probe._validate_target(DSN, "agnoclaw-postgres-test", 30)

    assert target.database == "agnoclaw_test"
    assert target.port == 55442


@pytest.mark.parametrize(
    ("dsn", "container", "timeout"),
    [
        ("https://127.0.0.1:55442/agnoclaw_test", "pg-test", 30),
        ("postgresql://postgres:x@db.example.com/db_test", "pg-test", 30),
        ("postgresql://postgres:x@127.0.0.1/production", "pg-test", 30),
        ("postgresql://postgres:x@127.0.0.1/bad-name-test", "pg-test", 30),
        ("postgresql://127.0.0.1/agnoclaw_test", "pg-test", 30),
        (DSN, "--dangerous", 30),
        (DSN, "pg test", 30),
        (DSN, "pg-test", 0),
        (DSN, "pg-test", 301),
    ],
)
def test_restart_probe_rejects_ambiguous_or_broad_targets(
    dsn: str,
    container: str,
    timeout: float,
) -> None:
    with pytest.raises(ValueError):
        probe._validate_target(dsn, container, timeout)


def test_docker_timeout_is_normalized_without_command_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.TimeoutExpired(["docker", "stop"], timeout=1)

    monkeypatch.setattr(probe.subprocess, "run", timeout)

    with pytest.raises(RuntimeError, match="Docker action 'stop' failed"):
        probe._docker("stop", "pg-test", timeout=1)


def test_cleanup_heals_before_closing_and_removing_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    states = iter((False, True))

    monkeypatch.setattr(
        probe,
        "_container_running",
        lambda *args, **kwargs: next(states),
    )
    def docker(*args: str, **kwargs: float) -> subprocess.CompletedProcess[str]:
        calls.append(f"docker:{args[0]}")
        return subprocess.CompletedProcess(args, 0, "", "")

    def cleanup_source(dsn: str, run_id: str) -> int:
        del dsn
        calls.append(f"source:{run_id}")
        return 1

    monkeypatch.setattr(probe, "_docker", docker)
    monkeypatch.setattr(probe, "_cleanup_source", cleanup_source)

    class Store:
        def close(self) -> None:
            calls.append("pool:close")

    result = probe._cleanup_probe(
        container="pg-test",
        source_dsn=DSN,
        run_id="pg_restart_probe_exact",
        store=Store(),  # type: ignore[arg-type]
        timeout=1,
    )

    assert calls == [
        "docker:start",
        "pool:close",
        "source:pg_restart_probe_exact",
    ]
    assert result.container_running
    assert result.source_rows_cleaned == 1
    assert result.failures == ()


def test_cleanup_reports_each_stage_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        probe,
        "_container_running",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated")),
    )

    class Store:
        def close(self) -> None:
            raise RuntimeError("simulated")

    result = probe._cleanup_probe(
        container="pg-test",
        source_dsn=DSN,
        run_id="pg_restart_probe_exact",
        store=Store(),  # type: ignore[arg-type]
        timeout=1,
    )

    assert result.failures == (
        "container healing failed (RuntimeError)",
        "pool cleanup failed (RuntimeError)",
    )


def test_main_preserves_primary_failure_and_attaches_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        probe,
        "_arguments",
        lambda: argparse.Namespace(
            dsn=DSN,
            container="pg-test",
            timeout=30.0,
            outage_timeout=1.0,
        ),
    )
    monkeypatch.setattr(probe, "_verify_container", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        probe,
        "PostgresRuntimeStore",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("primary failure")),
    )
    monkeypatch.setattr(
        probe,
        "_cleanup_probe",
        lambda **kwargs: probe.CleanupResult(
            container_running=False,
            source_rows_cleaned=0,
            failures=("container healing failed (RuntimeError)",),
        ),
    )

    with pytest.raises(RuntimeError, match="primary failure") as caught:
        probe.main()

    assert caught.value.__notes__ == ["container healing failed (RuntimeError)"]
