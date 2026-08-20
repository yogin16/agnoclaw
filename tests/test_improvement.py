"""Contracts for evidence-gated harness self-improvement."""

from dataclasses import FrozenInstanceError, replace

import pytest

from agnoclaw import (
    ChangeBudget,
    ChangeHypothesis,
    EvaluationGatePolicy,
    EvaluationResourceUsage,
    EvaluationSlice,
    EvaluationSliceResult,
    EvaluationVerdict,
    FailureCluster,
    FeedbackStrength,
    HarnessComponentClass,
    HarnessComponentManifest,
    HarnessError,
    ImprovementEvaluation,
    ImprovementEvaluationGate,
    ImprovementRole,
    JudgeAudit,
    ObjectiveVector,
    PairedQualityStatistic,
    ParetoEntry,
    PromotionActor,
    pareto_frontier,
)


def _sha(character: str) -> str:
    return "sha256:" + character * 64


def _manifest(*, path: str = "src/agnoclaw/agent.py") -> HarnessComponentManifest:
    return HarnessComponentManifest(
        component_id="agent-system-prompt",
        component_class=HarnessComponentClass.SYSTEM_PROMPT,
        version="12",
        implementation_digest=_sha("a"),
        editable_paths=(path,),
        rollback_reference="git:baseline",
        description="Agent system-prompt component.",
        metadata={"owner": "runtime"},
    )


def _cluster(*, causal_mechanism: str = "missing retry classification") -> FailureCluster:
    return FailureCluster(
        cluster_id="retry-failures",
        causal_mechanism=causal_mechanism,
        verifier_digest=_sha("b"),
        failure_run_ids=("run-1", "run-2"),
        evidence_artifact_ids=("artifact-cluster",),
        terminal_labels=("tool_error",),
        mechanism_version="clusterer:v1",
    )


def _hypothesis(
    manifest: HarnessComponentManifest,
    cluster: FailureCluster,
    *,
    proposed_by: ImprovementRole = ImprovementRole.GENERATOR,
    budget: ChangeBudget | None = None,
) -> ChangeHypothesis:
    return ChangeHypothesis(
        change_id="change-retry-prompt",
        target_component_ids=(manifest.component_id,),
        component_manifest_digests=(manifest.digest,),
        failure_cluster_ids=(cluster.cluster_id,),
        failure_cluster_digests=(cluster.digest,),
        evidence_artifact_ids=("artifact-hypothesis",),
        inferred_root_cause="The prompt omits effect-aware retry rules.",
        bounded_edit_surface=manifest.editable_paths,
        predicted_fixes=("classify repeatable reads before retry",),
        at_risk_regressions=("excessive refusal",),
        behaviors_to_preserve=("never retry an unknown non-repeatable effect",),
        previous_attempt_ids=(),
        model_config_digest=_sha("c"),
        evaluator_digest=_sha("d"),
        permission_digest=_sha("e"),
        proposer_identity_digest=_sha("6"),
        budget=budget
        or ChangeBudget(
            max_rollouts=100,
            max_tokens=100_000,
            max_wall_seconds=3_600,
            max_cost_usd=20,
        ),
        rollback_target="git:baseline",
        proposed_by=proposed_by,
        mechanism_version="generator:v1",
    )


def _slice_result(
    slice_name: EvaluationSlice,
    *,
    candidate: bool,
    quality: float | None = None,
    safety: float = 0.99,
    dataset_digest: str | None = None,
) -> EvaluationSliceResult:
    baseline_quality = {
        EvaluationSlice.HELD_IN: 0.70,
        EvaluationSlice.HELD_OUT: 0.75,
        EvaluationSlice.TRANSFER: 0.70,
    }[slice_name]
    candidate_quality = {
        EvaluationSlice.HELD_IN: 0.75,
        EvaluationSlice.HELD_OUT: 0.76,
        EvaluationSlice.TRANSFER: 0.71,
    }[slice_name]
    sample_count = 10 if slice_name is EvaluationSlice.TRANSFER else 20
    digest_character = {
        EvaluationSlice.HELD_IN: "1",
        EvaluationSlice.HELD_OUT: "2",
        EvaluationSlice.TRANSFER: "3",
    }[slice_name]
    label = "candidate" if candidate else "baseline"
    return EvaluationSliceResult(
        slice=slice_name,
        task_class=f"{slice_name.value}-tasks",
        dataset_digest=dataset_digest or _sha(digest_character),
        verifier_digest=_sha("f"),
        sample_count=sample_count,
        quality=(candidate_quality if candidate else baseline_quality)
        if quality is None
        else quality,
        safety=safety,
        cost_usd=1.1 if candidate else 1.0,
        latency_seconds=2.1 if candidate else 2.0,
        objective_fraction=0.9,
        evidence_artifact_ids=(f"artifact-{label}-{slice_name.value}",),
    )


def _report(
    hypothesis: ChangeHypothesis,
    *,
    feedback_strength: FeedbackStrength = FeedbackStrength.OBJECTIVE,
    held_out_quality: float | None = None,
    held_out_dataset_digest: str | None = None,
    judge_audit: JudgeAudit | None = None,
    usage: EvaluationResourceUsage | None = None,
    evaluator_digest: str | None = None,
    evaluator_identity_digest: str = "sha256:" + "7" * 64,
    governed_corpus: bool = True,
) -> ImprovementEvaluation:
    baseline = tuple(_slice_result(slice_name, candidate=False) for slice_name in EvaluationSlice)
    candidate = tuple(
        _slice_result(
            slice_name,
            candidate=True,
            quality=(held_out_quality if slice_name is EvaluationSlice.HELD_OUT else None),
            dataset_digest=(
                held_out_dataset_digest if slice_name is EvaluationSlice.HELD_OUT else None
            ),
        )
        for slice_name in EvaluationSlice
    )
    return ImprovementEvaluation(
        candidate_id="learning-candidate-1",
        hypothesis_digest=hypothesis.digest,
        baseline=baseline,
        candidate=candidate,
        evaluator_digest=evaluator_digest or hypothesis.evaluator_digest,
        evaluator_identity_digest=evaluator_identity_digest,
        model_config_digest=hypothesis.model_config_digest,
        permission_digest=hypothesis.permission_digest,
        feedback_strength=feedback_strength,
        usage=usage
        or EvaluationResourceUsage(
            rollouts=60,
            tokens=60_000,
            wall_seconds=600,
            cost_usd=6,
        ),
        safety_passed=True,
        privacy_passed=True,
        novelty_score=0.7,
        diversity_score=0.8,
        added_complexity=0.1,
        judge_audit=judge_audit or JudgeAudit(),
        corpus_manifest_digest=_sha("8") if governed_corpus else None,
        corpus_evidence_artifact_ids=(
            ("artifact-corpus-source", "artifact-corpus-decontamination")
            if governed_corpus
            else ()
        ),
    )


def _evaluate(report: ImprovementEvaluation, hypothesis: ChangeHypothesis):
    manifest = _manifest()
    cluster = _cluster()
    assert manifest.digest == hypothesis.component_manifest_digests[0]
    assert cluster.digest == hypothesis.failure_cluster_digests[0]
    return ImprovementEvaluationGate().evaluate(
        report,
        hypothesis=hypothesis,
        manifests=(manifest,),
        failure_clusters=(cluster,),
    )


def test_component_taxonomy_manifest_and_paths_are_frozen() -> None:
    assert {item.value for item in HarnessComponentClass} == {
        "system_prompt",
        "tool_description",
        "tool_implementation",
        "middleware",
        "skill",
        "subagent_configuration",
        "long_term_memory",
    }
    manifest = _manifest()
    with pytest.raises(FrozenInstanceError):
        manifest.version = "13"  # type: ignore[misc]
    with pytest.raises(TypeError):
        manifest.metadata["owner"] = "attacker"

    caller_paths = ["src/agnoclaw/agent.py"]
    copied = HarnessComponentManifest(
        component_id="copied",
        component_class=HarnessComponentClass.MIDDLEWARE,
        version="1",
        implementation_digest=_sha("4"),
        editable_paths=caller_paths,  # type: ignore[arg-type]
        rollback_reference="git:baseline",
        description="List input is defensively frozen.",
    )
    caller_paths.append("src/agnoclaw/other.py")
    assert copied.editable_paths == ("src/agnoclaw/agent.py",)

    for path in (".", "../agent.py", "/tmp/agent.py", "src//agent.py"):
        with pytest.raises(HarnessError) as error:
            _manifest(path=path)
        assert error.value.code == "IMPROVEMENT_EDIT_SURFACE_INVALID"


def test_failure_cluster_requires_a_causal_mechanism() -> None:
    with pytest.raises(HarnessError) as error:
        _cluster(causal_mechanism="tool_error")
    assert error.value.code == "IMPROVEMENT_CAUSAL_CLUSTER_INVALID"


def test_hypothesis_binds_exact_manifests_clusters_and_roles() -> None:
    manifest = _manifest()
    cluster = _cluster()
    hypothesis = _hypothesis(manifest, cluster)
    hypothesis.verify_manifests((manifest,))
    hypothesis.verify_failure_clusters((cluster,))

    changed_manifest = HarnessComponentManifest(
        component_id=manifest.component_id,
        component_class=manifest.component_class,
        version="13",
        implementation_digest=manifest.implementation_digest,
        editable_paths=manifest.editable_paths,
        rollback_reference=manifest.rollback_reference,
        description=manifest.description,
    )
    with pytest.raises(HarnessError) as digest_error:
        hypothesis.verify_manifests((changed_manifest,))
    assert digest_error.value.code == "IMPROVEMENT_MANIFEST_DIGEST_MISMATCH"

    with pytest.raises(HarnessError) as role_error:
        _hypothesis(manifest, cluster, proposed_by=ImprovementRole.EVALUATOR)
    assert role_error.value.code == "IMPROVEMENT_ROLE_CONFLICT"


def test_gate_qualifies_frozen_non_regressing_evidence() -> None:
    manifest = _manifest()
    cluster = _cluster()
    hypothesis = _hypothesis(manifest, cluster)
    report = _report(hypothesis)

    decision = _evaluate(report, hypothesis)

    assert decision.qualified is True
    assert decision.verdict is EvaluationVerdict.QUALIFIED
    assert decision.reasons == ()
    assert decision.hypothesis_digest == hypothesis.digest
    assert decision.policy_digest == EvaluationGatePolicy().digest
    assert len(decision.evidence_artifact_ids) == 10
    assert "artifact-cluster" in decision.evidence_artifact_ids
    assert "artifact-corpus-decontamination" in decision.evidence_artifact_ids

    candidate_evaluation = decision.to_candidate_evaluation(
        evaluation_id="evaluation-1",
        evaluated_by=PromotionActor.OPERATOR,
    )
    assert candidate_evaluation.candidate_id == report.candidate_id
    assert candidate_evaluation.evaluator_digest == hypothesis.evaluator_digest
    assert candidate_evaluation.safety_passed is True
    assert candidate_evaluation.metrics["gate"]["evaluation_digest"] == (decision.evaluation_digest)


def test_hard_regression_rejects_even_with_heuristic_uncertainty() -> None:
    manifest = _manifest()
    cluster = _cluster()
    hypothesis = _hypothesis(manifest, cluster)
    report = _report(
        hypothesis,
        feedback_strength=FeedbackStrength.HEURISTIC,
        held_out_quality=0.70,
    )

    decision = _evaluate(report, hypothesis)

    assert decision.verdict is EvaluationVerdict.REJECTED
    assert "held_out_quality_regression" in decision.reasons
    assert "heuristic_feedback_only" in decision.reasons


def test_gate_requires_governed_corpus_by_default_with_explicit_legacy_opt_out() -> None:
    manifest = _manifest()
    cluster = _cluster()
    hypothesis = _hypothesis(manifest, cluster)
    report = _report(hypothesis, governed_corpus=False)

    decision = _evaluate(report, hypothesis)
    assert decision.verdict is EvaluationVerdict.REJECTED
    assert "governed_corpus_required" in decision.reasons

    compatibility = ImprovementEvaluationGate(
        EvaluationGatePolicy(require_governed_corpus=False)
    ).evaluate(
        report,
        hypothesis=hypothesis,
        manifests=(manifest,),
        failure_clusters=(cluster,),
    )
    assert compatibility.verdict is EvaluationVerdict.QUALIFIED


def test_judge_uncertainty_is_inconclusive_and_auditable() -> None:
    manifest = _manifest()
    cluster = _cluster()
    hypothesis = _hypothesis(manifest, cluster)
    audit = JudgeAudit(
        used=True,
        balanced_order=False,
        disagreement_rate=0.3,
        disagreements_reviewed=False,
        judge_model_digest=_sha("7"),
        judge_prompt_digest=_sha("8"),
        calibration_artifact_ids=("artifact-judge-calibration",),
    )

    decision = _evaluate(_report(hypothesis, judge_audit=audit), hypothesis)

    assert decision.verdict is EvaluationVerdict.INCONCLUSIVE
    assert set(decision.reasons) == {
        "judge_order_unbalanced",
        "judge_disagreement_unreviewed",
    }
    assert "artifact-judge-calibration" in decision.evidence_artifact_ids


def test_control_plane_drift_and_budget_overrun_invalidate_evaluation() -> None:
    manifest = _manifest()
    cluster = _cluster()
    hypothesis = _hypothesis(manifest, cluster)

    with pytest.raises(HarnessError) as drift_error:
        _evaluate(_report(hypothesis, evaluator_digest=_sha("9")), hypothesis)
    assert drift_error.value.code == "IMPROVEMENT_CONTROL_PLANE_DRIFT"

    with pytest.raises(HarnessError) as identity_error:
        _evaluate(
            _report(
                hypothesis,
                evaluator_identity_digest=hypothesis.proposer_identity_digest,
            ),
            hypothesis,
        )
    assert identity_error.value.code == "IMPROVEMENT_EVALUATOR_INDEPENDENCE_REQUIRED"

    over_budget = EvaluationResourceUsage(
        rollouts=101,
        tokens=60_000,
        wall_seconds=600,
        cost_usd=6,
    )
    with pytest.raises(HarnessError) as budget_error:
        _evaluate(_report(hypothesis, usage=over_budget), hypothesis)
    assert budget_error.value.code == "IMPROVEMENT_BUDGET_EXCEEDED"
    assert budget_error.value.details["dimensions"] == ["rollouts"]


def test_dataset_drift_is_rejected() -> None:
    manifest = _manifest()
    cluster = _cluster()
    hypothesis = _hypothesis(manifest, cluster)

    decision = _evaluate(
        _report(hypothesis, held_out_dataset_digest=_sha("0")),
        hypothesis,
    )

    assert decision.verdict is EvaluationVerdict.REJECTED
    assert "held_out_dataset_mismatch" in decision.reasons


def test_paired_confidence_is_fail_closed_when_aggregate_means_look_safe() -> None:
    manifest = _manifest()
    cluster = _cluster()
    hypothesis = _hypothesis(manifest, cluster)
    report = _report(hypothesis)
    statistics = tuple(
        PairedQualityStatistic(
            slice=slice_name,
            sample_count=10 if slice_name is EvaluationSlice.TRANSFER else 20,
            mean_delta=(0.05 if slice_name is EvaluationSlice.HELD_IN else 0.01),
            confidence_low=(-0.02 if slice_name is EvaluationSlice.HELD_OUT else 0.01),
            confidence_high=(0.06 if slice_name is EvaluationSlice.HELD_OUT else 0.09),
            wins=10 if slice_name is EvaluationSlice.TRANSFER else 20,
            ties=0,
            losses=0,
        )
        for slice_name in EvaluationSlice
    )
    report = replace(
        report,
        paired_statistics=statistics,
        runner_digest=_sha("9"),
    )

    decision = _evaluate(report, hypothesis)

    assert decision.verdict is EvaluationVerdict.REJECTED
    assert "held_out_confidence_regression" in decision.reasons


def test_pareto_frontier_retains_tradeoffs_without_scalarization() -> None:
    dominant = ParetoEntry(
        candidate_id="candidate-a",
        hypothesis_digest=_sha("a"),
        objective=ObjectiveVector(0.9, 0.9, 1.0, 1.0, 0.1),
        evaluation_digest=_sha("1"),
    )
    dominated = ParetoEntry(
        candidate_id="candidate-b",
        hypothesis_digest=_sha("b"),
        objective=ObjectiveVector(0.8, 0.8, 2.0, 2.0, 0.2),
        evaluation_digest=_sha("2"),
    )
    quality_tradeoff = ParetoEntry(
        candidate_id="candidate-c",
        hypothesis_digest=_sha("c"),
        objective=ObjectiveVector(0.95, 0.9, 2.0, 1.0, 0.1),
        evaluation_digest=_sha("3"),
    )

    assert pareto_frontier((dominated, quality_tradeoff, dominant)) == (
        dominant,
        quality_tradeoff,
    )


def test_pareto_frontier_rejects_conflicting_evaluation_identity() -> None:
    first = ParetoEntry(
        candidate_id="candidate-a",
        hypothesis_digest=_sha("a"),
        objective=ObjectiveVector(0.9, 0.9, 1.0, 1.0, 0.1),
        evaluation_digest=_sha("1"),
    )
    conflict = ParetoEntry(
        candidate_id="candidate-a",
        hypothesis_digest=_sha("a"),
        objective=ObjectiveVector(0.8, 0.9, 1.0, 1.0, 0.1),
        evaluation_digest=_sha("1"),
    )
    with pytest.raises(HarnessError) as error:
        pareto_frontier((first, conflict))
    assert error.value.code == "IMPROVEMENT_PARETO_ENTRY_CONFLICT"
