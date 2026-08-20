#!/usr/bin/env python3
"""Benchmark bounded SQLite recovery discovery against growing terminal history."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agnoclaw.runtime.lifecycle import RunSnapshot, RunState
from agnoclaw.runtime.operations import (
    EffectClass,
    OperationIntent,
    OperationKind,
    OperationRecord,
    OperationSettlement,
    OperationState,
)
from agnoclaw.runtime.store import RunOwner, SQLiteRuntimeStore, _canonical_json

_OWNER = RunOwner(tenant_id="benchmark-tenant", user_id="benchmark-user")
_OLD = "2000-01-01T00:00:00+00:00"


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


def _run_row(run_id: str, state: RunState) -> tuple[object, ...]:
    snapshot = RunSnapshot(
        run_id=run_id,
        state=state,
        revision=1,
        tenant_id=_OWNER.tenant_id,
        user_id=_OWNER.user_id,
        created_at=_OLD,
        updated_at=_OLD,
    )
    return (
        run_id,
        _OWNER.tenant_id,
        _OWNER.user_id,
        None,
        state.value,
        1,
        1,
        _snapshot_json(snapshot),
        _OLD,
        _OLD,
        _OLD,
    )


def _operation_row(index: int, run_id: str) -> tuple[object, ...]:
    operation_id = f"noise-operation-{index:09d}"
    intent = OperationIntent(
        operation_id=operation_id,
        run_id=run_id,
        attempt_id=f"attempt-{index:09d}",
        kind=OperationKind.CAPABILITY,
        target="benchmark.terminal",
        request_digest="sha256:" + "0" * 64,
        effect_class=EffectClass.NON_REPEATABLE,
        prepared_at=_OLD,
    )
    record = OperationRecord(
        intent=intent,
        state=OperationState.SUCCEEDED,
        revision=2,
        dispatch_attempt=1,
        fence_token=1,
        worker_id="benchmark-worker",
        settlement=OperationSettlement(
            state=OperationState.SUCCEEDED,
            result_reference=f"result-{index:09d}",
            result_slot_id=intent.result_slot_id,
            settled_at=_OLD,
        ),
        updated_at=_OLD,
    )
    return (
        operation_id,
        run_id,
        intent.attempt_id,
        record.state.value,
        record.revision,
        intent.effect_class.value,
        record.fence_token,
        intent.digest,
        _canonical_json(record.to_dict()),
        "{}",
        _OLD,
        _OLD,
    )


def _candidate_operation_row(
    operation_id: str,
    run_id: str,
    state: OperationState,
) -> tuple[object, ...]:
    intent = OperationIntent(
        operation_id=operation_id,
        run_id=run_id,
        attempt_id=f"{operation_id}:attempt",
        kind=OperationKind.MODEL,
        target="benchmark.candidate",
        request_digest="sha256:" + "1" * 64,
        effect_class=EffectClass.NON_REPEATABLE,
        prepared_at=_OLD,
    )
    settlement = (
        OperationSettlement(
            state=OperationState.UNKNOWN,
            safe_error={"code": "BENCHMARK_UNKNOWN"},
            settled_at=_OLD,
        )
        if state is OperationState.UNKNOWN
        else None
    )
    record = OperationRecord(
        intent=intent,
        state=state,
        revision=2 if settlement is not None else 0,
        dispatch_attempt=1 if settlement is not None else 0,
        fence_token=1 if settlement is not None else 0,
        worker_id="benchmark-worker" if settlement is not None else None,
        settlement=settlement,
        updated_at=_OLD,
    )
    return (
        operation_id,
        run_id,
        intent.attempt_id,
        record.state.value,
        record.revision,
        intent.effect_class.value,
        record.fence_token,
        intent.digest,
        _canonical_json(record.to_dict()),
        "{}",
        _OLD,
        _OLD,
    )


def _populate(path: Path, history_size: int) -> SQLiteRuntimeStore:
    store = SQLiteRuntimeStore(path)
    run_rows = [
        _run_row(f"noise-run-{index:09d}", RunState.COMPLETED)
        for index in range(history_size)
    ]
    run_rows.extend(
        [
            _run_row("recoverable-run", RunState.QUEUED),
            _run_row("reconciliation-run", RunState.WAITING_FOR_RECONCILIATION),
        ]
    )
    operation_rows = [
        _operation_row(index, f"noise-run-{index:09d}") for index in range(history_size)
    ]
    operation_rows.extend(
        [
            _candidate_operation_row(
                "recoverable-operation",
                "recoverable-run",
                OperationState.PLANNED,
            ),
            _candidate_operation_row(
                "reconciliation-operation",
                "reconciliation-run",
                OperationState.UNKNOWN,
            ),
        ]
    )
    with store._transaction() as conn:  # noqa: SLF001 - isolated benchmark fixture
        conn.executemany(
            """
            INSERT INTO runtime_runs(
                run_id, tenant_id, user_id, session_id, state, revision,
                next_sequence, snapshot_json, created_at, updated_at,
                authority_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            run_rows,
        )
        conn.executemany(
            """
            INSERT INTO runtime_operations(
                operation_id, run_id, attempt_id, state, revision, effect_class,
                fence_token, intent_digest, record_json, prepared_event_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            operation_rows,
        )
        conn.execute("ANALYZE")
    return store


def _timings(
    call: Callable[[], Any],
    repetitions: int,
    calls_per_sample: int,
) -> dict[str, float]:
    for _ in range(5):
        for _ in range(calls_per_sample):
            call()
    samples: list[float] = []
    for _ in range(repetitions):
        started = time.perf_counter_ns()
        for _ in range(calls_per_sample):
            call()
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        samples.append(elapsed_ms / calls_per_sample)
    ordered = sorted(samples)
    return {
        "p50_ms": round(statistics.median(ordered), 6),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 6),
        "max_ms": round(ordered[-1], 6),
    }


def _measure(
    history_size: int,
    repetitions: int,
    calls_per_sample: int,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agnoclaw-recovery-benchmark-") as directory:
        store = _populate(Path(directory) / "runtime.db", history_size)
        result = {
            "history_size": history_size,
            "recoverable_operations": _timings(
                lambda: store.list_recoverable_operations(limit=10),
                repetitions,
                calls_per_sample,
            ),
            "recoverable_runs": _timings(
                lambda: store.list_recoverable_runs(
                    owner=_OWNER,
                    minimum_age_seconds=0,
                    limit=10,
                ),
                repetitions,
                calls_per_sample,
            ),
            "reconciliation_operations": _timings(
                lambda: store.list_reconciliation_operations(
                    owner=_OWNER,
                    minimum_age_seconds=0,
                    limit=10,
                ),
                repetitions,
                calls_per_sample,
            ),
        }
        store.close()
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history-sizes", default="1000,10000")
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--calls-per-sample", type=int, default=100)
    parser.add_argument("--max-p95-ms", type=float, default=25.0)
    parser.add_argument("--max-growth-ratio", type=float, default=2.0)
    args = parser.parse_args()
    sizes = tuple(int(item) for item in args.history_sizes.split(","))
    if len(sizes) < 2 or any(item < 1 for item in sizes) or tuple(sorted(sizes)) != sizes:
        parser.error("--history-sizes requires at least two increasing positive integers")
    if args.repetitions < 10:
        parser.error("--repetitions must be at least 10")
    if args.calls_per_sample < 1:
        parser.error("--calls-per-sample must be positive")
    if args.max_p95_ms <= 0 or args.max_growth_ratio <= 1:
        parser.error("latency limits must be positive and growth ratio must exceed one")

    measurements = [
        _measure(size, args.repetitions, args.calls_per_sample) for size in sizes
    ]
    failures: list[str] = []
    ratios: dict[str, float] = {}
    for key in (
        "recoverable_operations",
        "recoverable_runs",
        "reconciliation_operations",
    ):
        first = float(measurements[0][key]["p95_ms"])  # type: ignore[index]
        last = float(measurements[-1][key]["p95_ms"])  # type: ignore[index]
        ratio = last / max(first, 0.001)
        ratios[key] = round(ratio, 3)
        if last > args.max_p95_ms:
            failures.append(f"{key} p95 {last:.3f}ms exceeds {args.max_p95_ms:.3f}ms")
        if ratio > args.max_growth_ratio:
            failures.append(
                f"{key} p95 growth {ratio:.2f}x exceeds {args.max_growth_ratio:.2f}x"
            )
    output = {
        "status": "failed" if failures else "passed",
        "history_sizes": sizes,
        "repetitions": args.repetitions,
        "calls_per_sample": args.calls_per_sample,
        "measurements": measurements,
        "p95_growth_ratio": ratios,
        "failures": failures,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
