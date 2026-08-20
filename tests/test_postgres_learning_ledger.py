"""Real-service conformance tests for PostgresLearningLedger."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agnoclaw import (
    CandidateAction,
    CandidateAuthor,
    CandidateEvaluation,
    CandidateReconciliation,
    CandidateRisk,
    CandidateState,
    EvaluationArchiveQuery,
    EvaluationVerdict,
    HarnessError,
    LearningApplication,
    LearningApplicationKind,
    LearningCandidate,
    LearningEffectivenessPolicy,
    LearningEffectivenessRecommendation,
    LearningGateway,
    LearningOutboxLeaseError,
    LearningOutcome,
    LearningOutcomeKind,
    LearningOwner,
    LearningReconciliationCoordinator,
    LearningReconciliationWorker,
    LearningReconciliationWorkerConfig,
    LearningReconciliationWorkerLeaseError,
    LearningTarget,
    LocalArtifactStore,
    PostgresLearningLedger,
    PromotionActor,
    ReconciliationCursor,
    ReconciliationCursorScopeError,
    ReconciliationKind,
    ReconciliationObservation,
    ReconciliationVerdict,
)
from agnoclaw.learning_candidates import CandidateNotFoundError, CandidateRevisionError
from agnoclaw.learning_reconciliation import RECONCILIATION_EVIDENCE_PURPOSE
from agnoclaw.runtime import ArtifactScope

POSTGRES_URL = os.getenv("AGNOCLAW_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="AGNOCLAW_TEST_POSTGRES_URL is not configured",
)


def _claim_learning_worker_lease_and_block(
    dsn: str,
    tenant_id: str,
    storage_namespace: str,
    ready,
) -> None:
    """Child-process crash fixture; the parent deliberately terminates it."""
    ledger = PostgresLearningLedger(
        dsn,
        min_pool_size=1,
        max_pool_size=1,
        max_waiting=2,
    )
    lease = ledger.claim_reconciliation_worker(
        owner=LearningOwner(tenant_id, storage_namespace),
        worker_id="pg-abrupt-child",
        lease_seconds=1,
    )
    ready.put(None if lease is None else lease.to_dict())
    while True:
        time.sleep(60)


@pytest.fixture
def store():
    assert POSTGRES_URL is not None
    value = PostgresLearningLedger(
        POSTGRES_URL,
        min_pool_size=1,
        max_pool_size=4,
        max_waiting=16,
    )
    with value._transaction() as conn:
        conn.execute(
            """
            TRUNCATE learning_candidates, learning_reconciliation_workers,
                learning_schema_migrations
            RESTART IDENTITY CASCADE
            """
        )
    value.migrate()
    yield value
    value.close()


def _candidate(tmp_path, *, candidate_id: str = "lc-pg-1") -> LearningCandidate:
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    reference = asyncio.run(
        artifacts.stage_json(
            {"title": "Retries", "learning": "Use bounded retries."},
            scope=ArtifactScope(
                run_id="run-pg-source",
                tenant_id="tenant-1",
                user_id="user-1",
            ),
            purpose="learning.candidate.content",
        )
    )
    return LearningCandidate(
        candidate_id=candidate_id,
        target=LearningTarget.LEARNED_KNOWLEDGE,
        tenant_id="tenant-1",
        storage_namespace="namespace-1",
        content_artifact=reference,
        source_run_ids=("run-pg-source",),
        evidence_artifact_ids=("artifact-source",),
        confidence=0.9,
        risk=CandidateRisk.MEDIUM,
        created_by=CandidateAuthor.AGENT,
        mechanism_version="reflector:v1",
        source_user_id="user-1",
    )


def _evaluation(candidate_id: str, *, suffix: str = "1") -> CandidateEvaluation:
    return CandidateEvaluation(
        evaluation_id=f"eval-pg-{suffix}",
        candidate_id=candidate_id,
        verdict=EvaluationVerdict.QUALIFIED,
        evaluator_digest="sha256:" + "a" * 64,
        evidence_artifact_ids=(f"artifact-eval-{suffix}",),
        safety_passed=True,
        evaluated_by=PromotionActor.OPERATOR,
        metrics={"held_out": 0.9},
        control_metrics={"held_out": 0.7},
    )


def test_postgres_candidate_scope_idempotency_and_evaluation(store, tmp_path) -> None:
    candidate = _candidate(tmp_path)
    owner = candidate.owner
    created = store.create_candidate(candidate)
    assert store.schema_version == 6
    assert store.create_candidate(candidate) == created
    assert store.list_candidates(owner=owner) == [created]
    assert store.list_artifact_storage_keys() == [candidate.content_artifact.storage_key]

    with pytest.raises(CandidateNotFoundError):
        store.get_candidate(
            candidate.candidate_id,
            owner=LearningOwner("other", owner.storage_namespace),
        )

    evaluation = _evaluation(candidate.candidate_id)
    qualified = store.record_evaluation(
        evaluation,
        owner=owner,
        expected_revision=0,
        mutation_id="evaluate:1",
    )
    assert qualified.state is CandidateState.QUALIFIED
    assert (
        store.record_evaluation(
            evaluation,
            owner=owner,
            expected_revision=0,
            mutation_id="evaluate:1",
        )
        == qualified
    )
    assert store.get_evaluation(evaluation.evaluation_id, owner=owner) == evaluation
    assert store.list_evaluations(candidate.candidate_id, owner=owner) == [evaluation]
    events = store.list_events(candidate.candidate_id, owner=owner)
    assert [item.event_type for item in events] == [
        "learning.candidate.captured",
        "learning.candidate.evaluated",
    ]
    leased = store.lease_outbox(owner="pg-exporter")
    assert [item.event for item in leased] == events
    with pytest.raises(LearningOutboxLeaseError):
        store.acknowledge_outbox(
            outbox_id=leased[0].outbox_id,
            lease_token="wrong-token",
        )
    for item in leased:
        store.acknowledge_outbox(
            outbox_id=item.outbox_id,
            lease_token=item.lease_token,
        )
    assert store.lease_outbox(owner="pg-exporter") == []


def test_postgres_evaluation_archive_owner_filters_and_keyset(store, tmp_path) -> None:
    with store._connection() as conn:
        index_rows = conn.execute(
            """
            SELECT indexname, indexdef FROM pg_indexes
            WHERE schemaname = current_schema()
              AND indexname IN (
                  'learning_evaluations_candidate_idx',
                  'learning_evaluations_archive_owner_idx',
                  'learning_evaluations_archive_filter_idx'
              )
            """
        ).fetchall()
    indexes = {str(row["indexname"]): str(row["indexdef"]) for row in index_rows}
    assert (
        "(candidate_id, created_at DESC, evaluation_id DESC)"
        in indexes["learning_evaluations_candidate_idx"]
    )
    assert (
        "(tenant_id, storage_namespace, created_at DESC, evaluation_id DESC)"
        in indexes["learning_evaluations_archive_owner_idx"]
    )
    assert (
        "verdict, evaluator_digest, safety_passed"
        in indexes["learning_evaluations_archive_filter_idx"]
    )

    first = _candidate(tmp_path, candidate_id="lc-pg-archive-1")
    second = _candidate(tmp_path, candidate_id="lc-pg-archive-2")
    store.create_candidate(first)
    store.create_candidate(second)

    def evaluation(
        candidate_id: str,
        *,
        evaluation_id: str,
        verdict: EvaluationVerdict,
        reason: str,
        evaluated_at: str,
    ) -> CandidateEvaluation:
        return CandidateEvaluation(
            evaluation_id=evaluation_id,
            candidate_id=candidate_id,
            verdict=verdict,
            evaluator_digest="sha256:" + "d" * 64,
            evidence_artifact_ids=(f"artifact-{evaluation_id}",),
            safety_passed=reason != "safety_gate_failed",
            evaluated_by=PromotionActor.OPERATOR,
            metrics={
                "gate": {
                    "reasons": [reason],
                    "policy_digest": "sha256:" + "1" * 64,
                    "hypothesis_digest": "sha256:" + "2" * 64,
                    "evaluation_digest": "sha256:" + "3" * 64,
                    "runner_digest": "sha256:" + "4" * 64,
                    "corpus_manifest_digest": "sha256:" + "5" * 64,
                }
            },
            evaluated_at=evaluated_at,
        )

    first_evaluation = evaluation(
        first.candidate_id,
        evaluation_id="eval-pg-archive-1",
        verdict=EvaluationVerdict.REJECTED,
        reason="safety_gate_failed",
        evaluated_at="2026-08-14T00:00:01+00:00",
    )
    second_evaluation = evaluation(
        second.candidate_id,
        evaluation_id="eval-pg-archive-2",
        verdict=EvaluationVerdict.INCONCLUSIVE,
        reason="judge_order_unbalanced",
        evaluated_at="2026-08-14T00:00:02+00:00",
    )
    store.record_evaluation(
        first_evaluation,
        owner=first.owner,
        expected_revision=0,
        mutation_id="archive:1",
    )
    store.record_evaluation(
        second_evaluation,
        owner=second.owner,
        expected_revision=0,
        mutation_id="archive:2",
    )

    page = store.query_evaluation_archive(
        owner=first.owner,
        query=EvaluationArchiveQuery(limit=1),
    )
    assert [item.evaluation_id for item in page.items] == ["eval-pg-archive-2"]
    assert page.next_cursor is not None
    tail = store.query_evaluation_archive(
        owner=first.owner,
        query=EvaluationArchiveQuery(limit=1, cursor=page.next_cursor),
    )
    assert [item.evaluation_id for item in tail.items] == ["eval-pg-archive-1"]
    assert tail.next_cursor is None

    safety = store.query_evaluation_archive(
        owner=first.owner,
        query=EvaluationArchiveQuery(
            evaluator_digest="sha256:" + "d" * 64,
            reason_code="safety_gate_failed",
            mechanism_version="reflector:v1",
            target=LearningTarget.LEARNED_KNOWLEDGE,
            safety_passed=False,
        ),
    )
    assert [item.evaluation_id for item in safety.items] == ["eval-pg-archive-1"]
    assert safety.items[0].corpus_manifest_digest == "sha256:" + "5" * 64
    assert (
        store.query_evaluation_archive(
            owner=LearningOwner("other", first.owner.storage_namespace),
            query=EvaluationArchiveQuery(),
        ).items
        == ()
    )


def test_postgres_v4_evaluations_backfill_into_v5_archive_projection(
    store,
    tmp_path,
) -> None:
    candidate = _candidate(tmp_path, candidate_id="lc-pg-archive-migration")
    store.create_candidate(candidate)
    evaluation = CandidateEvaluation(
        evaluation_id="eval-pg-archive-migration",
        candidate_id=candidate.candidate_id,
        verdict=EvaluationVerdict.REJECTED,
        evaluator_digest="sha256:" + "a" * 64,
        evidence_artifact_ids=("artifact-pg-archive-migration",),
        safety_passed=False,
        evaluated_by=PromotionActor.OPERATOR,
        metrics={"gate": {"reasons": ["safety_gate_failed"]}},
        evaluated_at="2026-08-14T00:00:00+00:00",
    )
    store.record_evaluation(
        evaluation,
        owner=candidate.owner,
        expected_revision=0,
        mutation_id="archive:migrate:v4",
    )

    with store._transaction() as conn:
        conn.execute("DROP INDEX learning_evaluations_archive_owner_idx")
        conn.execute("DROP INDEX learning_evaluations_archive_filter_idx")
        conn.execute("DROP TABLE learning_evaluation_reasons")
        conn.execute(
            """
            ALTER TABLE learning_evaluations
                DROP COLUMN tenant_id,
                DROP COLUMN storage_namespace,
                DROP COLUMN target,
                DROP COLUMN mechanism_version,
                DROP COLUMN verdict,
                DROP COLUMN evaluator_digest,
                DROP COLUMN safety_passed,
                DROP COLUMN reason_codes_json
            """
        )
        conn.execute("DELETE FROM learning_schema_migrations WHERE version >= 5")

    store.migrate()
    assert store.schema_version == 6
    page = store.query_evaluation_archive(
        owner=candidate.owner,
        query=EvaluationArchiveQuery(reason_code="safety_gate_failed"),
    )
    assert [item.evaluation_id for item in page.items] == ["eval-pg-archive-migration"]
    with store._connection() as conn:
        projection = conn.execute(
            """
            SELECT storage_namespace, target, mechanism_version, verdict,
                   evaluator_digest, safety_passed, reason_codes_json
            FROM learning_evaluations WHERE evaluation_id = %s
            """,
            (evaluation.evaluation_id,),
        ).fetchone()
    assert dict(projection) == {
        "storage_namespace": candidate.storage_namespace,
        "target": candidate.target.value,
        "mechanism_version": candidate.mechanism_version,
        "verdict": EvaluationVerdict.REJECTED.value,
        "evaluator_digest": evaluation.evaluator_digest,
        "safety_passed": False,
        "reason_codes_json": '["safety_gate_failed"]',
    }


def test_postgres_learning_outcomes_are_durable_scoped_and_summarized(store, tmp_path) -> None:
    candidate = _candidate(tmp_path)
    owner = candidate.owner
    store.create_candidate(candidate)
    qualified = store.record_evaluation(
        _evaluation(candidate.candidate_id),
        owner=owner,
        expected_revision=0,
        mutation_id="outcomes:evaluate",
    )
    promoting = store.begin_promotion(
        candidate.candidate_id,
        owner=owner,
        expected_revision=qualified.revision,
        mutation_id="outcomes:promote:begin",
        promotion_id="outcomes-promotion",
        actor=PromotionActor.OPERATOR,
    )
    promoted = store.settle_promotion(
        candidate.candidate_id,
        owner=owner,
        expected_revision=promoting.revision,
        mutation_id="outcomes:promote:settle",
        succeeded=True,
        target_reference="agno:learned_knowledge:outcomes",
    )
    for index in range(5):
        application = LearningApplication(
            application_id=f"pg-application-{index}",
            candidate_id=candidate.candidate_id,
            run_id=f"pg-run-{index}",
            target_reference=promoted.target_reference or "",
            kind=LearningApplicationKind.APPLIED,
            observer_digest="sha256:" + "c" * 64,
            evidence_artifact_ids=(f"pg-application-evidence-{index}",),
        )
        assert store.record_application(application, owner=owner) == application
        outcome = LearningOutcome(
            outcome_id=f"pg-outcome-{index}",
            application_id=application.application_id,
            candidate_id=candidate.candidate_id,
            run_id=application.run_id,
            kind=LearningOutcomeKind.SUCCESS,
            score=0.6,
            evaluator_digest="sha256:" + "d" * 64,
            evidence_artifact_ids=(f"pg-outcome-evidence-{index}",),
            evaluated_by=PromotionActor.HOST,
        )
        assert store.record_outcome(outcome, owner=owner) == outcome

    summary = store.summarize_effectiveness(
        candidate.candidate_id,
        owner=owner,
        policy=LearningEffectivenessPolicy(),
    )
    assert summary.recommendation is LearningEffectivenessRecommendation.RETAIN
    assert summary.evaluated_outcomes == 5
    assert summary.independent_runs == 5
    assert store.get_candidate(candidate.candidate_id, owner=owner) == promoted
    with pytest.raises(HarnessError) as hidden:
        store.get_application(
            "pg-application-0",
            owner=LearningOwner("other", owner.storage_namespace),
        )
    assert hidden.value.code == "LEARNING_APPLICATION_NOT_FOUND"


def test_postgres_promotion_rollback_and_tombstone(store, tmp_path) -> None:
    candidate = _candidate(tmp_path)
    owner = candidate.owner
    store.create_candidate(candidate)
    qualified = store.record_evaluation(
        _evaluation(candidate.candidate_id),
        owner=owner,
        expected_revision=0,
        mutation_id="evaluate:1",
    )
    promoting = store.begin_promotion(
        candidate.candidate_id,
        owner=owner,
        expected_revision=qualified.revision,
        mutation_id="promote:1:begin",
        promotion_id="promotion-1",
        actor=PromotionActor.OPERATOR,
    )
    promoted = store.settle_promotion(
        candidate.candidate_id,
        owner=owner,
        expected_revision=promoting.revision,
        mutation_id="promote:1:settle",
        succeeded=True,
        target_reference="agno:learned_knowledge:lc-pg-1",
    )
    rolling_back = store.begin_rollback(
        candidate.candidate_id,
        owner=owner,
        expected_revision=promoted.revision,
        mutation_id="rollback:1:begin",
        rollback_id="rollback-1",
        actor=PromotionActor.OPERATOR,
    )
    rolled_back = store.settle_rollback(
        candidate.candidate_id,
        owner=owner,
        expected_revision=rolling_back.revision,
        mutation_id="rollback:1:settle",
        succeeded=True,
    )
    deleted = store.transition_candidate(
        candidate.candidate_id,
        owner=owner,
        expected_revision=rolled_back.revision,
        mutation_id="delete:1",
        action=CandidateAction.DELETE,
    )
    assert deleted.state is CandidateState.DELETED
    assert store.list_artifact_storage_keys() == []


def test_postgres_candidate_revision_cas_is_concurrent(store, tmp_path) -> None:
    candidate = _candidate(tmp_path)
    store.create_candidate(candidate)

    def evaluate(suffix: str):
        return store.record_evaluation(
            _evaluation(candidate.candidate_id, suffix=suffix),
            owner=candidate.owner,
            expected_revision=0,
            mutation_id=f"evaluate:{suffix}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(evaluate, suffix) for suffix in ("a", "b")]
    results = []
    errors = []
    for future in futures:
        try:
            results.append(future.result())
        except Exception as exc:  # noqa: BLE001 - asserting concurrency outcome
            errors.append(exc)
    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], CandidateRevisionError)


def test_postgres_mutation_fault_rolls_back_record_and_evaluation(store, tmp_path) -> None:
    candidate = _candidate(tmp_path)
    store.create_candidate(candidate)

    def fail(stage: str) -> None:
        if stage == "after_candidate_update":
            raise RuntimeError("injected rollback")

    store._fault_injector = fail
    with pytest.raises(RuntimeError, match="injected rollback"):
        store.record_evaluation(
            _evaluation(candidate.candidate_id),
            owner=candidate.owner,
            expected_revision=0,
            mutation_id="evaluate:fault",
        )
    store._fault_injector = None
    assert (
        store.get_candidate(
            candidate.candidate_id,
            owner=candidate.owner,
        ).state
        is CandidateState.CAPTURED
    )
    assert store.list_evaluations(candidate.candidate_id, owner=candidate.owner) == []
    assert [
        item.event_type for item in store.list_events(candidate.candidate_id, owner=candidate.owner)
    ] == ["learning.candidate.captured"]


def test_postgres_unknown_promotion_reconciliation(store, tmp_path) -> None:
    candidate = _candidate(tmp_path)
    owner = candidate.owner
    store.create_candidate(candidate)
    qualified = store.record_evaluation(
        _evaluation(candidate.candidate_id),
        owner=owner,
        expected_revision=0,
        mutation_id="evaluate:reconcile",
    )
    promoting = store.begin_promotion(
        candidate.candidate_id,
        owner=owner,
        expected_revision=qualified.revision,
        mutation_id="promote:unknown:begin",
        promotion_id="promotion-unknown",
        actor=PromotionActor.HOST,
    )
    unknown = store.settle_promotion(
        candidate.candidate_id,
        owner=owner,
        expected_revision=promoting.revision,
        mutation_id="promote:unknown:settle",
        succeeded=False,
        target_reference=None,
    )
    second_candidate = _candidate(tmp_path, candidate_id="lc-pg-reconcile-2")
    store.create_candidate(second_candidate)
    second_qualified = store.record_evaluation(
        _evaluation(second_candidate.candidate_id, suffix="2"),
        owner=owner,
        expected_revision=0,
        mutation_id="evaluate:reconcile:2",
    )
    second_promoting = store.begin_promotion(
        second_candidate.candidate_id,
        owner=owner,
        expected_revision=second_qualified.revision,
        mutation_id="promote:unknown:2:begin",
        promotion_id="promotion-unknown-2",
        actor=PromotionActor.HOST,
    )
    second_unknown = store.settle_promotion(
        second_candidate.candidate_id,
        owner=owner,
        expected_revision=second_promoting.revision,
        mutation_id="promote:unknown:2:settle",
        succeeded=False,
        target_reference=None,
    )
    first_page = store.scan_reconciliation_required(owner=owner, limit=1)
    assert len(first_page.items) == 1
    assert first_page.items[0].kind is ReconciliationKind.PROMOTION
    assert first_page.next_cursor is not None
    second_page = store.scan_reconciliation_required(
        owner=owner,
        limit=1,
        cursor=first_page.next_cursor,
    )
    assert second_page.next_cursor is None
    assert {
        first_page.items[0].record.candidate.candidate_id,
        second_page.items[0].record.candidate.candidate_id,
    } == {candidate.candidate_id, second_candidate.candidate_id}
    with pytest.raises(ReconciliationCursorScopeError):
        store.scan_reconciliation_required(
            owner=LearningOwner("other", owner.storage_namespace),
            cursor=first_page.next_cursor,
        )
    reconciliation = CandidateReconciliation(
        reconciliation_id="reconcile-pg-1",
        candidate_id=candidate.candidate_id,
        kind=ReconciliationKind.PROMOTION,
        verdict=ReconciliationVerdict.EFFECT_ABSENT,
        reconciler_digest="sha256:" + "b" * 64,
        evidence_artifact_ids=("artifact-pg-inspection",),
        reconciled_by=PromotionActor.OPERATOR,
    )
    resolved = store.record_reconciliation(
        reconciliation,
        owner=owner,
        expected_revision=unknown.revision,
        mutation_id="reconcile:pg:1",
    )
    assert resolved.state is CandidateState.QUALIFIED
    assert (
        store.get_reconciliation(
            reconciliation.reconciliation_id,
            owner=owner,
        )
        == reconciliation
    )
    assert store.list_reconciliations(candidate.candidate_id, owner=owner) == [reconciliation]
    assert [item.record for item in store.scan_reconciliation_required(owner=owner).items] == [
        second_unknown
    ]
    assert store.list_events(candidate.candidate_id, owner=owner)[-1].event_type == (
        "learning.reconciliation.completed"
    )


def test_postgres_reconciliation_worker_lease_is_exclusive_fenced_and_durable(store) -> None:
    owner = LearningOwner("tenant-pg-worker", "namespace-pg-worker")
    first = store.claim_reconciliation_worker(
        owner=owner,
        worker_id="pg-worker-a",
        lease_seconds=30,
    )
    assert first is not None
    assert (
        store.claim_reconciliation_worker(
            owner=owner,
            worker_id="pg-worker-b",
            lease_seconds=30,
        )
        is None
    )
    durable_cursor = ReconciliationCursor(
        updated_at="2026-08-14T00:00:00+00:00",
        candidate_id="lc-pg-cursor",
        owner_digest=owner.digest,
    )
    checkpoint = store.checkpoint_reconciliation_worker(
        first,
        cursor=durable_cursor,
        lease_seconds=30,
    )
    assert store.release_reconciliation_worker(checkpoint) is True
    with pytest.raises(LearningReconciliationWorkerLeaseError):
        store.checkpoint_reconciliation_worker(
            first,
            cursor=None,
            lease_seconds=30,
        )
    takeover = store.claim_reconciliation_worker(
        owner=owner,
        worker_id="pg-worker-b",
        lease_seconds=30,
    )
    assert takeover is not None
    assert takeover.fence == first.fence + 1
    assert takeover.cursor == durable_cursor
    assert store.release_reconciliation_worker(first) is False
    assert store.release_reconciliation_worker(takeover) is True

    expiring = store.claim_reconciliation_worker(
        owner=owner,
        worker_id="pg-worker-expiring",
        lease_seconds=1,
    )
    assert expiring is not None
    time.sleep(1.05)
    reclaimed = store.claim_reconciliation_worker(
        owner=owner,
        worker_id="pg-worker-reclaimed",
        lease_seconds=30,
    )
    assert reclaimed is not None
    assert reclaimed.fence == expiring.fence + 1
    with pytest.raises(LearningReconciliationWorkerLeaseError):
        store.checkpoint_reconciliation_worker(
            expiring,
            cursor=None,
            lease_seconds=30,
        )
    assert store.release_reconciliation_worker(reclaimed) is True


@pytest.mark.asyncio
async def test_postgres_independent_pools_cannot_steal_slow_worker_lease(
    store,
    tmp_path,
) -> None:
    assert POSTGRES_URL is not None
    candidate = await asyncio.to_thread(
        _candidate,
        tmp_path,
        candidate_id="lc-pg-slow-worker",
    )
    owner = candidate.owner
    store.create_candidate(candidate)
    qualified = store.record_evaluation(
        _evaluation(candidate.candidate_id, suffix="slow-worker"),
        owner=owner,
        expected_revision=0,
        mutation_id="evaluate:slow-worker",
    )
    promoting = store.begin_promotion(
        candidate.candidate_id,
        owner=owner,
        expected_revision=qualified.revision,
        mutation_id="promote:slow-worker:begin",
        promotion_id="promotion-slow-worker",
        actor=PromotionActor.HOST,
    )
    store.settle_promotion(
        candidate.candidate_id,
        owner=owner,
        expected_revision=promoting.revision,
        mutation_id="promote:slow-worker:unknown",
        succeeded=False,
        target_reference=None,
    )
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    gateway = LearningGateway(store, artifacts)

    class SlowAbsentObserver:
        async def observe(self, request, content):
            await asyncio.sleep(1.25)
            assert content["learning"] == "Use bounded retries."
            evidence = await artifacts.stage_json(
                {"effect_present": False},
                scope=request.record.candidate.content_artifact.scope,
                purpose=RECONCILIATION_EVIDENCE_PURPOSE,
            )
            return ReconciliationObservation(
                candidate_id=request.record.candidate.candidate_id,
                kind=request.kind,
                expected_revision=request.record.revision,
                candidate_digest=request.record.candidate.digest,
                verdict=ReconciliationVerdict.EFFECT_ABSENT,
                evidence_artifacts=(evidence,),
            )

    worker = LearningReconciliationWorker(
        LearningReconciliationCoordinator(
            gateway,
            SlowAbsentObserver(),
            reconciler_digest="sha256:" + "9" * 64,
        ),
        owner=owner,
        config=LearningReconciliationWorkerConfig(
            worker_id="pg-slow-worker-a",
            lease_seconds=1,
            heartbeat_interval_seconds=0.2,
            poll_interval_seconds=0.01,
        ),
    )
    competitor = PostgresLearningLedger(
        POSTGRES_URL,
        min_pool_size=1,
        max_pool_size=2,
        max_waiting=4,
    )
    try:
        task = asyncio.create_task(worker.run_once())
        await asyncio.sleep(1.05)
        stolen = await asyncio.to_thread(
            competitor.claim_reconciliation_worker,
            owner=owner,
            worker_id="pg-slow-worker-b",
            lease_seconds=30,
        )
        assert stolen is None
        stats = await task
        assert (stats.claims, stats.reconciled, stats.lease_losses) == (1, 1, 0)
        assert store.scan_reconciliation_required(owner=owner).items == ()
    finally:
        competitor.close()


def test_postgres_worker_lease_recovers_after_abrupt_process_death(store) -> None:
    assert POSTGRES_URL is not None
    owner = LearningOwner("tenant-pg-abrupt", "namespace-pg-abrupt")
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    process = context.Process(
        target=_claim_learning_worker_lease_and_block,
        args=(POSTGRES_URL, owner.tenant_id, owner.storage_namespace, ready),
    )
    process.start()
    try:
        claimed = ready.get(timeout=10)
        assert claimed is not None
        assert (
            store.claim_reconciliation_worker(
                owner=owner,
                worker_id="pg-abrupt-parent-before-expiry",
                lease_seconds=30,
            )
            is None
        )

        process.terminate()
        process.join(timeout=10)
        assert process.exitcode is not None
        time.sleep(1.05)

        recovered = store.claim_reconciliation_worker(
            owner=owner,
            worker_id="pg-abrupt-parent-recovery",
            lease_seconds=30,
        )
        assert recovered is not None
        assert recovered.fence == int(claimed["fence"]) + 1
        assert recovered.cursor is None
        assert store.release_reconciliation_worker(recovered) is True
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        ready.close()
        ready.join_thread()
