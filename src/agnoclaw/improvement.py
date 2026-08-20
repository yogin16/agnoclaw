"""Immutable self-improvement contracts and multi-objective evaluation gates.

This module evaluates evidence. It never edits a harness component and never promotes a
learning candidate. Candidate capture and reviewed promotion remain separate authorities.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, TypeVar

from .learning_candidates import (
    CandidateEvaluation,
    EvaluationVerdict,
    PromotionActor,
)
from .runtime.errors import HarnessError
from .runtime.security import freeze_data, thaw_data

IMPROVEMENT_SCHEMA_VERSION = "1.0"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_T = TypeVar("_T")


def _require_text(value: str, *, field_name: str, maximum: int = 4096) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{field_name} cannot exceed {maximum} characters")


def _require_digest(value: str, *, field_name: str) -> None:
    if not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a canonical sha256 digest")


def _digest(value: Any) -> str:
    payload = json.dumps(
        thaw_data(freeze_data(value)),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _require_ids(values: tuple[str, ...], *, field_name: str) -> None:
    if not values:
        raise ValueError(f"{field_name} cannot be empty")
    for value in values:
        _require_text(value, field_name=field_name, maximum=512)
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} values must be unique")


def _immutable_tuple(value: tuple[_T, ...] | list[_T], *, field_name: str) -> tuple[_T, ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{field_name} must be a tuple or list")
    return tuple(value)


def _require_bounded_paths(
    values: tuple[str, ...],
    *,
    code: str,
    subject: str,
) -> None:
    _require_ids(values, field_name="editable_path")
    if len(values) != len(set(values)):
        raise ValueError("editable paths must be unique")
    for path_text in values:
        path = PurePosixPath(path_text)
        if path_text != path.as_posix() or path == PurePosixPath("."):
            raise HarnessError(
                code=code,
                category="evaluation",
                message="Editable paths must be normalized workspace-relative paths.",
                retryable=False,
                details={"subject": subject},
            )
        if path.is_absolute() or ".." in path.parts:
            raise HarnessError(
                code=code,
                category="evaluation",
                message="Editable paths must be bounded workspace-relative paths.",
                retryable=False,
                details={"subject": subject},
            )


class HarnessComponentClass(StrEnum):
    SYSTEM_PROMPT = "system_prompt"
    TOOL_DESCRIPTION = "tool_description"
    TOOL_IMPLEMENTATION = "tool_implementation"
    MIDDLEWARE = "middleware"
    SKILL = "skill"
    SUBAGENT_CONFIGURATION = "subagent_configuration"
    LONG_TERM_MEMORY = "long_term_memory"


class ImprovementRole(StrEnum):
    GENERATOR = "generator"
    REFLECTOR = "reflector"
    CURATOR = "curator"
    EVALUATOR = "evaluator"
    OPERATOR = "operator"


class EvaluationSlice(StrEnum):
    HELD_IN = "held_in"
    HELD_OUT = "held_out"
    TRANSFER = "transfer"


class FeedbackStrength(StrEnum):
    OBJECTIVE = "objective"
    MIXED = "mixed"
    HEURISTIC = "heuristic"


@dataclass(frozen=True, slots=True)
class HarnessComponentManifest:
    component_id: str
    component_class: HarnessComponentClass
    version: str
    implementation_digest: str
    editable_paths: tuple[str, ...]
    rollback_reference: str
    description: str
    metadata: Any = field(default_factory=dict)
    schema_version: str = IMPROVEMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "editable_paths",
            _immutable_tuple(self.editable_paths, field_name="editable_paths"),
        )
        _require_text(self.component_id, field_name="component_id", maximum=512)
        object.__setattr__(
            self,
            "component_class",
            HarnessComponentClass(self.component_class),
        )
        _require_text(self.version, field_name="version", maximum=128)
        _require_digest(self.implementation_digest, field_name="implementation_digest")
        _require_bounded_paths(
            self.editable_paths,
            code="IMPROVEMENT_EDIT_SURFACE_INVALID",
            subject=self.component_id,
        )
        _require_text(
            self.rollback_reference,
            field_name="rollback_reference",
            maximum=512,
        )
        _require_text(self.description, field_name="description")
        if self.schema_version != IMPROVEMENT_SCHEMA_VERSION:
            raise ValueError("unsupported improvement schema")
        object.__setattr__(self, "metadata", freeze_data(self.metadata))

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "component_id": self.component_id,
            "component_class": self.component_class.value,
            "version": self.version,
            "implementation_digest": self.implementation_digest,
            "editable_paths": list(self.editable_paths),
            "rollback_reference": self.rollback_reference,
            "description": self.description,
            "metadata": thaw_data(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class FailureCluster:
    cluster_id: str
    causal_mechanism: str
    verifier_digest: str
    failure_run_ids: tuple[str, ...]
    evidence_artifact_ids: tuple[str, ...]
    terminal_labels: tuple[str, ...]
    mechanism_version: str

    def __post_init__(self) -> None:
        for field_name in (
            "failure_run_ids",
            "evidence_artifact_ids",
            "terminal_labels",
        ):
            object.__setattr__(
                self,
                field_name,
                _immutable_tuple(getattr(self, field_name), field_name=field_name),
            )
        _require_text(self.cluster_id, field_name="cluster_id", maximum=512)
        _require_text(self.causal_mechanism, field_name="causal_mechanism")
        _require_digest(self.verifier_digest, field_name="verifier_digest")
        _require_ids(self.failure_run_ids, field_name="failure_run_id")
        _require_ids(self.evidence_artifact_ids, field_name="evidence_artifact_id")
        _require_ids(self.terminal_labels, field_name="terminal_label")
        _require_text(
            self.mechanism_version,
            field_name="mechanism_version",
            maximum=512,
        )
        mechanism = " ".join(self.causal_mechanism.lower().split())
        terminal_labels = {" ".join(item.lower().split()) for item in self.terminal_labels}
        if mechanism in terminal_labels:
            raise HarnessError(
                code="IMPROVEMENT_CAUSAL_CLUSTER_INVALID",
                category="evaluation",
                message="A terminal label is not a verifier-grounded causal mechanism.",
                retryable=False,
                details={"cluster_id": self.cluster_id},
            )

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "causal_mechanism": self.causal_mechanism,
            "verifier_digest": self.verifier_digest,
            "failure_run_ids": list(self.failure_run_ids),
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
            "terminal_labels": list(self.terminal_labels),
            "mechanism_version": self.mechanism_version,
        }


@dataclass(frozen=True, slots=True)
class ChangeBudget:
    max_rollouts: int
    max_tokens: int
    max_wall_seconds: float
    max_cost_usd: float

    def __post_init__(self) -> None:
        if isinstance(self.max_rollouts, bool) or not isinstance(self.max_rollouts, int):
            raise TypeError("max_rollouts must be an integer")
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int):
            raise TypeError("max_tokens must be an integer")
        if not 1 <= self.max_rollouts <= 100_000:
            raise ValueError("max_rollouts must be between 1 and 100000")
        if not 1 <= self.max_tokens <= 10_000_000_000:
            raise ValueError("max_tokens must be between 1 and 10000000000")
        for field_name in ("max_wall_seconds", "max_cost_usd"):
            value = getattr(self, field_name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{field_name} must be finite and positive")

    def to_dict(self) -> dict[str, int | float]:
        return {
            "max_rollouts": self.max_rollouts,
            "max_tokens": self.max_tokens,
            "max_wall_seconds": self.max_wall_seconds,
            "max_cost_usd": self.max_cost_usd,
        }


@dataclass(frozen=True, slots=True)
class EvaluationResourceUsage:
    rollouts: int
    tokens: int
    wall_seconds: float
    cost_usd: float

    def __post_init__(self) -> None:
        if isinstance(self.rollouts, bool) or not isinstance(self.rollouts, int):
            raise TypeError("rollouts must be an integer")
        if isinstance(self.tokens, bool) or not isinstance(self.tokens, int):
            raise TypeError("tokens must be an integer")
        if self.rollouts < 0 or self.tokens < 0:
            raise ValueError("rollouts and tokens must be non-negative")
        for field_name in ("wall_seconds", "cost_usd"):
            value = getattr(self, field_name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{field_name} must be finite and non-negative")

    def exceeds(self, budget: ChangeBudget) -> tuple[str, ...]:
        exceeded: list[str] = []
        if self.rollouts > budget.max_rollouts:
            exceeded.append("rollouts")
        if self.tokens > budget.max_tokens:
            exceeded.append("tokens")
        if self.wall_seconds > budget.max_wall_seconds:
            exceeded.append("wall_seconds")
        if self.cost_usd > budget.max_cost_usd:
            exceeded.append("cost_usd")
        return tuple(exceeded)

    def to_dict(self) -> dict[str, int | float]:
        return {
            "rollouts": self.rollouts,
            "tokens": self.tokens,
            "wall_seconds": self.wall_seconds,
            "cost_usd": self.cost_usd,
        }


@dataclass(frozen=True, slots=True)
class ChangeHypothesis:
    change_id: str
    target_component_ids: tuple[str, ...]
    component_manifest_digests: tuple[str, ...]
    failure_cluster_ids: tuple[str, ...]
    failure_cluster_digests: tuple[str, ...]
    evidence_artifact_ids: tuple[str, ...]
    inferred_root_cause: str
    bounded_edit_surface: tuple[str, ...]
    predicted_fixes: tuple[str, ...]
    at_risk_regressions: tuple[str, ...]
    behaviors_to_preserve: tuple[str, ...]
    previous_attempt_ids: tuple[str, ...]
    model_config_digest: str
    evaluator_digest: str
    permission_digest: str
    proposer_identity_digest: str
    budget: ChangeBudget
    rollback_target: str
    proposed_by: ImprovementRole
    mechanism_version: str

    def __post_init__(self) -> None:
        for field_name in (
            "target_component_ids",
            "component_manifest_digests",
            "failure_cluster_ids",
            "failure_cluster_digests",
            "evidence_artifact_ids",
            "bounded_edit_surface",
            "predicted_fixes",
            "at_risk_regressions",
            "behaviors_to_preserve",
            "previous_attempt_ids",
        ):
            object.__setattr__(
                self,
                field_name,
                _immutable_tuple(getattr(self, field_name), field_name=field_name),
            )
        _require_text(self.change_id, field_name="change_id", maximum=512)
        for field_name in (
            "target_component_ids",
            "component_manifest_digests",
            "failure_cluster_ids",
            "failure_cluster_digests",
            "evidence_artifact_ids",
            "bounded_edit_surface",
            "predicted_fixes",
            "at_risk_regressions",
            "behaviors_to_preserve",
        ):
            _require_ids(getattr(self, field_name), field_name=field_name)
        for digest in self.component_manifest_digests:
            _require_digest(digest, field_name="component_manifest_digest")
        for digest in self.failure_cluster_digests:
            _require_digest(digest, field_name="failure_cluster_digest")
        if len(self.target_component_ids) != len(self.component_manifest_digests):
            raise HarnessError(
                code="IMPROVEMENT_MANIFEST_CARDINALITY_MISMATCH",
                category="evaluation",
                message="Every target component requires exactly one frozen manifest.",
                retryable=False,
                details={"change_id": self.change_id},
            )
        if len(self.failure_cluster_ids) != len(self.failure_cluster_digests):
            raise HarnessError(
                code="IMPROVEMENT_CLUSTER_CARDINALITY_MISMATCH",
                category="evaluation",
                message="Every failure cluster requires exactly one frozen digest.",
                retryable=False,
                details={"change_id": self.change_id},
            )
        _require_bounded_paths(
            self.bounded_edit_surface,
            code="IMPROVEMENT_EDIT_SURFACE_INVALID",
            subject=self.change_id,
        )
        _require_text(self.inferred_root_cause, field_name="inferred_root_cause")
        for field_name in (
            "model_config_digest",
            "evaluator_digest",
            "permission_digest",
            "proposer_identity_digest",
        ):
            _require_digest(getattr(self, field_name), field_name=field_name)
        if not isinstance(self.budget, ChangeBudget):
            raise TypeError("budget must be a ChangeBudget")
        _require_text(self.rollback_target, field_name="rollback_target", maximum=512)
        object.__setattr__(self, "proposed_by", ImprovementRole(self.proposed_by))
        if self.proposed_by is ImprovementRole.EVALUATOR:
            raise HarnessError(
                code="IMPROVEMENT_ROLE_CONFLICT",
                category="evaluation",
                message="The evaluator cannot also be the change proposer.",
                retryable=False,
                details={"change_id": self.change_id},
            )
        _require_text(
            self.mechanism_version,
            field_name="mechanism_version",
            maximum=512,
        )
        for attempt_id in self.previous_attempt_ids:
            _require_text(attempt_id, field_name="previous_attempt_id", maximum=512)

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "target_component_ids": list(self.target_component_ids),
            "component_manifest_digests": list(self.component_manifest_digests),
            "failure_cluster_ids": list(self.failure_cluster_ids),
            "failure_cluster_digests": list(self.failure_cluster_digests),
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
            "inferred_root_cause": self.inferred_root_cause,
            "bounded_edit_surface": list(self.bounded_edit_surface),
            "predicted_fixes": list(self.predicted_fixes),
            "at_risk_regressions": list(self.at_risk_regressions),
            "behaviors_to_preserve": list(self.behaviors_to_preserve),
            "previous_attempt_ids": list(self.previous_attempt_ids),
            "model_config_digest": self.model_config_digest,
            "evaluator_digest": self.evaluator_digest,
            "permission_digest": self.permission_digest,
            "proposer_identity_digest": self.proposer_identity_digest,
            "budget": self.budget.to_dict(),
            "rollback_target": self.rollback_target,
            "proposed_by": self.proposed_by.value,
            "mechanism_version": self.mechanism_version,
        }

    def verify_manifests(
        self,
        manifests: Iterable[HarnessComponentManifest],
    ) -> None:
        """Fail closed unless the hypothesis exactly names its frozen edit surface."""
        by_id: dict[str, HarnessComponentManifest] = {}
        for manifest in manifests:
            if manifest.component_id in by_id:
                raise HarnessError(
                    code="IMPROVEMENT_MANIFEST_DUPLICATE",
                    category="evaluation",
                    message="Component manifests must have unique identifiers.",
                    retryable=False,
                    details={"component_id": manifest.component_id},
                )
            by_id[manifest.component_id] = manifest
        expected_ids = set(self.target_component_ids)
        if set(by_id) != expected_ids:
            raise HarnessError(
                code="IMPROVEMENT_MANIFEST_SET_MISMATCH",
                category="evaluation",
                message="The supplied manifests do not exactly match the hypothesis targets.",
                retryable=False,
                details={"change_id": self.change_id},
            )
        expected_pairs = dict(
            zip(
                self.target_component_ids,
                self.component_manifest_digests,
                strict=True,
            )
        )
        for component_id, manifest in by_id.items():
            if manifest.digest != expected_pairs[component_id]:
                raise HarnessError(
                    code="IMPROVEMENT_MANIFEST_DIGEST_MISMATCH",
                    category="evaluation",
                    message="A component manifest changed after the hypothesis was frozen.",
                    retryable=False,
                    details={"component_id": component_id},
                )
        allowed_paths = {path for manifest in by_id.values() for path in manifest.editable_paths}
        if not set(self.bounded_edit_surface).issubset(allowed_paths):
            raise HarnessError(
                code="IMPROVEMENT_EDIT_SURFACE_MISMATCH",
                category="evaluation",
                message="The hypothesis attempts to edit outside its component manifests.",
                retryable=False,
                details={"change_id": self.change_id},
            )

    def verify_failure_clusters(self, clusters: Iterable[FailureCluster]) -> None:
        """Fail closed unless diagnosis evidence matches the frozen hypothesis."""
        by_id: dict[str, FailureCluster] = {}
        for cluster in clusters:
            if cluster.cluster_id in by_id:
                raise HarnessError(
                    code="IMPROVEMENT_CLUSTER_DUPLICATE",
                    category="evaluation",
                    message="Failure clusters must have unique identifiers.",
                    retryable=False,
                    details={"cluster_id": cluster.cluster_id},
                )
            by_id[cluster.cluster_id] = cluster
        if set(by_id) != set(self.failure_cluster_ids):
            raise HarnessError(
                code="IMPROVEMENT_CLUSTER_SET_MISMATCH",
                category="evaluation",
                message="The supplied failure clusters do not match the hypothesis.",
                retryable=False,
                details={"change_id": self.change_id},
            )
        expected = dict(
            zip(
                self.failure_cluster_ids,
                self.failure_cluster_digests,
                strict=True,
            )
        )
        for cluster_id, cluster in by_id.items():
            if cluster.digest != expected[cluster_id]:
                raise HarnessError(
                    code="IMPROVEMENT_CLUSTER_DIGEST_MISMATCH",
                    category="evaluation",
                    message="Failure-cluster evidence changed after hypothesis freeze.",
                    retryable=False,
                    details={"cluster_id": cluster_id},
                )


@dataclass(frozen=True, slots=True)
class EvaluationSliceResult:
    slice: EvaluationSlice
    task_class: str
    dataset_digest: str
    verifier_digest: str
    sample_count: int
    quality: float
    safety: float
    cost_usd: float
    latency_seconds: float
    objective_fraction: float
    evidence_artifact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evidence_artifact_ids",
            _immutable_tuple(
                self.evidence_artifact_ids,
                field_name="evidence_artifact_ids",
            ),
        )
        object.__setattr__(self, "slice", EvaluationSlice(self.slice))
        _require_text(self.task_class, field_name="task_class", maximum=512)
        _require_digest(self.dataset_digest, field_name="dataset_digest")
        _require_digest(self.verifier_digest, field_name="verifier_digest")
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int):
            raise TypeError("sample_count must be an integer")
        if self.sample_count <= 0:
            raise ValueError("sample_count must be positive")
        for field_name in ("quality", "safety", "objective_fraction"):
            value = getattr(self, field_name)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{field_name} must be finite and between zero and one")
        for field_name in ("cost_usd", "latency_seconds"):
            value = getattr(self, field_name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{field_name} must be finite and non-negative")
        _require_ids(self.evidence_artifact_ids, field_name="evidence_artifact_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "slice": self.slice.value,
            "task_class": self.task_class,
            "dataset_digest": self.dataset_digest,
            "verifier_digest": self.verifier_digest,
            "sample_count": self.sample_count,
            "quality": self.quality,
            "safety": self.safety,
            "cost_usd": self.cost_usd,
            "latency_seconds": self.latency_seconds,
            "objective_fraction": self.objective_fraction,
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
        }


@dataclass(frozen=True, slots=True)
class JudgeAudit:
    used: bool = False
    balanced_order: bool = False
    disagreement_rate: float = 0.0
    disagreements_reviewed: bool = False
    judge_model_digest: str | None = None
    judge_prompt_digest: str | None = None
    calibration_artifact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "used",
            "balanced_order",
            "disagreements_reviewed",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be a boolean")
        object.__setattr__(
            self,
            "calibration_artifact_ids",
            _immutable_tuple(
                self.calibration_artifact_ids,
                field_name="calibration_artifact_ids",
            ),
        )
        if not math.isfinite(self.disagreement_rate) or not 0 <= self.disagreement_rate <= 1:
            raise ValueError("disagreement_rate must be finite and between zero and one")
        if self.used:
            if self.judge_model_digest is None or self.judge_prompt_digest is None:
                raise HarnessError(
                    code="IMPROVEMENT_JUDGE_PROVENANCE_REQUIRED",
                    category="evaluation",
                    message="Judge-backed evidence requires frozen model and prompt digests.",
                    retryable=False,
                )
            _require_digest(self.judge_model_digest, field_name="judge_model_digest")
            _require_digest(self.judge_prompt_digest, field_name="judge_prompt_digest")
            _require_ids(
                self.calibration_artifact_ids,
                field_name="calibration_artifact_id",
            )
        elif (
            self.balanced_order
            or self.disagreement_rate
            or self.disagreements_reviewed
            or self.judge_model_digest is not None
            or self.judge_prompt_digest is not None
            or self.calibration_artifact_ids
        ):
            raise ValueError("unused judge audit cannot contain judge observations")

    def to_dict(self) -> dict[str, Any]:
        return {
            "used": self.used,
            "balanced_order": self.balanced_order,
            "disagreement_rate": self.disagreement_rate,
            "disagreements_reviewed": self.disagreements_reviewed,
            "judge_model_digest": self.judge_model_digest,
            "judge_prompt_digest": self.judge_prompt_digest,
            "calibration_artifact_ids": list(self.calibration_artifact_ids),
        }


@dataclass(frozen=True, slots=True)
class PairedQualityStatistic:
    """Paired quality-delta confidence evidence for one frozen evaluation slice."""

    slice: EvaluationSlice
    sample_count: int
    mean_delta: float
    confidence_low: float
    confidence_high: float
    wins: int
    ties: int
    losses: int
    confidence_level: float = 0.95
    method: str = "paired_t_95"

    def __post_init__(self) -> None:
        object.__setattr__(self, "slice", EvaluationSlice(self.slice))
        for field_name in ("sample_count", "wins", "ties", "losses"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field_name} must be an integer")
            if value < 0:
                raise ValueError(f"{field_name} must be non-negative")
        if self.sample_count <= 0:
            raise ValueError("sample_count must be positive")
        if self.wins + self.ties + self.losses != self.sample_count:
            raise ValueError("wins, ties, and losses must sum to sample_count")
        for field_name in ("mean_delta", "confidence_low", "confidence_high"):
            value = getattr(self, field_name)
            if not math.isfinite(value) or not -1 <= value <= 1:
                raise ValueError(f"{field_name} must be finite and between -1 and 1")
        if not self.confidence_low <= self.mean_delta <= self.confidence_high:
            raise ValueError("confidence bounds must contain mean_delta")
        if self.confidence_level != 0.95 or self.method != "paired_t_95":
            raise ValueError("only the paired_t_95 method at 95% confidence is supported")

    def to_dict(self) -> dict[str, Any]:
        return {
            "slice": self.slice.value,
            "sample_count": self.sample_count,
            "mean_delta": self.mean_delta,
            "confidence_low": self.confidence_low,
            "confidence_high": self.confidence_high,
            "wins": self.wins,
            "ties": self.ties,
            "losses": self.losses,
            "confidence_level": self.confidence_level,
            "method": self.method,
        }


@dataclass(frozen=True, slots=True)
class ImprovementEvaluation:
    candidate_id: str
    hypothesis_digest: str
    baseline: tuple[EvaluationSliceResult, ...]
    candidate: tuple[EvaluationSliceResult, ...]
    evaluator_digest: str
    evaluator_identity_digest: str
    model_config_digest: str
    permission_digest: str
    feedback_strength: FeedbackStrength
    usage: EvaluationResourceUsage
    safety_passed: bool
    privacy_passed: bool
    novelty_score: float
    diversity_score: float
    added_complexity: float
    judge_audit: JudgeAudit = field(default_factory=JudgeAudit)
    paired_statistics: tuple[PairedQualityStatistic, ...] = ()
    runner_digest: str | None = None
    baseline_subject_contract_digest: str | None = None
    candidate_subject_contract_digest: str | None = None
    subject_isolation_digest: str | None = None
    corpus_manifest_digest: str | None = None
    corpus_evidence_artifact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "baseline",
            _immutable_tuple(self.baseline, field_name="baseline"),
        )
        object.__setattr__(
            self,
            "candidate",
            _immutable_tuple(self.candidate, field_name="candidate"),
        )
        _require_text(self.candidate_id, field_name="candidate_id", maximum=512)
        for field_name in (
            "hypothesis_digest",
            "evaluator_digest",
            "evaluator_identity_digest",
            "model_config_digest",
            "permission_digest",
        ):
            _require_digest(getattr(self, field_name), field_name=field_name)
        object.__setattr__(
            self,
            "feedback_strength",
            FeedbackStrength(self.feedback_strength),
        )
        for collection_name in ("baseline", "candidate"):
            collection = getattr(self, collection_name)
            if not collection:
                raise ValueError(f"{collection_name} cannot be empty")
            if any(not isinstance(item, EvaluationSliceResult) for item in collection):
                raise TypeError(f"{collection_name} must contain EvaluationSliceResult values")
            slices = [item.slice for item in collection]
            if len(set(slices)) != len(slices):
                raise ValueError(f"{collection_name} has duplicate evaluation slices")
        if not isinstance(self.usage, EvaluationResourceUsage):
            raise TypeError("usage must be EvaluationResourceUsage")
        if not isinstance(self.safety_passed, bool) or not isinstance(
            self.privacy_passed,
            bool,
        ):
            raise TypeError("safety_passed and privacy_passed must be booleans")
        for field_name in ("novelty_score", "diversity_score", "added_complexity"):
            value = getattr(self, field_name)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{field_name} must be finite and between zero and one")
        if not isinstance(self.judge_audit, JudgeAudit):
            raise TypeError("judge_audit must be a JudgeAudit")
        object.__setattr__(
            self,
            "paired_statistics",
            _immutable_tuple(self.paired_statistics, field_name="paired_statistics"),
        )
        if any(not isinstance(item, PairedQualityStatistic) for item in self.paired_statistics):
            raise TypeError("paired_statistics must contain PairedQualityStatistic values")
        statistic_slices = [item.slice for item in self.paired_statistics]
        if len(statistic_slices) != len(set(statistic_slices)):
            raise ValueError("paired_statistics has duplicate evaluation slices")
        if self.runner_digest is not None:
            _require_digest(self.runner_digest, field_name="runner_digest")
        if bool(self.paired_statistics) is not bool(self.runner_digest):
            raise ValueError("paired_statistics and runner_digest must be supplied together")
        for field_name in (
            "baseline_subject_contract_digest",
            "candidate_subject_contract_digest",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_digest(value, field_name=field_name)
        if bool(self.baseline_subject_contract_digest) is not bool(
            self.candidate_subject_contract_digest
        ):
            raise ValueError("baseline and candidate subject contracts must be paired")
        if self.subject_isolation_digest is not None:
            _require_digest(
                self.subject_isolation_digest,
                field_name="subject_isolation_digest",
            )
        if bool(self.subject_isolation_digest) is not bool(
            self.baseline_subject_contract_digest
        ):
            raise ValueError("bound subject contracts require one shared isolation digest")
        object.__setattr__(
            self,
            "corpus_evidence_artifact_ids",
            _immutable_tuple(
                self.corpus_evidence_artifact_ids,
                field_name="corpus_evidence_artifact_ids",
            ),
        )
        if self.corpus_manifest_digest is not None:
            _require_digest(
                self.corpus_manifest_digest,
                field_name="corpus_manifest_digest",
            )
        if bool(self.corpus_manifest_digest) is not bool(
            self.corpus_evidence_artifact_ids
        ):
            raise ValueError(
                "corpus_manifest_digest and corpus evidence must be supplied together"
            )
        if self.corpus_evidence_artifact_ids:
            _require_ids(
                self.corpus_evidence_artifact_ids,
                field_name="corpus_evidence_artifact_id",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "hypothesis_digest": self.hypothesis_digest,
            "baseline": [item.to_dict() for item in self.baseline],
            "candidate": [item.to_dict() for item in self.candidate],
            "evaluator_digest": self.evaluator_digest,
            "evaluator_identity_digest": self.evaluator_identity_digest,
            "model_config_digest": self.model_config_digest,
            "permission_digest": self.permission_digest,
            "feedback_strength": self.feedback_strength.value,
            "usage": self.usage.to_dict(),
            "safety_passed": self.safety_passed,
            "privacy_passed": self.privacy_passed,
            "novelty_score": self.novelty_score,
            "diversity_score": self.diversity_score,
            "added_complexity": self.added_complexity,
            "judge_audit": self.judge_audit.to_dict(),
            "paired_statistics": [item.to_dict() for item in self.paired_statistics],
            "runner_digest": self.runner_digest,
            "baseline_subject_contract_digest": self.baseline_subject_contract_digest,
            "candidate_subject_contract_digest": self.candidate_subject_contract_digest,
            "subject_isolation_digest": self.subject_isolation_digest,
            "corpus_manifest_digest": self.corpus_manifest_digest,
            "corpus_evidence_artifact_ids": list(self.corpus_evidence_artifact_ids),
        }


@dataclass(frozen=True, slots=True)
class ObjectiveVector:
    quality: float
    safety: float
    cost_usd: float
    latency_seconds: float
    complexity: float

    def __post_init__(self) -> None:
        for field_name in ("quality", "safety", "complexity"):
            value = getattr(self, field_name)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{field_name} must be finite and between zero and one")
        for field_name in ("cost_usd", "latency_seconds"):
            value = getattr(self, field_name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{field_name} must be finite and non-negative")

    def dominates(self, other: ObjectiveVector) -> bool:
        no_worse = (
            self.quality >= other.quality
            and self.safety >= other.safety
            and self.cost_usd <= other.cost_usd
            and self.latency_seconds <= other.latency_seconds
            and self.complexity <= other.complexity
        )
        strictly_better = (
            self.quality > other.quality
            or self.safety > other.safety
            or self.cost_usd < other.cost_usd
            or self.latency_seconds < other.latency_seconds
            or self.complexity < other.complexity
        )
        return no_worse and strictly_better

    def to_dict(self) -> dict[str, float]:
        return {
            "quality": self.quality,
            "safety": self.safety,
            "cost_usd": self.cost_usd,
            "latency_seconds": self.latency_seconds,
            "complexity": self.complexity,
        }


@dataclass(frozen=True, slots=True)
class EvaluationGatePolicy:
    min_held_in_samples: int = 20
    min_held_out_samples: int = 20
    min_transfer_samples: int = 10
    min_held_in_quality_delta: float = 0.01
    max_held_out_quality_regression: float = 0.0
    max_transfer_quality_regression: float = 0.0
    max_safety_regression: float = 0.0
    max_cost_ratio: float = 1.5
    max_latency_ratio: float = 1.5
    min_objective_fraction: float = 0.5
    min_novelty_score: float = 0.1
    min_diversity_score: float = 0.1
    max_judge_disagreement_rate: float = 0.2
    require_governed_corpus: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "min_held_in_samples",
            "min_held_out_samples",
            "min_transfer_samples",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field_name} must be an integer")
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")
        for field_name in (
            "min_held_in_quality_delta",
            "max_held_out_quality_regression",
            "max_transfer_quality_regression",
            "max_safety_regression",
            "min_objective_fraction",
            "min_novelty_score",
            "min_diversity_score",
            "max_judge_disagreement_rate",
        ):
            value = getattr(self, field_name)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{field_name} must be finite and between zero and one")
        for field_name in ("max_cost_ratio", "max_latency_ratio"):
            value = getattr(self, field_name)
            if not math.isfinite(value) or value < 1:
                raise ValueError(f"{field_name} must be finite and at least one")
        if not isinstance(self.require_governed_corpus, bool):
            raise TypeError("require_governed_corpus must be a boolean")

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, int | float | bool]:
        return {
            "min_held_in_samples": self.min_held_in_samples,
            "min_held_out_samples": self.min_held_out_samples,
            "min_transfer_samples": self.min_transfer_samples,
            "min_held_in_quality_delta": self.min_held_in_quality_delta,
            "max_held_out_quality_regression": self.max_held_out_quality_regression,
            "max_transfer_quality_regression": self.max_transfer_quality_regression,
            "max_safety_regression": self.max_safety_regression,
            "max_cost_ratio": self.max_cost_ratio,
            "max_latency_ratio": self.max_latency_ratio,
            "min_objective_fraction": self.min_objective_fraction,
            "min_novelty_score": self.min_novelty_score,
            "min_diversity_score": self.min_diversity_score,
            "max_judge_disagreement_rate": self.max_judge_disagreement_rate,
            "require_governed_corpus": self.require_governed_corpus,
        }


@dataclass(frozen=True, slots=True)
class EvaluationGateDecision:
    candidate_id: str
    qualified: bool
    verdict: EvaluationVerdict
    reasons: tuple[str, ...]
    deltas: Any
    baseline_metrics: Any
    candidate_metrics: Any
    objective: ObjectiveVector
    evidence_artifact_ids: tuple[str, ...]
    hypothesis_digest: str
    policy_digest: str
    evaluator_digest: str
    evaluation_digest: str
    runner_digest: str | None = None
    corpus_manifest_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.qualified, bool):
            raise TypeError("qualified must be a boolean")
        object.__setattr__(
            self,
            "reasons",
            _immutable_tuple(self.reasons, field_name="reasons"),
        )
        object.__setattr__(
            self,
            "evidence_artifact_ids",
            _immutable_tuple(
                self.evidence_artifact_ids,
                field_name="evidence_artifact_ids",
            ),
        )
        _require_text(self.candidate_id, field_name="candidate_id", maximum=512)
        object.__setattr__(self, "verdict", EvaluationVerdict(self.verdict))
        if self.qualified is not (self.verdict is EvaluationVerdict.QUALIFIED):
            raise ValueError("qualified must agree with verdict")
        object.__setattr__(self, "deltas", freeze_data(self.deltas))
        object.__setattr__(self, "baseline_metrics", freeze_data(self.baseline_metrics))
        object.__setattr__(self, "candidate_metrics", freeze_data(self.candidate_metrics))
        _require_ids(self.evidence_artifact_ids, field_name="evidence_artifact_id")
        _require_digest(self.hypothesis_digest, field_name="hypothesis_digest")
        _require_digest(self.policy_digest, field_name="policy_digest")
        _require_digest(self.evaluator_digest, field_name="evaluator_digest")
        _require_digest(self.evaluation_digest, field_name="evaluation_digest")
        for field_name in ("runner_digest", "corpus_manifest_digest"):
            value = getattr(self, field_name)
            if value is not None:
                _require_digest(value, field_name=field_name)

    def to_candidate_evaluation(
        self,
        *,
        evaluation_id: str,
        evaluated_by: PromotionActor,
    ) -> CandidateEvaluation:
        return CandidateEvaluation(
            evaluation_id=evaluation_id,
            candidate_id=self.candidate_id,
            verdict=self.verdict,
            evaluator_digest=self.evaluator_digest,
            evidence_artifact_ids=self.evidence_artifact_ids,
            safety_passed=(self.qualified or "safety_gate_failed" not in self.reasons),
            evaluated_by=evaluated_by,
            metrics={
                "candidate": thaw_data(self.candidate_metrics),
                "gate": {
                    "qualified": self.qualified,
                    "reasons": list(self.reasons),
                    "deltas": thaw_data(self.deltas),
                    "objective": self.objective.to_dict(),
                    "hypothesis_digest": self.hypothesis_digest,
                    "policy_digest": self.policy_digest,
                    "evaluation_digest": self.evaluation_digest,
                    "runner_digest": self.runner_digest,
                    "corpus_manifest_digest": self.corpus_manifest_digest,
                },
            },
            control_metrics={"baseline": thaw_data(self.baseline_metrics)},
        )


class ImprovementEvaluationGate:
    """Pure, fail-closed held-in/held-out/transfer acceptance gate."""

    def __init__(self, policy: EvaluationGatePolicy | None = None) -> None:
        self.policy = policy or EvaluationGatePolicy()

    @staticmethod
    def _index(
        values: tuple[EvaluationSliceResult, ...],
    ) -> dict[EvaluationSlice, EvaluationSliceResult]:
        return {item.slice: item for item in values}

    @staticmethod
    def _ratio(candidate: float, baseline: float) -> float:
        if baseline == 0:
            return 1.0 if candidate == 0 else math.inf
        return candidate / baseline

    def evaluate(
        self,
        report: ImprovementEvaluation,
        *,
        hypothesis: ChangeHypothesis,
        manifests: Iterable[HarnessComponentManifest],
        failure_clusters: Iterable[FailureCluster],
    ) -> EvaluationGateDecision:
        if not isinstance(report, ImprovementEvaluation):
            raise TypeError("report must be an ImprovementEvaluation")
        if not isinstance(hypothesis, ChangeHypothesis):
            raise TypeError("hypothesis must be a ChangeHypothesis")
        manifest_values = tuple(manifests)
        cluster_values = tuple(failure_clusters)
        hypothesis.verify_manifests(manifest_values)
        hypothesis.verify_failure_clusters(cluster_values)
        if report.hypothesis_digest != hypothesis.digest:
            raise HarnessError(
                code="IMPROVEMENT_HYPOTHESIS_DIGEST_MISMATCH",
                category="evaluation",
                message="The evaluation does not match the frozen change hypothesis.",
                retryable=False,
                details={"candidate_id": report.candidate_id},
            )
        if report.evaluator_identity_digest == hypothesis.proposer_identity_digest:
            raise HarnessError(
                code="IMPROVEMENT_EVALUATOR_INDEPENDENCE_REQUIRED",
                category="evaluation",
                message="The proposer identity cannot evaluate its own candidate.",
                retryable=False,
                details={"candidate_id": report.candidate_id},
            )
        control_mismatches = [
            name
            for name in ("evaluator_digest", "model_config_digest", "permission_digest")
            if getattr(report, name) != getattr(hypothesis, name)
        ]
        if control_mismatches:
            raise HarnessError(
                code="IMPROVEMENT_CONTROL_PLANE_DRIFT",
                category="evaluation",
                message="Frozen evaluator controls changed during the experiment.",
                retryable=False,
                details={"controls": control_mismatches},
            )
        budget_exceeded = report.usage.exceeds(hypothesis.budget)
        if budget_exceeded:
            raise HarnessError(
                code="IMPROVEMENT_BUDGET_EXCEEDED",
                category="evaluation",
                message="The experiment exceeded its frozen resource budget.",
                retryable=False,
                details={"dimensions": list(budget_exceeded)},
            )
        baseline = self._index(report.baseline)
        candidate = self._index(report.candidate)
        required = tuple(EvaluationSlice)
        reasons: list[str] = []
        if self.policy.require_governed_corpus and report.corpus_manifest_digest is None:
            reasons.append("governed_corpus_required")
        missing = [item.value for item in required if item not in baseline or item not in candidate]
        if missing:
            raise HarnessError(
                code="IMPROVEMENT_EVALUATION_SLICE_REQUIRED",
                category="evaluation",
                message="Held-in, held-out, and transfer results are all required.",
                retryable=False,
                details={"missing": missing},
            )

        sample_minimums = {
            EvaluationSlice.HELD_IN: self.policy.min_held_in_samples,
            EvaluationSlice.HELD_OUT: self.policy.min_held_out_samples,
            EvaluationSlice.TRANSFER: self.policy.min_transfer_samples,
        }
        deltas: dict[str, Any] = {}
        evidence: list[str] = list(hypothesis.evidence_artifact_ids)
        for cluster in cluster_values:
            evidence.extend(cluster.evidence_artifact_ids)
        for slice_name in required:
            base = baseline[slice_name]
            changed = candidate[slice_name]
            if base.task_class != changed.task_class:
                reasons.append(f"{slice_name.value}_task_class_mismatch")
            if base.dataset_digest != changed.dataset_digest:
                reasons.append(f"{slice_name.value}_dataset_mismatch")
            if base.verifier_digest != changed.verifier_digest:
                reasons.append(f"{slice_name.value}_verifier_mismatch")
            if base.sample_count != changed.sample_count:
                reasons.append(f"{slice_name.value}_sample_mismatch")
            if min(base.sample_count, changed.sample_count) < sample_minimums[slice_name]:
                reasons.append(f"{slice_name.value}_sample_floor")
            if min(base.objective_fraction, changed.objective_fraction) < (
                self.policy.min_objective_fraction
            ):
                reasons.append(f"{slice_name.value}_objective_feedback_floor")
            quality_delta = changed.quality - base.quality
            safety_delta = changed.safety - base.safety
            deltas[slice_name.value] = {
                "quality": quality_delta,
                "safety": safety_delta,
                "cost_ratio": self._ratio(changed.cost_usd, base.cost_usd),
                "latency_ratio": self._ratio(
                    changed.latency_seconds,
                    base.latency_seconds,
                ),
            }
            evidence.extend(base.evidence_artifact_ids)
            evidence.extend(changed.evidence_artifact_ids)
        evidence.extend(report.judge_audit.calibration_artifact_ids)
        evidence.extend(report.corpus_evidence_artifact_ids)

        if deltas[EvaluationSlice.HELD_IN.value]["quality"] < (
            self.policy.min_held_in_quality_delta
        ):
            reasons.append("held_in_benefit_insufficient")
        if deltas[EvaluationSlice.HELD_OUT.value]["quality"] < -(
            self.policy.max_held_out_quality_regression
        ):
            reasons.append("held_out_quality_regression")
        if deltas[EvaluationSlice.TRANSFER.value]["quality"] < -(
            self.policy.max_transfer_quality_regression
        ):
            reasons.append("transfer_quality_regression")
        if report.paired_statistics:
            statistics_by_slice = {item.slice: item for item in report.paired_statistics}
            missing_statistics = [
                item.value for item in required if item not in statistics_by_slice
            ]
            if missing_statistics:
                raise HarnessError(
                    code="IMPROVEMENT_PAIRED_STATISTIC_REQUIRED",
                    category="evaluation",
                    message="Runner evidence requires a paired statistic for every slice.",
                    retryable=False,
                    details={"missing": missing_statistics},
                )
            for slice_name in required:
                statistic = statistics_by_slice[slice_name]
                if statistic.sample_count != baseline[slice_name].sample_count:
                    raise HarnessError(
                        code="IMPROVEMENT_PAIRED_STATISTIC_MISMATCH",
                        category="evaluation",
                        message="Paired statistics must match the frozen case population.",
                        retryable=False,
                        details={"slice": slice_name.value},
                    )
                if not math.isclose(
                    statistic.mean_delta,
                    deltas[slice_name.value]["quality"],
                    rel_tol=1e-9,
                    abs_tol=1e-12,
                ):
                    raise HarnessError(
                        code="IMPROVEMENT_PAIRED_STATISTIC_MISMATCH",
                        category="evaluation",
                        message="Paired statistics must agree with aggregate quality deltas.",
                        retryable=False,
                        details={"slice": slice_name.value},
                    )
                deltas[slice_name.value]["paired_quality"] = statistic.to_dict()
            if (
                statistics_by_slice[EvaluationSlice.HELD_IN].confidence_low
                < self.policy.min_held_in_quality_delta
            ):
                reasons.append("held_in_confidence_insufficient")
            if (
                statistics_by_slice[EvaluationSlice.HELD_OUT].confidence_low
                < -self.policy.max_held_out_quality_regression
            ):
                reasons.append("held_out_confidence_regression")
            if (
                statistics_by_slice[EvaluationSlice.TRANSFER].confidence_low
                < -self.policy.max_transfer_quality_regression
            ):
                reasons.append("transfer_confidence_regression")
        if (
            any(
                deltas[item.value]["safety"] < -self.policy.max_safety_regression
                for item in required
            )
            or not report.safety_passed
        ):
            reasons.append("safety_gate_failed")
        if not report.privacy_passed:
            reasons.append("privacy_gate_failed")
        if any(deltas[item.value]["cost_ratio"] > self.policy.max_cost_ratio for item in required):
            reasons.append("cost_budget_regression")
        if any(
            deltas[item.value]["latency_ratio"] > self.policy.max_latency_ratio for item in required
        ):
            reasons.append("latency_budget_regression")
        if report.novelty_score < self.policy.min_novelty_score:
            reasons.append("novelty_floor")
        if report.diversity_score < self.policy.min_diversity_score:
            reasons.append("diversity_collapse")
        if report.feedback_strength is FeedbackStrength.HEURISTIC:
            reasons.append("heuristic_feedback_only")
        if report.judge_audit.used:
            if not report.judge_audit.balanced_order:
                reasons.append("judge_order_unbalanced")
            if (
                report.judge_audit.disagreement_rate > self.policy.max_judge_disagreement_rate
                and not report.judge_audit.disagreements_reviewed
            ):
                reasons.append("judge_disagreement_unreviewed")

        objective_slices = (
            candidate[EvaluationSlice.HELD_OUT],
            candidate[EvaluationSlice.TRANSFER],
        )
        objective = ObjectiveVector(
            quality=sum(item.quality for item in objective_slices) / len(objective_slices),
            safety=min(item.safety for item in candidate.values()),
            cost_usd=sum(item.cost_usd for item in candidate.values()),
            latency_seconds=sum(item.latency_seconds for item in candidate.values())
            / len(candidate),
            complexity=report.added_complexity,
        )
        reasons = list(dict.fromkeys(reasons))
        qualified = not reasons
        inconclusive_reasons = {
            "heuristic_feedback_only",
            "judge_disagreement_unreviewed",
            "judge_order_unbalanced",
        }
        hard_reasons = set(reasons) - inconclusive_reasons
        if qualified:
            verdict = EvaluationVerdict.QUALIFIED
        elif hard_reasons:
            verdict = EvaluationVerdict.REJECTED
        else:
            verdict = EvaluationVerdict.INCONCLUSIVE
        decision_payload = {
            "report": report.to_dict(),
            "hypothesis": hypothesis.to_dict(),
            "policy": self.policy.to_dict(),
            "qualified": qualified,
            "verdict": verdict.value,
            "reasons": reasons,
            "deltas": deltas,
            "objective": objective.to_dict(),
        }
        return EvaluationGateDecision(
            candidate_id=report.candidate_id,
            qualified=qualified,
            verdict=verdict,
            reasons=tuple(reasons),
            deltas=deltas,
            baseline_metrics={item.slice.value: item.to_dict() for item in report.baseline},
            candidate_metrics={item.slice.value: item.to_dict() for item in report.candidate},
            objective=objective,
            evidence_artifact_ids=tuple(dict.fromkeys(evidence)),
            hypothesis_digest=hypothesis.digest,
            policy_digest=self.policy.digest,
            evaluator_digest=report.evaluator_digest,
            evaluation_digest=_digest(decision_payload),
            runner_digest=report.runner_digest,
            corpus_manifest_digest=report.corpus_manifest_digest,
        )


@dataclass(frozen=True, slots=True)
class ParetoEntry:
    candidate_id: str
    hypothesis_digest: str
    objective: ObjectiveVector
    evaluation_digest: str

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, field_name="candidate_id", maximum=512)
        _require_digest(self.hypothesis_digest, field_name="hypothesis_digest")
        if not isinstance(self.objective, ObjectiveVector):
            raise TypeError("objective must be an ObjectiveVector")
        _require_digest(self.evaluation_digest, field_name="evaluation_digest")


def pareto_frontier(entries: Iterable[ParetoEntry]) -> tuple[ParetoEntry, ...]:
    """Return deterministic non-dominated entries without scalarizing objectives."""
    unique: dict[tuple[str, str], ParetoEntry] = {}
    for entry in entries:
        if not isinstance(entry, ParetoEntry):
            raise TypeError("entries must contain ParetoEntry values")
        key = (entry.candidate_id, entry.evaluation_digest)
        previous = unique.get(key)
        if previous is not None and previous != entry:
            raise HarnessError(
                code="IMPROVEMENT_PARETO_ENTRY_CONFLICT",
                category="evaluation",
                message="A candidate evaluation key maps to conflicting objectives.",
                retryable=False,
                details={"candidate_id": entry.candidate_id},
            )
        unique[key] = entry
    values = tuple(unique.values())
    frontier = [
        entry
        for entry in values
        if not any(
            other.objective.dominates(entry.objective) for other in values if other is not entry
        )
    ]
    return tuple(sorted(frontier, key=lambda item: (item.candidate_id, item.evaluation_digest)))


__all__ = [
    "ChangeBudget",
    "ChangeHypothesis",
    "EvaluationGateDecision",
    "EvaluationGatePolicy",
    "EvaluationResourceUsage",
    "EvaluationSlice",
    "EvaluationSliceResult",
    "FailureCluster",
    "FeedbackStrength",
    "HarnessComponentClass",
    "HarnessComponentManifest",
    "IMPROVEMENT_SCHEMA_VERSION",
    "ImprovementEvaluation",
    "ImprovementEvaluationGate",
    "ImprovementRole",
    "JudgeAudit",
    "ObjectiveVector",
    "PairedQualityStatistic",
    "ParetoEntry",
    "pareto_frontier",
]
