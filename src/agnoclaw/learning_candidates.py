"""Governed learning candidates and the first-party SQLite ledger.

Candidates are inert data.  They do not enter recall context until an independent
evaluation qualifies them and an authorized promotion adapter applies them.  Promotion
intent is persisted before the external Agno write; an ambiguous outcome is recorded as
``unknown`` and is never blindly replayed.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from .learning import LearningPolicy, LearningScope, LearningWritePath
from .runtime.artifacts import ArtifactReference, ArtifactScope, ArtifactStore
from .runtime.errors import HarnessError
from .runtime.security import freeze_data, thaw_data

LEARNING_CANDIDATE_SCHEMA_VERSION = "1.0"
LEARNING_LEDGER_SCHEMA_VERSION = 6
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_AGNO_LEARNED_KNOWLEDGE_REFERENCE_PREFIX = "agno:learned_knowledge:"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        thaw_data(freeze_data(value)),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


def _require_id(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > 512:
        raise ValueError(f"{field_name} cannot exceed 512 characters")


class LearningTarget(StrEnum):
    ENTITY_MEMORY = "entity_memory"
    LEARNED_KNOWLEDGE = "learned_knowledge"
    DECISION_LOG = "decision_log"
    HARNESS_COMPONENT = "harness_component"


class CandidateRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PROHIBITED = "prohibited"


class CandidateAuthor(StrEnum):
    AGENT = "agent"
    USER = "user"
    OPERATOR = "operator"
    RULE = "rule"


class CandidateState(StrEnum):
    CAPTURED = "captured"
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    PROMOTING = "promoting"
    PROMOTED = "promoted"
    PROMOTION_UNKNOWN = "promotion_unknown"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_UNKNOWN = "rollback_unknown"
    DELETED = "deleted"


class EvaluationVerdict(StrEnum):
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class ReconciliationKind(StrEnum):
    PROMOTION = "promotion"
    ROLLBACK = "rollback"


class ReconciliationVerdict(StrEnum):
    EFFECT_PRESENT = "effect_present"
    EFFECT_ABSENT = "effect_absent"


class CandidateAction(StrEnum):
    QUARANTINE = "quarantine"
    RESTORE = "restore"
    DELETE = "delete"


_CANDIDATE_TRANSITIONS = {
    CandidateAction.QUARANTINE: {
        CandidateState.CAPTURED: CandidateState.QUARANTINED,
        CandidateState.QUALIFIED: CandidateState.QUARANTINED,
        CandidateState.REJECTED: CandidateState.QUARANTINED,
    },
    CandidateAction.RESTORE: {
        CandidateState.QUARANTINED: CandidateState.CAPTURED,
    },
    CandidateAction.DELETE: {
        CandidateState.CAPTURED: CandidateState.DELETED,
        CandidateState.QUALIFIED: CandidateState.DELETED,
        CandidateState.REJECTED: CandidateState.DELETED,
        CandidateState.QUARANTINED: CandidateState.DELETED,
        CandidateState.ROLLED_BACK: CandidateState.DELETED,
    },
}


class PromotionActor(StrEnum):
    HOST = "host"
    OPERATOR = "operator"


class LearningApplicationKind(StrEnum):
    """How a promoted learning influenced one run."""

    RETRIEVED = "retrieved"
    APPLIED = "applied"


class LearningOutcomeKind(StrEnum):
    """Externally observed result of applying one promoted learning."""

    SUCCESS = "success"
    FAILURE = "failure"
    CORRECTION = "correction"
    NEUTRAL = "neutral"


class LearningEffectivenessRecommendation(StrEnum):
    """Read-only host recommendation; never an automatic state transition."""

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    RETAIN = "retain"
    REVIEW = "review"
    QUARANTINE = "quarantine"


@dataclass(frozen=True, slots=True)
class LearningOwner:
    """Exact authorization tuple for a candidate namespace."""

    tenant_id: str | None
    storage_namespace: str

    def __post_init__(self) -> None:
        _require_id(self.storage_namespace, field_name="storage_namespace")

    @property
    def digest(self) -> str:
        return _digest(
            {
                "tenant_id": self.tenant_id,
                "storage_namespace": self.storage_namespace,
            }
        )


@dataclass(frozen=True, slots=True)
class LearningCandidate:
    """Immutable candidate metadata; content remains in an ArtifactStore."""

    candidate_id: str
    target: LearningTarget
    tenant_id: str | None
    storage_namespace: str
    content_artifact: ArtifactReference
    source_run_ids: tuple[str, ...]
    evidence_artifact_ids: tuple[str, ...]
    confidence: float
    risk: CandidateRisk
    created_by: CandidateAuthor
    mechanism_version: str
    source_user_id: str | None = None
    expires_at: str | None = None
    change_hypothesis_artifact_id: str | None = None
    component_manifest_artifact_id: str | None = None
    supersedes_candidate_id: str | None = None
    created_at: str = field(default_factory=_now)
    schema_version: str = LEARNING_CANDIDATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_id(self.candidate_id, field_name="candidate_id")
        _require_id(self.storage_namespace, field_name="storage_namespace")
        _require_id(self.mechanism_version, field_name="mechanism_version")
        object.__setattr__(self, "target", LearningTarget(self.target))
        object.__setattr__(self, "risk", CandidateRisk(self.risk))
        object.__setattr__(self, "created_by", CandidateAuthor(self.created_by))
        if self.schema_version != LEARNING_CANDIDATE_SCHEMA_VERSION:
            raise ValueError("unsupported learning candidate schema")
        if not self.source_run_ids:
            raise ValueError("a learning candidate requires at least one source run")
        for value in (*self.source_run_ids, *self.evidence_artifact_ids):
            _require_id(value, field_name="provenance_id")
        for field_name in (
            "change_hypothesis_artifact_id",
            "component_manifest_artifact_id",
            "supersedes_candidate_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_id(value, field_name=field_name)
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be finite and between zero and one")
        if self.risk is CandidateRisk.PROHIBITED:
            raise HarnessError(
                code="LEARNING_CANDIDATE_PROHIBITED",
                category="learning",
                message="Prohibited content cannot enter the candidate ledger.",
                retryable=False,
                details={"candidate_id": self.candidate_id},
            )
        if self.expires_at is not None:
            try:
                expires = datetime.fromisoformat(self.expires_at)
            except ValueError as exc:
                raise ValueError("expires_at must be an ISO-8601 timestamp") from exc
            if expires.tzinfo is None:
                raise ValueError("expires_at must include a timezone")
        if self.content_artifact.scope.run_id not in self.source_run_ids:
            raise ValueError("candidate content artifact must be bound to a source run")
        if self.content_artifact.scope.tenant_id != self.tenant_id:
            raise ValueError("candidate and content artifact tenant scopes differ")
        if self.target is LearningTarget.HARNESS_COMPONENT and (
            self.change_hypothesis_artifact_id is None
            or self.component_manifest_artifact_id is None
        ):
            raise HarnessError(
                code="LEARNING_CHANGE_CONTRACT_REQUIRED",
                category="learning",
                message=(
                    "Harness-component candidates require immutable component-manifest "
                    "and change-hypothesis artifacts."
                ),
                retryable=False,
                details={"candidate_id": self.candidate_id},
            )

    @property
    def owner(self) -> LearningOwner:
        return LearningOwner(self.tenant_id, self.storage_namespace)

    @property
    def digest(self) -> str:
        payload = self.to_dict()
        payload.pop("created_at")
        payload["content_artifact"] = {
            "artifact_id": self.content_artifact.artifact_id,
            "storage_identity_digest": self.content_artifact.storage_identity_digest,
        }
        return _digest(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "target": self.target.value,
            "tenant_id": self.tenant_id,
            "storage_namespace": self.storage_namespace,
            "content_artifact": self.content_artifact.to_dict(),
            "source_run_ids": list(self.source_run_ids),
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
            "confidence": self.confidence,
            "risk": self.risk.value,
            "created_by": self.created_by.value,
            "mechanism_version": self.mechanism_version,
            "source_user_id": self.source_user_id,
            "expires_at": self.expires_at,
            "change_hypothesis_artifact_id": self.change_hypothesis_artifact_id,
            "component_manifest_artifact_id": self.component_manifest_artifact_id,
            "supersedes_candidate_id": self.supersedes_candidate_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LearningCandidate:
        payload = dict(value)
        payload["content_artifact"] = ArtifactReference.from_dict(payload["content_artifact"])
        payload["source_run_ids"] = tuple(payload["source_run_ids"])
        payload["evidence_artifact_ids"] = tuple(payload["evidence_artifact_ids"])
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    evaluation_id: str
    candidate_id: str
    verdict: EvaluationVerdict
    evaluator_digest: str
    evidence_artifact_ids: tuple[str, ...]
    safety_passed: bool
    evaluated_by: PromotionActor
    metrics: Any = field(default_factory=dict)
    control_metrics: Any = field(default_factory=dict)
    notes: str | None = None
    evaluated_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        _require_id(self.evaluation_id, field_name="evaluation_id")
        _require_id(self.candidate_id, field_name="candidate_id")
        object.__setattr__(self, "verdict", EvaluationVerdict(self.verdict))
        object.__setattr__(self, "evaluated_by", PromotionActor(self.evaluated_by))
        if not _SHA256_RE.fullmatch(self.evaluator_digest):
            raise ValueError("evaluator_digest must be a canonical sha256 digest")
        if not self.evidence_artifact_ids:
            raise HarnessError(
                code="LEARNING_EVALUATION_EVIDENCE_REQUIRED",
                category="learning",
                message="Candidate evaluation requires immutable evidence artifacts.",
                retryable=False,
                details={"candidate_id": self.candidate_id},
            )
        if self.verdict is EvaluationVerdict.QUALIFIED and not self.safety_passed:
            raise HarnessError(
                code="LEARNING_EVALUATION_SAFETY_FAILED",
                category="learning",
                message="A candidate cannot qualify when the safety gate failed.",
                retryable=False,
                details={"candidate_id": self.candidate_id},
            )
        object.__setattr__(self, "metrics", freeze_data(self.metrics))
        object.__setattr__(self, "control_metrics", freeze_data(self.control_metrics))

    @property
    def digest(self) -> str:
        payload = self.to_dict()
        payload.pop("evaluated_at")
        return _digest(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "candidate_id": self.candidate_id,
            "verdict": self.verdict.value,
            "evaluator_digest": self.evaluator_digest,
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
            "safety_passed": self.safety_passed,
            "evaluated_by": self.evaluated_by.value,
            "metrics": thaw_data(self.metrics),
            "control_metrics": thaw_data(self.control_metrics),
            "notes": self.notes,
            "evaluated_at": self.evaluated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CandidateEvaluation:
        payload = dict(value)
        payload["evidence_artifact_ids"] = tuple(payload["evidence_artifact_ids"])
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class CandidateReconciliation:
    reconciliation_id: str
    candidate_id: str
    kind: ReconciliationKind
    verdict: ReconciliationVerdict
    reconciler_digest: str
    evidence_artifact_ids: tuple[str, ...]
    reconciled_by: PromotionActor
    target_reference: str | None = None
    notes: str | None = None
    reconciled_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        _require_id(self.reconciliation_id, field_name="reconciliation_id")
        _require_id(self.candidate_id, field_name="candidate_id")
        object.__setattr__(self, "kind", ReconciliationKind(self.kind))
        object.__setattr__(self, "verdict", ReconciliationVerdict(self.verdict))
        object.__setattr__(self, "reconciled_by", PromotionActor(self.reconciled_by))
        if not _SHA256_RE.fullmatch(self.reconciler_digest):
            raise ValueError("reconciler_digest must be a canonical sha256 digest")
        if not self.evidence_artifact_ids:
            raise HarnessError(
                code="LEARNING_RECONCILIATION_EVIDENCE_REQUIRED",
                category="learning",
                message="Unknown-effect reconciliation requires immutable evidence.",
                retryable=False,
                details={"candidate_id": self.candidate_id},
            )
        for artifact_id in self.evidence_artifact_ids:
            _require_id(artifact_id, field_name="evidence_artifact_id")
        if self.target_reference is not None:
            _require_id(self.target_reference, field_name="target_reference")
        if (
            self.kind is ReconciliationKind.PROMOTION
            and self.verdict is ReconciliationVerdict.EFFECT_PRESENT
            and self.target_reference is None
        ):
            raise HarnessError(
                code="LEARNING_RECONCILIATION_TARGET_REQUIRED",
                category="learning",
                message="A present promotion effect requires its exact target reference.",
                retryable=False,
                details={"candidate_id": self.candidate_id},
            )

    @property
    def digest(self) -> str:
        payload = self.to_dict()
        payload.pop("reconciled_at")
        return _digest(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciliation_id": self.reconciliation_id,
            "candidate_id": self.candidate_id,
            "kind": self.kind.value,
            "verdict": self.verdict.value,
            "reconciler_digest": self.reconciler_digest,
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
            "reconciled_by": self.reconciled_by.value,
            "target_reference": self.target_reference,
            "notes": self.notes,
            "reconciled_at": self.reconciled_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CandidateReconciliation:
        payload = dict(value)
        payload["evidence_artifact_ids"] = tuple(payload["evidence_artifact_ids"])
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class LearningApplication:
    """Content-free attribution that a promoted learning reached one run."""

    application_id: str
    candidate_id: str
    run_id: str
    target_reference: str
    kind: LearningApplicationKind
    observer_digest: str
    evidence_artifact_ids: tuple[str, ...]
    observed_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        for field_name in ("application_id", "candidate_id", "run_id", "target_reference"):
            _require_id(getattr(self, field_name), field_name=field_name)
        object.__setattr__(self, "kind", LearningApplicationKind(self.kind))
        if _SHA256_RE.fullmatch(self.observer_digest) is None:
            raise ValueError("observer_digest must be a canonical sha256 digest")
        object.__setattr__(self, "evidence_artifact_ids", tuple(self.evidence_artifact_ids))
        if not self.evidence_artifact_ids:
            raise HarnessError(
                code="LEARNING_APPLICATION_EVIDENCE_REQUIRED",
                category="learning",
                message="Learning application attribution requires immutable evidence.",
                retryable=False,
                details={"candidate_id": self.candidate_id},
            )
        for artifact_id in self.evidence_artifact_ids:
            _require_id(artifact_id, field_name="evidence_artifact_id")
        observed_at = datetime.fromisoformat(self.observed_at)
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must include a timezone")

    @property
    def digest(self) -> str:
        payload = self.to_dict()
        payload.pop("observed_at")
        return _digest(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "candidate_id": self.candidate_id,
            "run_id": self.run_id,
            "target_reference": self.target_reference,
            "kind": self.kind.value,
            "observer_digest": self.observer_digest,
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LearningApplication:
        payload = dict(value)
        payload["evidence_artifact_ids"] = tuple(payload["evidence_artifact_ids"])
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class LearningOutcome:
    """Independent, immutable outcome attached to one applied learning."""

    outcome_id: str
    application_id: str
    candidate_id: str
    run_id: str
    kind: LearningOutcomeKind
    score: float
    evaluator_digest: str
    evidence_artifact_ids: tuple[str, ...]
    evaluated_by: PromotionActor
    recorded_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        for field_name in ("outcome_id", "application_id", "candidate_id", "run_id"):
            _require_id(getattr(self, field_name), field_name=field_name)
        object.__setattr__(self, "kind", LearningOutcomeKind(self.kind))
        object.__setattr__(self, "evaluated_by", PromotionActor(self.evaluated_by))
        if (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not math.isfinite(float(self.score))
            or not -1 <= float(self.score) <= 1
        ):
            raise ValueError("score must be finite and between -1 and 1")
        object.__setattr__(self, "score", float(self.score))
        expected_sign = {
            LearningOutcomeKind.SUCCESS: 1,
            LearningOutcomeKind.FAILURE: -1,
            LearningOutcomeKind.CORRECTION: -1,
            LearningOutcomeKind.NEUTRAL: 0,
        }[self.kind]
        if (
            (expected_sign > 0 and self.score <= 0)
            or (expected_sign < 0 and self.score >= 0)
            or (expected_sign == 0 and self.score != 0)
        ):
            raise ValueError("outcome kind and score sign must agree")
        if _SHA256_RE.fullmatch(self.evaluator_digest) is None:
            raise ValueError("evaluator_digest must be a canonical sha256 digest")
        object.__setattr__(self, "evidence_artifact_ids", tuple(self.evidence_artifact_ids))
        if not self.evidence_artifact_ids:
            raise HarnessError(
                code="LEARNING_OUTCOME_EVIDENCE_REQUIRED",
                category="learning",
                message="Learning outcomes require immutable external evidence.",
                retryable=False,
                details={"candidate_id": self.candidate_id},
            )
        for artifact_id in self.evidence_artifact_ids:
            _require_id(artifact_id, field_name="evidence_artifact_id")
        recorded_at = datetime.fromisoformat(self.recorded_at)
        if recorded_at.tzinfo is None:
            raise ValueError("recorded_at must include a timezone")

    @property
    def digest(self) -> str:
        payload = self.to_dict()
        payload.pop("recorded_at")
        return _digest(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "application_id": self.application_id,
            "candidate_id": self.candidate_id,
            "run_id": self.run_id,
            "kind": self.kind.value,
            "score": self.score,
            "evaluator_digest": self.evaluator_digest,
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
            "evaluated_by": self.evaluated_by.value,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LearningOutcome:
        payload = dict(value)
        payload["evidence_artifact_ids"] = tuple(payload["evidence_artifact_ids"])
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class LearningEffectivenessPolicy:
    """Conservative thresholds for a non-mutating effectiveness recommendation."""

    minimum_outcomes: int = 5
    minimum_independent_runs: int = 3
    review_mean_score: float = 0.0
    quarantine_mean_score: float = -0.25
    quarantine_negative_fraction: float = 0.6

    def __post_init__(self) -> None:
        for field_name in ("minimum_outcomes", "minimum_independent_runs"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field_name} must be an integer")
            if not 1 <= value <= 1_000_000:
                raise ValueError(f"{field_name} must be between 1 and 1000000")
        for field_name in (
            "review_mean_score",
            "quarantine_mean_score",
            "quarantine_negative_fraction",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{field_name} must be numeric")
            if not math.isfinite(float(value)):
                raise ValueError(f"{field_name} must be finite")
        if not -1 <= self.review_mean_score <= 1:
            raise ValueError("review_mean_score must be between -1 and 1")
        if not -1 <= self.quarantine_mean_score <= self.review_mean_score:
            raise ValueError("quarantine_mean_score must be between -1 and review_mean_score")
        if not 0 <= self.quarantine_negative_fraction <= 1:
            raise ValueError("quarantine_negative_fraction must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class LearningEffectivenessSummary:
    candidate_id: str
    total_applications: int
    applied_applications: int
    evaluated_outcomes: int
    independent_runs: int
    successes: int
    failures: int
    corrections: int
    neutral: int
    mean_score: float | None
    recommendation: LearningEffectivenessRecommendation

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "total_applications": self.total_applications,
            "applied_applications": self.applied_applications,
            "evaluated_outcomes": self.evaluated_outcomes,
            "independent_runs": self.independent_runs,
            "successes": self.successes,
            "failures": self.failures,
            "corrections": self.corrections,
            "neutral": self.neutral,
            "mean_score": self.mean_score,
            "recommendation": self.recommendation.value,
        }


def _learning_effectiveness_summary(
    *,
    candidate_id: str,
    total_applications: int,
    applied_applications: int,
    evaluated_outcomes: int,
    independent_runs: int,
    successes: int,
    failures: int,
    corrections: int,
    neutral: int,
    score_sum: float | None,
    policy: LearningEffectivenessPolicy,
) -> LearningEffectivenessSummary:
    mean_score = None if not evaluated_outcomes else float(score_sum or 0.0) / evaluated_outcomes
    if (
        evaluated_outcomes < policy.minimum_outcomes
        or independent_runs < policy.minimum_independent_runs
    ):
        recommendation = LearningEffectivenessRecommendation.INSUFFICIENT_EVIDENCE
    else:
        negative_fraction = (failures + corrections) / evaluated_outcomes
        if (
            mean_score is not None
            and mean_score <= policy.quarantine_mean_score
            and negative_fraction >= policy.quarantine_negative_fraction
        ):
            recommendation = LearningEffectivenessRecommendation.QUARANTINE
        elif mean_score is not None and mean_score <= policy.review_mean_score:
            recommendation = LearningEffectivenessRecommendation.REVIEW
        else:
            recommendation = LearningEffectivenessRecommendation.RETAIN
    return LearningEffectivenessSummary(
        candidate_id=candidate_id,
        total_applications=total_applications,
        applied_applications=applied_applications,
        evaluated_outcomes=evaluated_outcomes,
        independent_runs=independent_runs,
        successes=successes,
        failures=failures,
        corrections=corrections,
        neutral=neutral,
        mean_score=mean_score,
        recommendation=recommendation,
    )


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    candidate: LearningCandidate
    state: CandidateState = CandidateState.CAPTURED
    revision: int = 0
    latest_evaluation_id: str | None = None
    promotion_id: str | None = None
    promotion_request_id: str | None = None
    promotion_actor: PromotionActor | None = None
    promotion_version: int = 0
    target_reference: str | None = None
    rollback_id: str | None = None
    rollback_request_id: str | None = None
    rollback_actor: PromotionActor | None = None
    latest_reconciliation_id: str | None = None
    updated_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", CandidateState(self.state))
        if self.promotion_actor is not None:
            object.__setattr__(
                self,
                "promotion_actor",
                PromotionActor(self.promotion_actor),
            )
        if self.rollback_actor is not None:
            object.__setattr__(
                self,
                "rollback_actor",
                PromotionActor(self.rollback_actor),
            )
        if self.revision < 0 or self.promotion_version < 0:
            raise ValueError("candidate counters cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "state": self.state.value,
            "revision": self.revision,
            "latest_evaluation_id": self.latest_evaluation_id,
            "promotion_id": self.promotion_id,
            "promotion_request_id": self.promotion_request_id,
            "promotion_actor": (
                self.promotion_actor.value if self.promotion_actor is not None else None
            ),
            "promotion_version": self.promotion_version,
            "target_reference": self.target_reference,
            "rollback_id": self.rollback_id,
            "rollback_request_id": self.rollback_request_id,
            "rollback_actor": (
                self.rollback_actor.value if self.rollback_actor is not None else None
            ),
            "latest_reconciliation_id": self.latest_reconciliation_id,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CandidateRecord:
        payload = dict(value)
        payload["candidate"] = LearningCandidate.from_dict(payload["candidate"])
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class EvaluationArchiveCursor:
    """Owner-bound descending keyset cursor for evaluation history."""

    evaluated_at: str
    evaluation_id: str
    owner_digest: str

    def __post_init__(self) -> None:
        _require_id(self.evaluated_at, field_name="evaluated_at")
        evaluated_at = datetime.fromisoformat(self.evaluated_at)
        if evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must include a timezone")
        _require_id(self.evaluation_id, field_name="evaluation_id")
        if _SHA256_RE.fullmatch(self.owner_digest) is None:
            raise ValueError("owner_digest must be a canonical sha256 digest")

    def to_dict(self) -> dict[str, str]:
        return {
            "evaluated_at": self.evaluated_at,
            "evaluation_id": self.evaluation_id,
            "owner_digest": self.owner_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EvaluationArchiveCursor:
        if not isinstance(value, dict):
            raise TypeError("evaluation archive cursor payload must be a mapping")
        return cls(
            evaluated_at=value["evaluated_at"],
            evaluation_id=value["evaluation_id"],
            owner_digest=value["owner_digest"],
        )


@dataclass(frozen=True, slots=True)
class EvaluationArchiveQuery:
    """Bounded content-free evaluation archive filters; negative results by default."""

    verdicts: tuple[EvaluationVerdict, ...] = (
        EvaluationVerdict.REJECTED,
        EvaluationVerdict.INCONCLUSIVE,
    )
    evaluator_digest: str | None = None
    reason_code: str | None = None
    mechanism_version: str | None = None
    target: LearningTarget | None = None
    safety_passed: bool | None = None
    limit: int = 100
    cursor: EvaluationArchiveCursor | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "verdicts",
            tuple(EvaluationVerdict(item) for item in self.verdicts),
        )
        if not self.verdicts or len(self.verdicts) != len(set(self.verdicts)):
            raise ValueError("verdicts must be non-empty and unique")
        if (
            self.evaluator_digest is not None
            and _SHA256_RE.fullmatch(self.evaluator_digest) is None
        ):
            raise ValueError("evaluator_digest must be a canonical sha256 digest")
        if self.reason_code is not None and _REASON_CODE_RE.fullmatch(self.reason_code) is None:
            raise ValueError("reason_code must be a stable lowercase reason code")
        if self.mechanism_version is not None:
            _require_id(self.mechanism_version, field_name="mechanism_version")
        if self.target is not None:
            object.__setattr__(self, "target", LearningTarget(self.target))
        if self.safety_passed is not None and not isinstance(self.safety_passed, bool):
            raise TypeError("safety_passed must be a boolean when supplied")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= self.limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if self.cursor is not None and not isinstance(
            self.cursor,
            EvaluationArchiveCursor,
        ):
            raise TypeError("cursor must be an EvaluationArchiveCursor")


@dataclass(frozen=True, slots=True)
class EvaluationArchiveEntry:
    """Content-free operator projection over one immutable candidate evaluation."""

    evaluation_id: str
    candidate_id: str
    verdict: EvaluationVerdict
    evaluator_digest: str
    evaluated_by: PromotionActor
    safety_passed: bool
    evaluated_at: str
    candidate_state: CandidateState
    target: LearningTarget
    mechanism_version: str
    failure_reason_codes: tuple[str, ...]
    policy_digest: str | None
    hypothesis_digest: str | None
    evaluation_digest: str | None
    runner_digest: str | None
    corpus_manifest_digest: str | None
    evidence_artifact_count: int

    def __post_init__(self) -> None:
        _require_id(self.evaluation_id, field_name="evaluation_id")
        _require_id(self.candidate_id, field_name="candidate_id")
        object.__setattr__(self, "verdict", EvaluationVerdict(self.verdict))
        object.__setattr__(self, "evaluated_by", PromotionActor(self.evaluated_by))
        object.__setattr__(self, "candidate_state", CandidateState(self.candidate_state))
        object.__setattr__(self, "target", LearningTarget(self.target))
        if _SHA256_RE.fullmatch(self.evaluator_digest) is None:
            raise ValueError("evaluator_digest must be a canonical sha256 digest")
        if not isinstance(self.safety_passed, bool):
            raise TypeError("safety_passed must be a boolean")
        _require_id(self.evaluated_at, field_name="evaluated_at")
        _require_id(self.mechanism_version, field_name="mechanism_version")
        object.__setattr__(self, "failure_reason_codes", tuple(self.failure_reason_codes))
        if len(self.failure_reason_codes) != len(set(self.failure_reason_codes)) or any(
            _REASON_CODE_RE.fullmatch(item) is None for item in self.failure_reason_codes
        ):
            raise ValueError("failure_reason_codes must be unique stable reason codes")
        for field_name in (
            "policy_digest",
            "hypothesis_digest",
            "evaluation_digest",
            "runner_digest",
            "corpus_manifest_digest",
        ):
            value = getattr(self, field_name)
            if value is not None and _SHA256_RE.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a canonical sha256 digest")
        if (
            isinstance(self.evidence_artifact_count, bool)
            or not isinstance(self.evidence_artifact_count, int)
            or self.evidence_artifact_count <= 0
        ):
            raise ValueError("evidence_artifact_count must be a positive integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "candidate_id": self.candidate_id,
            "verdict": self.verdict.value,
            "evaluator_digest": self.evaluator_digest,
            "evaluated_by": self.evaluated_by.value,
            "safety_passed": self.safety_passed,
            "evaluated_at": self.evaluated_at,
            "candidate_state": self.candidate_state.value,
            "target": self.target.value,
            "mechanism_version": self.mechanism_version,
            "failure_reason_codes": list(self.failure_reason_codes),
            "policy_digest": self.policy_digest,
            "hypothesis_digest": self.hypothesis_digest,
            "evaluation_digest": self.evaluation_digest,
            "runner_digest": self.runner_digest,
            "corpus_manifest_digest": self.corpus_manifest_digest,
            "evidence_artifact_count": self.evidence_artifact_count,
        }


@dataclass(frozen=True, slots=True)
class EvaluationArchivePage:
    items: tuple[EvaluationArchiveEntry, ...]
    next_cursor: EvaluationArchiveCursor | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        if any(not isinstance(item, EvaluationArchiveEntry) for item in self.items):
            raise TypeError("items must contain EvaluationArchiveEntry values")
        if self.next_cursor is not None and not isinstance(
            self.next_cursor,
            EvaluationArchiveCursor,
        ):
            raise TypeError("next_cursor must be an EvaluationArchiveCursor")

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "next_cursor": self.next_cursor.to_dict() if self.next_cursor else None,
        }


def evaluation_archive_entry(
    record: CandidateRecord,
    evaluation: CandidateEvaluation,
) -> EvaluationArchiveEntry:
    """Project safe, indexable gate metadata without notes, metrics, or artifact IDs."""
    if not isinstance(record, CandidateRecord) or not isinstance(
        evaluation,
        CandidateEvaluation,
    ):
        raise TypeError("record and evaluation must use learning candidate contracts")
    safe_reasons, gate = _evaluation_archive_gate_metadata(evaluation)

    def digest_field(name: str) -> str | None:
        value = gate.get(name)
        return value if isinstance(value, str) and _SHA256_RE.fullmatch(value) else None

    return EvaluationArchiveEntry(
        evaluation_id=evaluation.evaluation_id,
        candidate_id=evaluation.candidate_id,
        verdict=evaluation.verdict,
        evaluator_digest=evaluation.evaluator_digest,
        evaluated_by=evaluation.evaluated_by,
        safety_passed=evaluation.safety_passed,
        evaluated_at=evaluation.evaluated_at,
        candidate_state=record.state,
        target=record.candidate.target,
        mechanism_version=record.candidate.mechanism_version,
        failure_reason_codes=safe_reasons,
        policy_digest=digest_field("policy_digest"),
        hypothesis_digest=digest_field("hypothesis_digest"),
        evaluation_digest=digest_field("evaluation_digest"),
        runner_digest=digest_field("runner_digest"),
        corpus_manifest_digest=digest_field("corpus_manifest_digest"),
        evidence_artifact_count=len(evaluation.evidence_artifact_ids),
    )


def _evaluation_archive_gate_metadata(
    evaluation: CandidateEvaluation,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Return only validated stable reason codes plus the immutable gate mapping."""
    metrics = thaw_data(evaluation.metrics)
    gate = metrics.get("gate", {}) if isinstance(metrics, dict) else {}
    if not isinstance(gate, dict):
        gate = {}
    reasons = gate.get("reasons", [])
    if not isinstance(reasons, list):
        reasons = []
    safe_reasons = tuple(
        dict.fromkeys(
            item for item in reasons if isinstance(item, str) and _REASON_CODE_RE.fullmatch(item)
        )
    )
    return safe_reasons, gate


@dataclass(frozen=True, slots=True)
class ReconciliationCursor:
    """Owner-bound keyset cursor for restart-safe unknown-effect discovery."""

    updated_at: str
    candidate_id: str
    owner_digest: str

    def __post_init__(self) -> None:
        _require_id(self.updated_at, field_name="updated_at")
        _require_id(self.candidate_id, field_name="candidate_id")
        if not _SHA256_RE.fullmatch(self.owner_digest):
            raise ValueError("owner_digest must be a canonical sha256 digest")

    def to_dict(self) -> dict[str, str]:
        return {
            "updated_at": self.updated_at,
            "candidate_id": self.candidate_id,
            "owner_digest": self.owner_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReconciliationCursor:
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ReconciliationRequest:
    """One unknown promotion/rollback that requires observation, never replay."""

    record: CandidateRecord
    kind: ReconciliationKind

    def __post_init__(self) -> None:
        if not isinstance(self.record, CandidateRecord):
            raise TypeError("record must be a CandidateRecord")
        object.__setattr__(self, "kind", ReconciliationKind(self.kind))
        expected = {
            CandidateState.PROMOTION_UNKNOWN: ReconciliationKind.PROMOTION,
            CandidateState.ROLLBACK_UNKNOWN: ReconciliationKind.ROLLBACK,
        }.get(self.record.state)
        if expected is None or self.kind is not expected:
            raise ValueError("reconciliation kind must match an unknown candidate state")

    def to_dict(self) -> dict[str, Any]:
        return {"record": self.record.to_dict(), "kind": self.kind.value}


@dataclass(frozen=True, slots=True)
class ReconciliationPage:
    items: tuple[ReconciliationRequest, ...]
    next_cursor: ReconciliationCursor | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        if any(not isinstance(item, ReconciliationRequest) for item in self.items):
            raise TypeError("items must contain ReconciliationRequest values")
        if self.next_cursor is not None and not isinstance(
            self.next_cursor,
            ReconciliationCursor,
        ):
            raise TypeError("next_cursor must be a ReconciliationCursor")

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "next_cursor": (self.next_cursor.to_dict() if self.next_cursor is not None else None),
        }


@dataclass(frozen=True, slots=True)
class LearningReconciliationWorkerLease:
    """One exact-owner maintenance lease with a durable sweep cursor."""

    owner_digest: str
    worker_id: str
    lease_token: str = field(repr=False)
    fence: int
    lease_expires_at: str
    cursor: ReconciliationCursor | None = None

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.owner_digest):
            raise ValueError("owner_digest must be a canonical sha256 digest")
        _require_id(self.worker_id, field_name="worker_id")
        _require_id(self.lease_token, field_name="lease_token")
        if isinstance(self.fence, bool) or not isinstance(self.fence, int) or self.fence < 1:
            raise ValueError("fence must be a positive integer")
        expires_at = datetime.fromisoformat(self.lease_expires_at)
        if expires_at.tzinfo is None:
            raise ValueError("lease_expires_at must include a timezone")
        if self.cursor is not None and self.cursor.owner_digest != self.owner_digest:
            raise ValueError("cursor must be bound to owner_digest")

    def to_dict(self) -> dict[str, Any]:
        """Return an operator-safe view that never serializes the credential."""
        return {
            "owner_digest": self.owner_digest,
            "worker_id": self.worker_id,
            "fence": self.fence,
            "lease_expires_at": self.lease_expires_at,
            "cursor": self.cursor.to_dict() if self.cursor is not None else None,
        }


@dataclass(frozen=True, slots=True)
class LearningCandidateExport:
    """Portable inspection record; deleted candidates intentionally omit content."""

    record: CandidateRecord
    content: Any
    evaluations: tuple[CandidateEvaluation, ...]
    reconciliations: tuple[CandidateReconciliation, ...]
    exported_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", freeze_data(self.content))

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "content": thaw_data(self.content),
            "evaluations": [item.to_dict() for item in self.evaluations],
            "reconciliations": [item.to_dict() for item in self.reconciliations],
            "exported_at": self.exported_at,
        }


@dataclass(frozen=True, slots=True)
class LearningEvent:
    """Canonical content-free event committed with one candidate revision."""

    event_id: str
    candidate_id: str
    sequence: int
    event_type: str
    occurred_at: str
    tenant_id: str | None
    storage_namespace: str
    payload: Any = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "event_id",
            "candidate_id",
            "event_type",
            "occurred_at",
            "storage_namespace",
        ):
            _require_id(getattr(self, field_name), field_name=field_name)
        if self.sequence <= 0:
            raise ValueError("learning event sequence must be positive")
        object.__setattr__(self, "payload", freeze_data(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "candidate_id": self.candidate_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "tenant_id": self.tenant_id,
            "storage_namespace": self.storage_namespace,
            "payload": thaw_data(self.payload),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LearningEvent:
        return cls(**value)


@dataclass(frozen=True, slots=True)
class LearningOutboxItem:
    outbox_id: int
    event: LearningEvent
    lease_owner: str
    lease_token: str
    lease_expires_at: str


def _learning_event(
    before: CandidateRecord | None,
    after: CandidateRecord,
) -> LearningEvent:
    if before is None:
        event_type = "learning.candidate.captured"
    elif before.latest_reconciliation_id != after.latest_reconciliation_id:
        event_type = "learning.reconciliation.completed"
    elif before.latest_evaluation_id != after.latest_evaluation_id:
        event_type = "learning.candidate.evaluated"
    else:
        event_type = {
            CandidateState.QUARANTINED: "learning.candidate.quarantined",
            CandidateState.CAPTURED: "learning.candidate.restored",
            CandidateState.DELETED: "learning.candidate.deleted",
            CandidateState.PROMOTING: "learning.promotion.started",
            CandidateState.PROMOTED: "learning.promotion.completed",
            CandidateState.PROMOTION_UNKNOWN: "learning.promotion.unknown",
            CandidateState.ROLLING_BACK: "learning.rollback.started",
            CandidateState.ROLLED_BACK: "learning.rollback.completed",
            CandidateState.ROLLBACK_UNKNOWN: "learning.rollback.unknown",
        }.get(after.state, "learning.candidate.updated")
    sequence = after.revision + 1
    candidate_id = after.candidate.candidate_id
    event_id = (
        "learning-event:v1:"
        + hashlib.sha256(f"{candidate_id}\x00{sequence}\x00{event_type}".encode()).hexdigest()
    )
    return LearningEvent(
        event_id=event_id,
        candidate_id=candidate_id,
        sequence=sequence,
        event_type=event_type,
        occurred_at=after.updated_at,
        tenant_id=after.candidate.tenant_id,
        storage_namespace=after.candidate.storage_namespace,
        payload={
            "before": before.state.value if before is not None else None,
            "after": after.state.value,
            "revision": after.revision,
            "target": after.candidate.target.value,
            "risk": after.candidate.risk.value,
            "latest_evaluation_id": after.latest_evaluation_id,
            "latest_reconciliation_id": after.latest_reconciliation_id,
            "promotion_version": after.promotion_version,
            "supersedes_candidate_id": after.candidate.supersedes_candidate_id,
        },
    )


class CandidateNotFoundError(HarnessError):
    def __init__(self, candidate_id: str):
        super().__init__(
            code="LEARNING_CANDIDATE_NOT_FOUND",
            category="learning",
            message="The learning candidate is absent or not visible to this owner.",
            retryable=False,
            details={"candidate_id": candidate_id},
        )


def _agno_learned_knowledge_identity(
    candidate: LearningCandidate,
    content: dict[str, Any],
) -> tuple[str, str]:
    """Return the collision-resistant Agno title and bounded ledger reference."""
    title = str(content.get("title") or candidate.candidate_id).strip()
    if not title:
        title = candidate.candidate_id
    candidate_label = candidate.candidate_id[:64]
    digest_label = candidate.digest.removeprefix("sha256:")[:32]
    marker = f"[{candidate_label}:{digest_label}] "
    maximum_title = 512 - len(_AGNO_LEARNED_KNOWLEDGE_REFERENCE_PREFIX) - len(marker)
    if maximum_title < 1:
        raise ValueError("candidate identity leaves no room for an Agno learning title")
    stable_title = marker + title[:maximum_title]
    target_reference = _AGNO_LEARNED_KNOWLEDGE_REFERENCE_PREFIX + stable_title
    return stable_title, target_reference


class ReconciliationCursorScopeError(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            code="LEARNING_RECONCILIATION_CURSOR_SCOPE_MISMATCH",
            category="learning",
            message="The reconciliation cursor belongs to another learning scope.",
            retryable=False,
        )


class CandidateEvaluationNotFoundError(HarnessError):
    def __init__(self, evaluation_id: str):
        super().__init__(
            code="LEARNING_EVALUATION_NOT_FOUND",
            category="learning",
            message="The evaluation is absent or not visible to this owner.",
            retryable=False,
            details={"evaluation_id": evaluation_id},
        )


class CandidateReconciliationNotFoundError(HarnessError):
    def __init__(self, reconciliation_id: str):
        super().__init__(
            code="LEARNING_RECONCILIATION_NOT_FOUND",
            category="learning",
            message="The reconciliation is absent or not visible to this owner.",
            retryable=False,
            details={"reconciliation_id": reconciliation_id},
        )


class LearningApplicationNotFoundError(HarnessError):
    def __init__(self, application_id: str):
        super().__init__(
            code="LEARNING_APPLICATION_NOT_FOUND",
            category="learning",
            message="The learning application is absent or not visible to this owner.",
            retryable=False,
            details={"application_id": application_id},
        )


class LearningOutcomeNotFoundError(HarnessError):
    def __init__(self, outcome_id: str):
        super().__init__(
            code="LEARNING_OUTCOME_NOT_FOUND",
            category="learning",
            message="The learning outcome is absent or not visible to this owner.",
            retryable=False,
            details={"outcome_id": outcome_id},
        )


class LearningAttributionConflictError(HarnessError):
    def __init__(self, *, record_id: str):
        super().__init__(
            code="LEARNING_ATTRIBUTION_CONFLICT",
            category="learning",
            message="A learning attribution identifier was reused for different evidence.",
            retryable=False,
            details={"record_id": record_id},
        )


class CandidateConflictError(HarnessError):
    def __init__(self, candidate_id: str):
        super().__init__(
            code="LEARNING_CANDIDATE_CONFLICT",
            category="learning",
            message="The candidate identifier was reused for different content.",
            retryable=False,
            details={"candidate_id": candidate_id},
        )


class CandidateRevisionError(HarnessError):
    def __init__(self, candidate_id: str, *, expected: int, actual: int):
        super().__init__(
            code="LEARNING_CANDIDATE_REVISION_CONFLICT",
            category="learning",
            message="The learning candidate changed since it was read.",
            retryable=True,
            details={
                "candidate_id": candidate_id,
                "expected_revision": expected,
                "actual_revision": actual,
            },
        )


class CandidateTransitionError(HarnessError):
    def __init__(self, candidate_id: str, *, state: CandidateState, action: str):
        super().__init__(
            code="LEARNING_CANDIDATE_TRANSITION_INVALID",
            category="learning",
            message=f"Cannot {action} a candidate in state '{state.value}'.",
            retryable=False,
            details={
                "candidate_id": candidate_id,
                "state": state.value,
                "action": action,
            },
        )


class LearningPromotionUnknownError(HarnessError):
    def __init__(self, candidate_id: str):
        super().__init__(
            code="LEARNING_PROMOTION_UNKNOWN",
            category="learning",
            message=(
                "Promotion may have reached the learning backend. Reconcile it; "
                "do not blindly retry."
            ),
            retryable=False,
            details={"candidate_id": candidate_id},
        )


class LearningRollbackUnknownError(HarnessError):
    def __init__(self, candidate_id: str):
        super().__init__(
            code="LEARNING_ROLLBACK_UNKNOWN",
            category="learning",
            message=(
                "Rollback may have reached the learning backend. Reconcile it; "
                "do not blindly retry."
            ),
            retryable=False,
            details={"candidate_id": candidate_id},
        )


class LearningOutboxLeaseError(HarnessError):
    def __init__(self, outbox_id: int):
        super().__init__(
            code="LEARNING_OUTBOX_LEASE_INVALID",
            category="learning",
            message="The learning outbox item is not owned by this live lease.",
            retryable=False,
            details={"outbox_id": outbox_id},
        )


class LearningReconciliationWorkerLeaseError(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            code="LEARNING_RECONCILIATION_WORKER_LEASE_LOST",
            category="learning",
            message="The learning reconciliation worker no longer owns its live lease.",
            retryable=True,
        )


@runtime_checkable
class LearningLedger(Protocol):
    @property
    def schema_version(self) -> int: ...

    def close(self) -> None: ...

    def create_candidate(self, candidate: LearningCandidate) -> CandidateRecord: ...

    def get_candidate(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
    ) -> CandidateRecord: ...

    def list_candidates(
        self,
        *,
        owner: LearningOwner,
        limit: int = 100,
        state: CandidateState | None = None,
    ) -> list[CandidateRecord]: ...

    def scan_reconciliation_required(
        self,
        *,
        owner: LearningOwner,
        limit: int = 100,
        cursor: ReconciliationCursor | None = None,
    ) -> ReconciliationPage: ...

    def get_evaluation(
        self,
        evaluation_id: str,
        *,
        owner: LearningOwner,
    ) -> CandidateEvaluation: ...

    def list_evaluations(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        limit: int = 100,
    ) -> list[CandidateEvaluation]: ...

    def get_reconciliation(
        self,
        reconciliation_id: str,
        *,
        owner: LearningOwner,
    ) -> CandidateReconciliation: ...

    def list_reconciliations(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        limit: int = 100,
    ) -> list[CandidateReconciliation]: ...

    def record_evaluation(
        self,
        evaluation: CandidateEvaluation,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
    ) -> CandidateRecord: ...

    def record_reconciliation(
        self,
        reconciliation: CandidateReconciliation,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
    ) -> CandidateRecord: ...

    def begin_promotion(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
        promotion_id: str,
        actor: PromotionActor,
    ) -> CandidateRecord: ...

    def settle_promotion(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
        succeeded: bool,
        target_reference: str | None,
    ) -> CandidateRecord: ...

    def transition_candidate(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
        action: CandidateAction,
    ) -> CandidateRecord: ...

    def begin_rollback(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
        rollback_id: str,
        actor: PromotionActor,
    ) -> CandidateRecord: ...

    def settle_rollback(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
        succeeded: bool,
    ) -> CandidateRecord: ...

    def list_artifact_storage_keys(self) -> list[str]: ...

    def list_events(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[LearningEvent]: ...

    def lease_outbox(
        self,
        *,
        owner: str,
        limit: int = 100,
        lease_seconds: int = 30,
    ) -> list[LearningOutboxItem]: ...

    def acknowledge_outbox(self, *, outbox_id: int, lease_token: str) -> None: ...


@runtime_checkable
class EvaluationArchiveLedger(Protocol):
    """Optional owner-scoped negative-evaluation read model for custom ledgers."""

    def query_evaluation_archive(
        self,
        *,
        owner: LearningOwner,
        query: EvaluationArchiveQuery,
    ) -> EvaluationArchivePage: ...


@runtime_checkable
class LearningOutcomeLedger(Protocol):
    """Optional durable attribution extension for custom learning ledgers."""

    def record_application(
        self,
        application: LearningApplication,
        *,
        owner: LearningOwner,
    ) -> LearningApplication: ...

    def get_application(
        self,
        application_id: str,
        *,
        owner: LearningOwner,
    ) -> LearningApplication: ...

    def list_applications(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        limit: int = 100,
    ) -> list[LearningApplication]: ...

    def record_outcome(
        self,
        outcome: LearningOutcome,
        *,
        owner: LearningOwner,
    ) -> LearningOutcome: ...

    def get_outcome(
        self,
        outcome_id: str,
        *,
        owner: LearningOwner,
    ) -> LearningOutcome: ...

    def list_outcomes(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        limit: int = 100,
    ) -> list[LearningOutcome]: ...

    def summarize_effectiveness(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        policy: LearningEffectivenessPolicy,
    ) -> LearningEffectivenessSummary: ...


class SQLiteLearningLedger:
    """Transactional local learning candidate authority."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            resolved = Path(self.path).expanduser().resolve(strict=False)
            resolved.parent.mkdir(parents=True, exist_ok=True)
            self.path = str(resolved)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM learning_schema_migrations"
            ).fetchone()
        return int(row["version"])

    def migrate(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS learning_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_candidates (
                candidate_id TEXT PRIMARY KEY,
                candidate_digest TEXT NOT NULL,
                tenant_id TEXT,
                storage_namespace TEXT NOT NULL,
                state TEXT NOT NULL,
                revision INTEGER NOT NULL,
                content_storage_key TEXT NOT NULL,
                record_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS learning_candidates_owner_idx
            ON learning_candidates(tenant_id, storage_namespace, state, updated_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                evaluation_digest TEXT NOT NULL,
                evaluation_json TEXT NOT NULL,
                tenant_id TEXT,
                storage_namespace TEXT,
                target TEXT,
                mechanism_version TEXT,
                verdict TEXT,
                evaluator_digest TEXT,
                safety_passed INTEGER,
                reason_codes_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(candidate_id) REFERENCES learning_candidates(candidate_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS learning_evaluations_candidate_idx
            ON learning_evaluations(candidate_id, created_at DESC, evaluation_id DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_applications (
                application_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                application_kind TEXT NOT NULL,
                application_digest TEXT NOT NULL,
                application_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                UNIQUE(candidate_id, run_id, application_kind),
                FOREIGN KEY(candidate_id) REFERENCES learning_candidates(candidate_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS learning_applications_candidate_idx
            ON learning_applications(candidate_id, observed_at DESC, application_id DESC)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS learning_applications_run_kind_idx
            ON learning_applications(candidate_id, run_id, application_kind)
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_outcomes (
                outcome_id TEXT PRIMARY KEY,
                application_id TEXT NOT NULL UNIQUE,
                candidate_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                outcome_kind TEXT NOT NULL,
                score REAL NOT NULL,
                outcome_digest TEXT NOT NULL,
                outcome_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY(application_id) REFERENCES learning_applications(application_id)
                    ON DELETE CASCADE,
                FOREIGN KEY(candidate_id) REFERENCES learning_candidates(candidate_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS learning_outcomes_candidate_idx
            ON learning_outcomes(candidate_id, recorded_at DESC, outcome_id DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_evaluation_reasons (
                evaluation_id TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(evaluation_id, reason_code),
                FOREIGN KEY(evaluation_id) REFERENCES learning_evaluations(evaluation_id)
                    ON DELETE CASCADE,
                FOREIGN KEY(candidate_id) REFERENCES learning_candidates(candidate_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_reconciliations (
                reconciliation_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                reconciliation_digest TEXT NOT NULL,
                reconciliation_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(candidate_id) REFERENCES learning_candidates(candidate_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_candidate_mutations (
                candidate_id TEXT NOT NULL,
                mutation_id TEXT NOT NULL,
                mutation_digest TEXT NOT NULL,
                record_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(candidate_id, mutation_id),
                FOREIGN KEY(candidate_id) REFERENCES learning_candidates(candidate_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_events (
                candidate_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                event_json TEXT NOT NULL,
                PRIMARY KEY(candidate_id, sequence),
                FOREIGN KEY(candidate_id) REFERENCES learning_candidates(candidate_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_outbox (
                outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                delivered_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(candidate_id, sequence),
                FOREIGN KEY(candidate_id, sequence)
                    REFERENCES learning_events(candidate_id, sequence)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS learning_outbox_ready_idx
            ON learning_outbox(status, lease_expires_at, outbox_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_reconciliation_workers (
                owner_digest TEXT PRIMARY KEY,
                worker_id TEXT,
                lease_token TEXT,
                lease_fence INTEGER NOT NULL DEFAULT 0,
                lease_expires_at REAL NOT NULL DEFAULT 0,
                cursor_json TEXT,
                updated_at TEXT NOT NULL
            )
            """,
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    self._connection.execute(statement)
                self._migrate_evaluation_archive_v5_locked()
                for version in range(1, LEARNING_LEDGER_SCHEMA_VERSION + 1):
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO learning_schema_migrations(version, applied_at)
                        VALUES (?, ?)
                        """,
                        (version, _now()),
                    )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def _migrate_evaluation_archive_v5_locked(self) -> None:
        columns = {
            str(row["name"])
            for row in self._connection.execute(
                "PRAGMA table_info(learning_evaluations)"
            ).fetchall()
        }
        for name, definition in (
            ("tenant_id", "TEXT"),
            ("storage_namespace", "TEXT"),
            ("target", "TEXT"),
            ("mechanism_version", "TEXT"),
            ("verdict", "TEXT"),
            ("evaluator_digest", "TEXT"),
            ("safety_passed", "INTEGER"),
            ("reason_codes_json", "TEXT"),
        ):
            if name not in columns:
                self._connection.execute(
                    f"ALTER TABLE learning_evaluations ADD COLUMN {name} {definition}"
                )
        self._connection.execute(
            """
            UPDATE learning_evaluations AS e
            SET tenant_id = (
                    SELECT c.tenant_id FROM learning_candidates AS c
                    WHERE c.candidate_id = e.candidate_id
                ),
                storage_namespace = (
                    SELECT c.storage_namespace FROM learning_candidates AS c
                    WHERE c.candidate_id = e.candidate_id
                ),
                target = (
                    SELECT json_extract(c.record_json, '$.candidate.target')
                    FROM learning_candidates AS c
                    WHERE c.candidate_id = e.candidate_id
                ),
                mechanism_version = (
                    SELECT json_extract(c.record_json, '$.candidate.mechanism_version')
                    FROM learning_candidates AS c
                    WHERE c.candidate_id = e.candidate_id
                ),
                verdict = json_extract(e.evaluation_json, '$.verdict'),
                evaluator_digest = json_extract(
                    e.evaluation_json,
                    '$.evaluator_digest'
                ),
                safety_passed = json_extract(e.evaluation_json, '$.safety_passed')
            WHERE storage_namespace IS NULL
               OR target IS NULL
               OR mechanism_version IS NULL
               OR verdict IS NULL
               OR evaluator_digest IS NULL
               OR safety_passed IS NULL
            """
        )
        cursor = self._connection.execute(
            """
            SELECT evaluation_id, candidate_id, evaluation_json, created_at
            FROM learning_evaluations
            WHERE reason_codes_json IS NULL
            """
        )
        while rows := cursor.fetchmany(1_000):
            reason_rows: list[tuple[str, str, str, str]] = []
            marker_rows: list[tuple[str, str]] = []
            for row in rows:
                evaluation = CandidateEvaluation.from_dict(json.loads(row["evaluation_json"]))
                reasons, _ = _evaluation_archive_gate_metadata(evaluation)
                marker_rows.append((_canonical_json(list(reasons)), str(row["evaluation_id"])))
                reason_rows.extend(
                    (
                        str(row["evaluation_id"]),
                        reason,
                        str(row["candidate_id"]),
                        str(row["created_at"]),
                    )
                    for reason in reasons
                )
            self._connection.executemany(
                """
                UPDATE learning_evaluations SET reason_codes_json = ?
                WHERE evaluation_id = ?
                """,
                marker_rows,
            )
            self._connection.executemany(
                """
                INSERT OR IGNORE INTO learning_evaluation_reasons(
                    evaluation_id, reason_code, candidate_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                reason_rows,
            )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS learning_evaluations_archive_owner_idx
            ON learning_evaluations(
                tenant_id, storage_namespace, created_at DESC, evaluation_id DESC
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS learning_evaluations_archive_filter_idx
            ON learning_evaluations(
                tenant_id, storage_namespace, verdict, evaluator_digest,
                safety_passed, target, mechanism_version,
                created_at DESC, evaluation_id DESC
            )
            """
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> CandidateRecord:
        return CandidateRecord.from_dict(json.loads(row["record_json"]))

    @staticmethod
    def _owner_matches(record: CandidateRecord, owner: LearningOwner) -> bool:
        return (
            record.candidate.tenant_id == owner.tenant_id
            and record.candidate.storage_namespace == owner.storage_namespace
        )

    def _get_locked(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
    ) -> CandidateRecord:
        row = self._connection.execute(
            "SELECT record_json FROM learning_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise CandidateNotFoundError(candidate_id)
        record = self._record(row)
        if not self._owner_matches(record, owner):
            raise CandidateNotFoundError(candidate_id)
        return record

    def _save_event(
        self,
        before: CandidateRecord | None,
        after: CandidateRecord,
    ) -> None:
        event = _learning_event(before, after)
        event_json = _canonical_json(event.to_dict())
        self._connection.execute(
            """
            INSERT INTO learning_events(
                candidate_id, sequence, event_id, event_type, occurred_at, event_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.candidate_id,
                event.sequence,
                event.event_id,
                event.event_type,
                event.occurred_at,
                event_json,
            ),
        )
        self._connection.execute(
            """
            INSERT INTO learning_outbox(
                candidate_id, sequence, event_json, status, created_at
            ) VALUES (?, ?, ?, 'pending', ?)
            """,
            (event.candidate_id, event.sequence, event_json, event.occurred_at),
        )

    def create_candidate(self, candidate: LearningCandidate) -> CandidateRecord:
        record = CandidateRecord(candidate=candidate)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    """
                    SELECT candidate_digest, record_json FROM learning_candidates
                    WHERE candidate_id = ?
                    """,
                    (candidate.candidate_id,),
                ).fetchone()
                if existing is not None:
                    if existing["candidate_digest"] != candidate.digest:
                        raise CandidateConflictError(candidate.candidate_id)
                    self._connection.execute("COMMIT")
                    return self._record(existing)
                if candidate.supersedes_candidate_id is not None:
                    parent = self._get_locked(
                        candidate.supersedes_candidate_id,
                        owner=candidate.owner,
                    )
                    if parent.state is CandidateState.DELETED:
                        raise CandidateTransitionError(
                            candidate.supersedes_candidate_id,
                            state=parent.state,
                            action="supersede",
                        )
                    if parent.candidate.target is not candidate.target:
                        raise HarnessError(
                            code="LEARNING_CANDIDATE_TARGET_CONFLICT",
                            category="learning",
                            message="An edited candidate cannot change learning target.",
                            retryable=False,
                            details={"candidate_id": candidate.candidate_id},
                        )
                self._connection.execute(
                    """
                    INSERT INTO learning_candidates(
                        candidate_id, candidate_digest, tenant_id, storage_namespace,
                        state, revision, content_storage_key, record_json, created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.candidate_id,
                        candidate.digest,
                        candidate.tenant_id,
                        candidate.storage_namespace,
                        record.state.value,
                        record.revision,
                        candidate.content_artifact.storage_key,
                        _canonical_json(record.to_dict()),
                        candidate.created_at,
                        record.updated_at,
                    ),
                )
                self._save_event(None, record)
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        return record

    def get_candidate(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
    ) -> CandidateRecord:
        with self._lock:
            return self._get_locked(candidate_id, owner=owner)

    def list_candidates(
        self,
        *,
        owner: LearningOwner,
        limit: int = 100,
        state: CandidateState | None = None,
    ) -> list[CandidateRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        sql = (
            "SELECT record_json FROM learning_candidates "
            "WHERE tenant_id IS ? AND storage_namespace = ?"
        )
        params: list[Any] = [owner.tenant_id, owner.storage_namespace]
        if state is not None:
            sql += " AND state = ?"
            params.append(CandidateState(state).value)
        sql += " ORDER BY created_at DESC, candidate_id LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [self._record(row) for row in rows]

    def scan_reconciliation_required(
        self,
        *,
        owner: LearningOwner,
        limit: int = 100,
        cursor: ReconciliationCursor | None = None,
    ) -> ReconciliationPage:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if cursor is not None and cursor.owner_digest != owner.digest:
            raise ReconciliationCursorScopeError()
        sql = (
            "SELECT record_json, updated_at, candidate_id FROM learning_candidates "
            "WHERE tenant_id IS ? AND storage_namespace = ? "
            "AND state IN (?, ?)"
        )
        params: list[Any] = [
            owner.tenant_id,
            owner.storage_namespace,
            CandidateState.PROMOTION_UNKNOWN.value,
            CandidateState.ROLLBACK_UNKNOWN.value,
        ]
        if cursor is not None:
            sql += " AND (updated_at > ? OR (updated_at = ? AND candidate_id > ?))"
            params.extend([cursor.updated_at, cursor.updated_at, cursor.candidate_id])
        sql += " ORDER BY updated_at ASC, candidate_id ASC LIMIT ?"
        params.append(limit + 1)
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        selected = rows[:limit]
        requests = tuple(
            ReconciliationRequest(
                record=(record := self._record(row)),
                kind=(
                    ReconciliationKind.PROMOTION
                    if record.state is CandidateState.PROMOTION_UNKNOWN
                    else ReconciliationKind.ROLLBACK
                ),
            )
            for row in selected
        )
        next_cursor = None
        if len(rows) > limit:
            last = selected[-1]
            next_cursor = ReconciliationCursor(
                updated_at=str(last["updated_at"]),
                candidate_id=str(last["candidate_id"]),
                owner_digest=owner.digest,
            )
        return ReconciliationPage(items=requests, next_cursor=next_cursor)

    def get_evaluation(
        self,
        evaluation_id: str,
        *,
        owner: LearningOwner,
    ) -> CandidateEvaluation:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT candidate_id, evaluation_json FROM learning_evaluations
                WHERE evaluation_id = ?
                """,
                (evaluation_id,),
            ).fetchone()
            if row is None:
                raise CandidateEvaluationNotFoundError(evaluation_id)
            try:
                self._get_locked(str(row["candidate_id"]), owner=owner)
            except CandidateNotFoundError as exc:
                raise CandidateEvaluationNotFoundError(evaluation_id) from exc
        return CandidateEvaluation.from_dict(json.loads(row["evaluation_json"]))

    def list_evaluations(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        limit: int = 100,
    ) -> list[CandidateEvaluation]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            self._get_locked(candidate_id, owner=owner)
            rows = self._connection.execute(
                """
                SELECT evaluation_json FROM learning_evaluations
                WHERE candidate_id = ?
                ORDER BY created_at DESC, evaluation_id LIMIT ?
                """,
                (candidate_id, limit),
            ).fetchall()
        return [CandidateEvaluation.from_dict(json.loads(row["evaluation_json"])) for row in rows]

    def record_application(
        self,
        application: LearningApplication,
        *,
        owner: LearningOwner,
    ) -> LearningApplication:
        if not isinstance(application, LearningApplication):
            raise TypeError("application must be a LearningApplication")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                candidate = self._get_locked(application.candidate_id, owner=owner)
                existing = self._connection.execute(
                    """
                    SELECT application_digest, application_json
                    FROM learning_applications
                    WHERE application_id = ?
                       OR (
                           candidate_id = ? AND run_id = ? AND application_kind = ?
                       )
                    """,
                    (
                        application.application_id,
                        application.candidate_id,
                        application.run_id,
                        application.kind.value,
                    ),
                ).fetchone()
                if existing is not None:
                    if existing["application_digest"] != application.digest:
                        raise LearningAttributionConflictError(record_id=application.application_id)
                    self._connection.execute("COMMIT")
                    return LearningApplication.from_dict(json.loads(existing["application_json"]))
                if candidate.state is not CandidateState.PROMOTED:
                    raise CandidateTransitionError(
                        application.candidate_id,
                        state=candidate.state,
                        action="record application",
                    )
                if candidate.target_reference != application.target_reference:
                    raise HarnessError(
                        code="LEARNING_APPLICATION_TARGET_MISMATCH",
                        category="learning",
                        message="Application evidence does not match the promoted target.",
                        retryable=False,
                        details={"candidate_id": application.candidate_id},
                    )
                if candidate.candidate.expires_at is not None and datetime.fromisoformat(
                    candidate.candidate.expires_at
                ) <= datetime.now(UTC):
                    raise HarnessError(
                        code="LEARNING_APPLICATION_EXPIRED",
                        category="learning",
                        message="An expired learning cannot receive new application evidence.",
                        retryable=False,
                        details={"candidate_id": application.candidate_id},
                    )
                self._connection.execute(
                    """
                    INSERT INTO learning_applications(
                        application_id, candidate_id, run_id, application_kind,
                        application_digest, application_json, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        application.application_id,
                        application.candidate_id,
                        application.run_id,
                        application.kind.value,
                        application.digest,
                        _canonical_json(application.to_dict()),
                        application.observed_at,
                    ),
                )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        return application

    def get_application(
        self,
        application_id: str,
        *,
        owner: LearningOwner,
    ) -> LearningApplication:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT candidate_id, application_json FROM learning_applications
                WHERE application_id = ?
                """,
                (application_id,),
            ).fetchone()
            if row is None:
                raise LearningApplicationNotFoundError(application_id)
            try:
                self._get_locked(str(row["candidate_id"]), owner=owner)
            except CandidateNotFoundError as exc:
                raise LearningApplicationNotFoundError(application_id) from exc
        return LearningApplication.from_dict(json.loads(row["application_json"]))

    def list_applications(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        limit: int = 100,
    ) -> list[LearningApplication]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            self._get_locked(candidate_id, owner=owner)
            rows = self._connection.execute(
                """
                SELECT application_json FROM learning_applications
                WHERE candidate_id = ?
                ORDER BY observed_at DESC, application_id DESC LIMIT ?
                """,
                (candidate_id, limit),
            ).fetchall()
        return [LearningApplication.from_dict(json.loads(row["application_json"])) for row in rows]

    def record_outcome(
        self,
        outcome: LearningOutcome,
        *,
        owner: LearningOwner,
    ) -> LearningOutcome:
        if not isinstance(outcome, LearningOutcome):
            raise TypeError("outcome must be a LearningOutcome")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                candidate = self._get_locked(outcome.candidate_id, owner=owner)
                if candidate.state is CandidateState.DELETED:
                    raise CandidateTransitionError(
                        outcome.candidate_id,
                        state=candidate.state,
                        action="record outcome",
                    )
                existing = self._connection.execute(
                    """
                    SELECT outcome_digest, outcome_json FROM learning_outcomes
                    WHERE outcome_id = ? OR application_id = ?
                    """,
                    (outcome.outcome_id, outcome.application_id),
                ).fetchone()
                if existing is not None:
                    if existing["outcome_digest"] != outcome.digest:
                        raise LearningAttributionConflictError(record_id=outcome.outcome_id)
                    self._connection.execute("COMMIT")
                    return LearningOutcome.from_dict(json.loads(existing["outcome_json"]))
                application_row = self._connection.execute(
                    """
                    SELECT application_json FROM learning_applications
                    WHERE application_id = ? AND candidate_id = ?
                    """,
                    (outcome.application_id, outcome.candidate_id),
                ).fetchone()
                if application_row is None:
                    raise LearningApplicationNotFoundError(outcome.application_id)
                application = LearningApplication.from_dict(
                    json.loads(application_row["application_json"])
                )
                if application.kind is not LearningApplicationKind.APPLIED:
                    raise HarnessError(
                        code="LEARNING_OUTCOME_NOT_APPLIED",
                        category="learning",
                        message="An outcome can only be attributed to an applied learning.",
                        retryable=False,
                        details={"application_id": outcome.application_id},
                    )
                if application.run_id != outcome.run_id:
                    raise HarnessError(
                        code="LEARNING_OUTCOME_RUN_MISMATCH",
                        category="learning",
                        message="Outcome evidence must match the attributed run.",
                        retryable=False,
                        details={"application_id": outcome.application_id},
                    )
                if application.observer_digest == outcome.evaluator_digest or set(
                    application.evidence_artifact_ids
                ).intersection(outcome.evidence_artifact_ids):
                    raise HarnessError(
                        code="LEARNING_OUTCOME_INDEPENDENCE_REQUIRED",
                        category="learning",
                        message=("Outcome evaluation must use a distinct evaluator and evidence."),
                        retryable=False,
                        details={"application_id": outcome.application_id},
                    )
                self._connection.execute(
                    """
                    INSERT INTO learning_outcomes(
                        outcome_id, application_id, candidate_id, run_id, outcome_kind,
                        score, outcome_digest, outcome_json, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        outcome.outcome_id,
                        outcome.application_id,
                        outcome.candidate_id,
                        outcome.run_id,
                        outcome.kind.value,
                        outcome.score,
                        outcome.digest,
                        _canonical_json(outcome.to_dict()),
                        outcome.recorded_at,
                    ),
                )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        return outcome

    def get_outcome(
        self,
        outcome_id: str,
        *,
        owner: LearningOwner,
    ) -> LearningOutcome:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT candidate_id, outcome_json FROM learning_outcomes
                WHERE outcome_id = ?
                """,
                (outcome_id,),
            ).fetchone()
            if row is None:
                raise LearningOutcomeNotFoundError(outcome_id)
            try:
                self._get_locked(str(row["candidate_id"]), owner=owner)
            except CandidateNotFoundError as exc:
                raise LearningOutcomeNotFoundError(outcome_id) from exc
        return LearningOutcome.from_dict(json.loads(row["outcome_json"]))

    def list_outcomes(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        limit: int = 100,
    ) -> list[LearningOutcome]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            self._get_locked(candidate_id, owner=owner)
            rows = self._connection.execute(
                """
                SELECT outcome_json FROM learning_outcomes
                WHERE candidate_id = ?
                ORDER BY recorded_at DESC, outcome_id DESC LIMIT ?
                """,
                (candidate_id, limit),
            ).fetchall()
        return [LearningOutcome.from_dict(json.loads(row["outcome_json"])) for row in rows]

    def summarize_effectiveness(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        policy: LearningEffectivenessPolicy,
    ) -> LearningEffectivenessSummary:
        if not isinstance(policy, LearningEffectivenessPolicy):
            raise TypeError("policy must be a LearningEffectivenessPolicy")
        with self._lock:
            self._get_locked(candidate_id, owner=owner)
            applications = self._connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN application_kind = 'applied' THEN 1 ELSE 0 END)
                           AS applied
                FROM learning_applications WHERE candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            outcomes = self._connection.execute(
                """
                SELECT COUNT(*) AS total, COUNT(DISTINCT run_id) AS runs,
                       SUM(CASE WHEN outcome_kind = 'success' THEN 1 ELSE 0 END)
                           AS successes,
                       SUM(CASE WHEN outcome_kind = 'failure' THEN 1 ELSE 0 END)
                           AS failures,
                       SUM(CASE WHEN outcome_kind = 'correction' THEN 1 ELSE 0 END)
                           AS corrections,
                       SUM(CASE WHEN outcome_kind = 'neutral' THEN 1 ELSE 0 END)
                           AS neutral,
                       SUM(score) AS score_sum
                FROM learning_outcomes WHERE candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
        return _learning_effectiveness_summary(
            candidate_id=candidate_id,
            total_applications=int(applications["total"] or 0),
            applied_applications=int(applications["applied"] or 0),
            evaluated_outcomes=int(outcomes["total"] or 0),
            independent_runs=int(outcomes["runs"] or 0),
            successes=int(outcomes["successes"] or 0),
            failures=int(outcomes["failures"] or 0),
            corrections=int(outcomes["corrections"] or 0),
            neutral=int(outcomes["neutral"] or 0),
            score_sum=(float(outcomes["score_sum"]) if outcomes["score_sum"] is not None else None),
            policy=policy,
        )

    def query_evaluation_archive(
        self,
        *,
        owner: LearningOwner,
        query: EvaluationArchiveQuery,
    ) -> EvaluationArchivePage:
        if not isinstance(query, EvaluationArchiveQuery):
            raise TypeError("query must be an EvaluationArchiveQuery")
        if query.cursor is not None and query.cursor.owner_digest != owner.digest:
            raise HarnessError(
                code="LEARNING_EVALUATION_ARCHIVE_CURSOR_SCOPE",
                category="learning",
                message="The evaluation archive cursor belongs to another owner.",
                retryable=False,
            )
        verdict_placeholders = ",".join("?" for _ in query.verdicts)
        sql = f"""
            SELECT e.evaluation_json, c.record_json, e.created_at, e.evaluation_id
            FROM learning_candidates AS c
            JOIN learning_evaluations AS e ON e.candidate_id = c.candidate_id
            WHERE e.tenant_id IS ? AND e.storage_namespace = ?
              AND e.verdict IN ({verdict_placeholders})
        """
        params: list[Any] = [
            owner.tenant_id,
            owner.storage_namespace,
            *(item.value for item in query.verdicts),
        ]
        if query.evaluator_digest is not None:
            sql += " AND e.evaluator_digest = ?"
            params.append(query.evaluator_digest)
        if query.reason_code is not None:
            sql += """
                AND EXISTS (
                    SELECT 1 FROM learning_evaluation_reasons AS reason
                    WHERE reason.evaluation_id = e.evaluation_id
                      AND reason.reason_code = ?
                )
            """
            params.append(query.reason_code)
        if query.mechanism_version is not None:
            sql += " AND e.mechanism_version = ?"
            params.append(query.mechanism_version)
        if query.target is not None:
            sql += " AND e.target = ?"
            params.append(query.target.value)
        if query.safety_passed is not None:
            sql += " AND e.safety_passed = ?"
            params.append(int(query.safety_passed))
        if query.cursor is not None:
            sql += """
                AND (
                    e.created_at < ?
                    OR (e.created_at = ? AND e.evaluation_id < ?)
                )
            """
            params.extend(
                [
                    query.cursor.evaluated_at,
                    query.cursor.evaluated_at,
                    query.cursor.evaluation_id,
                ]
            )
        sql += " ORDER BY e.created_at DESC, e.evaluation_id DESC LIMIT ?"
        params.append(query.limit + 1)
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        selected = rows[: query.limit]
        items = tuple(
            evaluation_archive_entry(
                self._record(row),
                CandidateEvaluation.from_dict(json.loads(row["evaluation_json"])),
            )
            for row in selected
        )
        next_cursor = None
        if len(rows) > query.limit:
            last = selected[-1]
            next_cursor = EvaluationArchiveCursor(
                evaluated_at=str(last["created_at"]),
                evaluation_id=str(last["evaluation_id"]),
                owner_digest=owner.digest,
            )
        return EvaluationArchivePage(items=items, next_cursor=next_cursor)

    def get_reconciliation(
        self,
        reconciliation_id: str,
        *,
        owner: LearningOwner,
    ) -> CandidateReconciliation:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT candidate_id, reconciliation_json FROM learning_reconciliations
                WHERE reconciliation_id = ?
                """,
                (reconciliation_id,),
            ).fetchone()
            if row is None:
                raise CandidateReconciliationNotFoundError(reconciliation_id)
            try:
                self._get_locked(str(row["candidate_id"]), owner=owner)
            except CandidateNotFoundError as exc:
                raise CandidateReconciliationNotFoundError(reconciliation_id) from exc
        return CandidateReconciliation.from_dict(json.loads(row["reconciliation_json"]))

    def list_reconciliations(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        limit: int = 100,
    ) -> list[CandidateReconciliation]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            self._get_locked(candidate_id, owner=owner)
            rows = self._connection.execute(
                """
                SELECT reconciliation_json FROM learning_reconciliations
                WHERE candidate_id = ?
                ORDER BY created_at DESC, reconciliation_id LIMIT ?
                """,
                (candidate_id, limit),
            ).fetchall()
        return [
            CandidateReconciliation.from_dict(json.loads(row["reconciliation_json"]))
            for row in rows
        ]

    def _idempotent_mutation(
        self,
        candidate_id: str,
        *,
        mutation_id: str,
        mutation_digest: str,
    ) -> CandidateRecord | None:
        row = self._connection.execute(
            """
            SELECT mutation_digest, record_json FROM learning_candidate_mutations
            WHERE candidate_id = ? AND mutation_id = ?
            """,
            (candidate_id, mutation_id),
        ).fetchone()
        if row is None:
            return None
        if row["mutation_digest"] != mutation_digest:
            raise CandidateConflictError(candidate_id)
        return CandidateRecord.from_dict(json.loads(row["record_json"]))

    def _save_mutation(
        self,
        before: CandidateRecord,
        after: CandidateRecord,
        *,
        mutation_id: str,
        mutation_digest: str,
    ) -> None:
        updated = self._connection.execute(
            """
            UPDATE learning_candidates
            SET state = ?, revision = ?, record_json = ?, updated_at = ?
            WHERE candidate_id = ? AND revision = ?
            """,
            (
                after.state.value,
                after.revision,
                _canonical_json(after.to_dict()),
                after.updated_at,
                after.candidate.candidate_id,
                before.revision,
            ),
        )
        if updated.rowcount != 1:
            raise CandidateRevisionError(
                before.candidate.candidate_id,
                expected=before.revision,
                actual=self._get_locked(
                    before.candidate.candidate_id,
                    owner=before.candidate.owner,
                ).revision,
            )
        self._connection.execute(
            """
            INSERT INTO learning_candidate_mutations(
                candidate_id, mutation_id, mutation_digest, record_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                after.candidate.candidate_id,
                mutation_id,
                mutation_digest,
                _canonical_json(after.to_dict()),
                after.updated_at,
            ),
        )
        self._save_event(before, after)

    def record_evaluation(
        self,
        evaluation: CandidateEvaluation,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
    ) -> CandidateRecord:
        mutation_digest = _digest({"action": "evaluate", "evaluation_digest": evaluation.digest})
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                before = self._get_locked(evaluation.candidate_id, owner=owner)
                replay = self._idempotent_mutation(
                    evaluation.candidate_id,
                    mutation_id=mutation_id,
                    mutation_digest=mutation_digest,
                )
                if replay is not None:
                    self._connection.execute("COMMIT")
                    return replay
                if before.revision != expected_revision:
                    raise CandidateRevisionError(
                        evaluation.candidate_id,
                        expected=expected_revision,
                        actual=before.revision,
                    )
                if before.state not in {
                    CandidateState.CAPTURED,
                    CandidateState.QUALIFIED,
                    CandidateState.REJECTED,
                }:
                    raise CandidateTransitionError(
                        evaluation.candidate_id,
                        state=before.state,
                        action="evaluate",
                    )
                next_state = {
                    EvaluationVerdict.QUALIFIED: CandidateState.QUALIFIED,
                    EvaluationVerdict.REJECTED: CandidateState.REJECTED,
                    EvaluationVerdict.INCONCLUSIVE: CandidateState.CAPTURED,
                }[evaluation.verdict]
                after = replace(
                    before,
                    state=next_state,
                    revision=before.revision + 1,
                    latest_evaluation_id=evaluation.evaluation_id,
                    updated_at=evaluation.evaluated_at,
                )
                reason_codes, _ = _evaluation_archive_gate_metadata(evaluation)
                self._connection.execute(
                    """
                    INSERT INTO learning_evaluations(
                        evaluation_id, candidate_id, evaluation_digest,
                        evaluation_json, tenant_id, storage_namespace, target,
                        mechanism_version, verdict, evaluator_digest, safety_passed,
                        reason_codes_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evaluation.evaluation_id,
                        evaluation.candidate_id,
                        evaluation.digest,
                        _canonical_json(evaluation.to_dict()),
                        owner.tenant_id,
                        owner.storage_namespace,
                        before.candidate.target.value,
                        before.candidate.mechanism_version,
                        evaluation.verdict.value,
                        evaluation.evaluator_digest,
                        int(evaluation.safety_passed),
                        _canonical_json(list(reason_codes)),
                        evaluation.evaluated_at,
                    ),
                )
                self._connection.executemany(
                    """
                    INSERT INTO learning_evaluation_reasons(
                        evaluation_id, reason_code, candidate_id, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (
                            evaluation.evaluation_id,
                            reason,
                            evaluation.candidate_id,
                            evaluation.evaluated_at,
                        )
                        for reason in reason_codes
                    ],
                )
                self._save_mutation(
                    before,
                    after,
                    mutation_id=mutation_id,
                    mutation_digest=mutation_digest,
                )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        return after

    def record_reconciliation(
        self,
        reconciliation: CandidateReconciliation,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
    ) -> CandidateRecord:
        mutation_digest = _digest(
            {"action": "reconcile", "reconciliation_digest": reconciliation.digest}
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                before = self._get_locked(reconciliation.candidate_id, owner=owner)
                replay = self._idempotent_mutation(
                    reconciliation.candidate_id,
                    mutation_id=mutation_id,
                    mutation_digest=mutation_digest,
                )
                if replay is not None:
                    self._connection.execute("COMMIT")
                    return replay
                existing = self._connection.execute(
                    """
                    SELECT reconciliation_digest FROM learning_reconciliations
                    WHERE reconciliation_id = ?
                    """,
                    (reconciliation.reconciliation_id,),
                ).fetchone()
                if existing is not None:
                    if existing["reconciliation_digest"] != reconciliation.digest:
                        raise CandidateConflictError(reconciliation.candidate_id)
                    self._connection.execute("COMMIT")
                    return before
                if before.revision != expected_revision:
                    raise CandidateRevisionError(
                        reconciliation.candidate_id,
                        expected=expected_revision,
                        actual=before.revision,
                    )
                if reconciliation.kind is ReconciliationKind.PROMOTION:
                    allowed = {
                        CandidateState.PROMOTING,
                        CandidateState.PROMOTION_UNKNOWN,
                    }
                    next_state = (
                        CandidateState.PROMOTED
                        if reconciliation.verdict is ReconciliationVerdict.EFFECT_PRESENT
                        else CandidateState.QUALIFIED
                    )
                    target_reference = (
                        reconciliation.target_reference
                        if next_state is CandidateState.PROMOTED
                        else None
                    )
                else:
                    allowed = {
                        CandidateState.ROLLING_BACK,
                        CandidateState.ROLLBACK_UNKNOWN,
                    }
                    next_state = (
                        CandidateState.PROMOTED
                        if reconciliation.verdict is ReconciliationVerdict.EFFECT_PRESENT
                        else CandidateState.ROLLED_BACK
                    )
                    target_reference = before.target_reference
                if before.state not in allowed:
                    raise CandidateTransitionError(
                        reconciliation.candidate_id,
                        state=before.state,
                        action=f"reconcile {reconciliation.kind.value}",
                    )
                after = replace(
                    before,
                    state=next_state,
                    revision=before.revision + 1,
                    target_reference=target_reference,
                    latest_reconciliation_id=reconciliation.reconciliation_id,
                    updated_at=reconciliation.reconciled_at,
                )
                self._connection.execute(
                    """
                    INSERT INTO learning_reconciliations(
                        reconciliation_id, candidate_id, reconciliation_digest,
                        reconciliation_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        reconciliation.reconciliation_id,
                        reconciliation.candidate_id,
                        reconciliation.digest,
                        _canonical_json(reconciliation.to_dict()),
                        reconciliation.reconciled_at,
                    ),
                )
                self._save_mutation(
                    before,
                    after,
                    mutation_id=mutation_id,
                    mutation_digest=mutation_digest,
                )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        return after

    def begin_promotion(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
        promotion_id: str,
        actor: PromotionActor,
    ) -> CandidateRecord:
        mutation_digest = _digest(
            {
                "action": "begin_promotion",
                "promotion_id": promotion_id,
                "actor": PromotionActor(actor).value,
            }
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                before = self._get_locked(candidate_id, owner=owner)
                replay = self._idempotent_mutation(
                    candidate_id,
                    mutation_id=mutation_id,
                    mutation_digest=mutation_digest,
                )
                if replay is not None:
                    self._connection.execute("COMMIT")
                    return replay
                if before.revision != expected_revision:
                    raise CandidateRevisionError(
                        candidate_id,
                        expected=expected_revision,
                        actual=before.revision,
                    )
                if before.state is not CandidateState.QUALIFIED:
                    raise CandidateTransitionError(
                        candidate_id,
                        state=before.state,
                        action="promote",
                    )
                after = replace(
                    before,
                    state=CandidateState.PROMOTING,
                    revision=before.revision + 1,
                    promotion_id=promotion_id,
                    promotion_request_id=mutation_id.removesuffix(":begin"),
                    promotion_actor=PromotionActor(actor),
                    promotion_version=before.promotion_version + 1,
                    updated_at=_now(),
                )
                self._save_mutation(
                    before,
                    after,
                    mutation_id=mutation_id,
                    mutation_digest=mutation_digest,
                )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        return after

    def settle_promotion(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
        succeeded: bool,
        target_reference: str | None,
    ) -> CandidateRecord:
        if succeeded and not target_reference:
            raise ValueError("successful promotion requires target_reference")
        mutation_digest = _digest(
            {
                "action": "settle_promotion",
                "succeeded": succeeded,
                "target_reference": target_reference,
            }
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                before = self._get_locked(candidate_id, owner=owner)
                replay = self._idempotent_mutation(
                    candidate_id,
                    mutation_id=mutation_id,
                    mutation_digest=mutation_digest,
                )
                if replay is not None:
                    self._connection.execute("COMMIT")
                    return replay
                if before.revision != expected_revision:
                    raise CandidateRevisionError(
                        candidate_id,
                        expected=expected_revision,
                        actual=before.revision,
                    )
                if before.state is not CandidateState.PROMOTING:
                    raise CandidateTransitionError(
                        candidate_id,
                        state=before.state,
                        action="settle promotion for",
                    )
                after = replace(
                    before,
                    state=(
                        CandidateState.PROMOTED if succeeded else CandidateState.PROMOTION_UNKNOWN
                    ),
                    revision=before.revision + 1,
                    target_reference=target_reference,
                    updated_at=_now(),
                )
                self._save_mutation(
                    before,
                    after,
                    mutation_id=mutation_id,
                    mutation_digest=mutation_digest,
                )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        return after

    def transition_candidate(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
        action: CandidateAction,
    ) -> CandidateRecord:
        action = CandidateAction(action)
        mutation_digest = _digest({"action": action.value})
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                before = self._get_locked(candidate_id, owner=owner)
                replay = self._idempotent_mutation(
                    candidate_id,
                    mutation_id=mutation_id,
                    mutation_digest=mutation_digest,
                )
                if replay is not None:
                    self._connection.execute("COMMIT")
                    return replay
                if before.revision != expected_revision:
                    raise CandidateRevisionError(
                        candidate_id,
                        expected=expected_revision,
                        actual=before.revision,
                    )
                next_state = _CANDIDATE_TRANSITIONS[action].get(before.state)
                if next_state is None:
                    raise CandidateTransitionError(
                        candidate_id,
                        state=before.state,
                        action=action.value,
                    )
                after = replace(
                    before,
                    state=next_state,
                    revision=before.revision + 1,
                    updated_at=_now(),
                )
                self._save_mutation(
                    before,
                    after,
                    mutation_id=mutation_id,
                    mutation_digest=mutation_digest,
                )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        return after

    def begin_rollback(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
        rollback_id: str,
        actor: PromotionActor,
    ) -> CandidateRecord:
        mutation_digest = _digest(
            {
                "action": "begin_rollback",
                "rollback_id": rollback_id,
                "actor": PromotionActor(actor).value,
            }
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                before = self._get_locked(candidate_id, owner=owner)
                replay = self._idempotent_mutation(
                    candidate_id,
                    mutation_id=mutation_id,
                    mutation_digest=mutation_digest,
                )
                if replay is not None:
                    self._connection.execute("COMMIT")
                    return replay
                if before.revision != expected_revision:
                    raise CandidateRevisionError(
                        candidate_id,
                        expected=expected_revision,
                        actual=before.revision,
                    )
                if before.state is not CandidateState.PROMOTED:
                    raise CandidateTransitionError(
                        candidate_id,
                        state=before.state,
                        action="roll back",
                    )
                after = replace(
                    before,
                    state=CandidateState.ROLLING_BACK,
                    revision=before.revision + 1,
                    rollback_id=rollback_id,
                    rollback_request_id=mutation_id.removesuffix(":begin"),
                    rollback_actor=PromotionActor(actor),
                    updated_at=_now(),
                )
                self._save_mutation(
                    before,
                    after,
                    mutation_id=mutation_id,
                    mutation_digest=mutation_digest,
                )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        return after

    def settle_rollback(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
        succeeded: bool,
    ) -> CandidateRecord:
        mutation_digest = _digest({"action": "settle_rollback", "succeeded": succeeded})
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                before = self._get_locked(candidate_id, owner=owner)
                replay = self._idempotent_mutation(
                    candidate_id,
                    mutation_id=mutation_id,
                    mutation_digest=mutation_digest,
                )
                if replay is not None:
                    self._connection.execute("COMMIT")
                    return replay
                if before.revision != expected_revision:
                    raise CandidateRevisionError(
                        candidate_id,
                        expected=expected_revision,
                        actual=before.revision,
                    )
                if before.state is not CandidateState.ROLLING_BACK:
                    raise CandidateTransitionError(
                        candidate_id,
                        state=before.state,
                        action="settle rollback for",
                    )
                after = replace(
                    before,
                    state=(
                        CandidateState.ROLLED_BACK if succeeded else CandidateState.ROLLBACK_UNKNOWN
                    ),
                    revision=before.revision + 1,
                    updated_at=_now(),
                )
                self._save_mutation(
                    before,
                    after,
                    mutation_id=mutation_id,
                    mutation_digest=mutation_digest,
                )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        return after

    def list_artifact_storage_keys(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT content_storage_key FROM learning_candidates
                WHERE state != ?
                """,
                (CandidateState.DELETED.value,),
            ).fetchall()
        return [str(row["content_storage_key"]) for row in rows]

    def list_events(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[LearningEvent]:
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            self._get_locked(candidate_id, owner=owner)
            rows = self._connection.execute(
                """
                SELECT event_json FROM learning_events
                WHERE candidate_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (candidate_id, after_sequence, limit),
            ).fetchall()
        return [LearningEvent.from_dict(json.loads(row["event_json"])) for row in rows]

    def lease_outbox(
        self,
        *,
        owner: str,
        limit: int = 100,
        lease_seconds: int = 30,
    ) -> list[LearningOutboxItem]:
        _require_id(owner, field_name="outbox_owner")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        now = datetime.now(UTC)
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        leased: list[LearningOutboxItem] = []
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self._connection.execute(
                    """
                    SELECT outbox_id, event_json FROM learning_outbox
                    WHERE delivered_at IS NULL
                      AND (status = 'pending' OR lease_expires_at <= ?)
                    ORDER BY outbox_id LIMIT ?
                    """,
                    (now.isoformat(), limit),
                ).fetchall()
                for row in rows:
                    token = f"learning-lease:{uuid4().hex}"
                    updated = self._connection.execute(
                        """
                        UPDATE learning_outbox
                        SET status = 'leased', lease_owner = ?, lease_token = ?,
                            lease_expires_at = ?
                        WHERE outbox_id = ? AND delivered_at IS NULL
                        """,
                        (owner, token, expires_at, int(row["outbox_id"])),
                    )
                    if updated.rowcount != 1:  # pragma: no cover - write lock invariant
                        continue
                    leased.append(
                        LearningOutboxItem(
                            outbox_id=int(row["outbox_id"]),
                            event=LearningEvent.from_dict(json.loads(row["event_json"])),
                            lease_owner=owner,
                            lease_token=token,
                            lease_expires_at=expires_at,
                        )
                    )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        return leased

    def acknowledge_outbox(self, *, outbox_id: int, lease_token: str) -> None:
        _require_id(lease_token, field_name="lease_token")
        now = _now()
        with self._lock:
            updated = self._connection.execute(
                """
                UPDATE learning_outbox
                SET status = 'delivered', delivered_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL
                WHERE outbox_id = ? AND lease_token = ? AND delivered_at IS NULL
                  AND lease_expires_at > ?
                """,
                (now, outbox_id, lease_token, now),
            )
        if updated.rowcount != 1:
            raise LearningOutboxLeaseError(outbox_id)

    def claim_reconciliation_worker(
        self,
        *,
        owner: LearningOwner,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> LearningReconciliationWorkerLease | None:
        if not isinstance(owner, LearningOwner):
            raise TypeError("owner must be a LearningOwner")
        _require_id(worker_id, field_name="worker_id")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        token = f"learning-reconciler:{uuid4().hex}"
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                now_epoch = float(
                    self._connection.execute(
                        "SELECT (julianday('now') - 2440587.5) * 86400.0 AS value"
                    ).fetchone()["value"]
                )
                row = self._connection.execute(
                    """
                    SELECT lease_fence, lease_expires_at, cursor_json
                    FROM learning_reconciliation_workers WHERE owner_digest = ?
                    """,
                    (owner.digest,),
                ).fetchone()
                if row is not None and float(row["lease_expires_at"]) > now_epoch:
                    self._connection.execute("COMMIT")
                    return None
                fence = int(row["lease_fence"]) + 1 if row is not None else 1
                expires_epoch = now_epoch + lease_seconds
                cursor_json = row["cursor_json"] if row is not None else None
                self._connection.execute(
                    """
                    INSERT INTO learning_reconciliation_workers(
                        owner_digest, worker_id, lease_token, lease_fence,
                        lease_expires_at, cursor_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(owner_digest) DO UPDATE SET
                        worker_id = excluded.worker_id,
                        lease_token = excluded.lease_token,
                        lease_fence = excluded.lease_fence,
                        lease_expires_at = excluded.lease_expires_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        owner.digest,
                        worker_id,
                        token,
                        fence,
                        expires_epoch,
                        cursor_json,
                        _now(),
                    ),
                )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        cursor = ReconciliationCursor.from_dict(json.loads(cursor_json)) if cursor_json else None
        return LearningReconciliationWorkerLease(
            owner_digest=owner.digest,
            worker_id=worker_id,
            lease_token=token,
            fence=fence,
            lease_expires_at=datetime.fromtimestamp(expires_epoch, UTC).isoformat(),
            cursor=cursor,
        )

    def checkpoint_reconciliation_worker(
        self,
        lease: LearningReconciliationWorkerLease,
        *,
        cursor: ReconciliationCursor | None,
        lease_seconds: int = 30,
    ) -> LearningReconciliationWorkerLease:
        if not isinstance(lease, LearningReconciliationWorkerLease):
            raise TypeError("lease must be a LearningReconciliationWorkerLease")
        if cursor is not None and cursor.owner_digest != lease.owner_digest:
            raise ValueError("cursor must be bound to the lease owner")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        cursor_json = _canonical_json(cursor.to_dict()) if cursor is not None else None
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                now_epoch = float(
                    self._connection.execute(
                        "SELECT (julianday('now') - 2440587.5) * 86400.0 AS value"
                    ).fetchone()["value"]
                )
                expires_epoch = now_epoch + lease_seconds
                updated = self._connection.execute(
                    """
                    UPDATE learning_reconciliation_workers
                    SET cursor_json = ?, lease_expires_at = ?, updated_at = ?
                    WHERE owner_digest = ? AND worker_id = ? AND lease_token = ?
                      AND lease_fence = ? AND lease_expires_at > ?
                    """,
                    (
                        cursor_json,
                        expires_epoch,
                        _now(),
                        lease.owner_digest,
                        lease.worker_id,
                        lease.lease_token,
                        lease.fence,
                        now_epoch,
                    ),
                )
                if updated.rowcount != 1:
                    raise LearningReconciliationWorkerLeaseError()
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        return replace(
            lease,
            lease_expires_at=datetime.fromtimestamp(expires_epoch, UTC).isoformat(),
            cursor=cursor,
        )

    def release_reconciliation_worker(
        self,
        lease: LearningReconciliationWorkerLease,
    ) -> bool:
        if not isinstance(lease, LearningReconciliationWorkerLease):
            raise TypeError("lease must be a LearningReconciliationWorkerLease")
        with self._lock:
            now_epoch = float(
                self._connection.execute(
                    "SELECT (julianday('now') - 2440587.5) * 86400.0 AS value"
                ).fetchone()["value"]
            )
            updated = self._connection.execute(
                """
                UPDATE learning_reconciliation_workers
                SET worker_id = NULL, lease_token = NULL, lease_expires_at = 0,
                    updated_at = ?
                WHERE owner_digest = ? AND worker_id = ? AND lease_token = ?
                  AND lease_fence = ? AND lease_expires_at > ?
                """,
                (
                    _now(),
                    lease.owner_digest,
                    lease.worker_id,
                    lease.lease_token,
                    lease.fence,
                    now_epoch,
                ),
            )
        return updated.rowcount == 1


@runtime_checkable
class LearningPromotionAdapter(Protocol):
    async def apply(
        self,
        candidate: LearningCandidate,
        content: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> str: ...


@runtime_checkable
class LearningRollbackAdapter(Protocol):
    async def rollback(
        self,
        candidate: LearningCandidate,
        content: dict[str, Any],
        *,
        target_reference: str,
        idempotency_key: str,
    ) -> None: ...


class LearningGateway:
    """Async candidate capture/evaluation/promotion coordinator."""

    def __init__(
        self,
        ledger: LearningLedger,
        artifact_store: ArtifactStore,
        *,
        promotion_adapter: LearningPromotionAdapter | None = None,
    ) -> None:
        self.ledger = ledger
        self.artifact_store = artifact_store
        self.promotion_adapter = promotion_adapter

    def _require_outcome_ledger(self) -> LearningOutcomeLedger:
        if not isinstance(self.ledger, LearningOutcomeLedger):
            raise HarnessError(
                code="LEARNING_OUTCOME_LEDGER_UNSUPPORTED",
                category="learning",
                message="This learning ledger does not support durable outcome attribution.",
                retryable=False,
            )
        return self.ledger

    @staticmethod
    def _target_policy(policy: LearningPolicy, target: LearningTarget):
        if target is LearningTarget.HARNESS_COMPONENT:
            return None
        return getattr(policy, target.value)

    async def capture(
        self,
        *,
        policy: LearningPolicy,
        scope: LearningScope,
        target: LearningTarget,
        content: dict[str, Any],
        source_run_ids: tuple[str, ...],
        evidence_artifact_ids: tuple[str, ...],
        confidence: float,
        risk: CandidateRisk,
        created_by: CandidateAuthor,
        mechanism_version: str,
        candidate_id: str | None = None,
        expires_at: str | None = None,
        change_hypothesis_artifact_id: str | None = None,
        component_manifest_artifact_id: str | None = None,
        supersedes_candidate_id: str | None = None,
    ) -> CandidateRecord:
        target = LearningTarget(target)
        store_policy = self._target_policy(policy, target)
        if target is not LearningTarget.HARNESS_COMPONENT and (
            store_policy is None or store_policy.write_path is not LearningWritePath.CANDIDATE
        ):
            raise HarnessError(
                code="LEARNING_CANDIDATE_TARGET_FORBIDDEN",
                category="learning",
                message=f"The policy does not allow candidates for {target.value}.",
                retryable=False,
                details={"target": target.value},
            )
        if not source_run_ids:
            raise ValueError("source_run_ids cannot be empty")
        resolved_id = candidate_id or f"lc_{uuid4().hex}"
        artifact = await self.artifact_store.stage_json(
            content,
            scope=ArtifactScope(
                run_id=source_run_ids[0],
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
            ),
            purpose="learning.candidate.content",
            metadata={
                "candidate_id": resolved_id,
                "target": target.value,
                "mechanism_version": mechanism_version,
            },
        )
        candidate = LearningCandidate(
            candidate_id=resolved_id,
            target=target,
            tenant_id=scope.tenant_id,
            storage_namespace=scope.storage_namespace,
            content_artifact=artifact,
            source_run_ids=source_run_ids,
            evidence_artifact_ids=evidence_artifact_ids,
            confidence=confidence,
            risk=risk,
            created_by=created_by,
            mechanism_version=mechanism_version,
            source_user_id=scope.user_id,
            expires_at=expires_at,
            change_hypothesis_artifact_id=change_hypothesis_artifact_id,
            component_manifest_artifact_id=component_manifest_artifact_id,
            supersedes_candidate_id=supersedes_candidate_id,
        )
        return await asyncio.to_thread(self.ledger.create_candidate, candidate)

    async def read_content(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
    ) -> dict[str, Any]:
        record = await asyncio.to_thread(
            self.ledger.get_candidate,
            candidate_id,
            owner=owner,
        )
        if record.state is CandidateState.DELETED:
            raise HarnessError(
                code="LEARNING_CANDIDATE_DELETED",
                category="learning",
                message="Deleted candidate content is no longer available.",
                retryable=False,
                details={"candidate_id": candidate_id},
            )
        value = await self.artifact_store.load_json(record.candidate.content_artifact)
        if not isinstance(value, dict):
            raise HarnessError(
                code="LEARNING_CANDIDATE_CONTENT_INVALID",
                category="learning",
                message="Candidate content is not a JSON object.",
                retryable=False,
                details={"candidate_id": candidate_id},
            )
        return value

    async def get(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
    ) -> CandidateRecord:
        return await asyncio.to_thread(
            self.ledger.get_candidate,
            candidate_id,
            owner=owner,
        )

    async def list_candidates(
        self,
        *,
        owner: LearningOwner,
        limit: int = 100,
        state: CandidateState | None = None,
    ) -> list[CandidateRecord]:
        return await asyncio.to_thread(
            self.ledger.list_candidates,
            owner=owner,
            limit=limit,
            state=state,
        )

    async def record_application(
        self,
        application: LearningApplication,
        *,
        owner: LearningOwner,
    ) -> LearningApplication:
        ledger = self._require_outcome_ledger()
        return await asyncio.to_thread(
            ledger.record_application,
            application,
            owner=owner,
        )

    async def get_application(
        self,
        application_id: str,
        *,
        owner: LearningOwner,
    ) -> LearningApplication:
        ledger = self._require_outcome_ledger()
        return await asyncio.to_thread(
            ledger.get_application,
            application_id,
            owner=owner,
        )

    async def list_applications(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        limit: int = 100,
    ) -> list[LearningApplication]:
        ledger = self._require_outcome_ledger()
        return await asyncio.to_thread(
            ledger.list_applications,
            candidate_id,
            owner=owner,
            limit=limit,
        )

    async def record_outcome(
        self,
        outcome: LearningOutcome,
        *,
        owner: LearningOwner,
    ) -> LearningOutcome:
        ledger = self._require_outcome_ledger()
        return await asyncio.to_thread(
            ledger.record_outcome,
            outcome,
            owner=owner,
        )

    async def get_outcome(
        self,
        outcome_id: str,
        *,
        owner: LearningOwner,
    ) -> LearningOutcome:
        ledger = self._require_outcome_ledger()
        return await asyncio.to_thread(
            ledger.get_outcome,
            outcome_id,
            owner=owner,
        )

    async def list_outcomes(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        limit: int = 100,
    ) -> list[LearningOutcome]:
        ledger = self._require_outcome_ledger()
        return await asyncio.to_thread(
            ledger.list_outcomes,
            candidate_id,
            owner=owner,
            limit=limit,
        )

    async def summarize_effectiveness(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        policy: LearningEffectivenessPolicy | None = None,
    ) -> LearningEffectivenessSummary:
        ledger = self._require_outcome_ledger()
        return await asyncio.to_thread(
            ledger.summarize_effectiveness,
            candidate_id,
            owner=owner,
            policy=policy or LearningEffectivenessPolicy(),
        )

    async def scan_reconciliation_required(
        self,
        *,
        owner: LearningOwner,
        limit: int = 100,
        cursor: ReconciliationCursor | None = None,
    ) -> ReconciliationPage:
        return await asyncio.to_thread(
            self.ledger.scan_reconciliation_required,
            owner=owner,
            limit=limit,
            cursor=cursor,
        )

    async def get_evaluation(
        self,
        evaluation_id: str,
        *,
        owner: LearningOwner,
    ) -> CandidateEvaluation:
        return await asyncio.to_thread(
            self.ledger.get_evaluation,
            evaluation_id,
            owner=owner,
        )

    async def list_events(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[LearningEvent]:
        return await asyncio.to_thread(
            self.ledger.list_events,
            candidate_id,
            owner=owner,
            after_sequence=after_sequence,
            limit=limit,
        )

    async def list_evaluations(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        limit: int = 100,
    ) -> list[CandidateEvaluation]:
        return await asyncio.to_thread(
            self.ledger.list_evaluations,
            candidate_id,
            owner=owner,
            limit=limit,
        )

    async def query_evaluation_archive(
        self,
        *,
        owner: LearningOwner,
        query: EvaluationArchiveQuery | None = None,
    ) -> EvaluationArchivePage:
        if not isinstance(self.ledger, EvaluationArchiveLedger):
            raise HarnessError(
                code="LEARNING_EVALUATION_ARCHIVE_UNSUPPORTED",
                category="learning",
                message="This learning ledger does not expose the archive read model.",
                retryable=False,
            )
        return await asyncio.to_thread(
            self.ledger.query_evaluation_archive,
            owner=owner,
            query=query or EvaluationArchiveQuery(),
        )

    async def get_reconciliation(
        self,
        reconciliation_id: str,
        *,
        owner: LearningOwner,
    ) -> CandidateReconciliation:
        return await asyncio.to_thread(
            self.ledger.get_reconciliation,
            reconciliation_id,
            owner=owner,
        )

    async def list_reconciliations(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        limit: int = 100,
    ) -> list[CandidateReconciliation]:
        return await asyncio.to_thread(
            self.ledger.list_reconciliations,
            candidate_id,
            owner=owner,
            limit=limit,
        )

    async def export(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        evaluation_limit: int = 100,
    ) -> LearningCandidateExport:
        record = await self.get(candidate_id, owner=owner)
        content = (
            None
            if record.state is CandidateState.DELETED
            else await self.read_content(candidate_id, owner=owner)
        )
        evaluations = await self.list_evaluations(
            candidate_id,
            owner=owner,
            limit=evaluation_limit,
        )
        reconciliations = await self.list_reconciliations(
            candidate_id,
            owner=owner,
            limit=evaluation_limit,
        )
        return LearningCandidateExport(
            record=record,
            content=content,
            evaluations=tuple(evaluations),
            reconciliations=tuple(reconciliations),
        )

    async def evaluate(
        self,
        evaluation: CandidateEvaluation,
        *,
        owner: LearningOwner,
        mutation_id: str,
    ) -> CandidateRecord:
        record = await self.get(evaluation.candidate_id, owner=owner)
        return await asyncio.to_thread(
            self.ledger.record_evaluation,
            evaluation,
            owner=owner,
            expected_revision=record.revision,
            mutation_id=mutation_id,
        )

    async def reconcile(
        self,
        reconciliation: CandidateReconciliation,
        *,
        owner: LearningOwner,
        mutation_id: str,
    ) -> CandidateRecord:
        record = await self.get(reconciliation.candidate_id, owner=owner)
        return await asyncio.to_thread(
            self.ledger.record_reconciliation,
            reconciliation,
            owner=owner,
            expected_revision=record.revision,
            mutation_id=mutation_id,
        )

    async def transition(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        action: CandidateAction,
        mutation_id: str,
    ) -> CandidateRecord:
        record = await self.get(candidate_id, owner=owner)
        return await asyncio.to_thread(
            self.ledger.transition_candidate,
            candidate_id,
            owner=owner,
            expected_revision=record.revision,
            mutation_id=mutation_id,
            action=action,
        )

    async def promote(
        self,
        candidate_id: str,
        *,
        policy: LearningPolicy,
        owner: LearningOwner,
        actor: PromotionActor,
        mutation_id: str,
    ) -> CandidateRecord:
        actor = PromotionActor(actor)
        if policy.promotion.value != "reviewed":
            raise HarnessError(
                code="LEARNING_PROMOTION_NOT_REVIEWED",
                category="learning",
                message="Stable promotion requires the reviewed policy.",
                retryable=False,
                details={"promotion": policy.promotion.value},
            )
        if self.promotion_adapter is None:
            raise HarnessError(
                code="LEARNING_PROMOTION_ADAPTER_REQUIRED",
                category="learning",
                message="Promotion requires a host-supplied learning adapter.",
                retryable=False,
            )
        record = await asyncio.to_thread(
            self.ledger.get_candidate,
            candidate_id,
            owner=owner,
        )
        if record.promotion_request_id == mutation_id:
            if record.state is CandidateState.PROMOTED:
                return record
            if record.state in {
                CandidateState.PROMOTING,
                CandidateState.PROMOTION_UNKNOWN,
            }:
                raise LearningPromotionUnknownError(candidate_id)
        if record.candidate.expires_at is not None and datetime.fromisoformat(
            record.candidate.expires_at
        ) <= datetime.now(UTC):
            raise HarnessError(
                code="LEARNING_CANDIDATE_EXPIRED",
                category="learning",
                message="Expired learning candidates cannot be promoted.",
                retryable=False,
                details={"candidate_id": candidate_id},
            )
        store_policy = self._target_policy(policy, record.candidate.target)
        if record.candidate.target is not LearningTarget.HARNESS_COMPONENT and (
            store_policy is None or store_policy.write_path is not LearningWritePath.CANDIDATE
        ):
            raise HarnessError(
                code="LEARNING_PROMOTION_TARGET_FORBIDDEN",
                category="learning",
                message="The active policy cannot promote this candidate target.",
                retryable=False,
                details={"target": record.candidate.target.value},
            )
        promotion_id = (
            "lp_" + hashlib.sha256(f"{candidate_id}\x00{mutation_id}".encode()).hexdigest()
        )
        begun = await asyncio.to_thread(
            self.ledger.begin_promotion,
            candidate_id,
            owner=owner,
            expected_revision=record.revision,
            mutation_id=f"{mutation_id}:begin",
            promotion_id=promotion_id,
            actor=actor,
        )
        content = await self.read_content(candidate_id, owner=owner)
        try:
            target_reference = await self.promotion_adapter.apply(
                begun.candidate,
                content,
                idempotency_key=promotion_id,
            )
        except BaseException as exc:
            await asyncio.shield(
                asyncio.to_thread(
                    self.ledger.settle_promotion,
                    candidate_id,
                    owner=owner,
                    expected_revision=begun.revision,
                    mutation_id=f"{mutation_id}:unknown",
                    succeeded=False,
                    target_reference=None,
                )
            )
            raise LearningPromotionUnknownError(candidate_id) from exc
        return await asyncio.shield(
            asyncio.to_thread(
                self.ledger.settle_promotion,
                candidate_id,
                owner=owner,
                expected_revision=begun.revision,
                mutation_id=f"{mutation_id}:settle",
                succeeded=True,
                target_reference=target_reference,
            )
        )

    async def rollback(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        actor: PromotionActor,
        mutation_id: str,
    ) -> CandidateRecord:
        actor = PromotionActor(actor)
        adapter = self.promotion_adapter
        if adapter is None or not isinstance(adapter, LearningRollbackAdapter):
            raise HarnessError(
                code="LEARNING_ROLLBACK_ADAPTER_REQUIRED",
                category="learning",
                message="Rollback requires a host adapter with an explicit rollback method.",
                retryable=False,
            )
        record = await self.get(candidate_id, owner=owner)
        if record.rollback_request_id == mutation_id:
            if record.state is CandidateState.ROLLED_BACK:
                return record
            if record.state in {
                CandidateState.ROLLING_BACK,
                CandidateState.ROLLBACK_UNKNOWN,
            }:
                raise LearningRollbackUnknownError(candidate_id)
        if record.target_reference is None:
            raise HarnessError(
                code="LEARNING_ROLLBACK_TARGET_REQUIRED",
                category="learning",
                message="The promoted candidate has no recorded rollback target.",
                retryable=False,
                details={"candidate_id": candidate_id},
            )
        rollback_id = (
            "lr_" + hashlib.sha256(f"{candidate_id}\x00{mutation_id}".encode()).hexdigest()
        )
        begun = await asyncio.to_thread(
            self.ledger.begin_rollback,
            candidate_id,
            owner=owner,
            expected_revision=record.revision,
            mutation_id=f"{mutation_id}:begin",
            rollback_id=rollback_id,
            actor=actor,
        )
        content = await self.read_content(candidate_id, owner=owner)
        try:
            await adapter.rollback(
                begun.candidate,
                content,
                target_reference=record.target_reference,
                idempotency_key=rollback_id,
            )
        except BaseException as exc:
            await asyncio.shield(
                asyncio.to_thread(
                    self.ledger.settle_rollback,
                    candidate_id,
                    owner=owner,
                    expected_revision=begun.revision,
                    mutation_id=f"{mutation_id}:unknown",
                    succeeded=False,
                )
            )
            raise LearningRollbackUnknownError(candidate_id) from exc
        return await asyncio.shield(
            asyncio.to_thread(
                self.ledger.settle_rollback,
                candidate_id,
                owner=owner,
                expected_revision=begun.revision,
                mutation_id=f"{mutation_id}:settle",
                succeeded=True,
            )
        )


class AgnoLearningPromotionAdapter:
    """Narrow Agno adapter for uniquely named, reversibly promoted learnings.

    Learned Knowledge is supported because each candidate receives a unique title and
    Agno exposes deletion by title. Entity Memory merges may touch pre-existing state,
    Decision Log has no delete operation, and harness components are outside this adapter;
    those targets therefore fail closed instead of pretending rollback is possible.
    """

    def __init__(self, machine_factory: Callable[[LearningCandidate], Any]) -> None:
        self.machine_factory = machine_factory

    @staticmethod
    async def _resolve(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    async def apply(
        self,
        candidate: LearningCandidate,
        content: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> str:
        machine = await self._resolve(self.machine_factory(candidate))
        if candidate.target is LearningTarget.LEARNED_KNOWLEDGE:
            learning = content.get("learning")
            if not isinstance(learning, str) or not learning.strip():
                raise HarnessError(
                    code="LEARNING_PROMOTION_CONTENT_INVALID",
                    category="learning",
                    message="Learned Knowledge promotion requires non-empty 'learning'.",
                    retryable=False,
                    details={"candidate_id": candidate.candidate_id},
                )
            stable_title, target_reference = _agno_learned_knowledge_identity(
                candidate,
                content,
            )
            saved = await machine.learned_knowledge_store.asave(
                title=stable_title,
                learning=learning,
                context=content.get("context"),
                tags=list(content.get("tags") or ()),
                user_id=None,
                namespace=candidate.storage_namespace,
            )
            if saved is not True:
                raise HarnessError(
                    code="LEARNING_PROMOTION_BACKEND_REJECTED",
                    category="learning",
                    message="Agno did not confirm the Learned Knowledge write.",
                    retryable=False,
                    details={"candidate_id": candidate.candidate_id},
                )
            return target_reference
        if candidate.target is LearningTarget.ENTITY_MEMORY:
            raise HarnessError(
                code="LEARNING_PROMOTION_TARGET_UNSUPPORTED",
                category="learning",
                message=(
                    "Entity Memory promotion can merge with pre-existing state and "
                    "has no certified snapshot rollback adapter."
                ),
                retryable=False,
                details={"target": candidate.target.value},
            )
        raise HarnessError(
            code="LEARNING_PROMOTION_TARGET_UNSUPPORTED",
            category="learning",
            message=(
                f"Agno promotion for {candidate.target.value} lacks a certified reversible adapter."
            ),
            retryable=False,
            details={"target": candidate.target.value},
        )

    async def rollback(
        self,
        candidate: LearningCandidate,
        content: dict[str, Any],
        *,
        target_reference: str,
        idempotency_key: str,
    ) -> None:
        del idempotency_key
        if candidate.target is not LearningTarget.LEARNED_KNOWLEDGE:
            raise HarnessError(
                code="LEARNING_ROLLBACK_TARGET_UNSUPPORTED",
                category="learning",
                message="This Agno adapter can only roll back Learned Knowledge.",
                retryable=False,
                details={"target": candidate.target.value},
            )
        stable_title, expected_reference = _agno_learned_knowledge_identity(candidate, content)
        if target_reference != expected_reference:
            raise HarnessError(
                code="LEARNING_ROLLBACK_TARGET_MISMATCH",
                category="learning",
                message="The rollback target does not match the promoted candidate.",
                retryable=False,
                details={"candidate_id": candidate.candidate_id},
            )
        machine = await self._resolve(self.machine_factory(candidate))
        deleted = await machine.learned_knowledge_store.adelete(stable_title)
        if deleted is not True:
            raise HarnessError(
                code="LEARNING_ROLLBACK_BACKEND_REJECTED",
                category="learning",
                message="Agno did not confirm deletion of the promoted learning.",
                retryable=False,
                details={"candidate_id": candidate.candidate_id},
            )


__all__ = [
    "AgnoLearningPromotionAdapter",
    "CandidateAuthor",
    "CandidateAction",
    "CandidateConflictError",
    "CandidateEvaluation",
    "CandidateEvaluationNotFoundError",
    "CandidateNotFoundError",
    "CandidateRecord",
    "CandidateReconciliation",
    "CandidateReconciliationNotFoundError",
    "CandidateRevisionError",
    "CandidateRisk",
    "CandidateState",
    "CandidateTransitionError",
    "EvaluationArchiveCursor",
    "EvaluationArchiveEntry",
    "EvaluationArchiveLedger",
    "EvaluationArchivePage",
    "EvaluationArchiveQuery",
    "EvaluationVerdict",
    "LEARNING_CANDIDATE_SCHEMA_VERSION",
    "LEARNING_LEDGER_SCHEMA_VERSION",
    "LearningCandidate",
    "LearningCandidateExport",
    "LearningApplication",
    "LearningApplicationKind",
    "LearningApplicationNotFoundError",
    "LearningAttributionConflictError",
    "LearningEffectivenessPolicy",
    "LearningEffectivenessRecommendation",
    "LearningEffectivenessSummary",
    "LearningEvent",
    "LearningGateway",
    "LearningLedger",
    "LearningOwner",
    "LearningOutboxItem",
    "LearningOutboxLeaseError",
    "LearningOutcome",
    "LearningOutcomeKind",
    "LearningOutcomeLedger",
    "LearningOutcomeNotFoundError",
    "LearningReconciliationWorkerLease",
    "LearningReconciliationWorkerLeaseError",
    "LearningPromotionAdapter",
    "LearningPromotionUnknownError",
    "LearningRollbackAdapter",
    "LearningRollbackUnknownError",
    "LearningTarget",
    "PromotionActor",
    "ReconciliationKind",
    "ReconciliationCursor",
    "ReconciliationCursorScopeError",
    "ReconciliationPage",
    "ReconciliationRequest",
    "ReconciliationVerdict",
    "SQLiteLearningLedger",
]
