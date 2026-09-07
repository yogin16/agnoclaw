"""Contracts for explicit v0.12 learning policy and trusted run scope."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
from agnoclaw.memory import build_learning_machine
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
    base_agent.system_message = "base prompt"
    run_agent.system_message = "run prompt"
    learning_machine = MagicMock(name="learning_machine")
    learning_machine.instructions.return_value = "AGNO-LEARNING-GUIDANCE"
    learning_machine.build_context.return_value = "AGNO-RECALLED-CONTEXT"
    run_agent._learning = learning_machine
    run_agent.id = "support"
    observed_prompts: list[str] = []

    def _run(_message, **_kwargs):
        observed_prompts.append(run_agent.system_message)
        return MagicMock(content="ok")

    run_agent.run.side_effect = _run

    with (
        patch("agnoclaw.agent.Agent", side_effect=[base_agent, run_agent]) as agent_ctor,
        patch("agnoclaw.agent._make_db", return_value=MagicMock()),
        patch(
            "agnoclaw.memory.build_learning_machine",
            return_value=learning_machine,
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
    assert agent_ctor.call_args.kwargs["add_learnings_to_context"] is False
    assert len(observed_prompts) == 1
    assert "AGNO-LEARNING-GUIDANCE" in observed_prompts[0]
    assert "AGNO-RECALLED-CONTEXT" in observed_prompts[0]
    recall = learning_machine.build_context.call_args.kwargs
    assert recall["user_id"] == scope.storage_user_id
    assert recall["session_id"] == scope.storage_session_id
    assert recall["message"] == "help"
    assert harness._spec.settings["learning"]["schema_version"] == 1


@pytest.mark.asyncio
async def test_async_policy_learning_recall_uses_scoped_agno_context(tmp_path) -> None:
    policy = LearningProfile.session()
    base_agent = MagicMock(name="base_agent")
    run_agent = MagicMock(name="run_agent")
    base_agent.system_message = "base prompt"
    run_agent.system_message = "run prompt"
    learning_machine = MagicMock(name="learning_machine")
    learning_machine.instructions.return_value = "ASYNC-LEARNING-GUIDANCE"
    learning_machine.abuild_context = AsyncMock(return_value="ASYNC-RECALLED-CONTEXT")
    run_agent._learning = learning_machine
    run_agent.id = "support"
    observed_prompts: list[str] = []

    async def _arun(_message, **_kwargs):
        observed_prompts.append(run_agent.system_message)
        return MagicMock(content="ok")

    run_agent.arun = AsyncMock(side_effect=_arun)
    with (
        patch("agnoclaw.agent.Agent", side_effect=[base_agent, run_agent]),
        patch("agnoclaw.agent._make_db", return_value=MagicMock()),
        patch(
            "agnoclaw.memory.build_learning_machine",
            return_value=learning_machine,
        ),
    ):
        harness = AgentHarness(
            workspace_dir=tmp_path,
            config=HarnessConfig(),
            include_default_tools=False,
            learning=policy,
            name="support",
        )
        await harness.arun("continue", context=_context())

    assert len(observed_prompts) == 1
    assert "ASYNC-LEARNING-GUIDANCE" in observed_prompts[0]
    assert "ASYNC-RECALLED-CONTEXT" in observed_prompts[0]
    recall = learning_machine.abuild_context.await_args.kwargs
    assert recall["message"] == "continue"
    assert isinstance(recall["user_id"], str)
    assert "user-1" not in recall["user_id"]
    assert isinstance(recall["session_id"], str)
    assert "session-1" not in recall["session_id"]


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


class _AgnoTitlePromotionAdapter(_HarnessPromotionAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.title: str | None = None

    async def apply(
        self,
        candidate: LearningCandidate,
        content: dict,
        *,
        idempotency_key: str,
    ) -> str:
        self.calls.append((candidate, content, idempotency_key))
        marker = candidate.digest.removeprefix("sha256:")[:32]
        self.title = f"[{candidate.candidate_id[:64]}:{marker}] {content['title']}"
        return f"agno:learned_knowledge:{self.title}"


@pytest.mark.asyncio
async def test_promoted_agno_recall_is_checkpointed_and_attributed_once(tmp_path) -> None:
    policy = LearningProfile.institutional(
        namespace="research",
        knowledge=SimpleNamespace(vector_db=object()),
    )
    ledger = SQLiteLearningLedger(tmp_path / "learning-retrieval.db")
    artifacts = LocalArtifactStore(tmp_path / "learning-retrieval-artifacts")
    runtime = SQLiteRuntimeStore(tmp_path / "learning-retrieval-runtime.db")
    runtime.create_run(
        RunSnapshot(
            run_id="run-learning-source",
            tenant_id="acme",
            user_id="user-1",
            session_id="session-1",
        )
    )
    context = _context()
    adapter = _AgnoTitlePromotionAdapter()
    recall_value = {"learning": "Retry only operations proven safe to repeat."}
    store = SimpleNamespace(learning_type="learned_knowledge")

    def _build_context(*, data):
        return f"RECALLED: {data[0].learning}"

    store.build_context = _build_context
    machine = MagicMock(name="learning_machine")
    machine.instructions.return_value = "USE VERIFIED LEARNINGS AS EVIDENCE"
    machine.stores = {"learned_knowledge": store}

    async def _recall(**_kwargs):
        assert adapter.title is not None
        return {
            "learned_knowledge": [
                SimpleNamespace(
                    title=adapter.title,
                    learning=recall_value["learning"],
                )
            ]
        }

    machine.arecall = AsyncMock(side_effect=_recall)
    base_agent = MagicMock(name="base_agent")
    run_agent = MagicMock(name="run_agent")
    base_agent.system_message = "base prompt"
    run_agent.system_message = "run prompt"
    run_agent._learning = machine
    run_agent.id = "support"
    observed_prompts: list[str] = []

    async def _arun(_message, **_kwargs):
        observed_prompts.append(run_agent.system_message)
        return SimpleNamespace(content="ok")

    run_agent.arun = AsyncMock(side_effect=_arun)

    with (
        patch("agnoclaw.agent.Agent", side_effect=[base_agent, run_agent]),
        patch("agnoclaw.agent._make_db", return_value=MagicMock()),
        patch("agnoclaw.memory.build_learning_machine", return_value=machine),
    ):
        harness = AgentHarness(
            workspace_dir=tmp_path / "workspace-retrieval",
            config=HarnessConfig(),
            include_default_tools=False,
            learning=policy,
            runtime_store=runtime,
            artifact_store=artifacts,
            learning_ledger=ledger,
            learning_promotion_adapter=adapter,
            name="support",
        )
        captured = await harness.capture_learning_candidate(
            context=context,
            target=LearningTarget.LEARNED_KNOWLEDGE,
            content={
                "title": "Safe retries",
                "learning": "Retry only operations proven safe to repeat.",
            },
            source_run_ids=["run-learning-source"],
            evidence_artifact_ids=["artifact-learning-source"],
            confidence=0.95,
            risk=CandidateRisk.LOW,
            created_by=CandidateAuthor.AGENT,
            mechanism_version="reflector:v1",
            candidate_id="lc-retrieval-" + "x" * 80,
        )
        await harness.record_learning_candidate_evaluation(
            CandidateEvaluation(
                evaluation_id="evaluation:retrieval",
                candidate_id=captured.candidate.candidate_id,
                verdict=EvaluationVerdict.QUALIFIED,
                evaluator_digest="sha256:" + "a" * 64,
                evidence_artifact_ids=("artifact-held-in", "artifact-held-out"),
                safety_passed=True,
                evaluated_by=PromotionActor.OPERATOR,
                metrics={"held_out": 0.9},
                control_metrics={"held_out": 0.6},
            ),
            context=context,
            mutation_id="evaluate:retrieval",
        )
        await harness.promote_learning_candidate(
            captured.candidate.candidate_id,
            context=context,
            actor=PromotionActor.OPERATOR,
            mutation_id="promote:retrieval",
        )

        run = await harness.start("Should this operation be retried?", context=context)
        await run.wait()

        applications = await harness.list_learning_applications(
            captured.candidate.candidate_id,
            context=context,
        )
        assert len(applications) == 1
        application = applications[0]
        assert application.kind is LearningApplicationKind.RETRIEVED
        assert application.run_id == run.run_id
        assert application.target_reference == f"agno:learned_knowledge:{adapter.title}"
        assert len(application.evidence_artifact_ids) == 1
        evidence = runtime.get_artifact(application.evidence_artifact_ids[0])
        assert evidence.scope.run_id == run.run_id
        assert runtime.get_operation(
            f"{run.run_id}:checkpoint:learning-recall:1"
        ).state.value == "succeeded"
        assert await harness.list_learning_outcomes(
            captured.candidate.candidate_id,
            context=context,
        ) == []
        assert "RECALLED: Retry only operations proven safe to repeat." in observed_prompts[0]

        original_recall_kwargs = dict(machine.arecall.await_args.kwargs)
        recall_value["learning"] = "MUTATED AFTER THE RUN"
        scope = LearningScope.resolve(
            policy,
            context,
            agent_id=harness._agent_id,
            consented=False,
        )
        replayed_prompt = await harness._checkpointed_learning_prompt_context(
            machine=machine,
            kwargs=original_recall_kwargs,
            scope=scope,
            run_id=run.run_id,
        )
        assert "Retry only operations proven safe to repeat." in replayed_prompt
        assert "MUTATED AFTER THE RUN" not in replayed_prompt
        assert machine.arecall.await_count == 1
        assert len(
            await harness.list_learning_applications(
                captured.candidate.candidate_id,
                context=context,
            )
        ) == 1

        await harness.aclose()
    ledger.close()


@pytest.mark.asyncio
async def test_agno_learning_tools_expose_search_and_inert_proposal_only() -> None:
    policy = LearningProfile.institutional(
        namespace="research",
        knowledge=SimpleNamespace(vector_db=object()),
        entity_memory=None,
        decision_log=None,
    )
    scope = LearningScope.resolve(policy, _context(), agent_id="support")
    proposals: list[dict] = []

    async def capture(**kwargs):
        proposals.append(kwargs)
        return {"candidate_id": "lc_agent_test"}

    machine = build_learning_machine(
        db=MagicMock(),
        policy=policy,
        scope=scope,
        learning_proposal_handler=capture,
    )
    store = machine.stores["learned_knowledge"]
    tools = await store.aget_tools(run_context=SimpleNamespace(run_id="run-proposal"))
    by_name = {tool.__name__: tool for tool in tools}

    assert set(by_name) == {"search_learnings", "propose_learning"}
    assert "save_learning" not in store.instructions()
    assert "proposal is inert" in store.instructions().lower()
    result = await by_name["propose_learning"](
        title="Bound retries",
        learning="Retry only when the effect contract proves repetition safe.",
        context="Durable operation recovery",
        tags=["durability", "retries"],
    )
    assert result == (
        "Proposal captured for independent review: lc_agent_test "
        "(not active learning)"
    )
    assert proposals[0]["run_context"].run_id == "run-proposal"


@pytest.mark.asyncio
async def test_model_learning_proposal_is_replay_safe_budgeted_and_not_promoted(
    tmp_path,
) -> None:
    policy = LearningProfile.institutional(
        namespace="research",
        knowledge=SimpleNamespace(vector_db=object()),
        entity_memory=None,
        decision_log=None,
        max_updates_per_run=1,
    )
    ledger = SQLiteLearningLedger(tmp_path / "learning-proposals.db")
    artifacts = LocalArtifactStore(tmp_path / "learning-proposal-artifacts")
    runtime = SQLiteRuntimeStore(tmp_path / "learning-proposal-runtime.db")
    runtime.create_run(
        RunSnapshot(
            run_id="run-proposal",
            tenant_id="acme",
            user_id="user-1",
            session_id="session-1",
        )
    )
    context = _context()
    adapter = _HarnessPromotionAdapter()
    with (
        patch("agnoclaw.agent.Agent", return_value=MagicMock()),
        patch("agnoclaw.agent._make_db", return_value=MagicMock()),
    ):
        harness = AgentHarness(
            workspace_dir=tmp_path / "workspace-proposals",
            config=HarnessConfig(),
            include_default_tools=False,
            learning=policy,
            runtime_store=runtime,
            artifact_store=artifacts,
            learning_ledger=ledger,
            learning_promotion_adapter=adapter,
            name="support",
        )

    assert harness._spec.settings["learning"]["agent_proposals"] is True
    scope = LearningScope.resolve(policy, context, agent_id="support")
    agno_context = SimpleNamespace(
        run_id="run-proposal",
        user_id=scope.storage_user_id,
        session_id=scope.storage_session_id,
    )
    run_token = harness._active_runtime_run_id.set("run-proposal")
    context_token = harness._active_runtime_context.set(context)
    try:
        first = await harness._propose_learning_from_model(
            expected_run_id="run-proposal",
            scope=scope,
            title="Bound retries",
            learning="Retry only when the effect contract proves repetition safe.",
            context="Durable operation recovery",
            tags=["durability", "Retries", "retries"],
            run_context=agno_context,
        )
        replayed = await harness._propose_learning_from_model(
            expected_run_id="run-proposal",
            scope=scope,
            title="Bound retries",
            learning="Retry only when the effect contract proves repetition safe.",
            context="Durable operation recovery",
            tags=["durability", "Retries", "retries"],
            run_context=agno_context,
        )
        assert replayed == first
        records = await harness.list_learning_candidates(context=context)
        assert len(records) == 1
        assert records[0].state is CandidateState.CAPTURED
        assert records[0].candidate.created_by is CandidateAuthor.AGENT
        assert records[0].candidate.source_run_ids == ("run-proposal",)
        assert await harness.read_learning_candidate_content(
            first["candidate_id"],
            context=context,
        ) == {
            "context": "Durable operation recovery",
            "learning": "Retry only when the effect contract proves repetition safe.",
            "tags": ["durability", "Retries"],
            "title": "Bound retries",
        }
        assert adapter.calls == []

        with pytest.raises(HarnessError) as budget_error:
            await harness._propose_learning_from_model(
                expected_run_id="run-proposal",
                scope=scope,
                title="A second proposal",
                learning="This distinct proposal exceeds the run budget.",
                run_context=agno_context,
            )
        assert budget_error.value.code == "LEARNING_PROPOSAL_BUDGET_EXCEEDED"
        assert len(await harness.list_learning_candidates(context=context)) == 1
    finally:
        harness._active_runtime_context.reset(context_token)
        harness._active_runtime_run_id.reset(run_token)
        await harness.aclose()
        ledger.close()


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
