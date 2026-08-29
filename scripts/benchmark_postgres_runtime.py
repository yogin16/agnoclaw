#!/usr/bin/env python3
"""Measure isolated PostgreSQL runtime latency, isolation, and saturation behavior."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from agnoclaw.runtime import (
    AsyncSessionLanes,
    PostgresRuntimeStore,
    RuntimeStoreOverloadedError,
)
from agnoclaw.runtime.lifecycle import (
    LifecycleTransition,
    RunSnapshot,
    RunState,
    TransitionKind,
)
from agnoclaw.runtime.store import RunOwner, _canonical_json

_OLD = "2000-01-01T00:00:00+00:00"
_P95_RATIO_BASELINE_FLOOR_MS = 0.5


def _validate_target(dsn: str) -> None:
    parsed = urlparse(dsn)
    database = parsed.path.removeprefix("/")
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("--dsn must use postgres:// or postgresql://")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("benchmark refuses a non-loopback database")
    if "test" not in database.lower():
        raise ValueError("benchmark requires a database name containing 'test'")


def _percentiles(samples: list[float]) -> dict[str, float]:
    if not samples:
        raise ValueError("latency samples cannot be empty")
    ordered = sorted(samples)

    def percentile(fraction: float) -> float:
        return ordered[math.ceil(len(ordered) * fraction) - 1]

    return {
        "p50_ms": round(statistics.median(ordered), 6),
        "p95_ms": round(percentile(0.95), 6),
        "p99_ms": round(percentile(0.99), 6),
        "max_ms": round(ordered[-1], 6),
    }


def _p95_slowdown_ratio(*, baseline_ms: float, noisy_ms: float) -> float:
    """Return a stable relative signal when the baseline is sub-millisecond."""
    return round(noisy_ms / max(baseline_ms, _P95_RATIO_BASELINE_FLOOR_MS), 3)


def _timings(call: Callable[[], Any], samples: int) -> tuple[dict[str, float], int]:
    for _ in range(min(20, samples)):
        call()
    elapsed: list[float] = []
    result_count = 0
    for _ in range(samples):
        started = time.perf_counter_ns()
        result = call()
        elapsed.append((time.perf_counter_ns() - started) / 1_000_000)
        if isinstance(result, list):
            result_count += len(result)
    return _percentiles(elapsed), result_count


def _snapshot_json(snapshot: RunSnapshot) -> str:
    return _canonical_json(
        {
            "schema_version": snapshot.schema_version,
            "run_id": snapshot.run_id,
            "state": snapshot.state.value,
            "revision": snapshot.revision,
            "tenant_id": snapshot.tenant_id,
            "user_id": snapshot.user_id,
            "session_id": snapshot.session_id,
            "parent_run_id": snapshot.parent_run_id,
            "root_run_id": snapshot.root_run_id,
            "child_depth": snapshot.child_depth,
            "created_at": snapshot.created_at,
            "updated_at": snapshot.updated_at,
            "steering_open": snapshot.steering_open,
            "pending_request_id": snapshot.pending_request_id,
            "last_transition_id": snapshot.last_transition_id,
            "last_reason_code": snapshot.last_reason_code,
            "metadata": {},
        }
    )


def _populate_terminal_history(
    store: PostgresRuntimeStore,
    *,
    prefix: str,
    history_size: int,
) -> None:
    rows: list[tuple[object, ...]] = []
    for index in range(history_size):
        snapshot = RunSnapshot(
            run_id=f"{prefix}:history:{index:09d}",
            state=RunState.COMPLETED,
            revision=1,
            tenant_id=f"{prefix}:history-tenant",
            user_id="history-user",
            created_at=_OLD,
            updated_at=_OLD,
        )
        rows.append(
            (
                snapshot.run_id,
                snapshot.tenant_id,
                snapshot.user_id,
                None,
                snapshot.state.value,
                snapshot.revision,
                1,
                _snapshot_json(snapshot),
                _OLD,
                _OLD,
                _OLD,
            )
        )
    with store._transaction() as conn:  # noqa: SLF001 - isolated synthetic load fixture
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO runtime_runs(
                    run_id, tenant_id, user_id, session_id, state, revision,
                    next_sequence, snapshot_json, created_at, updated_at,
                    authority_updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                rows,
            )


def _create_recoverable(
    store: PostgresRuntimeStore,
    *,
    run_id: str,
    owner: RunOwner,
) -> None:
    created = store.create_run(
        RunSnapshot(
            run_id=run_id,
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            session_id=f"session:{run_id}",
        )
    )
    store.apply_transition(
        LifecycleTransition(
            run_id=run_id,
            kind=TransitionKind.QUEUE,
            transition_id=f"{run_id}:queue",
        ),
        expected_revision=created.snapshot.revision,
    )


def _assert_owner(rows: list[RunSnapshot], owner: RunOwner) -> list[RunSnapshot]:
    if not rows:
        raise RuntimeError("owner-scoped recovery returned no candidate")
    if any((row.tenant_id, row.user_id) != (owner.tenant_id, owner.user_id) for row in rows):
        raise RuntimeError("owner-scoped recovery leaked another owner")
    return rows


def _latency_probe(
    store: PostgresRuntimeStore,
    *,
    probe_owner: RunOwner,
    hot_owner: RunOwner,
    probe_run_id: str,
    samples: int,
    hot_workers: int,
) -> dict[str, object]:
    probe_recovery = lambda: _assert_owner(  # noqa: E731 - measured callable
        store.list_recoverable_runs(
            owner=probe_owner,
            minimum_age_seconds=0,
            limit=10,
        ),
        probe_owner,
    )
    baseline_get, _ = _timings(lambda: store.get_run(probe_run_id), samples)
    baseline_recovery, baseline_rows = _timings(probe_recovery, samples)

    stop = threading.Event()
    ready = threading.Barrier(hot_workers + 1)
    hot_counts = [0 for _ in range(hot_workers)]

    def noisy_neighbor(index: int) -> None:
        ready.wait()
        while not stop.is_set():
            rows = store.list_recoverable_runs(
                owner=hot_owner,
                minimum_age_seconds=0,
                limit=10,
            )
            _assert_owner(rows, hot_owner)
            hot_counts[index] += 1

    with ThreadPoolExecutor(max_workers=hot_workers) as executor:
        futures = [executor.submit(noisy_neighbor, index) for index in range(hot_workers)]
        ready.wait()
        try:
            noisy_get, _ = _timings(lambda: store.get_run(probe_run_id), samples)
            noisy_recovery, noisy_rows = _timings(probe_recovery, samples)
        finally:
            stop.set()
        for future in futures:
            future.result(timeout=5)

    return {
        "baseline": {
            "get_run": baseline_get,
            "owner_recovery": baseline_recovery,
            "owner_rows_observed": baseline_rows,
        },
        "under_noisy_neighbor": {
            "get_run": noisy_get,
            "owner_recovery": noisy_recovery,
            "owner_rows_observed": noisy_rows,
            "hot_queries_completed": sum(hot_counts),
            "hot_worker_queries": hot_counts,
        },
        "p95_slowdown_ratio": {
            "get_run": _p95_slowdown_ratio(
                baseline_ms=baseline_get["p95_ms"],
                noisy_ms=noisy_get["p95_ms"],
            ),
            "owner_recovery": _p95_slowdown_ratio(
                baseline_ms=baseline_recovery["p95_ms"],
                noisy_ms=noisy_recovery["p95_ms"],
            ),
        },
        "owner_isolation": True,
        "probe_samples_completed": samples * 2,
    }


def _saturation_probe(dsn: str, *, run_id: str, pool_timeout_seconds: float) -> dict[str, object]:
    pool_size = 2
    max_waiting = 2
    excess = 2
    store = PostgresRuntimeStore(
        dsn,
        min_pool_size=pool_size,
        max_pool_size=pool_size,
        max_waiting=max_waiting,
        pool_timeout_seconds=pool_timeout_seconds,
        application_name="agnoclaw-postgres-saturation-probe",
    )
    release = threading.Event()
    ready = threading.Barrier(pool_size + 1)

    def hold_connection() -> None:
        with store._connection():  # noqa: SLF001 - exact isolated saturation seam
            ready.wait()
            release.wait(timeout=5)

    def contend() -> RuntimeStoreOverloadedError | None:
        try:
            store.get_run(run_id)
        except RuntimeStoreOverloadedError as exc:
            return exc
        return None

    started = time.perf_counter_ns()
    try:
        with ThreadPoolExecutor(max_workers=pool_size + max_waiting + excess) as executor:
            holders = [executor.submit(hold_connection) for _ in range(pool_size)]
            ready.wait()
            contenders = [
                executor.submit(contend) for _ in range(max_waiting + excess)
            ]
            errors = [future.result(timeout=5) for future in contenders]
            release.set()
            for future in holders:
                future.result(timeout=5)
    finally:
        release.set()
    stats = store.pool_stats
    store.close()
    overloaded = [error for error in errors if error is not None]
    if len(overloaded) != max_waiting + excess:
        raise RuntimeError("pool saturation admitted work while every connection was held")
    if any(error.code != "RUNTIME_STORE_OVERLOADED" for error in overloaded):
        raise RuntimeError("pool saturation did not return the stable overload error")
    retry_after: set[float] = set()
    for error in overloaded:
        if error.details is None or not isinstance(
            error.details.get("retry_after_seconds"),
            (int, float),
        ):
            raise RuntimeError("pool overload error omitted its numeric retry hint")
        retry_after.add(float(error.details["retry_after_seconds"]))
    return {
        "pool_size": pool_size,
        "max_waiting": max_waiting,
        "contenders": max_waiting + excess,
        "typed_overload_errors": len(overloaded),
        "retry_after_seconds": sorted(retry_after),
        "elapsed_ms": round((time.perf_counter_ns() - started) / 1_000_000, 3),
        "pool_stats": stats,
        "bounded": True,
    }


async def _admission_fairness_probe() -> dict[str, object]:
    lanes = AsyncSessionLanes(
        max_concurrency=1,
        max_waiting=32,
        admission_timeout_seconds=2,
    )
    holder_release = asyncio.Event()
    order: list[str] = []

    async def work(name: str, tenant_id: str, *, hold: bool = False) -> None:
        async with lanes.hold(
            tenant_id=tenant_id,
            session_id=name,
            run_id=name,
        ):
            order.append(name)
            if hold:
                await holder_release.wait()
            else:
                await asyncio.sleep(0)

    holder = asyncio.create_task(work("holder", "hot", hold=True))
    while order != ["holder"]:
        await asyncio.sleep(0)
    hot = [asyncio.create_task(work(f"hot-{index}", "hot")) for index in range(8)]
    while lanes.admission_stats["waiting"] != len(hot):
        await asyncio.sleep(0)
    cool = asyncio.create_task(work("cool-0", "cool"))
    while lanes.admission_stats["waiting"] != len(hot) + 1:
        await asyncio.sleep(0)
    holder_release.set()
    await asyncio.gather(holder, *hot, cool)
    cool_position = order.index("cool-0")
    if cool_position > 2:
        raise RuntimeError("tenant round-robin admission allowed a noisy tenant to starve")
    return {
        "entry_order": order,
        "cool_tenant_position": cool_position,
        "bounded_bypass": cool_position <= 2,
        "stats": lanes.admission_stats,
    }


def _cleanup(store: PostgresRuntimeStore, prefix: str) -> int:
    with store._transaction() as conn:  # noqa: SLF001 - exact random-prefix cleanup
        row = conn.execute(
            "DELETE FROM runtime_runs WHERE run_id LIKE %s RETURNING run_id",
            (f"{prefix}:%",),
        ).fetchall()
    return len(row)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure a loopback PostgreSQL test database under bounded concurrent "
            "runtime traffic. Synthetic rows use one random prefix and are removed."
        )
    )
    parser.add_argument("--dsn", required=True, help="Loopback PostgreSQL test DSN")
    parser.add_argument("--history-size", type=int, default=10_000)
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--pool-size", type=int, default=4)
    parser.add_argument("--hot-workers", type=int, default=3)
    parser.add_argument("--pool-timeout-seconds", type=float, default=0.1)
    parser.add_argument("--max-p99-ms", type=float, default=25.0)
    parser.add_argument("--max-p95-slowdown-ratio", type=float, default=4.0)
    return parser.parse_args()


def _validate_arguments(args: argparse.Namespace) -> None:
    _validate_target(args.dsn)
    if args.history_size < 1:
        raise ValueError("--history-size must be positive")
    if args.samples < 50:
        raise ValueError("--samples must be at least 50")
    if args.pool_size < 2:
        raise ValueError("--pool-size must be at least 2")
    if not 1 <= args.hot_workers < args.pool_size:
        raise ValueError("--hot-workers must be positive and lower than --pool-size")
    if not 0 < args.pool_timeout_seconds <= 5:
        raise ValueError("--pool-timeout-seconds must be between 0 and 5")
    if args.max_p99_ms <= 0 or args.max_p95_slowdown_ratio <= 1:
        raise ValueError("latency limits must be positive and slowdown must exceed one")


def main() -> int:
    args = _arguments()
    try:
        _validate_arguments(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    prefix = f"agnoclaw_pg_bench_{uuid4().hex}"
    probe_owner = RunOwner(f"{prefix}:probe", "probe-user")
    hot_owner = RunOwner(f"{prefix}:hot", "hot-user")
    probe_run_id = f"{prefix}:probe-run"
    hot_run_id = f"{prefix}:hot-run"
    store = PostgresRuntimeStore(
        args.dsn,
        min_pool_size=1,
        max_pool_size=args.pool_size,
        max_waiting=max(32, args.hot_workers * 4),
        pool_timeout_seconds=max(args.pool_timeout_seconds, 1.0),
        application_name="agnoclaw-postgres-runtime-benchmark",
    )
    cleaned_rows = 0
    failures: list[str] = []
    started = time.monotonic()
    try:
        _populate_terminal_history(
            store,
            prefix=prefix,
            history_size=args.history_size,
        )
        _create_recoverable(store, run_id=probe_run_id, owner=probe_owner)
        _create_recoverable(store, run_id=hot_run_id, owner=hot_owner)
        latency = _latency_probe(
            store,
            probe_owner=probe_owner,
            hot_owner=hot_owner,
            probe_run_id=probe_run_id,
            samples=args.samples,
            hot_workers=args.hot_workers,
        )
        saturation = _saturation_probe(
            args.dsn,
            run_id=probe_run_id,
            pool_timeout_seconds=args.pool_timeout_seconds,
        )
        admission = asyncio.run(_admission_fairness_probe())

        noisy = latency["under_noisy_neighbor"]
        ratios = latency["p95_slowdown_ratio"]
        for key in ("get_run", "owner_recovery"):
            p99 = float(noisy[key]["p99_ms"])  # type: ignore[index]
            ratio = float(ratios[key])  # type: ignore[index]
            if p99 > args.max_p99_ms:
                failures.append(
                    f"{key} noisy-neighbor p99 {p99:.3f}ms exceeds {args.max_p99_ms:.3f}ms"
                )
            if ratio > args.max_p95_slowdown_ratio:
                failures.append(
                    f"{key} p95 slowdown {ratio:.3f}x exceeds "
                    f"{args.max_p95_slowdown_ratio:.3f}x"
                )
    finally:
        cleaned_rows = _cleanup(store, prefix)
        store.close()

    output = {
        "status": "failed" if failures else "passed",
        "scope": "isolated_loopback_service_probe",
        "production_certification": False,
        "history_size": args.history_size,
        "samples": args.samples,
        "pool_size": args.pool_size,
        "hot_workers": args.hot_workers,
        "latency": latency,
        "saturation": saturation,
        "process_admission_fairness": admission,
        "thresholds": {
            "max_p99_ms": args.max_p99_ms,
            "max_p95_slowdown_ratio": args.max_p95_slowdown_ratio,
            "p95_ratio_baseline_floor_ms": _P95_RATIO_BASELINE_FLOOR_MS,
        },
        "synthetic_rows_cleaned": cleaned_rows,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "failures": failures,
        "open_production_gates": [
            "cross-process tenant fairness",
            "primary failover and network partition",
            "production-scale memory and queue ceilings",
            "production RPO/RTO",
        ],
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
