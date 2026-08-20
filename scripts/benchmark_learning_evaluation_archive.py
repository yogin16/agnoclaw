#!/usr/bin/env python3
"""Benchmark exact-owner PostgreSQL evaluation-archive reads under bounded load."""

from __future__ import annotations

import argparse
import asyncio
import math
import statistics
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from agnoclaw import (
    CandidateAuthor,
    CandidateEvaluation,
    CandidateRisk,
    CandidateState,
    EvaluationArchiveQuery,
    EvaluationVerdict,
    LearningCandidate,
    LearningOwner,
    LearningTarget,
    LocalArtifactStore,
    PostgresLearningLedger,
    PromotionActor,
)
from agnoclaw.learning_candidates import CandidateRecord, _canonical_json
from agnoclaw.runtime import ArtifactScope

_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
_EVALUATOR_DIGEST = "sha256:" + "a" * 64
_MECHANISM_VERSION = "reflector:archive-benchmark:v1"


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


def _timings(call: Callable[[], int], samples: int) -> tuple[dict[str, float], int]:
    for _ in range(min(10, samples)):
        call()
    elapsed: list[float] = []
    rows_observed = 0
    for _ in range(samples):
        started = time.perf_counter_ns()
        rows_observed += call()
        elapsed.append((time.perf_counter_ns() - started) / 1_000_000)
    return _percentiles(elapsed), rows_observed


def _evaluated_at(index: int) -> str:
    return (_BASE_TIME + timedelta(microseconds=index)).isoformat()


def _seed_owner(
    store: PostgresLearningLedger,
    artifact_store: LocalArtifactStore,
    *,
    prefix: str,
    lane: str,
    history_size: int,
) -> tuple[LearningOwner, int, int]:
    tenant_id = f"{prefix}:{lane}"
    namespace = f"{prefix}:{lane}:namespace"
    owner = LearningOwner(tenant_id, namespace)
    source_run_id = f"{prefix}:{lane}:source"
    reference = asyncio.run(
        artifact_store.stage_json(
            {"benchmark": True, "lane": lane},
            scope=ArtifactScope(
                run_id=source_run_id,
                tenant_id=tenant_id,
                user_id="archive-benchmark",
            ),
            purpose="learning.candidate.content",
        )
    )
    candidate_count = min(100, history_size)
    evaluation_counts = [0 for _ in range(candidate_count)]
    latest_evaluations = ["" for _ in range(candidate_count)]
    for index in range(history_size):
        candidate_index = index % candidate_count
        evaluation_counts[candidate_index] += 1
        latest_evaluations[candidate_index] = (
            f"{prefix}:{lane}:evaluation:{index:09d}"
        )

    candidate_rows: list[tuple[object, ...]] = []
    for index in range(candidate_count):
        candidate_id = f"{prefix}:{lane}:candidate:{index:05d}"
        candidate = LearningCandidate(
            candidate_id=candidate_id,
            target=LearningTarget.LEARNED_KNOWLEDGE,
            tenant_id=tenant_id,
            storage_namespace=namespace,
            content_artifact=reference,
            source_run_ids=(source_run_id,),
            evidence_artifact_ids=(f"{prefix}:{lane}:candidate-evidence",),
            confidence=0.8,
            risk=CandidateRisk.LOW,
            created_by=CandidateAuthor.OPERATOR,
            mechanism_version=_MECHANISM_VERSION,
            source_user_id="archive-benchmark",
            created_at=_BASE_TIME.isoformat(),
        )
        record = CandidateRecord(
            candidate=candidate,
            state=CandidateState.REJECTED,
            revision=evaluation_counts[index],
            latest_evaluation_id=latest_evaluations[index],
            updated_at=_evaluated_at(
                index + (evaluation_counts[index] - 1) * candidate_count
            ),
        )
        candidate_rows.append(
            (
                candidate_id,
                candidate.digest,
                tenant_id,
                namespace,
                record.state.value,
                record.revision,
                reference.storage_key,
                _canonical_json(record.to_dict()),
                candidate.created_at,
                record.updated_at,
            )
        )

    evaluation_rows: list[tuple[object, ...]] = []
    reason_rows: list[tuple[object, ...]] = []
    for index in range(history_size):
        candidate_index = index % candidate_count
        candidate_id = f"{prefix}:{lane}:candidate:{candidate_index:05d}"
        evaluation_id = f"{prefix}:{lane}:evaluation:{index:09d}"
        safety_failure = index % 4 == 0
        evaluation = CandidateEvaluation(
            evaluation_id=evaluation_id,
            candidate_id=candidate_id,
            verdict=EvaluationVerdict.REJECTED,
            evaluator_digest=_EVALUATOR_DIGEST,
            evidence_artifact_ids=(f"{prefix}:{lane}:evidence:{index:09d}",),
            safety_passed=not safety_failure,
            evaluated_by=PromotionActor.OPERATOR,
            metrics={
                "gate": {
                    "reasons": [
                        "safety_gate_failed"
                        if safety_failure
                        else "held_out_quality_regression"
                    ],
                    "policy_digest": "sha256:" + "1" * 64,
                    "hypothesis_digest": "sha256:" + "2" * 64,
                    "evaluation_digest": "sha256:" + "3" * 64,
                    "runner_digest": "sha256:" + "4" * 64,
                    "corpus_manifest_digest": "sha256:" + "5" * 64,
                }
            },
            evaluated_at=_evaluated_at(index),
        )
        reason_code = (
            "safety_gate_failed"
            if safety_failure
            else "held_out_quality_regression"
        )
        evaluation_rows.append(
            (
                evaluation_id,
                candidate_id,
                evaluation.digest,
                _canonical_json(evaluation.to_dict()),
                tenant_id,
                namespace,
                LearningTarget.LEARNED_KNOWLEDGE.value,
                _MECHANISM_VERSION,
                evaluation.verdict.value,
                evaluation.evaluator_digest,
                evaluation.safety_passed,
                _canonical_json([reason_code]),
                evaluation.evaluated_at,
            )
        )
        reason_rows.append(
            (evaluation_id, reason_code, candidate_id, evaluation.evaluated_at)
        )

    with store._transaction() as conn:  # noqa: SLF001 - isolated synthetic load fixture
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO learning_candidates(
                    candidate_id, candidate_digest, tenant_id, storage_namespace,
                    state, revision, content_storage_key, record_json,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                candidate_rows,
            )
            cursor.executemany(
                """
                INSERT INTO learning_evaluations(
                    evaluation_id, candidate_id, evaluation_digest,
                    evaluation_json, tenant_id, storage_namespace, target,
                    mechanism_version, verdict, evaluator_digest, safety_passed,
                    reason_codes_json, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                """,
                evaluation_rows,
            )
            cursor.executemany(
                """
                INSERT INTO learning_evaluation_reasons(
                    evaluation_id, reason_code, candidate_id, created_at
                ) VALUES (%s, %s, %s, %s)
                """,
                reason_rows,
            )
        conn.execute("ANALYZE learning_candidates")
        conn.execute("ANALYZE learning_evaluations")
    return owner, candidate_count, history_size


def _query_pair(
    store: PostgresLearningLedger,
    *,
    owner: LearningOwner,
    candidate_prefix: str,
    page_limit: int,
) -> int:
    query = EvaluationArchiveQuery(
        evaluator_digest=_EVALUATOR_DIGEST,
        reason_code="safety_gate_failed",
        mechanism_version=_MECHANISM_VERSION,
        target=LearningTarget.LEARNED_KNOWLEDGE,
        safety_passed=False,
        limit=page_limit,
    )
    first = store.query_evaluation_archive(owner=owner, query=query)
    if len(first.items) != page_limit or first.next_cursor is None:
        raise RuntimeError("archive benchmark could not fill its first keyset page")
    second = store.query_evaluation_archive(
        owner=owner,
        query=EvaluationArchiveQuery(
            evaluator_digest=query.evaluator_digest,
            reason_code=query.reason_code,
            mechanism_version=query.mechanism_version,
            target=query.target,
            safety_passed=query.safety_passed,
            limit=query.limit,
            cursor=first.next_cursor,
        ),
    )
    if len(second.items) != page_limit:
        raise RuntimeError("archive benchmark could not fill its second keyset page")
    first_ids = {item.evaluation_id for item in first.items}
    second_ids = {item.evaluation_id for item in second.items}
    if first_ids & second_ids:
        raise RuntimeError("archive keyset pages overlapped")
    all_items = (*first.items, *second.items)
    if any(not item.candidate_id.startswith(candidate_prefix) for item in all_items):
        raise RuntimeError("archive query crossed its exact owner boundary")
    return len(first.items) + len(second.items)


def _latency_probe(
    store: PostgresLearningLedger,
    *,
    target_owner: LearningOwner,
    target_prefix: str,
    hot_owner: LearningOwner,
    hot_prefix: str,
    samples: int,
    hot_workers: int,
    page_limit: int,
) -> dict[str, object]:
    target_query = lambda: _query_pair(  # noqa: E731 - measured callable
        store,
        owner=target_owner,
        candidate_prefix=target_prefix,
        page_limit=page_limit,
    )
    hot_query = lambda: _query_pair(  # noqa: E731 - measured callable
        store,
        owner=hot_owner,
        candidate_prefix=hot_prefix,
        page_limit=page_limit,
    )
    baseline, baseline_rows = _timings(target_query, samples)

    stop = threading.Event()
    ready = threading.Barrier(hot_workers + 1)
    hot_counts = [0 for _ in range(hot_workers)]

    def noisy_neighbor(index: int) -> None:
        ready.wait()
        while not stop.is_set():
            hot_query()
            hot_counts[index] += 1

    with ThreadPoolExecutor(max_workers=hot_workers) as executor:
        futures = [executor.submit(noisy_neighbor, index) for index in range(hot_workers)]
        ready.wait()
        try:
            noisy, noisy_rows = _timings(target_query, samples)
        finally:
            stop.set()
        for future in futures:
            future.result(timeout=10)

    return {
        "baseline": {
            "latency": baseline,
            "rows_observed": baseline_rows,
        },
        "under_noisy_neighbor": {
            "latency": noisy,
            "rows_observed": noisy_rows,
            "hot_queries_completed": sum(hot_counts),
            "hot_worker_queries": hot_counts,
        },
        "p95_slowdown_ratio": round(
            noisy["p95_ms"] / max(baseline["p95_ms"], 0.1),
            3,
        ),
        "owner_isolation": True,
        "keyset_pages_disjoint": True,
        "queries_per_sample": 2,
    }


def _cleanup(store: PostgresLearningLedger, owners: tuple[LearningOwner, ...]) -> dict[str, int]:
    tenants = tuple(owner.tenant_id for owner in owners)
    with store._transaction() as conn:  # noqa: SLF001 - exact random-tenant cleanup
        evaluation_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM learning_evaluations AS e
                JOIN learning_candidates AS c ON c.candidate_id = e.candidate_id
                WHERE c.tenant_id = ANY(%s)
                """,
                (list(tenants),),
            ).fetchone()["count"]
        )
        candidate_count = len(
            conn.execute(
                """
                DELETE FROM learning_candidates
                WHERE tenant_id = ANY(%s)
                RETURNING candidate_id
                """,
                (list(tenants),),
            ).fetchall()
        )
    return {
        "candidates": candidate_count,
        "evaluations": evaluation_count,
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure content-free evaluation archive reads in a loopback PostgreSQL "
            "test database. Synthetic rows use random tenants and are removed."
        )
    )
    parser.add_argument("--dsn", required=True, help="Loopback PostgreSQL test DSN")
    parser.add_argument("--history-size", type=int, default=10_000)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--pool-size", type=int, default=6)
    parser.add_argument("--hot-workers", type=int, default=3)
    parser.add_argument("--page-limit", type=int, default=50)
    parser.add_argument("--max-p99-ms", type=float, default=100.0)
    parser.add_argument("--max-p95-slowdown-ratio", type=float, default=5.0)
    return parser.parse_args()


def _validate_arguments(args: argparse.Namespace) -> None:
    _validate_target(args.dsn)
    if not 1_000 <= args.history_size <= 100_000:
        raise ValueError("--history-size must be between 1000 and 100000")
    if args.samples < 50:
        raise ValueError("--samples must be at least 50")
    if args.pool_size < 2:
        raise ValueError("--pool-size must be at least 2")
    if not 1 <= args.hot_workers < args.pool_size:
        raise ValueError("--hot-workers must be positive and lower than --pool-size")
    if not 1 <= args.page_limit <= min(1000, args.history_size // 8):
        raise ValueError("--page-limit is too large for two full filtered pages")
    if args.max_p99_ms <= 0 or args.max_p95_slowdown_ratio <= 1:
        raise ValueError("latency limits must be positive and slowdown must exceed one")


def main() -> int:
    args = _arguments()
    try:
        _validate_arguments(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    prefix = f"agnoclaw_learning_archive_bench_{uuid4().hex}"
    store = PostgresLearningLedger(
        args.dsn,
        min_pool_size=1,
        max_pool_size=args.pool_size,
        max_waiting=max(32, args.hot_workers * 4),
        application_name="agnoclaw-learning-archive-benchmark",
    )
    owners: tuple[LearningOwner, ...] = ()
    seeded_candidates = 0
    seeded_evaluations = 0
    cleaned = {"candidates": 0, "evaluations": 0}
    failures: list[str] = []
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="agnoclaw-learning-archive-") as directory:
            artifacts = LocalArtifactStore(Path(directory) / "artifacts")
            target_owner, target_candidates, target_evaluations = _seed_owner(
                store,
                artifacts,
                prefix=prefix,
                lane="target",
                history_size=args.history_size,
            )
            hot_owner, hot_candidates, hot_evaluations = _seed_owner(
                store,
                artifacts,
                prefix=prefix,
                lane="hot",
                history_size=args.history_size,
            )
            owners = (target_owner, hot_owner)
            seeded_candidates = target_candidates + hot_candidates
            seeded_evaluations = target_evaluations + hot_evaluations
            latency = _latency_probe(
                store,
                target_owner=target_owner,
                target_prefix=f"{prefix}:target:candidate:",
                hot_owner=hot_owner,
                hot_prefix=f"{prefix}:hot:candidate:",
                samples=args.samples,
                hot_workers=args.hot_workers,
                page_limit=args.page_limit,
            )
            noisy = latency["under_noisy_neighbor"]
            noisy_p99 = float(noisy["latency"]["p99_ms"])  # type: ignore[index]
            slowdown = float(latency["p95_slowdown_ratio"])
            if noisy_p99 > args.max_p99_ms:
                failures.append(
                    f"noisy-neighbor p99 {noisy_p99:.3f}ms exceeds "
                    f"{args.max_p99_ms:.3f}ms"
                )
            if slowdown > args.max_p95_slowdown_ratio:
                failures.append(
                    f"p95 slowdown {slowdown:.3f}x exceeds "
                    f"{args.max_p95_slowdown_ratio:.3f}x"
                )
    finally:
        if owners:
            cleaned = _cleanup(store, owners)
        store.close()

    if cleaned != {
        "candidates": seeded_candidates,
        "evaluations": seeded_evaluations,
    }:
        failures.append("synthetic row cleanup count did not match the seeded count")
    output: dict[str, Any] = {
        "status": "failed" if failures else "passed",
        "scope": "isolated_loopback_postgresql_learning_archive_probe",
        "production_certification": False,
        "history_size_per_owner": args.history_size,
        "samples": args.samples,
        "page_limit": args.page_limit,
        "pool_size": args.pool_size,
        "hot_workers": args.hot_workers,
        "seeded": {
            "candidates": seeded_candidates,
            "evaluations": seeded_evaluations,
        },
        "latency": latency,
        "thresholds": {
            "max_p99_ms": args.max_p99_ms,
            "max_p95_slowdown_ratio": args.max_p95_slowdown_ratio,
        },
        "synthetic_rows_cleaned": cleaned,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "failures": failures,
        "open_production_gates": [
            "production-volume and retention-age distributions",
            "multi-AZ failover and network partition",
            "production memory, connection, and queue budgets",
            "additional projection-index policy beyond the measured volume",
        ],
    }
    print(_canonical_json(output))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
