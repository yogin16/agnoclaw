"""Executable paired-evaluation contracts for harness self-improvement."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agnoclaw import (
    IMPROVEMENT_RUNNER_IMPLEMENTATION_DIGEST,
    ArtifactReference,
    ArtifactScope,
    ChangeBudget,
    ChangeHypothesis,
    EvaluationCase,
    EvaluationCaseExposure,
    EvaluationCorpusEntry,
    EvaluationCorpusManifest,
    EvaluationGatePolicy,
    EvaluationRollout,
    EvaluationScore,
    EvaluationSlice,
    EvaluationVerdict,
    FailureCluster,
    HarnessComponentClass,
    HarnessComponentManifest,
    HarnessError,
    ImprovementEvaluationGate,
    ImprovementEvaluationRunner,
    ImprovementRole,
    LocalArtifactStore,
    evaluation_corpus_case_set_digest,
    process_evaluation_subject_factory,
)

PROCESS_WORKER = Path(__file__).parent / "fixtures" / "improvement_process_worker.py"


def _sha(character: str) -> str:
    return "sha256:" + character * 64


async def _experiment_inputs(
    tmp_path: Path,
    *,
    max_rollouts: int = 20,
) -> tuple[
    LocalArtifactStore,
    ArtifactScope,
    HarnessComponentManifest,
    FailureCluster,
    ChangeHypothesis,
    tuple[ArtifactReference, ...],
]:
    store = LocalArtifactStore(tmp_path / "artifacts")
    scope = ArtifactScope(run_id="evaluation-run", tenant_id="tenant-1", user_id="user-1")
    hypothesis_evidence = await store.stage_json(
        {"evidence": "hypothesis"},
        scope=scope,
        purpose="improvement_hypothesis_evidence",
    )
    cluster_evidence = await store.stage_json(
        {"evidence": "cluster"},
        scope=scope,
        purpose="improvement_cluster_evidence",
    )
    manifest = HarnessComponentManifest(
        component_id="retry-prompt",
        component_class=HarnessComponentClass.SYSTEM_PROMPT,
        version="1",
        implementation_digest=_sha("a"),
        editable_paths=("src/agnoclaw/prompts/retry.md",),
        rollback_reference="git:baseline",
        description="Effect-aware retry prompt.",
    )
    cluster = FailureCluster(
        cluster_id="unsafe-retry",
        causal_mechanism="missing non-repeatable effect classification",
        verifier_digest=_sha("b"),
        failure_run_ids=("run-1",),
        evidence_artifact_ids=(cluster_evidence.artifact_id,),
        terminal_labels=("tool_error",),
        mechanism_version="clusterer:v1",
    )
    hypothesis = ChangeHypothesis(
        change_id="retry-prompt-v2",
        target_component_ids=(manifest.component_id,),
        component_manifest_digests=(manifest.digest,),
        failure_cluster_ids=(cluster.cluster_id,),
        failure_cluster_digests=(cluster.digest,),
        evidence_artifact_ids=(hypothesis_evidence.artifact_id,),
        inferred_root_cause="The prompt omits the governed effect classification.",
        bounded_edit_surface=manifest.editable_paths,
        predicted_fixes=("classify before retry",),
        at_risk_regressions=("unnecessary refusal",),
        behaviors_to_preserve=("never replay unknown effects",),
        previous_attempt_ids=(),
        model_config_digest=_sha("c"),
        evaluator_digest=_sha("d"),
        permission_digest=_sha("e"),
        proposer_identity_digest=_sha("6"),
        budget=ChangeBudget(
            max_rollouts=max_rollouts,
            max_tokens=100,
            max_wall_seconds=30,
            max_cost_usd=10,
        ),
        rollback_target="git:baseline",
        proposed_by=ImprovementRole.GENERATOR,
        mechanism_version="generator:v1",
    )
    return (
        store,
        scope,
        manifest,
        cluster,
        hypothesis,
        (hypothesis_evidence, cluster_evidence),
    )


def _cases(*, per_slice: int = 2) -> tuple[EvaluationCase, ...]:
    values: list[EvaluationCase] = []
    for slice_name in EvaluationSlice:
        for index in range(per_slice):
            values.append(
                EvaluationCase(
                    case_id=f"{slice_name.value}-{index}",
                    slice=slice_name,
                    task_class=f"{slice_name.value}-tasks",
                    payload={
                        "baseline_quality": 0.5,
                        "index": index,
                        "slice": slice_name.value,
                    },
                )
            )
    return tuple(values)


async def _corpus(
    store: LocalArtifactStore,
    scope: ArtifactScope,
    cases: tuple[EvaluationCase, ...],
    *,
    known_overlap_case_ids: list[str] | None = None,
    valid_source: bool = True,
) -> tuple[EvaluationCorpusManifest, tuple[ArtifactReference, ...]]:
    source = await store.stage_json(
        {
            "type": "agnoclaw.evaluation_corpus_source",
            "schema_version": "1.0",
            "source_id": "reviewed-incident-response-fixtures",
            "source_digest": _sha("b"),
            "usage_basis": "internal_authorized",
            **({"retention_policy_digest": _sha("c")} if valid_source else {}),
        },
        scope=scope,
        purpose="evaluation_corpus_source",
    )
    entries = tuple(
        EvaluationCorpusEntry.from_case(
            case,
            lineage_digest=_sha(format(index + 1, "x")),
            source_artifact_id=source.artifact_id,
            exposure=(
                EvaluationCaseExposure.DEVELOPMENT
                if case.slice is EvaluationSlice.HELD_IN
                else EvaluationCaseExposure.SEALED
            ),
        )
        for index, case in enumerate(cases)
    )
    method_digest = _sha("9")
    curator_digest = _sha("8")
    decontamination = await store.stage_json(
        {
            "type": "agnoclaw.evaluation_corpus_decontamination",
            "schema_version": "1.0",
            "case_set_digest": evaluation_corpus_case_set_digest(entries),
            "method_digest": method_digest,
            "checked_case_count": len(entries),
            "reviewer_identity_digest": curator_digest,
            "comparison_corpus_digests": [_sha("a")],
            "known_overlap_case_ids": known_overlap_case_ids or [],
            "unresolved_case_ids": [],
        },
        scope=scope,
        purpose="evaluation_corpus_decontamination",
    )
    return (
        EvaluationCorpusManifest(
            corpus_id="incident-response-v1",
            version="1",
            entries=entries,
            selection_policy_digest=_sha("4"),
            sampling_seed_digest=_sha("5"),
            sealed_access_policy_digest=_sha("6"),
            decontamination_method_digest=method_digest,
            decontamination_artifact_id=decontamination.artifact_id,
            curator_identity_digest=curator_digest,
        ),
        (source, decontamination),
    )


class _Subject:
    def __init__(
        self,
        delta: Callable[[EvaluationCase], float],
        lifecycle: dict[str, int],
        *,
        delay: float = 0,
    ) -> None:
        self.delta = delta
        self.lifecycle = lifecycle
        self.delay = delay

    async def asetup(self) -> None:
        self.lifecycle["setup"] += 1

    async def __call__(self, case: EvaluationCase) -> EvaluationRollout:
        if self.delay:
            await asyncio.sleep(self.delay)
        return EvaluationRollout(
            output={
                "quality": float(case.payload["baseline_quality"]) + self.delta(case),
            },
            tokens=1,
            cost_usd=0.01,
        )

    async def aclose(self) -> None:
        self.lifecycle["close"] += 1


def _factory(
    delta: Callable[[EvaluationCase], float],
    lifecycle: dict[str, int],
    *,
    delay: float = 0,
) -> Callable[[], _Subject]:
    def create() -> _Subject:
        lifecycle["created"] += 1
        return _Subject(delta, lifecycle, delay=delay)

    return create


def _verifier(_case: EvaluationCase, rollout: EvaluationRollout) -> EvaluationScore:
    return EvaluationScore(
        quality=float(rollout.output["quality"]),
        safety=0.99,
        safety_passed=True,
        privacy_passed=True,
    )


async def test_runner_executes_fresh_balanced_pairs_and_qualifies_evidence(
    tmp_path: Path,
) -> None:
    store, scope, manifest, cluster, hypothesis, evidence = await _experiment_inputs(tmp_path)
    lifecycle = {"created": 0, "setup": 0, "close": 0}
    baseline_factory = _factory(lambda _case: 0.0, lifecycle)
    candidate_factory = _factory(
        lambda case: 0.2 if case.slice is EvaluationSlice.HELD_IN else 0.0,
        lifecycle,
    )
    runner = ImprovementEvaluationRunner(
        store,
        artifact_scope=scope,
        evaluator_identity_digest=_sha("7"),
        per_rollout_timeout=1,
    )
    cases = _cases()
    corpus, corpus_evidence = await _corpus(store, scope, cases)

    result = await runner.run(
        candidate_id="candidate-1",
        hypothesis=hypothesis,
        manifests=(manifest,),
        failure_clusters=(cluster,),
        cases=cases,
        baseline_factory=baseline_factory,
        candidate_factory=candidate_factory,
        baseline_digest=_sha("1"),
        candidate_digest=_sha("2"),
        verifier=_verifier,
        upstream_artifacts=reversed(evidence + corpus_evidence),
        novelty_score=0.5,
        diversity_score=0.5,
        added_complexity=0.1,
        corpus_manifest=corpus,
    )

    assert result.execution_order_balanced is True
    assert len(result.case_artifacts) == 6
    assert len(result.evidence_artifacts) == 10
    assert lifecycle == {"created": 12, "setup": 12, "close": 12}
    assert result.report.usage.rollouts == 12
    assert result.report.usage.tokens == 12
    assert result.report.usage.cost_usd == pytest.approx(0.12)
    assert result.report.runner_digest is not None
    assert result.report.corpus_manifest_digest == corpus.digest
    assert set(result.report.corpus_evidence_artifact_ids) == {
        item.artifact_id for item in corpus_evidence
    }
    assert {item.slice for item in result.report.paired_statistics} == set(EvaluationSlice)
    held_in = next(
        item
        for item in result.report.paired_statistics
        if item.slice is EvaluationSlice.HELD_IN
    )
    assert held_in.mean_delta == pytest.approx(0.2)
    assert held_in.confidence_low == pytest.approx(0.2)
    first = await store.load_json(result.case_artifacts[0])
    second = await store.load_json(result.case_artifacts[1])
    assert first["execution_order"] == ["baseline", "candidate"]
    assert second["execution_order"] == ["candidate", "baseline"]
    assert first["runner_digest"] == result.report.runner_digest
    assert first["runner_implementation_digest"] == (
        IMPROVEMENT_RUNNER_IMPLEMENTATION_DIGEST
    )

    decision = ImprovementEvaluationGate(
        EvaluationGatePolicy(
            min_held_in_samples=2,
            min_held_out_samples=2,
            min_transfer_samples=2,
            max_latency_ratio=1_000,
        )
    ).evaluate(
        result.report,
        hypothesis=hypothesis,
        manifests=(manifest,),
        failure_clusters=(cluster,),
    )
    assert decision.verdict is EvaluationVerdict.QUALIFIED
    assert decision.qualified is True
    assert set(decision.evidence_artifact_ids) == {
        *(item.artifact_id for item in evidence),
        *(item.artifact_id for item in corpus_evidence),
        *(item.artifact_id for item in result.case_artifacts),
    }


async def test_runner_binds_fresh_process_contracts_into_report_and_case_evidence(
    tmp_path: Path,
) -> None:
    store, scope, manifest, cluster, hypothesis, evidence = await _experiment_inputs(tmp_path)
    runner = ImprovementEvaluationRunner(
        store,
        artifact_scope=scope,
        evaluator_identity_digest=_sha("7"),
        per_rollout_timeout=2,
    )
    baseline_factory = process_evaluation_subject_factory(
        (sys.executable, str(PROCESS_WORKER), "baseline")
    )
    candidate_factory = process_evaluation_subject_factory(
        (sys.executable, str(PROCESS_WORKER), "candidate")
    )

    result = await runner.run(
        candidate_id="candidate-process-isolated",
        hypothesis=hypothesis,
        manifests=(manifest,),
        failure_clusters=(cluster,),
        cases=_cases(per_slice=1),
        baseline_factory=baseline_factory,
        candidate_factory=candidate_factory,
        baseline_digest=_sha("1"),
        candidate_digest=_sha("2"),
        verifier=_verifier,
        upstream_artifacts=evidence,
        novelty_score=0.5,
        diversity_score=0.5,
        added_complexity=0.1,
    )

    assert result.report.baseline_subject_contract_digest is not None
    assert result.report.candidate_subject_contract_digest is not None
    assert (
        result.report.baseline_subject_contract_digest
        != result.report.candidate_subject_contract_digest
    )
    assert result.report.subject_isolation_digest is not None
    pids: set[int] = set()
    for reference in result.case_artifacts:
        artifact = await store.load_json(reference)
        assert artifact["baseline_subject_contract_digest"] == (
            result.report.baseline_subject_contract_digest
        )
        assert artifact["candidate_subject_contract_digest"] == (
            result.report.candidate_subject_contract_digest
        )
        assert artifact["subject_isolation_digest"] == (
            result.report.subject_isolation_digest
        )
        pids.add(artifact["baseline"]["rollout"]["output"]["pid"])
        pids.add(artifact["candidate"]["rollout"]["output"]["pid"])
    assert len(pids) == 6


async def test_runner_rejects_asymmetric_or_malformed_subject_contracts(
    tmp_path: Path,
) -> None:
    store, scope, manifest, cluster, hypothesis, evidence = await _experiment_inputs(tmp_path)
    lifecycle = {"created": 0, "setup": 0, "close": 0}
    runner = ImprovementEvaluationRunner(
        store,
        artifact_scope=scope,
        evaluator_identity_digest=_sha("7"),
    )
    process_factory = process_evaluation_subject_factory(
        (sys.executable, str(PROCESS_WORKER), "baseline")
    )

    with pytest.raises(HarnessError) as asymmetric:
        await runner.run(
            candidate_id="candidate-asymmetric",
            hypothesis=hypothesis,
            manifests=(manifest,),
            failure_clusters=(cluster,),
            cases=_cases(),
            baseline_factory=process_factory,
            candidate_factory=_factory(lambda _case: 0, lifecycle),
            baseline_digest=_sha("1"),
            candidate_digest=_sha("2"),
            verifier=_verifier,
            upstream_artifacts=evidence,
            novelty_score=0.5,
            diversity_score=0.5,
            added_complexity=0.1,
        )
    assert asymmetric.value.code == "IMPROVEMENT_SUBJECT_CONTRACT_INVALID"
    assert asymmetric.value.details == {"phase": "factory_contract"}
    assert lifecycle["created"] == 0

    with pytest.raises(HarnessError) as isolation_mismatch:
        await runner.run(
            candidate_id="candidate-isolation-mismatch",
            hypothesis=hypothesis,
            manifests=(manifest,),
            failure_clusters=(cluster,),
            cases=_cases(),
            baseline_factory=process_factory,
            candidate_factory=process_evaluation_subject_factory(
                (sys.executable, str(PROCESS_WORKER), "candidate"),
                max_stdout_bytes=2 * 1024 * 1024,
            ),
            baseline_digest=_sha("1"),
            candidate_digest=_sha("2"),
            verifier=_verifier,
            upstream_artifacts=evidence,
            novelty_score=0.5,
            diversity_score=0.5,
            added_complexity=0.1,
        )
    assert isolation_mismatch.value.code == "IMPROVEMENT_SUBJECT_CONTRACT_INVALID"
    assert isolation_mismatch.value.details == {"phase": "factory_contract"}

    class MalformedFactory:
        subject_contract_digest = "not-a-digest"

        def __call__(self) -> _Subject:
            return _Subject(lambda _case: 0, lifecycle)

    with pytest.raises(HarnessError) as malformed:
        await runner.run(
            candidate_id="candidate-malformed-contract",
            hypothesis=hypothesis,
            manifests=(manifest,),
            failure_clusters=(cluster,),
            cases=_cases(),
            baseline_factory=MalformedFactory(),
            candidate_factory=MalformedFactory(),
            baseline_digest=_sha("1"),
            candidate_digest=_sha("2"),
            verifier=_verifier,
            upstream_artifacts=evidence,
            novelty_score=0.5,
            diversity_score=0.5,
            added_complexity=0.1,
        )
    assert malformed.value.code == "IMPROVEMENT_SUBJECT_CONTRACT_INVALID"
    assert malformed.value.details == {"phase": "factory_contract"}


async def test_runner_records_process_timeout_as_negative_evidence_and_reaps_child(
    tmp_path: Path,
) -> None:
    store, scope, manifest, cluster, hypothesis, evidence = await _experiment_inputs(tmp_path)
    runner = ImprovementEvaluationRunner(
        store,
        artifact_scope=scope,
        evaluator_identity_digest=_sha("7"),
        per_rollout_timeout=0.05,
    )
    result = await runner.run(
        candidate_id="candidate-process-timeout",
        hypothesis=hypothesis,
        manifests=(manifest,),
        failure_clusters=(cluster,),
        cases=_cases(per_slice=1),
        baseline_factory=process_evaluation_subject_factory(
            (sys.executable, str(PROCESS_WORKER), "baseline")
        ),
        candidate_factory=process_evaluation_subject_factory(
            (sys.executable, str(PROCESS_WORKER), "sleep")
        ),
        baseline_digest=_sha("1"),
        candidate_digest=_sha("2"),
        verifier=_verifier,
        upstream_artifacts=evidence,
        novelty_score=0.5,
        diversity_score=0.5,
        added_complexity=0.1,
    )

    assert result.report.safety_passed is False
    assert result.report.privacy_passed is False
    for reference in result.case_artifacts:
        artifact = await store.load_json(reference)
        assert artifact["candidate"]["rollout"] is None
        assert artifact["candidate"]["error_type"] == "TimeoutError"


async def test_runner_rejects_corpus_contamination_before_subject_execution(
    tmp_path: Path,
) -> None:
    store, scope, manifest, cluster, hypothesis, evidence = await _experiment_inputs(tmp_path)
    cases = _cases()
    corpus, corpus_evidence = await _corpus(
        store,
        scope,
        cases,
        known_overlap_case_ids=[cases[0].case_id],
    )
    lifecycle = {"created": 0, "setup": 0, "close": 0}
    runner = ImprovementEvaluationRunner(
        store,
        artifact_scope=scope,
        evaluator_identity_digest=_sha("7"),
    )

    with pytest.raises(HarnessError) as error:
        await runner.run(
            candidate_id="candidate-contaminated",
            hypothesis=hypothesis,
            manifests=(manifest,),
            failure_clusters=(cluster,),
            cases=cases,
            baseline_factory=_factory(lambda _case: 0, lifecycle),
            candidate_factory=_factory(lambda _case: 0, lifecycle),
            baseline_digest=_sha("1"),
            candidate_digest=_sha("2"),
            verifier=_verifier,
            upstream_artifacts=evidence + corpus_evidence,
            novelty_score=0.5,
            diversity_score=0.5,
            added_complexity=0.1,
            corpus_manifest=corpus,
        )

    assert error.value.code == "IMPROVEMENT_CORPUS_CONTAMINATION_DETECTED"
    assert error.value.details == {"known_count": 1, "unresolved_count": 0}
    assert lifecycle == {"created": 0, "setup": 0, "close": 0}


async def test_runner_rejects_malformed_corpus_provenance_before_subject_execution(
    tmp_path: Path,
) -> None:
    store, scope, manifest, cluster, hypothesis, evidence = await _experiment_inputs(tmp_path)
    cases = _cases()
    corpus, corpus_evidence = await _corpus(store, scope, cases, valid_source=False)
    lifecycle = {"created": 0, "setup": 0, "close": 0}
    runner = ImprovementEvaluationRunner(
        store,
        artifact_scope=scope,
        evaluator_identity_digest=_sha("7"),
    )

    with pytest.raises(HarnessError) as error:
        await runner.run(
            candidate_id="candidate-unprovenanced",
            hypothesis=hypothesis,
            manifests=(manifest,),
            failure_clusters=(cluster,),
            cases=cases,
            baseline_factory=_factory(lambda _case: 0, lifecycle),
            candidate_factory=_factory(lambda _case: 0, lifecycle),
            baseline_digest=_sha("1"),
            candidate_digest=_sha("2"),
            verifier=_verifier,
            upstream_artifacts=evidence + corpus_evidence,
            novelty_score=0.5,
            diversity_score=0.5,
            added_complexity=0.1,
            corpus_manifest=corpus,
        )

    assert error.value.code == "IMPROVEMENT_CORPUS_EVIDENCE_INVALID"
    assert error.value.details == {"reason": "source_schema"}
    assert lifecycle == {"created": 0, "setup": 0, "close": 0}


async def test_runner_verifies_exact_upstream_artifacts_before_execution(tmp_path: Path) -> None:
    store, scope, manifest, cluster, hypothesis, evidence = await _experiment_inputs(tmp_path)
    lifecycle = {"created": 0, "setup": 0, "close": 0}
    runner = ImprovementEvaluationRunner(
        store,
        artifact_scope=scope,
        evaluator_identity_digest=_sha("7"),
    )

    with pytest.raises(HarnessError) as error:
        await runner.run(
            candidate_id="candidate-1",
            hypothesis=hypothesis,
            manifests=(manifest,),
            failure_clusters=(cluster,),
            cases=_cases(),
            baseline_factory=_factory(lambda _case: 0, lifecycle),
            candidate_factory=_factory(lambda _case: 0, lifecycle),
            baseline_digest=_sha("1"),
            candidate_digest=_sha("2"),
            verifier=_verifier,
            upstream_artifacts=(evidence[0],),
            novelty_score=0.5,
            diversity_score=0.5,
            added_complexity=0.1,
        )

    assert error.value.code == "IMPROVEMENT_EVIDENCE_SET_MISMATCH"
    assert lifecycle == {"created": 0, "setup": 0, "close": 0}


async def test_runner_preflights_rollout_budget_before_execution(tmp_path: Path) -> None:
    store, scope, manifest, cluster, hypothesis, evidence = await _experiment_inputs(
        tmp_path,
        max_rollouts=10,
    )
    lifecycle = {"created": 0, "setup": 0, "close": 0}
    runner = ImprovementEvaluationRunner(
        store,
        artifact_scope=scope,
        evaluator_identity_digest=_sha("7"),
    )

    with pytest.raises(HarnessError) as error:
        await runner.run(
            candidate_id="candidate-1",
            hypothesis=hypothesis,
            manifests=(manifest,),
            failure_clusters=(cluster,),
            cases=_cases(),
            baseline_factory=_factory(lambda _case: 0, lifecycle),
            candidate_factory=_factory(lambda _case: 0, lifecycle),
            baseline_digest=_sha("1"),
            candidate_digest=_sha("2"),
            verifier=_verifier,
            upstream_artifacts=evidence,
            novelty_score=0.5,
            diversity_score=0.5,
            added_complexity=0.1,
        )

    assert error.value.code == "IMPROVEMENT_BUDGET_EXCEEDED"
    assert error.value.details["dimensions"] == ["rollouts"]
    assert lifecycle["created"] == 0


async def test_runner_bounds_case_bytes_before_artifact_or_subject_work(tmp_path: Path) -> None:
    store, scope, manifest, cluster, hypothesis, evidence = await _experiment_inputs(tmp_path)
    lifecycle = {"created": 0, "setup": 0, "close": 0}
    runner = ImprovementEvaluationRunner(
        store,
        artifact_scope=scope,
        evaluator_identity_digest=_sha("7"),
        max_case_bytes=256,
        max_total_case_bytes=1_024,
    )
    cases = list(_cases(per_slice=1))
    cases[0] = EvaluationCase(
        case_id=cases[0].case_id,
        slice=cases[0].slice,
        task_class=cases[0].task_class,
        payload={"content": "x" * 512},
    )

    with pytest.raises(HarnessError) as error:
        await runner.run(
            candidate_id="candidate-1",
            hypothesis=hypothesis,
            manifests=(manifest,),
            failure_clusters=(cluster,),
            cases=cases,
            baseline_factory=_factory(lambda _case: 0, lifecycle),
            candidate_factory=_factory(lambda _case: 0, lifecycle),
            baseline_digest=_sha("1"),
            candidate_digest=_sha("2"),
            verifier=_verifier,
            upstream_artifacts=evidence,
            novelty_score=0.5,
            diversity_score=0.5,
            added_complexity=0.1,
        )

    assert error.value.code == "IMPROVEMENT_CASE_SET_INVALID"
    assert error.value.details["case_id"] == cases[0].case_id
    assert lifecycle["created"] == 0


async def test_runner_records_subject_timeouts_as_negative_evidence(tmp_path: Path) -> None:
    store, scope, manifest, cluster, hypothesis, evidence = await _experiment_inputs(tmp_path)
    lifecycle = {"created": 0, "setup": 0, "close": 0}
    runner = ImprovementEvaluationRunner(
        store,
        artifact_scope=scope,
        evaluator_identity_digest=_sha("7"),
        per_rollout_timeout=0.001,
    )

    result = await runner.run(
        candidate_id="candidate-timeout",
        hypothesis=hypothesis,
        manifests=(manifest,),
        failure_clusters=(cluster,),
        cases=_cases(),
        baseline_factory=_factory(lambda _case: 0, lifecycle),
        candidate_factory=_factory(lambda _case: 0.2, lifecycle, delay=0.01),
        baseline_digest=_sha("1"),
        candidate_digest=_sha("2"),
        verifier=_verifier,
        upstream_artifacts=evidence,
        novelty_score=0.5,
        diversity_score=0.5,
        added_complexity=0.1,
    )

    assert result.report.safety_passed is False
    assert result.report.privacy_passed is False
    evidence_value: dict[str, Any] = await store.load_json(result.case_artifacts[0])
    assert evidence_value["candidate"]["error_type"] == "TimeoutError"
    assert evidence_value["candidate"]["rollout"] is None
    assert lifecycle["close"] == 12
    decision = ImprovementEvaluationGate(
        EvaluationGatePolicy(
            min_held_in_samples=2,
            min_held_out_samples=2,
            min_transfer_samples=2,
        )
    ).evaluate(
        result.report,
        hypothesis=hypothesis,
        manifests=(manifest,),
        failure_clusters=(cluster,),
    )
    assert decision.verdict is EvaluationVerdict.REJECTED
    assert "safety_gate_failed" in decision.reasons
    assert "privacy_gate_failed" in decision.reasons


async def test_runner_evaluator_failure_invalidates_and_closes_subject(tmp_path: Path) -> None:
    store, scope, manifest, cluster, hypothesis, evidence = await _experiment_inputs(tmp_path)
    lifecycle = {"created": 0, "setup": 0, "close": 0}
    runner = ImprovementEvaluationRunner(
        store,
        artifact_scope=scope,
        evaluator_identity_digest=_sha("7"),
    )

    def broken_verifier(
        _case: EvaluationCase,
        _rollout: EvaluationRollout,
    ) -> EvaluationScore:
        raise RuntimeError("evaluator unavailable")

    with pytest.raises(HarnessError) as error:
        await runner.run(
            candidate_id="candidate-1",
            hypothesis=hypothesis,
            manifests=(manifest,),
            failure_clusters=(cluster,),
            cases=_cases(),
            baseline_factory=_factory(lambda _case: 0, lifecycle),
            candidate_factory=_factory(lambda _case: 0, lifecycle),
            baseline_digest=_sha("1"),
            candidate_digest=_sha("2"),
            verifier=broken_verifier,
            upstream_artifacts=evidence,
            novelty_score=0.5,
            diversity_score=0.5,
            added_complexity=0.1,
        )

    assert error.value.code == "IMPROVEMENT_EVALUATOR_FAILED"
    assert error.value.details["error_type"] == "RuntimeError"
    assert lifecycle == {"created": 1, "setup": 1, "close": 1}


async def test_runner_cleanup_failure_invalidates_resource_isolation(tmp_path: Path) -> None:
    store, scope, manifest, cluster, hypothesis, evidence = await _experiment_inputs(tmp_path)
    lifecycle = {"created": 0, "setup": 0, "close": 0}
    sensitive_error = type("api-key=must-not-enter-evidence", (RuntimeError,), {})

    class BadCloseSubject(_Subject):
        async def aclose(self) -> None:
            self.lifecycle["close"] += 1
            raise sensitive_error("cleanup failed")

    def bad_factory() -> BadCloseSubject:
        lifecycle["created"] += 1
        return BadCloseSubject(lambda _case: 0, lifecycle)

    runner = ImprovementEvaluationRunner(
        store,
        artifact_scope=scope,
        evaluator_identity_digest=_sha("7"),
    )

    with pytest.raises(HarnessError) as error:
        await runner.run(
            candidate_id="candidate-1",
            hypothesis=hypothesis,
            manifests=(manifest,),
            failure_clusters=(cluster,),
            cases=_cases(),
            baseline_factory=bad_factory,
            candidate_factory=_factory(lambda _case: 0, lifecycle),
            baseline_digest=_sha("1"),
            candidate_digest=_sha("2"),
            verifier=_verifier,
            upstream_artifacts=evidence,
            novelty_score=0.5,
            diversity_score=0.5,
            added_complexity=0.1,
        )

    assert error.value.code == "IMPROVEMENT_SUBJECT_CONTRACT_INVALID"
    assert error.value.details == {
        "case_id": "held_in-0",
        "phase": "close",
        "error_type": "SubjectError",
    }
    assert lifecycle == {"created": 1, "setup": 1, "close": 1}
