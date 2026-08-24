#!/usr/bin/env python3
"""Stop/start one isolated PostgreSQL container and prove bounded store recovery."""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from urllib.parse import ParseResult, urlparse
from uuid import uuid4

from agnoclaw.runtime import (
    PostgresRuntimeStore,
    RuntimeStoreConnectionLostError,
    RuntimeStoreOverloadedError,
)
from agnoclaw.runtime.lifecycle import (
    LifecycleTransition,
    RunSnapshot,
    RunState,
    TransitionKind,
)
from agnoclaw.runtime.store import RunOwner

_DATABASE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class ProbeTarget:
    parsed: ParseResult
    database: str
    port: int


@dataclass(frozen=True)
class CleanupResult:
    container_running: bool
    source_rows_cleaned: int
    failures: tuple[str, ...]


def _validate_target(dsn: str, container: str, timeout: float) -> ProbeTarget:
    parsed = urlparse(dsn)
    database = parsed.path.removeprefix("/")
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("--dsn must use postgres:// or postgresql://")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("restart probe refuses a non-loopback database")
    if "test" not in database.lower():
        raise ValueError("restart probe requires a database name containing 'test'")
    if not _DATABASE_RE.fullmatch(database):
        raise ValueError("restart probe requires a simple database identifier")
    if not parsed.username:
        raise ValueError("--dsn requires an explicit database user")
    if not _CONTAINER_RE.fullmatch(container):
        raise ValueError("--container must be one exact Docker container name")
    if not 1 <= timeout <= 300:
        raise ValueError("--timeout must be between 1 and 300 seconds")
    return ProbeTarget(parsed=parsed, database=database, port=parsed.port or 5432)


def _docker(*arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        action = arguments[0] if arguments else "docker"
        raise RuntimeError(f"Docker action {action!r} failed") from exc


def _container_running(container: str, *, timeout: float) -> bool:
    result = _docker(
        "inspect",
        "--format",
        "{{.State.Running}}",
        container,
        timeout=timeout,
    )
    return result.stdout.strip() == "true"


def _verify_container(container: str, *, port: int, timeout: float) -> None:
    if not _container_running(container, timeout=timeout):
        raise RuntimeError("the exact PostgreSQL container is not running")
    published = _docker("port", container, "5432/tcp", timeout=timeout)
    published_ports = {
        line.rsplit(":", 1)[-1].strip()
        for line in published.stdout.splitlines()
        if ":" in line
    }
    if str(port) not in published_ports:
        raise RuntimeError("container port 5432 is not published on the DSN port")


def _cleanup_source(source_dsn: str, run_id: str) -> int:
    with PostgresRuntimeStore(
        source_dsn,
        min_pool_size=1,
        max_pool_size=1,
        pool_timeout_seconds=1,
        connect_timeout_seconds=2,
        application_name="agnoclaw-restart-cleanup",
    ) as store:
        with store._transaction() as conn:  # noqa: SLF001 - exact probe-row cleanup
            rows = conn.execute(
                "DELETE FROM runtime_runs WHERE run_id = %s RETURNING run_id",
                (run_id,),
            ).fetchall()
    return len(rows)


def _cleanup_probe(
    *,
    container: str,
    source_dsn: str,
    run_id: str | None,
    store: PostgresRuntimeStore | None,
    timeout: float,
) -> CleanupResult:
    """Heal the exact container and attempt every cleanup without masking failure."""
    failures: list[str] = []
    container_running = False
    source_rows_cleaned = 0

    try:
        if not _container_running(container, timeout=timeout):
            _docker("start", container, timeout=timeout)
        container_running = _container_running(container, timeout=timeout)
        if not container_running:
            raise RuntimeError("container did not return to running state")
    except Exception as exc:  # noqa: BLE001 - subsequent cleanup must still run
        failures.append(f"container healing failed ({type(exc).__name__})")

    if store is not None:
        try:
            store.close()
        except Exception as exc:  # noqa: BLE001 - source cleanup must still run
            failures.append(f"pool cleanup failed ({type(exc).__name__})")

    if run_id is not None and container_running:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                source_rows_cleaned = _cleanup_source(source_dsn, run_id)
                break
            except Exception as exc:  # noqa: BLE001 - database may still be starting
                last_error = exc
                time.sleep(0.2)
        else:
            error_name = type(last_error).__name__ if last_error is not None else "unknown"
            failures.append(f"source marker cleanup failed ({error_name})")

    return CleanupResult(
        container_running=container_running,
        source_rows_cleaned=source_rows_cleaned,
        failures=tuple(failures),
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Stop one exact disposable local PostgreSQL container, require a bounded "
            "typed outage, start it, and verify existing/fresh pool continuity."
        )
    )
    parser.add_argument("--dsn", required=True, help="Loopback PostgreSQL test DSN")
    parser.add_argument("--container", required=True, help="Exact Docker container name")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--outage-timeout", type=float, default=1.0)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    try:
        target = _validate_target(args.dsn, args.container, args.timeout)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not 0 < args.outage_timeout <= min(10, args.timeout):
        raise SystemExit(
            "--outage-timeout must be positive and no greater than both 10 seconds "
            "and --timeout"
        )
    _verify_container(args.container, port=target.port, timeout=args.timeout)

    suffix = uuid4().hex
    run_id = f"pg_restart_probe_{suffix}"
    owner = RunOwner("agnoclaw-probe", "agnoclaw-probe")
    application_name = f"agnoclaw-restart-{suffix[:12]}"
    store: PostgresRuntimeStore | None = None
    marker_created = False
    output: dict[str, object] | None = None
    primary_failure: BaseException | None = None
    cleanup = CleanupResult(False, 0, ())
    started = time.monotonic()
    pool_logger = logging.getLogger("psycopg.pool")
    previous_pool_log_level = pool_logger.level
    pool_logger.setLevel(logging.CRITICAL)

    try:
        store = PostgresRuntimeStore(
            args.dsn,
            min_pool_size=1,
            max_pool_size=2,
            max_waiting=2,
            pool_timeout_seconds=args.outage_timeout,
            connect_timeout_seconds=min(args.timeout, 5),
            application_name=application_name,
        )
        store.create_run(
            RunSnapshot(
                run_id=run_id,
                tenant_id=owner.tenant_id,
                user_id=owner.user_id,
                session_id=f"restart-probe-{suffix}",
            )
        )
        marker_created = True
        claim = store.acquire_run_lease(
            run_id,
            worker_id="probe-before-outage",
            claim_id=f"probe:{suffix}",
            lease_seconds=max(60, int(args.timeout) + 15),
            owner=owner,
        ).claim

        _docker("stop", "--time", "1", args.container, timeout=args.timeout)
        if _container_running(args.container, timeout=args.timeout):
            raise RuntimeError("the exact container did not enter the stopped state")

        outage_started = time.monotonic()
        try:
            store.get_run(run_id, owner=owner)
        except (RuntimeStoreOverloadedError, RuntimeStoreConnectionLostError) as exc:
            outage_error = exc
        else:
            raise RuntimeError("runtime read unexpectedly succeeded while PostgreSQL stopped")
        outage_failure_seconds = time.monotonic() - outage_started
        if outage_failure_seconds > args.outage_timeout + 2:
            raise RuntimeError("runtime-store outage did not fail within the bounded window")

        _docker("start", args.container, timeout=args.timeout)
        deadline = time.monotonic() + args.timeout
        reconnect_attempts = 0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            reconnect_attempts += 1
            try:
                snapshot = store.get_run(run_id, owner=owner)
                break
            except Exception as exc:  # noqa: BLE001 - bounded reconnect observation
                last_error = exc
                time.sleep(0.2)
        else:
            raise RuntimeError("existing pool did not reconnect before timeout") from last_error

        if snapshot.state is not RunState.CREATED or snapshot.revision != 0:
            raise RuntimeError("acknowledged pre-outage state changed or disappeared")
        renewed = store.renew_run_lease(claim, lease_seconds=60)
        if renewed.run.fence_token != claim.run.fence_token:
            raise RuntimeError("the exact unexpired claim changed fence across the outage")
        queued = store.apply_transition(
            LifecycleTransition(
                run_id=run_id,
                kind=TransitionKind.QUEUE,
                transition_id=f"{run_id}:queue-after-outage",
            ),
            expected_revision=snapshot.revision,
        ).lifecycle.after
        store.release_run_lease(renewed)
        events = store.list_events(run_id, owner=owner)
        if queued.state is not RunState.QUEUED:
            raise RuntimeError("post-outage transition did not commit")
        if [event.sequence for event in events] != list(range(1, len(events) + 1)):
            raise RuntimeError("event sequence is not contiguous after outage")

        with store._connection() as conn:  # noqa: SLF001 - bounded connection oracle
            connection_count = int(
                conn.execute(
                    """
                    SELECT count(*) AS count FROM pg_stat_activity
                    WHERE application_name = %s
                    """,
                    (application_name,),
                ).fetchone()["count"]
            )
        if connection_count > 2:
            raise RuntimeError("probe exceeded its declared PostgreSQL connection bound")
        pool_stats = store.pool_stats

        with PostgresRuntimeStore(
            args.dsn,
            min_pool_size=1,
            max_pool_size=1,
            application_name="agnoclaw-restart-fresh-verifier",
        ) as reopened:
            durable = reopened.get_run(run_id, owner=owner)
            if durable.state is not RunState.QUEUED:
                raise RuntimeError("a fresh pool cannot observe post-outage state")

        output = {
            "status": "passed",
            "scope": "isolated_single_primary_stop_start_probe",
            "production_certification": False,
            "bounded_outage_error": outage_error.code,
            "bounded_outage_retryable": outage_error.retryable,
            "outage_failure_seconds": round(outage_failure_seconds, 3),
            "outage_timeout_seconds": args.outage_timeout,
            "existing_pool_reconnected": True,
            "fresh_pool_verified": True,
            "acknowledged_state_preserved": True,
            "event_sequence_contiguous": True,
            "run_fence_preserved": True,
            "run_fence_token": claim.run.fence_token,
            "reconnect_attempts": reconnect_attempts,
            "connection_bound": 2,
            "observed_application_connections": connection_count,
            "pool_stats": pool_stats,
            "primary_promotion": False,
            "open_production_gates": [
                "replica promotion and lag",
                "network partition and split-brain fencing",
                "production memory and connection budgets",
                "timed production RPO/RTO",
            ],
        }
    except BaseException as exc:
        primary_failure = exc
    finally:
        cleanup = _cleanup_probe(
            container=args.container,
            source_dsn=args.dsn,
            run_id=run_id if marker_created else None,
            store=store,
            timeout=args.timeout,
        )
        pool_logger.setLevel(previous_pool_log_level)

    if primary_failure is not None:
        if cleanup.failures:
            primary_failure.add_note("; ".join(cleanup.failures))
        raise primary_failure.with_traceback(primary_failure.__traceback__)
    if cleanup.failures:
        raise RuntimeError(
            "PostgreSQL restart-probe cleanup failed: " + "; ".join(cleanup.failures)
        )
    if output is None:
        raise AssertionError("PostgreSQL restart probe completed without a result")

    output["container_running"] = cleanup.container_running
    output["source_rows_cleaned"] = cleanup.source_rows_cleaned
    output["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
