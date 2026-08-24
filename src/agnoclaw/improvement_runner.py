"""Async paired evaluator for frozen harness-improvement experiments.

The runner owns experiment mechanics and evidence capture. It does not generate edits,
choose acceptance thresholds, mutate a learning ledger, or promote a candidate.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import statistics
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .improvement import (
    ChangeHypothesis,
    EvaluationResourceUsage,
    EvaluationSlice,
    EvaluationSliceResult,
    FailureCluster,
    FeedbackStrength,
    HarnessComponentManifest,
    ImprovementEvaluation,
    JudgeAudit,
    PairedQualityStatistic,
)
from .improvement_corpus import EvaluationCase, EvaluationCorpusManifest
from .runtime.artifacts import ArtifactReference, ArtifactScope, ArtifactStore
from .runtime.errors import HarnessError
from .runtime.security import freeze_data, thaw_data

EVALUATION_RUNNER_SCHEMA_VERSION = "1.2"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_T95 = (
    0.0,
    12.706,
    4.303,
    3.182,
    2.776,
    2.571,
    2.447,
    2.365,
    2.306,
    2.262,
    2.228,
    2.201,
    2.179,
    2.160,
    2.145,
    2.131,
    2.120,
    2.110,
    2.101,
    2.093,
    2.086,
    2.080,
    2.074,
    2.069,
    2.064,
    2.060,
    2.056,
    2.052,
    2.048,
    2.045,
    2.042,
)

IMPROVEMENT_RUNNER_IMPLEMENTATION_DIGEST = (
    f"sha256:{hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}"
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        thaw_data(freeze_data(value)),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def _require_text(value: str, *, field_name: str, maximum: int = 512) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{field_name} cannot exceed {maximum} characters")


def _require_digest(value: str, *, field_name: str) -> None:
    if not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a canonical sha256 digest")


def _error(code: str, message: str, **details: Any) -> HarnessError:
    return HarnessError(
        code=code,
        category="evaluation",
        message=message,
        retryable=False,
        details=details,
    )


def _safe_error_type(error: BaseException) -> str:
    name = type(error).__name__
    return name if _SAFE_ERROR_TYPE_RE.fullmatch(name) else "SubjectError"


def _subject_contract_digests(
    factory: SubjectFactory,
) -> tuple[str | None, str | None]:
    contract = getattr(factory, "subject_contract_digest", None)
    isolation = getattr(factory, "subject_isolation_digest", None)
    if contract is None and isolation is None:
        return None, None
    if (
        not isinstance(contract, str)
        or _DIGEST_RE.fullmatch(contract) is None
        or not isinstance(isolation, str)
        or _DIGEST_RE.fullmatch(isolation) is None
    ):
        raise _error(
            "IMPROVEMENT_SUBJECT_CONTRACT_INVALID",
            "A subject factory exposed invalid execution-boundary digests.",
            phase="factory_contract",
        )
    return contract, isolation


@dataclass(frozen=True, slots=True)
class EvaluationRollout:
    """A subject result plus independently metered usage."""

    output: Any
    tokens: int = 0
    cost_usd: float = 0.0

    def __post_init__(self) -> None:
        if isinstance(self.tokens, bool) or not isinstance(self.tokens, int):
            raise TypeError("tokens must be an integer")
        if self.tokens < 0:
            raise ValueError("tokens must be non-negative")
        if not math.isfinite(self.cost_usd) or self.cost_usd < 0:
            raise ValueError("cost_usd must be finite and non-negative")
        object.__setattr__(self, "output", freeze_data(self.output))

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": thaw_data(self.output),
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
        }


@dataclass(frozen=True, slots=True)
class EvaluationScore:
    """Independent verifier result for one rollout."""

    quality: float
    safety: float
    safety_passed: bool
    privacy_passed: bool
    objective: bool = True

    def __post_init__(self) -> None:
        for field_name in ("quality", "safety"):
            value = getattr(self, field_name)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{field_name} must be finite and between zero and one")
        for field_name in ("safety_passed", "privacy_passed", "objective"):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be a boolean")

    def to_dict(self) -> dict[str, float | bool]:
        return {
            "quality": self.quality,
            "safety": self.safety,
            "safety_passed": self.safety_passed,
            "privacy_passed": self.privacy_passed,
            "objective": self.objective,
        }


class EvaluationSubject(Protocol):
    def __call__(self, case: EvaluationCase) -> Awaitable[EvaluationRollout]: ...


SubjectFactory = Callable[[], EvaluationSubject]
EvaluationVerifier = Callable[
    [EvaluationCase, EvaluationRollout],
    EvaluationScore | Awaitable[EvaluationScore],
]


@dataclass(frozen=True, slots=True)
class ImprovementEvaluationRun:
    """Runner report plus exact verified and generated artifact references."""

    report: ImprovementEvaluation
    upstream_artifacts: tuple[ArtifactReference, ...]
    case_artifacts: tuple[ArtifactReference, ...]
    execution_order_balanced: bool

    def __post_init__(self) -> None:
        if not isinstance(self.report, ImprovementEvaluation):
            raise TypeError("report must be an ImprovementEvaluation")
        for field_name in ("upstream_artifacts", "case_artifacts"):
            values = tuple(getattr(self, field_name))
            if any(not isinstance(item, ArtifactReference) for item in values):
                raise TypeError(f"{field_name} must contain ArtifactReference values")
            object.__setattr__(self, field_name, values)
        if not self.case_artifacts:
            raise ValueError("case_artifacts cannot be empty")
        if not isinstance(self.execution_order_balanced, bool):
            raise TypeError("execution_order_balanced must be a boolean")

    @property
    def evidence_artifacts(self) -> tuple[ArtifactReference, ...]:
        return self.upstream_artifacts + self.case_artifacts


@dataclass(slots=True)
class _Observation:
    rollout: EvaluationRollout | None
    score: EvaluationScore
    latency_seconds: float
    error_type: str | None
    executed_first: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "rollout": self.rollout.to_dict() if self.rollout else None,
            "score": self.score.to_dict(),
            "latency_seconds": self.latency_seconds,
            "error_type": self.error_type,
            "executed_first": self.executed_first,
        }


@dataclass(slots=True)
class _Usage:
    rollouts: int = 0
    tokens: int = 0
    cost_usd: float = 0.0


class ImprovementEvaluationRunner:
    """Run fresh-resource baseline/candidate pairs and stage scoped case evidence."""

    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        artifact_scope: ArtifactScope,
        evaluator_identity_digest: str,
        per_rollout_timeout: float = 120.0,
        max_cases: int = 5_000,
        max_case_bytes: int = 256 * 1024,
        max_total_case_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if not isinstance(artifact_store, ArtifactStore):
            raise TypeError("artifact_store must implement ArtifactStore")
        if not isinstance(artifact_scope, ArtifactScope):
            raise TypeError("artifact_scope must be an ArtifactScope")
        _require_digest(evaluator_identity_digest, field_name="evaluator_identity_digest")
        if not math.isfinite(per_rollout_timeout) or per_rollout_timeout <= 0:
            raise ValueError("per_rollout_timeout must be finite and positive")
        if isinstance(max_cases, bool) or not isinstance(max_cases, int):
            raise TypeError("max_cases must be an integer")
        if not 1 <= max_cases <= 50_000:
            raise ValueError("max_cases must be between 1 and 50000")
        for field_name, value, maximum in (
            ("max_case_bytes", max_case_bytes, 16 * 1024 * 1024),
            ("max_total_case_bytes", max_total_case_bytes, 256 * 1024 * 1024),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field_name} must be an integer")
            if not 1 <= value <= maximum:
                raise ValueError(f"{field_name} must be between 1 and {maximum}")
        if max_case_bytes > max_total_case_bytes:
            raise ValueError("max_case_bytes cannot exceed max_total_case_bytes")
        self.artifact_store = artifact_store
        self.artifact_scope = artifact_scope
        self.evaluator_identity_digest = evaluator_identity_digest
        self.per_rollout_timeout = per_rollout_timeout
        self.max_cases = max_cases
        self.max_case_bytes = max_case_bytes
        self.max_total_case_bytes = max_total_case_bytes

    async def _verify_upstream_artifacts(
        self,
        references: Iterable[ArtifactReference],
        *,
        required_ids: set[str],
    ) -> tuple[tuple[ArtifactReference, ...], dict[str, Any]]:
        values = tuple(references)
        if any(not isinstance(item, ArtifactReference) for item in values):
            raise TypeError("upstream_artifacts must contain ArtifactReference values")
        by_id = {item.artifact_id: item for item in values}
        if len(by_id) != len(values) or set(by_id) != required_ids:
            raise _error(
                "IMPROVEMENT_EVIDENCE_SET_MISMATCH",
                "Upstream artifact references must exactly match the frozen evidence set.",
                required=sorted(required_ids),
                supplied=sorted(by_id),
            )
        loaded: dict[str, Any] = {}
        for reference in values:
            if reference.scope != self.artifact_scope:
                raise _error(
                    "IMPROVEMENT_EVIDENCE_SCOPE_MISMATCH",
                    "Evaluation evidence must use the runner's exact owner scope.",
                    artifact_id=reference.artifact_id,
                )
            loaded[reference.artifact_id] = await self.artifact_store.load_json(reference)
        return tuple(sorted(values, key=lambda item: item.artifact_id)), loaded

    @staticmethod
    def _validate_cases(
        cases: Iterable[EvaluationCase],
        *,
        maximum: int,
        max_case_bytes: int,
        max_total_case_bytes: int,
    ) -> tuple[EvaluationCase, ...]:
        values = tuple(cases)
        if not values or len(values) > maximum:
            raise _error(
                "IMPROVEMENT_CASE_SET_INVALID",
                "Evaluation cases must be non-empty and within the configured bound.",
                count=len(values),
                maximum=maximum,
            )
        if any(not isinstance(item, EvaluationCase) for item in values):
            raise TypeError("cases must contain EvaluationCase values")
        ids = [item.case_id for item in values]
        if len(ids) != len(set(ids)):
            raise _error(
                "IMPROVEMENT_CASE_SET_INVALID",
                "Evaluation case identifiers must be globally unique.",
                count=len(values),
                maximum=maximum,
            )
        encoded_sizes = [len(_canonical_bytes(item.to_dict())) for item in values]
        oversized = next(
            (
                (item.case_id, size)
                for item, size in zip(values, encoded_sizes, strict=True)
                if size > max_case_bytes
            ),
            None,
        )
        if oversized is not None or sum(encoded_sizes) > max_total_case_bytes:
            raise _error(
                "IMPROVEMENT_CASE_SET_INVALID",
                "Evaluation case content exceeds the configured byte boundary.",
                case_id=oversized[0] if oversized else None,
                case_bytes=oversized[1] if oversized else None,
                total_bytes=sum(encoded_sizes),
                max_case_bytes=max_case_bytes,
                max_total_case_bytes=max_total_case_bytes,
            )
        for slice_name in EvaluationSlice:
            sliced = [item for item in values if item.slice is slice_name]
            if not sliced:
                raise _error(
                    "IMPROVEMENT_CASE_SET_INVALID",
                    "Held-in, held-out, and transfer cases are all required.",
                    missing=slice_name.value,
                )
            if len({item.task_class for item in sliced}) != 1:
                raise _error(
                    "IMPROVEMENT_CASE_SET_INVALID",
                    "Every evaluation slice must have one stable task class.",
                    slice=slice_name.value,
                )
        return values

    async def _close_subject(self, subject: Any) -> str | None:
        close = getattr(subject, "aclose", None)
        if close is None:
            return None
        try:
            result = close()
            if not inspect.isawaitable(result):
                raise TypeError("aclose() must return an awaitable")
            async with asyncio.timeout(self.per_rollout_timeout):
                await result
        except Exception as exc:
            return _safe_error_type(exc)
        return None

    async def _execute(
        self,
        factory: SubjectFactory,
        case: EvaluationCase,
        verifier: EvaluationVerifier,
        *,
        executed_first: bool,
    ) -> _Observation:
        started = time.monotonic()
        try:
            subject: Any = factory()
        except Exception as exc:
            raise _error(
                "IMPROVEMENT_SUBJECT_CONTRACT_INVALID",
                "A subject factory failed; the experiment is invalid.",
                case_id=case.case_id,
                phase="factory",
                error_type=_safe_error_type(exc),
            ) from exc
        if subject is None or not callable(subject):
            raise _error(
                "IMPROVEMENT_SUBJECT_CONTRACT_INVALID",
                "A subject factory did not return an async callable.",
                case_id=case.case_id,
                phase="factory",
            )
        rollout: EvaluationRollout | None = None
        error_type: str | None = None
        close_error: str | None = None
        try:
            setup = getattr(subject, "asetup", None)
            if setup is not None:
                try:
                    setup_result = setup()
                    if not inspect.isawaitable(setup_result):
                        raise TypeError("asetup() must return an awaitable")
                    async with asyncio.timeout(self.per_rollout_timeout):
                        await setup_result
                except Exception as exc:
                    raise _error(
                        "IMPROVEMENT_SUBJECT_CONTRACT_INVALID",
                        "Subject setup failed; the experiment is invalid.",
                        case_id=case.case_id,
                        phase="setup",
                        error_type=_safe_error_type(exc),
                    ) from exc
            result: Any = None
            try:
                result = subject(case)
            except Exception as exc:
                error_type = _safe_error_type(exc)
                rollout = None
            if error_type is None:
                if not inspect.isawaitable(result):
                    raise _error(
                        "IMPROVEMENT_SUBJECT_CONTRACT_INVALID",
                        "An evaluation subject did not return an awaitable.",
                        case_id=case.case_id,
                        phase="call",
                    )
                try:
                    async with asyncio.timeout(self.per_rollout_timeout):
                        rollout = await result
                except Exception as exc:
                    error_type = _safe_error_type(exc)
                    rollout = None
            if not isinstance(rollout, EvaluationRollout):
                if rollout is not None:
                    raise _error(
                        "IMPROVEMENT_SUBJECT_CONTRACT_INVALID",
                        "An evaluation subject returned an invalid rollout contract.",
                        case_id=case.case_id,
                        phase="result",
                    )
        finally:
            close_error = await self._close_subject(subject)
        if close_error is not None:
            raise _error(
                "IMPROVEMENT_SUBJECT_CONTRACT_INVALID",
                "Subject cleanup failed; resource isolation is not trustworthy.",
                case_id=case.case_id,
                phase="close",
                error_type=close_error,
            )
        latency = time.monotonic() - started
        if rollout is None:
            return _Observation(
                rollout=None,
                score=EvaluationScore(0.0, 0.0, False, False),
                latency_seconds=latency,
                error_type=error_type or "UnknownError",
                executed_first=executed_first,
            )
        try:
            score_value = verifier(case, rollout)
            if inspect.isawaitable(score_value):
                async with asyncio.timeout(self.per_rollout_timeout):
                    score_value = await score_value
        except Exception as exc:
            raise _error(
                "IMPROVEMENT_EVALUATOR_FAILED",
                "The frozen evaluator failed; the experiment is invalid.",
                case_id=case.case_id,
                error_type=_safe_error_type(exc),
            ) from exc
        if not isinstance(score_value, EvaluationScore):
            raise _error(
                "IMPROVEMENT_EVALUATOR_FAILED",
                "The frozen evaluator returned an invalid score contract.",
                case_id=case.case_id,
            )
        return _Observation(
            rollout=rollout,
            score=score_value,
            latency_seconds=latency,
            error_type=None,
            executed_first=executed_first,
        )

    @staticmethod
    def _check_budget(
        hypothesis: ChangeHypothesis,
        usage: _Usage,
        *,
        wall_seconds: float,
    ) -> None:
        dimensions: list[str] = []
        if usage.rollouts > hypothesis.budget.max_rollouts:
            dimensions.append("rollouts")
        if usage.tokens > hypothesis.budget.max_tokens:
            dimensions.append("tokens")
        if wall_seconds > hypothesis.budget.max_wall_seconds:
            dimensions.append("wall_seconds")
        if usage.cost_usd > hypothesis.budget.max_cost_usd:
            dimensions.append("cost_usd")
        if dimensions:
            raise _error(
                "IMPROVEMENT_BUDGET_EXCEEDED",
                "The experiment exceeded its frozen resource budget.",
                dimensions=dimensions,
            )

    @staticmethod
    def _paired_statistic(
        slice_name: EvaluationSlice,
        deltas: list[float],
    ) -> PairedQualityStatistic:
        mean = statistics.fmean(deltas)
        if len(deltas) < 2:
            low, high = -1.0, 1.0
        else:
            deviation = statistics.stdev(deltas)
            if deviation == 0:
                low = high = mean
            else:
                critical = _T95[len(deltas) - 1] if len(deltas) <= 31 else 1.96
                margin = critical * deviation / math.sqrt(len(deltas))
                low, high = max(-1.0, mean - margin), min(1.0, mean + margin)
        return PairedQualityStatistic(
            slice=slice_name,
            sample_count=len(deltas),
            mean_delta=mean,
            confidence_low=low,
            confidence_high=high,
            wins=sum(value > 0 for value in deltas),
            ties=sum(value == 0 for value in deltas),
            losses=sum(value < 0 for value in deltas),
        )

    async def run(
        self,
        *,
        candidate_id: str,
        hypothesis: ChangeHypothesis,
        manifests: Iterable[HarnessComponentManifest],
        failure_clusters: Iterable[FailureCluster],
        cases: Iterable[EvaluationCase],
        baseline_factory: SubjectFactory,
        candidate_factory: SubjectFactory,
        baseline_digest: str,
        candidate_digest: str,
        verifier: EvaluationVerifier,
        upstream_artifacts: Iterable[ArtifactReference],
        novelty_score: float,
        diversity_score: float,
        added_complexity: float,
        feedback_strength: FeedbackStrength = FeedbackStrength.OBJECTIVE,
        judge_audit: JudgeAudit | None = None,
        corpus_manifest: EvaluationCorpusManifest | None = None,
    ) -> ImprovementEvaluationRun:
        """Execute one frozen paired experiment; never gate, persist, or promote it."""
        _require_text(candidate_id, field_name="candidate_id")
        if not isinstance(hypothesis, ChangeHypothesis):
            raise TypeError("hypothesis must be a ChangeHypothesis")
        _require_digest(baseline_digest, field_name="baseline_digest")
        _require_digest(candidate_digest, field_name="candidate_digest")
        for field_name, metric_value in (
            ("novelty_score", novelty_score),
            ("diversity_score", diversity_score),
            ("added_complexity", added_complexity),
        ):
            if not math.isfinite(metric_value) or not 0 <= metric_value <= 1:
                raise ValueError(f"{field_name} must be finite and between zero and one")
        for field_name, callback in (
            ("baseline_factory", baseline_factory),
            ("candidate_factory", candidate_factory),
            ("verifier", verifier),
        ):
            if not callable(callback):
                raise TypeError(f"{field_name} must be callable")
        (
            baseline_subject_contract_digest,
            baseline_subject_isolation_digest,
        ) = _subject_contract_digests(baseline_factory)
        (
            candidate_subject_contract_digest,
            candidate_subject_isolation_digest,
        ) = _subject_contract_digests(candidate_factory)
        if bool(baseline_subject_contract_digest) is not bool(
            candidate_subject_contract_digest
        ):
            raise _error(
                "IMPROVEMENT_SUBJECT_CONTRACT_INVALID",
                "Baseline and candidate execution boundaries must be equally bound.",
                phase="factory_contract",
            )
        if baseline_subject_isolation_digest != candidate_subject_isolation_digest:
            raise _error(
                "IMPROVEMENT_SUBJECT_CONTRACT_INVALID",
                "Baseline and candidate isolation policies must match exactly.",
                phase="factory_contract",
            )
        subject_isolation_digest = baseline_subject_isolation_digest
        resolved_feedback_strength = FeedbackStrength(feedback_strength)
        if judge_audit is not None and not isinstance(judge_audit, JudgeAudit):
            raise TypeError("judge_audit must be a JudgeAudit")
        if self.evaluator_identity_digest == hypothesis.proposer_identity_digest:
            raise _error(
                "IMPROVEMENT_EVALUATOR_INDEPENDENCE_REQUIRED",
                "The proposer identity cannot execute its own evaluation.",
                candidate_id=candidate_id,
            )
        manifest_values = tuple(manifests)
        cluster_values = tuple(failure_clusters)
        hypothesis.verify_manifests(manifest_values)
        hypothesis.verify_failure_clusters(cluster_values)
        case_values = self._validate_cases(
            cases,
            maximum=self.max_cases,
            max_case_bytes=self.max_case_bytes,
            max_total_case_bytes=self.max_total_case_bytes,
        )
        if corpus_manifest is not None:
            if not isinstance(corpus_manifest, EvaluationCorpusManifest):
                raise TypeError("corpus_manifest must be an EvaluationCorpusManifest")
            corpus_manifest.verify_authority(
                proposer_identity_digest=hypothesis.proposer_identity_digest,
            )
            corpus_manifest.verify_cases(case_values)
        if len(case_values) * 2 > hypothesis.budget.max_rollouts:
            raise _error(
                "IMPROVEMENT_BUDGET_EXCEEDED",
                "The frozen rollout budget cannot cover the paired case set.",
                dimensions=["rollouts"],
            )
        audit = judge_audit or JudgeAudit()
        required_ids = set(hypothesis.evidence_artifact_ids)
        for cluster in cluster_values:
            required_ids.update(cluster.evidence_artifact_ids)
        required_ids.update(audit.calibration_artifact_ids)
        if corpus_manifest is not None:
            required_ids.update(corpus_manifest.evidence_artifact_ids)
        verified_upstream, loaded_upstream = await self._verify_upstream_artifacts(
            upstream_artifacts,
            required_ids=required_ids,
        )
        if corpus_manifest is not None:
            corpus_manifest.verify_evidence(
                {item.artifact_id: item for item in verified_upstream},
                loaded_upstream,
            )
        runner_digest = _digest(
            {
                "schema_version": EVALUATION_RUNNER_SCHEMA_VERSION,
                "implementation_digest": IMPROVEMENT_RUNNER_IMPLEMENTATION_DIGEST,
                "candidate_id": candidate_id,
                "hypothesis_digest": hypothesis.digest,
                "baseline_digest": baseline_digest,
                "candidate_digest": candidate_digest,
                "baseline_subject_contract_digest": baseline_subject_contract_digest,
                "candidate_subject_contract_digest": candidate_subject_contract_digest,
                "subject_isolation_digest": subject_isolation_digest,
                "evaluator_digest": hypothesis.evaluator_digest,
                "evaluator_identity_digest": self.evaluator_identity_digest,
                "artifact_scope": self.artifact_scope.to_dict(),
                "per_rollout_timeout": self.per_rollout_timeout,
                "max_cases": self.max_cases,
                "max_case_bytes": self.max_case_bytes,
                "max_total_case_bytes": self.max_total_case_bytes,
                "feedback_strength": resolved_feedback_strength.value,
                "judge_audit": audit.to_dict(),
                "corpus_manifest_digest": (
                    corpus_manifest.digest if corpus_manifest is not None else None
                ),
                "case_order": [item.to_dict() for item in case_values],
            }
        )
        started = time.monotonic()
        usage = _Usage()
        case_artifacts: list[ArtifactReference] = []
        observations: dict[
            EvaluationSlice,
            list[tuple[EvaluationCase, _Observation, _Observation, ArtifactReference]],
        ] = {item: [] for item in EvaluationSlice}
        for index, case in enumerate(case_values):
            baseline_first = index % 2 == 0
            ordered = (
                (("baseline", baseline_factory), ("candidate", candidate_factory))
                if baseline_first
                else (("candidate", candidate_factory), ("baseline", baseline_factory))
            )
            pair: dict[str, _Observation] = {}
            for position, (name, factory) in enumerate(ordered):
                self._check_budget(hypothesis, usage, wall_seconds=time.monotonic() - started)
                observation = await self._execute(
                    factory,
                    case,
                    verifier,
                    executed_first=position == 0,
                )
                pair[name] = observation
                usage.rollouts += 1
                if observation.rollout is not None:
                    usage.tokens += observation.rollout.tokens
                    usage.cost_usd += observation.rollout.cost_usd
                self._check_budget(hypothesis, usage, wall_seconds=time.monotonic() - started)
            artifact = await self.artifact_store.stage_json(
                {
                    "type": "agnoclaw.improvement_evaluation_case",
                    "schema_version": EVALUATION_RUNNER_SCHEMA_VERSION,
                    "runner_digest": runner_digest,
                    "runner_implementation_digest": (
                        IMPROVEMENT_RUNNER_IMPLEMENTATION_DIGEST
                    ),
                    "hypothesis_digest": hypothesis.digest,
                    "candidate_id": candidate_id,
                    "baseline_digest": baseline_digest,
                    "candidate_digest": candidate_digest,
                    "baseline_subject_contract_digest": baseline_subject_contract_digest,
                    "candidate_subject_contract_digest": candidate_subject_contract_digest,
                    "subject_isolation_digest": subject_isolation_digest,
                    "evaluator_digest": hypothesis.evaluator_digest,
                    "corpus_manifest_digest": (
                        corpus_manifest.digest if corpus_manifest is not None else None
                    ),
                    "case": case.to_dict(),
                    "execution_order": [name for name, _ in ordered],
                    "baseline": pair["baseline"].to_dict(),
                    "candidate": pair["candidate"].to_dict(),
                },
                scope=self.artifact_scope,
                purpose="improvement_evaluation_case",
                metadata={
                    "candidate_id": candidate_id,
                    "case_id": case.case_id,
                    "slice": case.slice.value,
                    "runner_digest": runner_digest,
                },
            )
            case_artifacts.append(artifact)
            observations[case.slice].append(
                (case, pair["baseline"], pair["candidate"], artifact)
            )
            self._check_budget(hypothesis, usage, wall_seconds=time.monotonic() - started)

        baseline_results: list[EvaluationSliceResult] = []
        candidate_results: list[EvaluationSliceResult] = []
        paired_statistics: list[PairedQualityStatistic] = []
        candidate_safety_passed = True
        candidate_privacy_passed = True
        for slice_name in EvaluationSlice:
            values = observations[slice_name]
            dataset_digest = _digest([item[0].to_dict() for item in values])
            evidence_ids = tuple(item[3].artifact_id for item in values)
            task_class = values[0][0].task_class
            for is_candidate, target in (
                (False, baseline_results),
                (True, candidate_results),
            ):
                selected = [item[2] if is_candidate else item[1] for item in values]
                target.append(
                    EvaluationSliceResult(
                        slice=slice_name,
                        task_class=task_class,
                        dataset_digest=dataset_digest,
                        verifier_digest=hypothesis.evaluator_digest,
                        sample_count=len(selected),
                        quality=statistics.fmean(item.score.quality for item in selected),
                        safety=statistics.fmean(item.score.safety for item in selected),
                        cost_usd=sum(
                            item.rollout.cost_usd if item.rollout else 0.0
                            for item in selected
                        ),
                        latency_seconds=statistics.fmean(
                            item.latency_seconds for item in selected
                        ),
                        objective_fraction=statistics.fmean(
                            float(item.score.objective) for item in selected
                        ),
                        evidence_artifact_ids=evidence_ids,
                    )
                )
            candidate_values = [item[2] for item in values]
            candidate_safety_passed = candidate_safety_passed and all(
                item.score.safety_passed for item in candidate_values
            )
            candidate_privacy_passed = candidate_privacy_passed and all(
                item.score.privacy_passed for item in candidate_values
            )
            paired_statistics.append(
                self._paired_statistic(
                    slice_name,
                    [item[2].score.quality - item[1].score.quality for item in values],
                )
            )
        wall_seconds = time.monotonic() - started
        self._check_budget(hypothesis, usage, wall_seconds=wall_seconds)
        report = ImprovementEvaluation(
            candidate_id=candidate_id,
            hypothesis_digest=hypothesis.digest,
            baseline=tuple(baseline_results),
            candidate=tuple(candidate_results),
            evaluator_digest=hypothesis.evaluator_digest,
            evaluator_identity_digest=self.evaluator_identity_digest,
            model_config_digest=hypothesis.model_config_digest,
            permission_digest=hypothesis.permission_digest,
            feedback_strength=resolved_feedback_strength,
            usage=EvaluationResourceUsage(
                rollouts=usage.rollouts,
                tokens=usage.tokens,
                wall_seconds=wall_seconds,
                cost_usd=usage.cost_usd,
            ),
            safety_passed=candidate_safety_passed,
            privacy_passed=candidate_privacy_passed,
            novelty_score=novelty_score,
            diversity_score=diversity_score,
            added_complexity=added_complexity,
            judge_audit=audit,
            paired_statistics=tuple(paired_statistics),
            runner_digest=runner_digest,
            baseline_subject_contract_digest=baseline_subject_contract_digest,
            candidate_subject_contract_digest=candidate_subject_contract_digest,
            subject_isolation_digest=subject_isolation_digest,
            corpus_manifest_digest=(
                corpus_manifest.digest if corpus_manifest is not None else None
            ),
            corpus_evidence_artifact_ids=(
                corpus_manifest.evidence_artifact_ids
                if corpus_manifest is not None
                else ()
            ),
        )
        baseline_first_count = (len(case_values) + 1) // 2
        candidate_first_count = len(case_values) // 2
        return ImprovementEvaluationRun(
            report=report,
            upstream_artifacts=verified_upstream,
            case_artifacts=tuple(case_artifacts),
            execution_order_balanced=abs(baseline_first_count - candidate_first_count) <= 1,
        )


__all__ = [
    "EVALUATION_RUNNER_SCHEMA_VERSION",
    "EvaluationCase",
    "EvaluationRollout",
    "EvaluationScore",
    "EvaluationSubject",
    "EvaluationVerifier",
    "ImprovementEvaluationRun",
    "ImprovementEvaluationRunner",
    "IMPROVEMENT_RUNNER_IMPLEMENTATION_DIGEST",
    "SubjectFactory",
]
