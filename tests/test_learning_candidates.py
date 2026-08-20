"""Governed learning candidate, ledger, and promotion contracts."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agnoclaw import (
    AgnoLearnedKnowledgeReconciliationObserver,
    AgnoLearningPromotionAdapter,
    CandidateAction,
    CandidateAuthor,
    CandidateEvaluation,
    CandidateReconciliation,
    CandidateRisk,
    CandidateState,
    EvaluationArchiveCursor,
    EvaluationArchiveQuery,
    EvaluationVerdict,
    HarnessError,
    LearningApplication,
    LearningApplicationKind,
    LearningAttributionConflictError,
    LearningEffectivenessPolicy,
    LearningEffectivenessRecommendation,
    LearningGateway,
    LearningOutboxLeaseError,
    LearningOutcome,
    LearningOutcomeKind,
    LearningOwner,
    LearningProfile,
    LearningPromotionUnknownError,
    LearningReconciliationCoordinator,
    LearningRollbackUnknownError,
    LearningScope,
    LearningTarget,
    LocalArtifactStore,
    PromotionActor,
    ReconciliationBatchOutcome,
    ReconciliationCursor,
    ReconciliationCursorScopeError,
    ReconciliationItemStatus,
    ReconciliationKind,
    ReconciliationObservation,
    ReconciliationVerdict,
    SQLiteLearningLedger,
)
from agnoclaw.learning_candidates import CandidateNotFoundError
from agnoclaw.learning_reconciliation import RECONCILIATION_EVIDENCE_PURPOSE
from agnoclaw.runtime import ExecutionContext


def _policy_and_scope():
    policy = LearningProfile.institutional(
        namespace="research",
        knowledge=SimpleNamespace(vector_db=object()),
    )
    context = ExecutionContext.create(
        tenant_id="acme",
        user_id="analyst-1",
        session_id="session-1",
        workspace_id="workspace-1",
    )
    return policy, LearningScope.resolve(policy, context, agent_id="researcher")


async def _captured(tmp_path, *, candidate_id: str = "lc_test"):
    policy, scope = _policy_and_scope()
    ledger = SQLiteLearningLedger(tmp_path / "learning.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    gateway = LearningGateway(ledger, artifacts)
    record = await gateway.capture(
        policy=policy,
        scope=scope,
        target=LearningTarget.LEARNED_KNOWLEDGE,
        content={"title": "Retry rule", "learning": "Retry only idempotent reads."},
        source_run_ids=("run-1",),
        evidence_artifact_ids=("artifact-evidence-1",),
        confidence=0.85,
        risk=CandidateRisk.MEDIUM,
        created_by=CandidateAuthor.AGENT,
        mechanism_version="extractor:v1",
        candidate_id=candidate_id,
    )
    return policy, scope, ledger, artifacts, gateway, record


@pytest.mark.asyncio
async def test_capture_keeps_content_in_artifact_and_authorizes_exact_scope(
    tmp_path,
) -> None:
    policy, scope, ledger, _, gateway, record = await _captured(tmp_path)

    assert record.state is CandidateState.CAPTURED
    assert await gateway.read_content(
        record.candidate.candidate_id, owner=record.candidate.owner
    ) == {
        "learning": "Retry only idempotent reads.",
        "title": "Retry rule",
    }
    database_bytes = (tmp_path / "learning.db").read_bytes()
    assert b"Retry only idempotent reads" not in database_bytes
    assert ledger.list_artifact_storage_keys() == [record.candidate.content_artifact.storage_key]
    events = ledger.list_events(
        record.candidate.candidate_id,
        owner=record.candidate.owner,
    )
    assert [(event.sequence, event.event_type) for event in events] == [
        (1, "learning.candidate.captured")
    ]
    leased = ledger.lease_outbox(owner="test-exporter")
    assert [item.event for item in leased] == events
    with pytest.raises(LearningOutboxLeaseError):
        ledger.acknowledge_outbox(
            outbox_id=leased[0].outbox_id,
            lease_token="wrong-token",
        )
    ledger.acknowledge_outbox(
        outbox_id=leased[0].outbox_id,
        lease_token=leased[0].lease_token,
    )
    assert ledger.lease_outbox(owner="test-exporter") == []

    wrong_owner = LearningOwner("other", scope.storage_namespace)
    with pytest.raises(CandidateNotFoundError):
        await gateway.get(record.candidate.candidate_id, owner=wrong_owner)
    ledger.close()


@pytest.mark.asyncio
async def test_capture_is_idempotent_and_conflicting_content_fails(tmp_path) -> None:
    policy, scope, ledger, _, gateway, first = await _captured(tmp_path)
    same = await gateway.capture(
        policy=policy,
        scope=scope,
        target=LearningTarget.LEARNED_KNOWLEDGE,
        content={"title": "Retry rule", "learning": "Retry only idempotent reads."},
        source_run_ids=("run-1",),
        evidence_artifact_ids=("artifact-evidence-1",),
        confidence=0.85,
        risk=CandidateRisk.MEDIUM,
        created_by=CandidateAuthor.AGENT,
        mechanism_version="extractor:v1",
        candidate_id="lc_test",
    )
    assert same == first

    with pytest.raises(HarnessError) as exc:
        await gateway.capture(
            policy=policy,
            scope=scope,
            target=LearningTarget.LEARNED_KNOWLEDGE,
            content={"title": "Different", "learning": "Different"},
            source_run_ids=("run-1",),
            evidence_artifact_ids=("artifact-evidence-1",),
            confidence=0.85,
            risk=CandidateRisk.MEDIUM,
            created_by=CandidateAuthor.AGENT,
            mechanism_version="extractor:v1",
            candidate_id="lc_test",
        )
    assert exc.value.code == "LEARNING_CANDIDATE_CONFLICT"
    ledger.close()


def test_qualified_evaluation_requires_evidence_and_safety() -> None:
    base = {
        "evaluation_id": "eval-1",
        "candidate_id": "lc-1",
        "verdict": EvaluationVerdict.QUALIFIED,
        "evaluator_digest": "sha256:" + "a" * 64,
        "evaluated_by": PromotionActor.HOST,
    }
    with pytest.raises(HarnessError) as evidence_error:
        CandidateEvaluation(
            **base,
            evidence_artifact_ids=(),
            safety_passed=True,
        )
    assert evidence_error.value.code == "LEARNING_EVALUATION_EVIDENCE_REQUIRED"

    with pytest.raises(HarnessError) as safety_error:
        CandidateEvaluation(
            **base,
            evidence_artifact_ids=("artifact-eval",),
            safety_passed=False,
        )
    assert safety_error.value.code == "LEARNING_EVALUATION_SAFETY_FAILED"


async def _qualify(gateway, record):
    evaluation = CandidateEvaluation(
        evaluation_id="eval-qualified",
        candidate_id=record.candidate.candidate_id,
        verdict=EvaluationVerdict.QUALIFIED,
        evaluator_digest="sha256:" + "b" * 64,
        evidence_artifact_ids=("artifact-held-out", "artifact-safety"),
        safety_passed=True,
        evaluated_by=PromotionActor.OPERATOR,
        metrics={"held_in": 0.9, "held_out": 0.88, "transfer": 0.81},
        control_metrics={"held_in": 0.7, "held_out": 0.71, "transfer": 0.72},
    )
    return await gateway.evaluate(
        evaluation,
        owner=record.candidate.owner,
        mutation_id="evaluate-1",
    )


def _promote_direct(ledger, record, *, target_reference: str = "agno:learning:test"):
    started = ledger.begin_promotion(
        record.candidate.candidate_id,
        owner=record.candidate.owner,
        expected_revision=record.revision,
        mutation_id=f"promote:start:{record.candidate.candidate_id}",
        promotion_id=f"promotion:{record.candidate.candidate_id}",
        actor=PromotionActor.OPERATOR,
    )
    return ledger.settle_promotion(
        record.candidate.candidate_id,
        owner=record.candidate.owner,
        expected_revision=started.revision,
        mutation_id=f"promote:settle:{record.candidate.candidate_id}",
        succeeded=True,
        target_reference=target_reference,
    )


@pytest.mark.asyncio
async def test_evaluation_is_cas_and_idempotent(tmp_path) -> None:
    _, _, ledger, _, gateway, captured = await _captured(tmp_path)
    qualified = await _qualify(gateway, captured)
    assert qualified.state is CandidateState.QUALIFIED
    assert qualified.revision == 1

    replay = await _qualify(gateway, captured)
    assert replay == qualified
    evaluations = await gateway.list_evaluations(
        captured.candidate.candidate_id,
        owner=captured.candidate.owner,
    )
    assert [item.evaluation_id for item in evaluations] == ["eval-qualified"]
    assert (
        await gateway.get_evaluation(
            "eval-qualified",
            owner=captured.candidate.owner,
        )
        == evaluations[0]
    )
    exported = await gateway.export(
        captured.candidate.candidate_id,
        owner=captured.candidate.owner,
    )
    assert exported.record == qualified
    assert exported.content["learning"] == "Retry only idempotent reads."
    assert exported.evaluations == tuple(evaluations)
    events = ledger.list_events(
        captured.candidate.candidate_id,
        owner=captured.candidate.owner,
    )
    assert [item.event_type for item in events] == [
        "learning.candidate.captured",
        "learning.candidate.evaluated",
    ]
    ledger.close()


@pytest.mark.asyncio
async def test_learning_outcomes_close_the_promoted_knowledge_feedback_loop(tmp_path) -> None:
    _, _, ledger, _, gateway, captured = await _captured(tmp_path)
    qualified = await _qualify(gateway, captured)
    promoted = _promote_direct(ledger, qualified)

    for index in range(5):
        application = LearningApplication(
            application_id=f"application-{index}",
            candidate_id=promoted.candidate.candidate_id,
            run_id=f"run-applied-{index}",
            target_reference=promoted.target_reference or "",
            kind=LearningApplicationKind.APPLIED,
            observer_digest="sha256:" + "c" * 64,
            evidence_artifact_ids=(f"artifact-application-{index}",),
        )
        assert (
            await gateway.record_application(application, owner=promoted.candidate.owner)
            == application
        )
        outcome = LearningOutcome(
            outcome_id=f"outcome-{index}",
            application_id=application.application_id,
            candidate_id=application.candidate_id,
            run_id=application.run_id,
            kind=LearningOutcomeKind.CORRECTION if index == 0 else LearningOutcomeKind.FAILURE,
            score=-0.8,
            evaluator_digest="sha256:" + "d" * 64,
            evidence_artifact_ids=(f"artifact-outcome-{index}",),
            evaluated_by=PromotionActor.HOST,
        )
        assert await gateway.record_outcome(outcome, owner=promoted.candidate.owner) == outcome

    summary = await gateway.summarize_effectiveness(
        promoted.candidate.candidate_id,
        owner=promoted.candidate.owner,
    )
    assert summary.to_dict() == {
        "candidate_id": promoted.candidate.candidate_id,
        "total_applications": 5,
        "applied_applications": 5,
        "evaluated_outcomes": 5,
        "independent_runs": 5,
        "successes": 0,
        "failures": 4,
        "corrections": 1,
        "neutral": 0,
        "mean_score": -0.8,
        "recommendation": "quarantine",
    }
    assert summary.recommendation is LearningEffectivenessRecommendation.QUARANTINE
    assert (
        len(
            await gateway.list_applications(
                promoted.candidate.candidate_id,
                owner=promoted.candidate.owner,
            )
        )
        == 5
    )
    assert (
        len(
            await gateway.list_outcomes(
                promoted.candidate.candidate_id,
                owner=promoted.candidate.owner,
            )
        )
        == 5
    )
    # High-volume feedback remains append-only and does not contend on promotion CAS.
    assert (
        await gateway.get(promoted.candidate.candidate_id, owner=promoted.candidate.owner)
    ) == promoted
    ledger.close()


@pytest.mark.asyncio
async def test_learning_outcome_attribution_is_exact_scoped_and_fail_closed(tmp_path) -> None:
    _, _, ledger, _, gateway, captured = await _captured(tmp_path)
    base = {
        "application_id": "application-exact",
        "candidate_id": captured.candidate.candidate_id,
        "run_id": "run-exact",
        "target_reference": "agno:learning:exact",
        "kind": LearningApplicationKind.APPLIED,
        "observer_digest": "sha256:" + "e" * 64,
        "evidence_artifact_ids": ("artifact-application-exact",),
    }
    with pytest.raises(HarnessError) as unpromoted:
        await gateway.record_application(
            LearningApplication(**base),
            owner=captured.candidate.owner,
        )
    assert unpromoted.value.code == "LEARNING_CANDIDATE_TRANSITION_INVALID"

    qualified = await _qualify(gateway, captured)
    promoted = _promote_direct(ledger, qualified, target_reference="agno:learning:exact")
    application = LearningApplication(**base)
    await gateway.record_application(application, owner=promoted.candidate.owner)
    assert (
        await gateway.record_application(application, owner=promoted.candidate.owner) == application
    )
    with pytest.raises(LearningAttributionConflictError):
        await gateway.record_application(
            LearningApplication(**{**base, "run_id": "run-conflict"}),
            owner=promoted.candidate.owner,
        )
    with pytest.raises(LearningAttributionConflictError):
        await gateway.record_application(
            LearningApplication(**{**base, "application_id": "application-duplicate-weight"}),
            owner=promoted.candidate.owner,
        )
    with pytest.raises(HarnessError) as wrong_owner:
        await gateway.get_application(
            application.application_id,
            owner=LearningOwner("other", promoted.candidate.storage_namespace),
        )
    assert wrong_owner.value.code == "LEARNING_APPLICATION_NOT_FOUND"

    retrieved = LearningApplication(
        **{
            **base,
            "application_id": "application-retrieved",
            "kind": LearningApplicationKind.RETRIEVED,
        }
    )
    await gateway.record_application(retrieved, owner=promoted.candidate.owner)
    invalid_outcome = LearningOutcome(
        outcome_id="outcome-retrieved",
        application_id=retrieved.application_id,
        candidate_id=retrieved.candidate_id,
        run_id=retrieved.run_id,
        kind=LearningOutcomeKind.SUCCESS,
        score=0.5,
        evaluator_digest="sha256:" + "f" * 64,
        evidence_artifact_ids=("artifact-outcome-retrieved",),
        evaluated_by=PromotionActor.HOST,
    )
    with pytest.raises(HarnessError) as not_applied:
        await gateway.record_outcome(invalid_outcome, owner=promoted.candidate.owner)
    assert not_applied.value.code == "LEARNING_OUTCOME_NOT_APPLIED"

    dependent_outcome = LearningOutcome(
        **{
            **invalid_outcome.to_dict(),
            "outcome_id": "outcome-dependent",
            "application_id": application.application_id,
            "kind": LearningOutcomeKind.SUCCESS,
            "evaluator_digest": application.observer_digest,
        }
    )
    with pytest.raises(HarnessError) as not_independent:
        await gateway.record_outcome(dependent_outcome, owner=promoted.candidate.owner)
    assert not_independent.value.code == "LEARNING_OUTCOME_INDEPENDENCE_REQUIRED"

    with pytest.raises(ValueError, match="kind and score sign"):
        LearningOutcome(
            **{
                **invalid_outcome.to_dict(),
                "outcome_id": "outcome-invalid-sign",
                "kind": LearningOutcomeKind.FAILURE,
            }
        )
    assert (
        await gateway.summarize_effectiveness(
            promoted.candidate.candidate_id,
            owner=promoted.candidate.owner,
            policy=LearningEffectivenessPolicy(minimum_outcomes=1),
        )
    ).recommendation is LearningEffectivenessRecommendation.INSUFFICIENT_EVIDENCE

    late_application = LearningApplication(
        **{
            **base,
            "application_id": "application-late",
            "run_id": "run-late",
        }
    )
    await gateway.record_application(late_application, owner=promoted.candidate.owner)
    rolling_back = ledger.begin_rollback(
        promoted.candidate.candidate_id,
        owner=promoted.candidate.owner,
        expected_revision=promoted.revision,
        mutation_id="feedback:rollback:begin",
        rollback_id="feedback-rollback",
        actor=PromotionActor.OPERATOR,
    )
    rolled_back = ledger.settle_rollback(
        promoted.candidate.candidate_id,
        owner=promoted.candidate.owner,
        expected_revision=rolling_back.revision,
        mutation_id="feedback:rollback:settle",
        succeeded=True,
    )
    ledger.transition_candidate(
        promoted.candidate.candidate_id,
        owner=promoted.candidate.owner,
        expected_revision=rolled_back.revision,
        mutation_id="feedback:delete",
        action=CandidateAction.DELETE,
    )
    late_outcome = LearningOutcome(
        **{
            **invalid_outcome.to_dict(),
            "outcome_id": "outcome-late",
            "application_id": late_application.application_id,
            "run_id": late_application.run_id,
        }
    )
    with pytest.raises(HarnessError) as deleted:
        await gateway.record_outcome(late_outcome, owner=promoted.candidate.owner)
    assert deleted.value.code == "LEARNING_CANDIDATE_TRANSITION_INVALID"
    ledger.close()


@pytest.mark.asyncio
async def test_evaluation_archive_is_owner_scoped_keyset_filtered_and_content_free(
    tmp_path,
) -> None:
    policy, scope, ledger, _, gateway, first = await _captured(
        tmp_path,
        candidate_id="lc-archive-rejected",
    )
    index_columns = [
        (str(row["name"]), bool(row["desc"]))
        for row in ledger._connection.execute(  # noqa: SLF001 - schema contract
            "PRAGMA index_xinfo(learning_evaluations_candidate_idx)"
        ).fetchall()
        if row["key"]
    ]
    assert index_columns == [
        ("candidate_id", False),
        ("created_at", True),
        ("evaluation_id", True),
    ]
    plan = ledger._connection.execute(  # noqa: SLF001 - release query-plan evidence
        """
        EXPLAIN QUERY PLAN
        SELECT e.evaluation_id
        FROM learning_evaluations AS e
        JOIN learning_candidates AS c ON c.candidate_id = e.candidate_id
        WHERE e.tenant_id IS ? AND e.storage_namespace = ?
          AND e.verdict IN (?, ?)
        ORDER BY e.created_at DESC, e.evaluation_id DESC
        LIMIT ?
        """,
        (
            first.candidate.tenant_id,
            first.candidate.storage_namespace,
            EvaluationVerdict.REJECTED.value,
            EvaluationVerdict.INCONCLUSIVE.value,
            100,
        ),
    ).fetchall()
    plan_details = "\n".join(str(row["detail"]) for row in plan)
    assert "learning_evaluations_archive_owner_idx" in plan_details
    assert "USE TEMP B-TREE" not in plan_details

    async def capture(candidate_id: str, mechanism: str):
        return await gateway.capture(
            policy=policy,
            scope=scope,
            target=LearningTarget.LEARNED_KNOWLEDGE,
            content={"title": candidate_id, "learning": f"learning-{candidate_id}"},
            source_run_ids=(f"run-{candidate_id}",),
            evidence_artifact_ids=(f"artifact-{candidate_id}",),
            confidence=0.8,
            risk=CandidateRisk.LOW,
            created_by=CandidateAuthor.AGENT,
            mechanism_version=mechanism,
            candidate_id=candidate_id,
        )

    second = await capture("lc-archive-inconclusive", "reflector:v2")
    third = await capture("lc-archive-qualified", "reflector:v3")

    def evaluation(
        record,
        *,
        evaluation_id: str,
        verdict: EvaluationVerdict,
        reason: str,
        evaluator: str,
        evaluated_at: str,
        safety_passed: bool = True,
    ) -> CandidateEvaluation:
        return CandidateEvaluation(
            evaluation_id=evaluation_id,
            candidate_id=record.candidate.candidate_id,
            verdict=verdict,
            evaluator_digest="sha256:" + evaluator * 64,
            evidence_artifact_ids=(f"artifact-{evaluation_id}-secret",),
            safety_passed=safety_passed,
            evaluated_by=PromotionActor.OPERATOR,
            metrics={
                "private_metric": "must-not-enter-archive",
                "gate": {
                    "reasons": [reason] if reason else [],
                    "policy_digest": "sha256:" + "1" * 64,
                    "hypothesis_digest": "sha256:" + "2" * 64,
                    "evaluation_digest": "sha256:" + "3" * 64,
                    "runner_digest": "sha256:" + "4" * 64,
                    "corpus_manifest_digest": "sha256:" + "5" * 64,
                },
            },
            control_metrics={"private_control": "must-not-enter-archive"},
            notes="private operator notes",
            evaluated_at=evaluated_at,
        )

    for record, item in (
        (
            first,
            evaluation(
                first,
                evaluation_id="eval-archive-1",
                verdict=EvaluationVerdict.REJECTED,
                reason="safety_gate_failed",
                evaluator="a",
                evaluated_at="2026-08-14T00:00:01+00:00",
                safety_passed=False,
            ),
        ),
        (
            second,
            evaluation(
                second,
                evaluation_id="eval-archive-2",
                verdict=EvaluationVerdict.INCONCLUSIVE,
                reason="judge_order_unbalanced",
                evaluator="b",
                evaluated_at="2026-08-14T00:00:02+00:00",
            ),
        ),
        (
            third,
            evaluation(
                third,
                evaluation_id="eval-archive-3",
                verdict=EvaluationVerdict.QUALIFIED,
                reason="",
                evaluator="a",
                evaluated_at="2026-08-14T00:00:03+00:00",
            ),
        ),
    ):
        await gateway.evaluate(
            item,
            owner=record.candidate.owner,
            mutation_id=f"record:{item.evaluation_id}",
        )

    first_page = await gateway.query_evaluation_archive(
        owner=first.candidate.owner,
        query=EvaluationArchiveQuery(limit=1),
    )
    assert [item.evaluation_id for item in first_page.items] == ["eval-archive-2"]
    assert first_page.next_cursor is not None
    second_page = await gateway.query_evaluation_archive(
        owner=first.candidate.owner,
        query=EvaluationArchiveQuery(limit=1, cursor=first_page.next_cursor),
    )
    assert [item.evaluation_id for item in second_page.items] == ["eval-archive-1"]
    assert second_page.next_cursor is None

    safety = await gateway.query_evaluation_archive(
        owner=first.candidate.owner,
        query=EvaluationArchiveQuery(reason_code="safety_gate_failed"),
    )
    assert [item.evaluation_id for item in safety.items] == ["eval-archive-1"]
    assert safety.items[0].failure_reason_codes == ("safety_gate_failed",)
    assert safety.items[0].mechanism_version == "extractor:v1"
    assert safety.items[0].corpus_manifest_digest == "sha256:" + "5" * 64
    assert "private" not in str(safety.to_dict())
    assert "artifact-eval-archive-1-secret" not in str(safety.to_dict())

    qualified = await gateway.query_evaluation_archive(
        owner=first.candidate.owner,
        query=EvaluationArchiveQuery(
            verdicts=(EvaluationVerdict.QUALIFIED,),
            evaluator_digest="sha256:" + "a" * 64,
            mechanism_version="reflector:v3",
            target=LearningTarget.LEARNED_KNOWLEDGE,
            safety_passed=True,
        ),
    )
    assert [item.evaluation_id for item in qualified.items] == ["eval-archive-3"]

    assert (
        await gateway.query_evaluation_archive(
            owner=LearningOwner("other", first.candidate.owner.storage_namespace),
        )
    ).items == ()
    with pytest.raises(HarnessError) as cursor_scope:
        await gateway.query_evaluation_archive(
            owner=first.candidate.owner,
            query=EvaluationArchiveQuery(
                cursor=EvaluationArchiveCursor(
                    evaluated_at="2026-08-14T00:00:02+00:00",
                    evaluation_id="eval-archive-2",
                    owner_digest=LearningOwner(
                        "other",
                        first.candidate.owner.storage_namespace,
                    ).digest,
                )
            ),
        )
    assert cursor_scope.value.code == "LEARNING_EVALUATION_ARCHIVE_CURSOR_SCOPE"
    ledger.close()


@pytest.mark.asyncio
async def test_sqlite_v4_evaluations_backfill_through_v6_schema(tmp_path) -> None:
    _, _, ledger, _, gateway, record = await _captured(
        tmp_path,
        candidate_id="lc-archive-migration",
    )
    evaluation = CandidateEvaluation(
        evaluation_id="eval-archive-migration",
        candidate_id=record.candidate.candidate_id,
        verdict=EvaluationVerdict.REJECTED,
        evaluator_digest="sha256:" + "a" * 64,
        evidence_artifact_ids=("artifact-archive-migration",),
        safety_passed=False,
        evaluated_by=PromotionActor.OPERATOR,
        metrics={"gate": {"reasons": ["safety_gate_failed"]}},
        evaluated_at="2026-08-14T00:00:00+00:00",
    )
    await gateway.evaluate(
        evaluation,
        owner=record.candidate.owner,
        mutation_id="archive:migrate:v4",
    )
    ledger.close()

    database = tmp_path / "learning.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TABLE learning_evaluation_reasons")
        connection.execute("DROP INDEX learning_evaluations_archive_owner_idx")
        connection.execute("DROP INDEX learning_evaluations_archive_filter_idx")
        connection.execute("ALTER TABLE learning_evaluations RENAME TO learning_evaluations_v4")
        connection.execute(
            """
            CREATE TABLE learning_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                evaluation_digest TEXT NOT NULL,
                evaluation_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(candidate_id) REFERENCES learning_candidates(candidate_id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            INSERT INTO learning_evaluations(
                evaluation_id, candidate_id, evaluation_digest,
                evaluation_json, created_at
            )
            SELECT evaluation_id, candidate_id, evaluation_digest,
                   evaluation_json, created_at
            FROM learning_evaluations_v4
            """
        )
        connection.execute("DROP TABLE learning_evaluations_v4")
        connection.execute("DELETE FROM learning_schema_migrations WHERE version >= 5")
        connection.commit()
    finally:
        connection.close()

    migrated = SQLiteLearningLedger(database)
    assert migrated.schema_version == 6
    page = migrated.query_evaluation_archive(
        owner=record.candidate.owner,
        query=EvaluationArchiveQuery(reason_code="safety_gate_failed"),
    )
    assert [item.evaluation_id for item in page.items] == ["eval-archive-migration"]
    projection = migrated._connection.execute(  # noqa: SLF001 - migration evidence
        """
        SELECT storage_namespace, target, mechanism_version, verdict,
               evaluator_digest, safety_passed, reason_codes_json
        FROM learning_evaluations WHERE evaluation_id = ?
        """,
        ("eval-archive-migration",),
    ).fetchone()
    assert dict(projection) == {
        "storage_namespace": record.candidate.storage_namespace,
        "target": record.candidate.target.value,
        "mechanism_version": record.candidate.mechanism_version,
        "verdict": EvaluationVerdict.REJECTED.value,
        "evaluator_digest": evaluation.evaluator_digest,
        "safety_passed": 0,
        "reason_codes_json": '["safety_gate_failed"]',
    }
    migrated.close()


@pytest.mark.parametrize(
    ("kwargs", "error_type"),
    [
        ({"verdicts": ()}, ValueError),
        (
            {
                "verdicts": (
                    EvaluationVerdict.REJECTED,
                    EvaluationVerdict.REJECTED,
                )
            },
            ValueError,
        ),
        ({"evaluator_digest": "sha256:not-a-digest"}, ValueError),
        ({"reason_code": "Not Stable"}, ValueError),
        ({"mechanism_version": ""}, ValueError),
        ({"safety_passed": "yes"}, TypeError),
        ({"limit": True}, TypeError),
        ({"limit": 1001}, ValueError),
        ({"cursor": "opaque"}, TypeError),
    ],
)
def test_evaluation_archive_query_rejects_ambiguous_or_unbounded_filters(
    kwargs,
    error_type,
) -> None:
    with pytest.raises(error_type):
        EvaluationArchiveQuery(**kwargs)


def test_evaluation_archive_cursor_requires_timezone() -> None:
    cursor = EvaluationArchiveCursor(
        evaluated_at="2026-08-14T00:00:00+00:00",
        evaluation_id="eval-archive",
        owner_digest="sha256:" + "a" * 64,
    )
    assert EvaluationArchiveCursor.from_dict(cursor.to_dict()) == cursor

    with pytest.raises(ValueError, match="timezone"):
        EvaluationArchiveCursor(
            evaluated_at="2026-08-14T00:00:00",
            evaluation_id="eval-archive",
            owner_digest="sha256:" + "a" * 64,
        )
    with pytest.raises(TypeError, match="mapping"):
        EvaluationArchiveCursor.from_dict("opaque")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_evaluation_archive_is_optional_for_custom_ledgers(tmp_path) -> None:
    gateway = LearningGateway(
        SimpleNamespace(),  # type: ignore[arg-type]
        LocalArtifactStore(tmp_path / "artifacts"),
    )
    with pytest.raises(HarnessError) as unsupported:
        await gateway.query_evaluation_archive(
            owner=LearningOwner("tenant", "namespace"),
        )
    assert unsupported.value.code == "LEARNING_EVALUATION_ARCHIVE_UNSUPPORTED"


class _RecordingAdapter:
    def __init__(self, ledger, owner, *, fail: bool = False):
        self.ledger = ledger
        self.owner = owner
        self.fail = fail
        self.calls = 0
        self.rollback_calls = 0

    async def apply(self, candidate, content, *, idempotency_key):
        self.calls += 1
        current = self.ledger.get_candidate(candidate.candidate_id, owner=self.owner)
        assert current.state is CandidateState.PROMOTING
        assert current.promotion_id == idempotency_key
        if self.fail:
            raise TimeoutError("backend response lost")
        assert content["learning"] == "Retry only idempotent reads."
        return f"agno:learned_knowledge:{candidate.candidate_id}"

    async def rollback(
        self,
        candidate,
        content,
        *,
        target_reference,
        idempotency_key,
    ):
        self.rollback_calls += 1
        current = self.ledger.get_candidate(candidate.candidate_id, owner=self.owner)
        assert current.state is CandidateState.ROLLING_BACK
        assert current.rollback_id == idempotency_key
        assert target_reference.endswith(candidate.candidate_id)
        assert content["learning"] == "Retry only idempotent reads."


class _Observer:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def observe(self, request, content):
        self.calls += 1
        assert content["learning"] == "Retry only idempotent reads."
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _BarrierObserver(_Observer):
    def __init__(self, result):
        super().__init__(result)
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()

    async def observe(self, request, content):
        async with self._lock:
            self.calls += 1
            if self.calls == 2:
                self._ready.set()
        await self._ready.wait()
        assert content["learning"] == "Retry only idempotent reads."
        return self.result


async def _promotion_unknown(tmp_path, *, candidate_id="lc-observer"):
    policy, _, ledger, artifacts, _, captured = await _captured(
        tmp_path,
        candidate_id=candidate_id,
    )
    adapter = _RecordingAdapter(ledger, captured.candidate.owner, fail=True)
    gateway = LearningGateway(ledger, artifacts, promotion_adapter=adapter)
    await _qualify(gateway, captured)
    with pytest.raises(LearningPromotionUnknownError):
        await gateway.promote(
            captured.candidate.candidate_id,
            policy=policy,
            owner=captured.candidate.owner,
            actor=PromotionActor.HOST,
            mutation_id=f"promote-unknown:{candidate_id}",
        )
    request = (await gateway.scan_reconciliation_required(owner=captured.candidate.owner)).items[0]
    return ledger, artifacts, gateway, adapter, request


@pytest.mark.asyncio
async def test_promotion_persists_intent_and_retry_does_not_redispatch(tmp_path) -> None:
    policy, _, ledger, artifacts, _, captured = await _captured(tmp_path)
    adapter = _RecordingAdapter(ledger, captured.candidate.owner)
    gateway = LearningGateway(ledger, artifacts, promotion_adapter=adapter)
    await _qualify(gateway, captured)

    promoted = await gateway.promote(
        captured.candidate.candidate_id,
        policy=policy,
        owner=captured.candidate.owner,
        actor=PromotionActor.OPERATOR,
        mutation_id="promote-1",
    )
    assert promoted.state is CandidateState.PROMOTED
    assert promoted.promotion_actor is PromotionActor.OPERATOR
    assert adapter.calls == 1

    replay = await gateway.promote(
        captured.candidate.candidate_id,
        policy=policy,
        owner=captured.candidate.owner,
        actor=PromotionActor.OPERATOR,
        mutation_id="promote-1",
    )
    assert replay == promoted
    assert adapter.calls == 1
    ledger.close()


@pytest.mark.asyncio
async def test_ambiguous_promotion_becomes_unknown_and_never_blindly_retries(
    tmp_path,
) -> None:
    policy, _, ledger, artifacts, _, captured = await _captured(tmp_path)
    adapter = _RecordingAdapter(ledger, captured.candidate.owner, fail=True)
    gateway = LearningGateway(ledger, artifacts, promotion_adapter=adapter)
    await _qualify(gateway, captured)

    with pytest.raises(LearningPromotionUnknownError):
        await gateway.promote(
            captured.candidate.candidate_id,
            policy=policy,
            owner=captured.candidate.owner,
            actor=PromotionActor.HOST,
            mutation_id="promote-unknown",
        )
    current = await gateway.get(
        captured.candidate.candidate_id,
        owner=captured.candidate.owner,
    )
    assert current.state is CandidateState.PROMOTION_UNKNOWN
    discovery = await gateway.scan_reconciliation_required(
        owner=captured.candidate.owner,
    )
    assert len(discovery.items) == 1
    assert discovery.items[0].record == current
    assert discovery.items[0].kind is ReconciliationKind.PROMOTION
    assert discovery.next_cursor is None

    with pytest.raises(LearningPromotionUnknownError):
        await gateway.promote(
            captured.candidate.candidate_id,
            policy=policy,
            owner=captured.candidate.owner,
            actor=PromotionActor.HOST,
            mutation_id="promote-unknown",
        )
    assert adapter.calls == 1
    reconciliation = CandidateReconciliation(
        reconciliation_id="reconcile-promotion-absent",
        candidate_id=captured.candidate.candidate_id,
        kind=ReconciliationKind.PROMOTION,
        verdict=ReconciliationVerdict.EFFECT_ABSENT,
        reconciler_digest="sha256:" + "c" * 64,
        evidence_artifact_ids=("artifact-backend-inspection",),
        reconciled_by=PromotionActor.OPERATOR,
    )
    reconciled = await gateway.reconcile(
        reconciliation,
        owner=captured.candidate.owner,
        mutation_id="reconcile-promotion-1",
    )
    assert reconciled.state is CandidateState.QUALIFIED
    assert (
        await gateway.get_reconciliation(
            reconciliation.reconciliation_id,
            owner=captured.candidate.owner,
        )
        == reconciliation
    )
    assert await gateway.list_reconciliations(
        captured.candidate.candidate_id,
        owner=captured.candidate.owner,
    ) == [reconciliation]
    assert (await gateway.scan_reconciliation_required(owner=captured.candidate.owner)).items == ()
    ledger.close()


@pytest.mark.asyncio
async def test_reconciliation_coordinator_verifies_evidence_and_settles_without_replay(
    tmp_path,
) -> None:
    ledger, artifacts, gateway, adapter, request = await _promotion_unknown(tmp_path)
    evidence = await artifacts.stage_json(
        {"backend": "learned-knowledge", "exact_title_present": True},
        scope=request.record.candidate.content_artifact.scope,
        purpose=RECONCILIATION_EVIDENCE_PURPOSE,
    )
    observation = ReconciliationObservation(
        candidate_id=request.record.candidate.candidate_id,
        kind=request.kind,
        expected_revision=request.record.revision,
        candidate_digest=request.record.candidate.digest,
        verdict=ReconciliationVerdict.EFFECT_PRESENT,
        evidence_artifacts=(evidence,),
        target_reference="agno:learned_knowledge:[lc-observer] Retry rule",
    )
    observer = _Observer(observation)
    coordinator = LearningReconciliationCoordinator(
        gateway,
        observer,
        reconciler_digest="sha256:" + "a" * 64,
        max_concurrency=2,
    )

    batch = await coordinator.run_page(owner=request.record.candidate.owner)

    assert batch.owner_digest == request.record.candidate.owner.digest
    assert batch.reconciled_count == 1
    assert batch.items[0].status is ReconciliationItemStatus.RECONCILED
    assert batch.items[0].resulting_state is CandidateState.PROMOTED
    assert observer.calls == 1
    assert adapter.calls == 1
    reconciliations = await gateway.list_reconciliations(
        request.record.candidate.candidate_id,
        owner=request.record.candidate.owner,
    )
    assert reconciliations[0].evidence_artifact_ids == (evidence.artifact_id,)
    assert (
        await gateway.scan_reconciliation_required(owner=request.record.candidate.owner)
    ).items == ()
    ledger.close()


@pytest.mark.asyncio
async def test_reconciliation_coordinator_rejects_unbound_or_unscoped_evidence(
    tmp_path,
) -> None:
    ledger, artifacts, gateway, _, request = await _promotion_unknown(tmp_path)
    wrong_purpose = await artifacts.stage_json(
        {"backend": "untrusted observation"},
        scope=request.record.candidate.content_artifact.scope,
        purpose="learning.candidate.content",
    )
    unbound = ReconciliationObservation(
        candidate_id="different-candidate",
        kind=request.kind,
        expected_revision=request.record.revision,
        candidate_digest=request.record.candidate.digest,
        verdict=ReconciliationVerdict.EFFECT_ABSENT,
        evidence_artifacts=(wrong_purpose,),
    )
    coordinator = LearningReconciliationCoordinator(
        gateway,
        _Observer(unbound),
        reconciler_digest="sha256:" + "b" * 64,
    )
    mismatch = await coordinator.run_page(owner=request.record.candidate.owner)
    assert mismatch.items[0].status is ReconciliationItemStatus.REJECTED
    assert mismatch.items[0].error_code == "LEARNING_RECONCILIATION_OBSERVATION_MISMATCH"

    wrong_scope = ReconciliationObservation(
        candidate_id=request.record.candidate.candidate_id,
        kind=request.kind,
        expected_revision=request.record.revision,
        candidate_digest=request.record.candidate.digest,
        verdict=ReconciliationVerdict.EFFECT_ABSENT,
        evidence_artifacts=(wrong_purpose,),
    )
    coordinator = LearningReconciliationCoordinator(
        gateway,
        _Observer(wrong_scope),
        reconciler_digest="sha256:" + "b" * 64,
    )
    rejected = await coordinator.run_page(owner=request.record.candidate.owner)
    assert rejected.items[0].status is ReconciliationItemStatus.REJECTED
    assert rejected.items[0].error_code == "LEARNING_RECONCILIATION_EVIDENCE_SCOPE_MISMATCH"

    failed = await LearningReconciliationCoordinator(
        gateway,
        _Observer(RuntimeError("secret observer backend detail")),
        reconciler_digest="sha256:" + "b" * 64,
    ).run_page(owner=request.record.candidate.owner)
    assert failed.items[0].status is ReconciliationItemStatus.FAILED
    assert failed.items[0].error_code == "LEARNING_RECONCILIATION_OBSERVER_FAILED"
    assert "secret observer backend detail" not in str(failed.to_dict())
    assert (
        await gateway.get(
            request.record.candidate.candidate_id,
            owner=request.record.candidate.owner,
        )
    ).state is CandidateState.PROMOTION_UNKNOWN
    ledger.close()


@pytest.mark.asyncio
async def test_concurrent_reconciliation_observers_converge_by_cas(tmp_path) -> None:
    ledger, artifacts, gateway, adapter, request = await _promotion_unknown(tmp_path)
    evidence = await artifacts.stage_json(
        {"backend": "learned-knowledge", "exact_title_present": False},
        scope=request.record.candidate.content_artifact.scope,
        purpose=RECONCILIATION_EVIDENCE_PURPOSE,
    )
    observation = ReconciliationObservation(
        candidate_id=request.record.candidate.candidate_id,
        kind=request.kind,
        expected_revision=request.record.revision,
        candidate_digest=request.record.candidate.digest,
        verdict=ReconciliationVerdict.EFFECT_ABSENT,
        evidence_artifacts=(evidence,),
    )
    observer = _BarrierObserver(observation)
    coordinators = [
        LearningReconciliationCoordinator(
            gateway,
            observer,
            reconciler_digest="sha256:" + "c" * 64,
        )
        for _ in range(2)
    ]

    batches = await asyncio.gather(
        *(item.run_page(owner=request.record.candidate.owner) for item in coordinators)
    )

    statuses = [batch.items[0].status for batch in batches]
    assert ReconciliationItemStatus.RECONCILED in statuses
    assert set(statuses) <= {
        ReconciliationItemStatus.RECONCILED,
        ReconciliationItemStatus.STALE,
    }
    assert observer.calls == 2
    assert adapter.calls == 1
    final = await gateway.get(
        request.record.candidate.candidate_id,
        owner=request.record.candidate.owner,
    )
    assert final.state is CandidateState.QUALIFIED
    assert (
        len(
            await gateway.list_reconciliations(
                request.record.candidate.candidate_id,
                owner=request.record.candidate.owner,
            )
        )
        == 1
    )
    ledger.close()


@pytest.mark.asyncio
async def test_reconciliation_scan_is_owner_bound_keyset_and_restart_safe(
    tmp_path,
) -> None:
    policy, scope, ledger, _, gateway, first = await _captured(
        tmp_path,
        candidate_id="lc-scan-1",
    )
    second = await gateway.capture(
        policy=policy,
        scope=scope,
        target=LearningTarget.LEARNED_KNOWLEDGE,
        content={"title": "Second", "learning": "Second bounded rule."},
        source_run_ids=("run-2",),
        evidence_artifact_ids=("artifact-evidence-2",),
        confidence=0.8,
        risk=CandidateRisk.LOW,
        created_by=CandidateAuthor.RULE,
        mechanism_version="extractor:v1",
        candidate_id="lc-scan-2",
    )
    owner = first.candidate.owner
    for index, record in enumerate((first, second), start=1):
        qualified = ledger.record_evaluation(
            CandidateEvaluation(
                evaluation_id=f"eval-scan-{index}",
                candidate_id=record.candidate.candidate_id,
                verdict=EvaluationVerdict.QUALIFIED,
                evaluator_digest="sha256:" + "e" * 64,
                evidence_artifact_ids=(f"artifact-eval-{index}",),
                safety_passed=True,
                evaluated_by=PromotionActor.HOST,
            ),
            owner=owner,
            expected_revision=0,
            mutation_id=f"evaluate:scan:{index}",
        )
        promoting = ledger.begin_promotion(
            record.candidate.candidate_id,
            owner=owner,
            expected_revision=qualified.revision,
            mutation_id=f"promote:scan:{index}:begin",
            promotion_id=f"promotion-scan-{index}",
            actor=PromotionActor.HOST,
        )
        ledger.settle_promotion(
            record.candidate.candidate_id,
            owner=owner,
            expected_revision=promoting.revision,
            mutation_id=f"promote:scan:{index}:settle",
            succeeded=False,
            target_reference=None,
        )

    first_page = ledger.scan_reconciliation_required(owner=owner, limit=1)
    assert len(first_page.items) == 1
    assert first_page.next_cursor is not None
    persisted_cursor = ReconciliationCursor.from_dict(first_page.next_cursor.to_dict())
    ledger.close()

    reopened = SQLiteLearningLedger(tmp_path / "learning.db")
    second_page = reopened.scan_reconciliation_required(
        owner=owner,
        limit=1,
        cursor=persisted_cursor,
    )
    assert len(second_page.items) == 1
    assert second_page.next_cursor is None
    assert {
        first_page.items[0].record.candidate.candidate_id,
        second_page.items[0].record.candidate.candidate_id,
    } == {"lc-scan-1", "lc-scan-2"}

    wrong_owner = LearningOwner("other", owner.storage_namespace)
    with pytest.raises(ReconciliationCursorScopeError):
        reopened.scan_reconciliation_required(
            owner=wrong_owner,
            cursor=persisted_cursor,
        )
    assert reopened.scan_reconciliation_required(owner=wrong_owner).items == ()
    reopened.close()


def test_reconciliation_batch_rejects_cursor_from_another_owner() -> None:
    owner_digest = "sha256:" + "a" * 64
    cursor = ReconciliationCursor(
        updated_at="2026-08-07T00:00:00+00:00",
        candidate_id="candidate-1",
        owner_digest="sha256:" + "b" * 64,
    )

    with pytest.raises(ValueError, match="bound to owner_digest"):
        ReconciliationBatchOutcome(
            owner_digest=owner_digest,
            items=(),
            next_cursor=cursor,
        )


@pytest.mark.asyncio
async def test_candidate_quarantine_restore_and_delete_are_idempotent(tmp_path) -> None:
    _, _, ledger, _, gateway, captured = await _captured(tmp_path)
    owner = captured.candidate.owner
    quarantined = await gateway.transition(
        captured.candidate.candidate_id,
        owner=owner,
        action=CandidateAction.QUARANTINE,
        mutation_id="quarantine-1",
    )
    assert quarantined.state is CandidateState.QUARANTINED
    assert (
        await gateway.transition(
            captured.candidate.candidate_id,
            owner=owner,
            action=CandidateAction.QUARANTINE,
            mutation_id="quarantine-1",
        )
        == quarantined
    )

    restored = await gateway.transition(
        captured.candidate.candidate_id,
        owner=owner,
        action=CandidateAction.RESTORE,
        mutation_id="restore-1",
    )
    assert restored.state is CandidateState.CAPTURED
    deleted = await gateway.transition(
        captured.candidate.candidate_id,
        owner=owner,
        action=CandidateAction.DELETE,
        mutation_id="delete-1",
    )
    assert deleted.state is CandidateState.DELETED
    assert ledger.list_artifact_storage_keys() == []
    with pytest.raises(HarnessError) as exc:
        await gateway.read_content(captured.candidate.candidate_id, owner=owner)
    assert exc.value.code == "LEARNING_CANDIDATE_DELETED"
    assert (await gateway.export(captured.candidate.candidate_id, owner=owner)).content is None
    ledger.close()


@pytest.mark.asyncio
async def test_candidate_edit_is_an_immutable_same_target_supersession(tmp_path) -> None:
    policy, scope, ledger, _, gateway, captured = await _captured(tmp_path)
    edited = await gateway.capture(
        policy=policy,
        scope=scope,
        target=LearningTarget.LEARNED_KNOWLEDGE,
        content={"title": "Retry rule", "learning": "Retry safe reads twice."},
        source_run_ids=("run-1",),
        evidence_artifact_ids=("artifact-edit-evidence",),
        confidence=0.9,
        risk=CandidateRisk.MEDIUM,
        created_by=CandidateAuthor.OPERATOR,
        mechanism_version="editor:v1",
        candidate_id="lc-edited",
        supersedes_candidate_id=captured.candidate.candidate_id,
    )
    assert edited.candidate.supersedes_candidate_id == captured.candidate.candidate_id
    assert (
        await gateway.get(captured.candidate.candidate_id, owner=captured.candidate.owner)
    ).state is (CandidateState.CAPTURED)

    with pytest.raises(HarnessError) as target_error:
        await gateway.capture(
            policy=policy,
            scope=scope,
            target=LearningTarget.ENTITY_MEMORY,
            content={"entity": "retry", "entity_type": "rule"},
            source_run_ids=("run-1",),
            evidence_artifact_ids=("artifact-edit-evidence",),
            confidence=0.9,
            risk=CandidateRisk.MEDIUM,
            created_by=CandidateAuthor.OPERATOR,
            mechanism_version="editor:v1",
            candidate_id="lc-invalid-edit",
            supersedes_candidate_id=captured.candidate.candidate_id,
        )
    assert target_error.value.code == "LEARNING_CANDIDATE_TARGET_CONFLICT"
    ledger.close()


@pytest.mark.asyncio
async def test_harness_change_requires_manifest_and_hypothesis_artifacts(tmp_path) -> None:
    policy, scope = _policy_and_scope()
    ledger = SQLiteLearningLedger(tmp_path / "learning.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    gateway = LearningGateway(ledger, artifacts)
    with pytest.raises(HarnessError) as exc:
        await gateway.capture(
            policy=policy,
            scope=scope,
            target=LearningTarget.HARNESS_COMPONENT,
            content={"patch": "bounded proposal"},
            source_run_ids=("run-harness",),
            evidence_artifact_ids=("artifact-verifier",),
            confidence=0.8,
            risk=CandidateRisk.HIGH,
            created_by=CandidateAuthor.AGENT,
            mechanism_version="self-harness:v1",
        )
    assert exc.value.code == "LEARNING_CHANGE_CONTRACT_REQUIRED"
    ledger.close()


@pytest.mark.asyncio
async def test_promoted_learning_rolls_back_before_it_can_be_deleted(tmp_path) -> None:
    policy, _, ledger, artifacts, _, captured = await _captured(tmp_path)
    adapter = _RecordingAdapter(ledger, captured.candidate.owner)
    gateway = LearningGateway(ledger, artifacts, promotion_adapter=adapter)
    await _qualify(gateway, captured)
    await gateway.promote(
        captured.candidate.candidate_id,
        policy=policy,
        owner=captured.candidate.owner,
        actor=PromotionActor.OPERATOR,
        mutation_id="promote-for-rollback",
    )
    with pytest.raises(HarnessError) as delete_error:
        await gateway.transition(
            captured.candidate.candidate_id,
            owner=captured.candidate.owner,
            action=CandidateAction.DELETE,
            mutation_id="premature-delete",
        )
    assert delete_error.value.code == "LEARNING_CANDIDATE_TRANSITION_INVALID"

    rolled_back = await gateway.rollback(
        captured.candidate.candidate_id,
        owner=captured.candidate.owner,
        actor=PromotionActor.OPERATOR,
        mutation_id="rollback-1",
    )
    assert rolled_back.state is CandidateState.ROLLED_BACK
    replay = await gateway.rollback(
        captured.candidate.candidate_id,
        owner=captured.candidate.owner,
        actor=PromotionActor.OPERATOR,
        mutation_id="rollback-1",
    )
    assert replay == rolled_back
    assert adapter.rollback_calls == 1
    ledger.close()


@pytest.mark.asyncio
async def test_ambiguous_rollback_is_not_blindly_retried(tmp_path) -> None:
    policy, _, ledger, artifacts, _, captured = await _captured(tmp_path)
    adapter = _RecordingAdapter(ledger, captured.candidate.owner)
    gateway = LearningGateway(ledger, artifacts, promotion_adapter=adapter)
    await _qualify(gateway, captured)
    await gateway.promote(
        captured.candidate.candidate_id,
        policy=policy,
        owner=captured.candidate.owner,
        actor=PromotionActor.OPERATOR,
        mutation_id="promote-before-unknown-rollback",
    )

    async def fail_rollback(*args, **kwargs):
        adapter.rollback_calls += 1
        raise TimeoutError("delete response lost")

    adapter.rollback = fail_rollback
    with pytest.raises(LearningRollbackUnknownError):
        await gateway.rollback(
            captured.candidate.candidate_id,
            owner=captured.candidate.owner,
            actor=PromotionActor.OPERATOR,
            mutation_id="rollback-unknown",
        )
    with pytest.raises(LearningRollbackUnknownError):
        await gateway.rollback(
            captured.candidate.candidate_id,
            owner=captured.candidate.owner,
            actor=PromotionActor.OPERATOR,
            mutation_id="rollback-unknown",
        )
    assert adapter.rollback_calls == 1
    discovery = await gateway.scan_reconciliation_required(
        owner=captured.candidate.owner,
    )
    assert discovery.items[0].kind is ReconciliationKind.ROLLBACK
    reconciliation = CandidateReconciliation(
        reconciliation_id="reconcile-rollback-absent",
        candidate_id=captured.candidate.candidate_id,
        kind=ReconciliationKind.ROLLBACK,
        verdict=ReconciliationVerdict.EFFECT_ABSENT,
        reconciler_digest="sha256:" + "d" * 64,
        evidence_artifact_ids=("artifact-backend-inspection",),
        reconciled_by=PromotionActor.OPERATOR,
    )
    reconciled = await gateway.reconcile(
        reconciliation,
        owner=captured.candidate.owner,
        mutation_id="reconcile-rollback-1",
    )
    assert reconciled.state is CandidateState.ROLLED_BACK
    ledger.close()


@pytest.mark.asyncio
async def test_expired_candidate_cannot_promote(tmp_path) -> None:
    policy, scope = _policy_and_scope()
    ledger = SQLiteLearningLedger(tmp_path / "learning.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    adapter = AsyncMock()
    gateway = LearningGateway(ledger, artifacts, promotion_adapter=adapter)
    captured = await gateway.capture(
        policy=policy,
        scope=scope,
        target=LearningTarget.LEARNED_KNOWLEDGE,
        content={"learning": "stale"},
        source_run_ids=("run-expired",),
        evidence_artifact_ids=("artifact-evidence",),
        confidence=0.7,
        risk=CandidateRisk.LOW,
        created_by=CandidateAuthor.RULE,
        mechanism_version="rule:v1",
        expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    await _qualify(gateway, captured)
    with pytest.raises(HarnessError) as exc:
        await gateway.promote(
            captured.candidate.candidate_id,
            policy=policy,
            owner=captured.candidate.owner,
            actor=PromotionActor.HOST,
            mutation_id="promote-expired",
        )
    assert exc.value.code == "LEARNING_CANDIDATE_EXPIRED"
    adapter.apply.assert_not_called()
    ledger.close()


@pytest.mark.asyncio
async def test_agno_adapter_uses_scoped_namespace(tmp_path) -> None:
    learned_store = MagicMock()
    learned_store.asave = AsyncMock(return_value=True)
    learned_store.adelete = AsyncMock(return_value=True)
    machine = SimpleNamespace(learned_knowledge_store=learned_store)
    adapter = AgnoLearningPromotionAdapter(lambda _candidate: machine)

    _, _, ledger, _, _, record = await _captured(tmp_path)
    # The adapter itself is independent of the ledger; use the captured candidate shape.
    reference = await adapter.apply(
        record.candidate,
        {"title": "Rule", "learning": "Use bounded retries", "tags": ["ops"]},
        idempotency_key="promotion-1",
    )
    assert reference.startswith("agno:learned_knowledge:")
    assert learned_store.asave.call_args.kwargs["namespace"] == (record.candidate.storage_namespace)
    assert record.candidate.candidate_id in learned_store.asave.call_args.kwargs["title"]
    await adapter.rollback(
        record.candidate,
        {"title": "Rule", "learning": "Use bounded retries", "tags": ["ops"]},
        target_reference=reference,
        idempotency_key="rollback-1",
    )
    assert record.candidate.candidate_id in learned_store.adelete.call_args.args[0]
    ledger.close()


@pytest.mark.asyncio
async def test_agno_exact_observer_reconciles_present_key_with_content_free_evidence(
    tmp_path,
) -> None:
    ledger, artifacts, gateway, dispatched, request = await _promotion_unknown(tmp_path)
    content = await gateway.read_content(
        request.record.candidate.candidate_id,
        owner=request.record.candidate.owner,
    )
    learned_store = SimpleNamespace(
        asave=AsyncMock(return_value=True),
        adelete=AsyncMock(return_value=True),
    )
    adapter = AgnoLearningPromotionAdapter(
        lambda _candidate: SimpleNamespace(learned_knowledge_store=learned_store)
    )
    expected_reference = await adapter.apply(
        request.record.candidate,
        content,
        idempotency_key="observer-identity",
    )
    exact_title = learned_store.asave.call_args.kwargs["title"]

    class ExactVector:
        def __init__(self) -> None:
            self.names: list[str] = []

        def name_exists(self, name: str) -> bool:
            self.names.append(name)
            return name == exact_title

    vector = ExactVector()
    machine = SimpleNamespace(
        learned_knowledge_store=SimpleNamespace(
            knowledge=SimpleNamespace(vector_db=vector),
        )
    )
    observer = AgnoLearnedKnowledgeReconciliationObserver(
        lambda _candidate: machine,
        artifacts,
        observer_identity_digest="sha256:" + "9" * 64,
    )
    coordinator = LearningReconciliationCoordinator(
        gateway,
        observer,
        reconciler_digest="sha256:" + "8" * 64,
    )
    direct_observation = await observer.observe(request, content)
    evidence = await artifacts.load_json(direct_observation.evidence_artifacts[0])

    batch = await coordinator.run_page(owner=request.record.candidate.owner)

    assert batch.items[0].status is ReconciliationItemStatus.RECONCILED
    assert batch.items[0].resulting_state is CandidateState.PROMOTED
    assert vector.names == [exact_title, exact_title]
    assert dispatched.calls == 1
    reconciliations = await gateway.list_reconciliations(
        request.record.candidate.candidate_id,
        owner=request.record.candidate.owner,
    )
    assert reconciliations[0].target_reference == expected_reference
    assert reconciliations[0].evidence_artifact_ids == (
        direct_observation.evidence_artifacts[0].artifact_id,
    )
    serialized = str(evidence)
    assert evidence["effect_present"] is True
    assert evidence["candidate_digest"] == request.record.candidate.digest
    assert "Retry rule" not in serialized
    assert "Retry only idempotent reads" not in serialized
    assert request.record.candidate.candidate_id not in serialized
    ledger.close()


@pytest.mark.asyncio
async def test_agno_exact_observer_absence_and_failures_are_safe(tmp_path) -> None:
    ledger, artifacts, gateway, _, request = await _promotion_unknown(
        tmp_path,
        candidate_id="lc-exact-absent",
    )

    class ExactVector:
        def name_exists(self, _name: str) -> bool:
            return False

    observer = AgnoLearnedKnowledgeReconciliationObserver(
        lambda _candidate: SimpleNamespace(
            learned_knowledge_store=SimpleNamespace(
                knowledge=SimpleNamespace(vector_db=ExactVector()),
            )
        ),
        artifacts,
        observer_identity_digest="sha256:" + "7" * 64,
    )
    absent = await LearningReconciliationCoordinator(
        gateway,
        observer,
        reconciler_digest="sha256:" + "6" * 64,
    ).run_page(owner=request.record.candidate.owner)
    assert absent.items[0].status is ReconciliationItemStatus.RECONCILED
    assert absent.items[0].resulting_state is CandidateState.QUALIFIED
    ledger.close()

    other_path = tmp_path / "other"
    other_path.mkdir()
    other_ledger, other_artifacts, other_gateway, _, other_request = await _promotion_unknown(
        other_path,
        candidate_id="lc-exact-failed",
    )

    class FailedVector:
        def name_exists(self, _name: str) -> bool:
            raise RuntimeError("private backend failure")

    failed_observer = AgnoLearnedKnowledgeReconciliationObserver(
        lambda _candidate: SimpleNamespace(
            learned_knowledge_store=SimpleNamespace(
                knowledge=SimpleNamespace(vector_db=FailedVector()),
            )
        ),
        other_artifacts,
        observer_identity_digest="sha256:" + "5" * 64,
    )
    failed = await LearningReconciliationCoordinator(
        other_gateway,
        failed_observer,
        reconciler_digest="sha256:" + "4" * 64,
    ).run_page(owner=other_request.record.candidate.owner)
    assert failed.items[0].status is ReconciliationItemStatus.FAILED
    assert failed.items[0].error_code == "LEARNING_AGNO_EXACT_OBSERVER_FAILED"
    assert "private backend failure" not in str(failed.to_dict())
    assert (
        await other_gateway.get(
            other_request.record.candidate.candidate_id,
            owner=other_request.record.candidate.owner,
        )
    ).state is CandidateState.PROMOTION_UNKNOWN
    other_ledger.close()


@pytest.mark.asyncio
async def test_agno_learning_identity_is_digest_bound_and_reference_bounded(tmp_path) -> None:
    _, _, ledger, _, _, record = await _captured(tmp_path, candidate_id="c" * 512)
    learned_store = SimpleNamespace(
        asave=AsyncMock(return_value=True),
        adelete=AsyncMock(return_value=True),
    )
    adapter = AgnoLearningPromotionAdapter(
        lambda _candidate: SimpleNamespace(learned_knowledge_store=learned_store)
    )
    content = {"title": "T" * 2_000, "learning": "bounded"}
    reference = await adapter.apply(
        record.candidate,
        content,
        idempotency_key="bounded-identity",
    )

    assert len(reference) <= 512
    assert record.candidate.digest.removeprefix("sha256:")[:32] in reference
    await adapter.rollback(
        record.candidate,
        content,
        target_reference=reference,
        idempotency_key="bounded-rollback",
    )
    assert learned_store.adelete.call_args.args[0] == learned_store.asave.call_args.kwargs["title"]
    ledger.close()


@pytest.mark.asyncio
async def test_agno_adapter_requires_confirmed_write_and_rejects_entity_merge(
    tmp_path,
) -> None:
    learned_store = SimpleNamespace(asave=AsyncMock(return_value=False))
    adapter = AgnoLearningPromotionAdapter(
        lambda _candidate: SimpleNamespace(learned_knowledge_store=learned_store)
    )
    _, _, ledger, _, _, record = await _captured(tmp_path)
    with pytest.raises(HarnessError) as false_write:
        await adapter.apply(
            record.candidate,
            {"learning": "unconfirmed"},
            idempotency_key="promotion-false",
        )
    assert false_write.value.code == "LEARNING_PROMOTION_BACKEND_REJECTED"

    entity_candidate = record.candidate.__class__(
        **{
            **record.candidate.to_dict(),
            "target": LearningTarget.ENTITY_MEMORY,
            "content_artifact": record.candidate.content_artifact,
            "source_run_ids": record.candidate.source_run_ids,
            "evidence_artifact_ids": record.candidate.evidence_artifact_ids,
        }
    )
    with pytest.raises(HarnessError) as entity_error:
        await adapter.apply(
            entity_candidate,
            {"entity": "service", "entity_type": "system"},
            idempotency_key="promotion-entity",
        )
    assert entity_error.value.code == "LEARNING_PROMOTION_TARGET_UNSUPPORTED"
    ledger.close()
