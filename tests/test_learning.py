"""Contracts for explicit v0.12 learning policy and trusted run scope."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agnoclaw import (
    AgentHarness,
    CandidateAction,
    CandidateAuthor,
    CandidateEvaluation,
    CandidateRisk,
    CandidateState,
    EvaluationArchiveQuery,
    EvaluationVerdict,
    HarnessConfig,
    HarnessError,
    LearningApplicationKind,
    LearningCandidate,
    LearningEffectivenessPolicy,
    LearningEffectivenessRecommendation,
    LearningMode,
    LearningOutcomeKind,
    LearningPolicy,
    LearningProfile,
    LearningPromotionAdapter,
    LearningPromotionUnknownError,
    LearningReconciliationWorkerConfig,
    LearningScope,
    LearningStorePolicy,
    LearningTarget,
    LearningWritePath,
    LocalArtifactStore,
    PromotionActor,
    RunSnapshot,
    SQLiteLearningLedger,
    SQLiteRuntimeStore,
)
from agnoclaw.learning_candidates import CandidateNotFoundError
from agnoclaw.runtime import ExecutionContext


def _context(
    *,
    tenant_id: str | None = "acme",
    user_id: str | None = "user-1",
    session_id: str | None = "session-1",
) -> ExecutionContext:
    return ExecutionContext.create(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        workspace_id="workspace-1",
    )


def test_personal_and_session_profile_is_immutable_and_explicit() -> None:
    policy = LearningProfile.personal_and_session(
        user_profile="always",
        user_memory="agentic",
        session_context="always",
        max_updates_per_run=3,
        consent_required=False,
    )

    assert policy.user_profile == LearningStorePolicy(
        LearningMode.ALWAYS,
        max_updates_per_run=3,
    )
    assert policy.user_memory is not None
    assert policy.session_context is not None
    assert policy.descriptor()["knowledge_configured"] is False
    with pytest.raises(FrozenInstanceError):
        policy.namespace = "changed"  # type: ignore[misc]


def test_learning_store_policy_rejects_unknown_mode_and_unbounded_budget() -> None:
    with pytest.raises(HarnessError) as mode_error:
        (LearningStorePolicy("silent"),)  # type: ignore[arg-type]
    assert mode_error.value.code == "LEARNING_MODE_UNSUPPORTED"

    with pytest.raises(HarnessError) as budget_error:
        LearningStorePolicy(LearningMode.ALWAYS, max_updates_per_run=101)
    assert budget_error.value.code == "LEARNING_BUDGET_INVALID"


def test_institutional_profile_requires_vector_knowledge() -> None:
    with pytest.raises(HarnessError) as exc:
        LearningProfile.institutional(namespace="research")
    assert exc.value.code == "LEARNING_KNOWLEDGE_REQUIRED"


def test_invalid_promotion_and_store_policy_have_typed_errors() -> None:
    knowledge = SimpleNamespace(vector_db=object())
    with pytest.raises(HarnessError) as promotion_error:
        LearningProfile.institutional(
            namespace="research",
            knowledge=knowledge,
            promotion="self_approve",
        )
    assert promotion_error.value.code == "LEARNING_PROMOTION_UNSUPPORTED"

    with pytest.raises(HarnessError) as store_error:
        LearningPolicy(user_memory="always")  # type: ignore[arg-type]
    assert store_error.value.code == "LEARNING_STORE_POLICY_INVALID"


def test_institutional_stores_cannot_use_direct_model_writes() -> None:
    knowledge = SimpleNamespace(vector_db=object())
    with pytest.raises(HarnessError) as exc:
        LearningPolicy(
            entity_memory=LearningStorePolicy(
                LearningMode.AGENTIC,
                write_path=LearningWritePath.DIRECT,
            ),
            namespace="research",
            tenant_required=True,
            knowledge=knowledge,
        )
    assert exc.value.code == "LEARNING_DIRECT_INSTITUTIONAL_WRITE_FORBIDDEN"


def test_learning_scope_requires_selected_identity_and_consent() -> None:
    personal = LearningProfile.personal()
    with pytest.raises(HarnessError) as user_error:
        LearningScope.resolve(personal, _context(user_id=None), agent_id="assistant")
    assert user_error.value.code == "LEARNING_SCOPE_USER_REQUIRED"

    with pytest.raises(HarnessError) as consent_error:
        LearningScope.resolve(personal, _context(), agent_id="assistant")
    assert consent_error.value.code == "LEARNING_CONSENT_REQUIRED"

    session = LearningProfile.session()
    with pytest.raises(HarnessError) as session_error:
        LearningScope.resolve(
            session,
            _context(session_id=None),
            agent_id="assistant",
        )
    assert session_error.value.code == "LEARNING_SCOPE_SESSION_REQUIRED"


def test_learning_scope_is_stable_opaque_and_cross_tenant_distinct() -> None:
    policy = LearningProfile.personal_and_session(consent_required=False)
    first = LearningScope.resolve(policy, _context(), agent_id="assistant")
    same = LearningScope.resolve(policy, _context(), agent_id="assistant")
    other = LearningScope.resolve(
        policy,
        _context(tenant_id="other"),
        agent_id="assistant",
    )

    assert first == same
    assert first.storage_user_id != other.storage_user_id
    assert first.storage_session_id != other.storage_session_id
    assert first.storage_namespace != other.storage_namespace
    serialized = str(first.descriptor())
    assert "acme" not in serialized
    assert "user-1" not in serialized
    assert "session-1" not in serialized


def test_institutional_scope_requires_tenant() -> None:
    policy = LearningProfile.institutional(
        namespace="research",
        knowledge=SimpleNamespace(vector_db=object()),
    )
    with pytest.raises(HarnessError) as exc:
        LearningScope.resolve(
            policy,
            _context(tenant_id=None),
            agent_id="assistant",
        )
    assert exc.value.code == "LEARNING_SCOPE_TENANT_REQUIRED"


def test_agent_rejects_mixed_legacy_and_policy_learning(tmp_path) -> None:
    policy = LearningProfile.session()
    with pytest.raises(HarnessError) as exc:
        AgentHarness(
            workspace_dir=tmp_path,
            include_default_tools=False,
            learning=policy,
            enable_session_context=True,
        )
    assert exc.value.code == "LEARNING_CONFIGURATION_CONFLICT"


def test_agent_materializes_policy_per_run_with_scoped_agno_identity(tmp_path) -> None:
    policy = LearningProfile.personal_and_session(consent_required=True)
    base_agent = MagicMock(name="base_agent")
    run_agent = MagicMock(name="run_agent")
    run_agent.run.return_value = MagicMock(content="ok")

    with (
        patch("agnoclaw.agent.Agent", side_effect=[base_agent, run_agent]) as agent_ctor,
        patch("agnoclaw.agent._make_db", return_value=MagicMock()),
        patch(
            "agnoclaw.memory.build_learning_machine",
            return_value=MagicMock(name="learning_machine"),
        ) as build_learning,
    ):
        harness = AgentHarness(
            workspace_dir=tmp_path,
            config=HarnessConfig(),
            include_default_tools=False,
            learning=policy,
            name="support",
        )
        harness.run(
            "help",
            context=_context(),
            learning_consent=True,
        )

    scope = build_learning.call_args.kwargs["scope"]
    assert isinstance(scope, LearningScope)
    assert build_learning.call_args.kwargs["policy"] is policy
    assert agent_ctor.call_count == 2
    assert agent_ctor.call_args.kwargs["user_id"] == scope.storage_user_id
    assert agent_ctor.call_args.kwargs["session_id"] == scope.storage_session_id
    assert run_agent.run.call_args.kwargs["user_id"] == scope.storage_user_id
    assert run_agent.run.call_args.kwargs["session_id"] == scope.storage_session_id
    assert harness._spec.settings["learning"]["schema_version"] == 1


def test_agent_fails_learning_scope_before_model_call(tmp_path) -> None:
    policy = LearningProfile.personal(consent_required=False)
    model_agent = MagicMock()
    with (
        patch("agnoclaw.agent.Agent", return_value=model_agent),
        patch("agnoclaw.agent._make_db", return_value=MagicMock()),
    ):
        harness = AgentHarness(
            workspace_dir=tmp_path,
            config=HarnessConfig(),
            include_default_tools=False,
            learning=policy,
        )
        with pytest.raises(HarnessError) as exc:
            harness.run("help", context=_context(user_id=None))

    assert exc.value.code == "LEARNING_SCOPE_USER_REQUIRED"
    model_agent.run.assert_not_called()


def test_agent_learning_gateway_requires_policy_artifacts_and_ledger(tmp_path) -> None:
    ledger = SQLiteLearningLedger(tmp_path / "learning.db")
    policy = LearningProfile.institutional(
        namespace="research",
        knowledge=SimpleNamespace(vector_db=object()),
    )
    with pytest.raises(HarnessError) as policy_error:
        AgentHarness(
            model=MagicMock(),
            workspace_dir=tmp_path / "no-policy",
            include_default_tools=False,
            learning_ledger=ledger,
            artifact_store=LocalArtifactStore(tmp_path / "artifacts-1"),
        )
    assert policy_error.value.code == "LEARNING_LEDGER_POLICY_REQUIRED"

    with pytest.raises(HarnessError) as artifact_error:
        AgentHarness(
            model=MagicMock(),
            workspace_dir=tmp_path / "no-artifacts",
            include_default_tools=False,
            learning=policy,
            learning_ledger=ledger,
        )
    assert artifact_error.value.code == "LEARNING_ARTIFACT_STORE_REQUIRED"

    adapter = MagicMock(spec=LearningPromotionAdapter)
    with pytest.raises(HarnessError) as ledger_error:
        AgentHarness(
            model=MagicMock(),
            workspace_dir=tmp_path / "no-ledger",
            include_default_tools=False,
            learning=policy,
            artifact_store=LocalArtifactStore(tmp_path / "artifacts-2"),
            learning_promotion_adapter=adapter,
        )
    assert ledger_error.value.code == "LEARNING_LEDGER_REQUIRED"
    ledger.close()


class _HarnessPromotionAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[LearningCandidate, dict, str]] = []
        self.rollback_calls: list[tuple[LearningCandidate, dict, str, str]] = []

    async def apply(
        self,
        candidate: LearningCandidate,
        content: dict,
        *,
        idempotency_key: str,
    ) -> str:
        self.calls.append((candidate, content, idempotency_key))
        return f"test:{candidate.target.value}:{candidate.candidate_id}"

    async def rollback(
        self,
        candidate: LearningCandidate,
        content: dict,
        *,
        target_reference: str,
        idempotency_key: str,
    ) -> None:
        self.rollback_calls.append((candidate, content, target_reference, idempotency_key))


@pytest.mark.asyncio
async def test_agent_learning_gateway_is_scoped_governed_and_host_only(tmp_path) -> None:
    policy = LearningProfile.institutional(
        namespace="research",
        knowledge=SimpleNamespace(vector_db=object()),
    )
    ledger = SQLiteLearningLedger(tmp_path / "learning.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    runtime = SQLiteRuntimeStore(tmp_path / "runtime.db")
    runtime.create_run(
        RunSnapshot(
            run_id="run-source-1",
            tenant_id="acme",
            user_id="analyst-1",
            session_id="session-1",
        )
    )
    context = ExecutionContext.create(
        tenant_id="acme",
        user_id="analyst-1",
        session_id="session-1",
        workspace_id=str(tmp_path / "workspace"),
    )
    adapter = _HarnessPromotionAdapter()
    with (
        patch("agnoclaw.agent.Agent", return_value=MagicMock()),
        patch("agnoclaw.agent._make_db", return_value=MagicMock()),
    ):
        harness = AgentHarness(
            workspace_dir=tmp_path / "workspace",
            config=HarnessConfig(),
            include_default_tools=False,
            learning=policy,
            runtime_store=runtime,
            artifact_store=artifacts,
            learning_ledger=ledger,
            learning_promotion_adapter=adapter,
        )

    record = await harness.capture_learning_candidate(
        context=context,
        target=LearningTarget.LEARNED_KNOWLEDGE,
        content={"title": "Retries", "learning": "Retry only safe reads."},
        source_run_ids=["run-source-1"],
        evidence_artifact_ids=["artifact-source-evidence"],
        confidence=0.91,
        risk=CandidateRisk.LOW,
        created_by=CandidateAuthor.AGENT,
        mechanism_version="reflector:v1",
        candidate_id="lc-harness-1",
    )
    assert record.state is CandidateState.CAPTURED
    assert "learning_ledger" in {resource.resource_id for resource in harness._spec.resources}
    assert harness._spec.settings["learning"]["candidate_gateway"] is True
    assert await harness.read_learning_candidate_content("lc-harness-1", context=context) == {
        "learning": "Retry only safe reads.",
        "title": "Retries",
    }
    assert await harness.list_learning_candidates(context=context) == [record]
    assert (await harness.scan_learning_reconciliation_required(context=context)).items == ()
    empty_observer = SimpleNamespace(observe=MagicMock())
    empty_batch = await harness.observe_learning_reconciliation_page(
        empty_observer,
        context=context,
        reconciler_digest="sha256:" + "f" * 64,
    )
    assert empty_batch.items == ()
    empty_observer.observe.assert_not_called()

    evaluation = CandidateEvaluation(
        evaluation_id="evaluation:harness:1",
        candidate_id="lc-harness-1",
        verdict=EvaluationVerdict.QUALIFIED,
        evaluator_digest="sha256:" + "a" * 64,
        evidence_artifact_ids=("artifact-held-in", "artifact-held-out"),
        safety_passed=True,
        evaluated_by=PromotionActor.OPERATOR,
        metrics={"held_in": 0.9, "held_out": 0.88, "transfer": 0.8},
        control_metrics={"held_in": 0.7, "held_out": 0.72, "transfer": 0.71},
    )
    qualified = await harness.record_learning_candidate_evaluation(
        evaluation,
        context=context,
        mutation_id="evaluate:harness:1",
    )
    assert qualified.state is CandidateState.QUALIFIED
    assert (await harness.query_learning_evaluation_archive(context=context)).items == ()
    qualified_archive = await harness.query_learning_evaluation_archive(
        context=context,
        query=EvaluationArchiveQuery(verdicts=(EvaluationVerdict.QUALIFIED,)),
    )
    assert [item.evaluation_id for item in qualified_archive.items] == ["evaluation:harness:1"]
    assert qualified_archive.items[0].candidate_id == "lc-harness-1"
    promoted = await harness.promote_learning_candidate(
        "lc-harness-1",
        context=context,
        actor=PromotionActor.OPERATOR,
        mutation_id="promote:harness:1",
    )
    assert promoted.state is CandidateState.PROMOTED
    assert len(adapter.calls) == 1
    application = await harness.observe_learning_application(
        "lc-harness-1",
        run_id="run-source-1",
        kind=LearningApplicationKind.APPLIED,
        observer_digest="sha256:" + "b" * 64,
        evidence_artifact_ids=["artifact-application"],
        context=context,
        application_id="application:harness:1",
    )
    outcome = await harness.observe_learning_outcome(
        application.application_id,
        kind=LearningOutcomeKind.SUCCESS,
        score=0.8,
        evaluator_digest="sha256:" + "c" * 64,
        evidence_artifact_ids=["artifact-outcome"],
        evaluated_by=PromotionActor.HOST,
        context=context,
        outcome_id="outcome:harness:1",
    )
    assert outcome.run_id == "run-source-1"
    assert await harness.list_learning_applications("lc-harness-1", context=context) == [
        application
    ]
    assert await harness.list_learning_outcomes("lc-harness-1", context=context) == [outcome]
    effectiveness = await harness.summarize_learning_effectiveness(
        "lc-harness-1",
        context=context,
        policy=LearningEffectivenessPolicy(
            minimum_outcomes=1,
            minimum_independent_runs=1,
        ),
    )
    assert effectiveness.recommendation is LearningEffectivenessRecommendation.RETAIN
    rolled_back = await harness.rollback_learning_candidate(
        "lc-harness-1",
        context=context,
        actor=PromotionActor.OPERATOR,
        mutation_id="rollback:harness:1",
    )
    assert rolled_back.state is CandidateState.ROLLED_BACK
    deleted = await harness.transition_learning_candidate(
        "lc-harness-1",
        context=context,
        action=CandidateAction.DELETE,
        mutation_id="delete:harness:1",
    )
    assert deleted.state is CandidateState.DELETED
    assert len(adapter.rollback_calls) == 1
    events = await harness.list_learning_candidate_events(
        "lc-harness-1",
        context=context,
    )
    assert [item.sequence for item in events] == list(range(1, 8))
    assert events[-1].event_type == "learning.candidate.deleted"

    other_tenant = ExecutionContext.create(
        tenant_id="other",
        user_id="analyst-1",
        session_id="session-1",
        workspace_id=str(tmp_path / "workspace"),
    )
    with pytest.raises(CandidateNotFoundError):
        await harness.get_learning_candidate(
            "lc-harness-1",
            context=other_tenant,
        )

    await harness.aclose()
    ledger.close()


@pytest.mark.asyncio
async def test_agent_default_agno_observer_reconciles_without_private_factory_access(
    tmp_path,
) -> None:
    policy = LearningProfile.institutional(
        namespace="research",
        knowledge=SimpleNamespace(vector_db=object()),
        promotion="reviewed",
    )
    ledger = SQLiteLearningLedger(tmp_path / "learning-default-observer.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts-default-observer")
    runtime = SQLiteRuntimeStore(tmp_path / "runtime-default-observer.db")
    runtime.create_run(
        RunSnapshot(
            run_id="run-default-observer",
            tenant_id="acme",
            user_id="analyst-1",
            session_id="session-1",
        )
    )
    context = ExecutionContext.create(
        tenant_id="acme",
        user_id="analyst-1",
        session_id="session-1",
        workspace_id=str(tmp_path / "workspace-default-observer"),
    )

    class ExactVector:
        present = False

        def name_exists(self, _name: str) -> bool:
            return self.present

    vector = ExactVector()

    class AmbiguousLearnedStore:
        knowledge = SimpleNamespace(vector_db=vector)

        async def asave(self, **_kwargs) -> bool:
            vector.present = True
            raise TimeoutError("private lost acknowledgement")

    machine = SimpleNamespace(learned_knowledge_store=AmbiguousLearnedStore())
    with (
        patch("agnoclaw.agent.Agent", return_value=MagicMock()),
        patch("agnoclaw.agent._make_db", return_value=MagicMock()),
        patch("agnoclaw.memory.build_learning_machine", return_value=machine),
    ):
        harness = AgentHarness(
            workspace_dir=tmp_path / "workspace-default-observer",
            config=HarnessConfig(),
            include_default_tools=False,
            learning=policy,
            runtime_store=runtime,
            artifact_store=artifacts,
            learning_ledger=ledger,
        )
        candidate = await harness.capture_learning_candidate(
            context=context,
            target=LearningTarget.LEARNED_KNOWLEDGE,
            content={"title": "Exact retry rule", "learning": "Retry only safe reads."},
            source_run_ids=["run-default-observer"],
            evidence_artifact_ids=["artifact-source-evidence"],
            confidence=0.91,
            risk=CandidateRisk.LOW,
            created_by=CandidateAuthor.AGENT,
            mechanism_version="reflector:v1",
            candidate_id="lc-default-observer",
        )
        await harness.evaluate_learning_candidate(
            candidate.candidate.candidate_id,
            context=context,
            verdict=EvaluationVerdict.QUALIFIED,
            evaluator_digest="sha256:" + "1" * 64,
            evidence_artifact_ids=("artifact-evaluation",),
            safety_passed=True,
            evaluated_by=PromotionActor.OPERATOR,
            mutation_id="evaluate-default-observer",
        )
        with pytest.raises(LearningPromotionUnknownError):
            await harness.promote_learning_candidate(
                candidate.candidate.candidate_id,
                context=context,
                actor=PromotionActor.OPERATOR,
                mutation_id="promote-default-observer",
            )

        worker = harness.build_learning_reconciliation_worker(
            context=context,
            reconciler_digest="sha256:" + "2" * 64,
            config=LearningReconciliationWorkerConfig(
                worker_id="default-agno-observer-test",
                poll_interval_seconds=0.01,
            ),
        )
        stats = await worker.run_once()

    assert (stats.claims, stats.items, stats.reconciled) == (1, 1, 1)
    reconciled = await harness.get_learning_candidate(
        candidate.candidate.candidate_id,
        context=context,
    )
    assert reconciled.state is CandidateState.PROMOTED
    assert "private lost acknowledgement" not in str(stats.to_dict())
    await harness.aclose()
    ledger.close()
    runtime.close()
    runtime.close()
