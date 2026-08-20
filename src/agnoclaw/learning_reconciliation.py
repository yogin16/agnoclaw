"""Bounded, evidence-verifying coordination for ambiguous learning effects.

Observers inspect external state.  They never retry promotion or rollback.  The
coordinator verifies immutable evidence and commits one revision-bound reconciliation;
the learning ledger remains the authority under concurrent workers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from .learning_candidates import (
    CandidateNotFoundError,
    CandidateReconciliation,
    CandidateRevisionError,
    CandidateState,
    CandidateTransitionError,
    LearningCandidate,
    LearningGateway,
    LearningOwner,
    LearningTarget,
    PromotionActor,
    ReconciliationCursor,
    ReconciliationKind,
    ReconciliationRequest,
    ReconciliationVerdict,
    _agno_learned_knowledge_identity,
)
from .runtime.artifacts import ArtifactReference, ArtifactScope, ArtifactStore
from .runtime.errors import HarnessError
from .runtime.security import freeze_data, thaw_data

RECONCILIATION_EVIDENCE_PURPOSE = "learning.reconciliation.evidence"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _require_id(value: str, *, field_name: str, maximum: int = 512) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{field_name} cannot exceed {maximum} characters")


def _require_digest(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a canonical sha256 digest")


def _digest(value: Any) -> str:
    payload = json.dumps(
        thaw_data(freeze_data(value)),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class ReconciliationItemStatus(StrEnum):
    RECONCILED = "reconciled"
    DEFERRED = "deferred"
    STALE = "stale"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ReconciliationObservation:
    """One observer result bound to the exact unknown candidate revision."""

    candidate_id: str
    kind: ReconciliationKind
    expected_revision: int
    candidate_digest: str
    verdict: ReconciliationVerdict
    evidence_artifacts: tuple[ArtifactReference, ...]
    target_reference: str | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.candidate_id, field_name="candidate_id")
        object.__setattr__(self, "kind", ReconciliationKind(self.kind))
        object.__setattr__(self, "verdict", ReconciliationVerdict(self.verdict))
        if self.expected_revision < 0:
            raise ValueError("expected_revision cannot be negative")
        _require_digest(self.candidate_digest, field_name="candidate_digest")
        object.__setattr__(self, "evidence_artifacts", tuple(self.evidence_artifacts))
        if not self.evidence_artifacts:
            raise ValueError("evidence_artifacts cannot be empty")
        if any(not isinstance(item, ArtifactReference) for item in self.evidence_artifacts):
            raise TypeError("evidence_artifacts must contain ArtifactReference values")
        artifact_ids = [item.artifact_id for item in self.evidence_artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("evidence artifacts must be unique")
        if self.target_reference is not None:
            _require_id(self.target_reference, field_name="target_reference")
        if (
            self.kind is ReconciliationKind.PROMOTION
            and self.verdict is ReconciliationVerdict.EFFECT_PRESENT
            and self.target_reference is None
        ):
            raise ValueError("a present promotion effect requires its target reference")
        if self.notes is not None:
            _require_id(self.notes, field_name="notes", maximum=4096)

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind.value,
            "expected_revision": self.expected_revision,
            "candidate_digest": self.candidate_digest,
            "verdict": self.verdict.value,
            "evidence_artifacts": [
                {
                    "artifact_id": item.artifact_id,
                    "storage_identity_digest": item.storage_identity_digest,
                }
                for item in self.evidence_artifacts
            ],
            "target_reference": self.target_reference,
            "notes": self.notes,
        }


@runtime_checkable
class LearningReconciliationObserver(Protocol):
    """Read-only external-state observer supplied and versioned by the host."""

    async def observe(
        self,
        request: ReconciliationRequest,
        content: dict[str, Any],
    ) -> ReconciliationObservation | None: ...


class AgnoLearnedKnowledgeReconciliationObserver:
    """Inspect Agno's exact candidate-derived vector key and stage safe evidence.

    This deliberately uses ``VectorDb.name_exists`` rather than semantic search. The
    candidate digest is embedded in the exact Agno title, so unrelated similar content
    cannot satisfy reconciliation.
    """

    def __init__(
        self,
        machine_factory: Callable[[LearningCandidate], Any],
        artifact_store: ArtifactStore,
        *,
        observer_identity_digest: str,
    ) -> None:
        if not callable(machine_factory):
            raise TypeError("machine_factory must be callable")
        if not isinstance(artifact_store, ArtifactStore):
            raise TypeError("artifact_store must implement ArtifactStore")
        _require_digest(observer_identity_digest, field_name="observer_identity_digest")
        self.machine_factory = machine_factory
        self.artifact_store = artifact_store
        self.observer_identity_digest = observer_identity_digest

    @staticmethod
    async def _resolve(value: Any) -> Any:
        return await value if hasattr(value, "__await__") else value

    async def observe(
        self,
        request: ReconciliationRequest,
        content: dict[str, Any],
    ) -> ReconciliationObservation:
        candidate = request.record.candidate
        if candidate.target is not LearningTarget.LEARNED_KNOWLEDGE:
            raise HarnessError(
                code="LEARNING_AGNO_EXACT_OBSERVER_TARGET_UNSUPPORTED",
                category="learning",
                message="The Agno exact observer supports only Learned Knowledge.",
                retryable=False,
                details={"target": candidate.target.value},
            )
        stable_title, target_reference = _agno_learned_knowledge_identity(candidate, content)
        try:
            machine = await self._resolve(self.machine_factory(candidate))
            store = getattr(machine, "learned_knowledge_store", None)
            knowledge = getattr(store, "knowledge", None)
            vector_db = getattr(knowledge, "vector_db", None)
            name_exists = getattr(vector_db, "name_exists", None)
            if not callable(name_exists):
                raise HarnessError(
                    code="LEARNING_AGNO_EXACT_OBSERVER_UNAVAILABLE",
                    category="learning",
                    message="The Agno vector backend has no exact-name inspection surface.",
                    retryable=False,
                )
            effect_present = await asyncio.to_thread(name_exists, stable_title)
        except asyncio.CancelledError:
            raise
        except HarnessError:
            raise
        except Exception as exc:
            raise HarnessError(
                code="LEARNING_AGNO_EXACT_OBSERVER_FAILED",
                category="learning",
                message="The Agno exact-name inspection failed.",
                retryable=True,
            ) from exc
        if not isinstance(effect_present, bool):
            raise HarnessError(
                code="LEARNING_AGNO_EXACT_OBSERVATION_INVALID",
                category="learning",
                message="The Agno exact-name inspection returned an invalid result.",
                retryable=False,
            )

        stable_title_digest = "sha256:" + hashlib.sha256(stable_title.encode()).hexdigest()
        evidence = await self.artifact_store.stage_json(
            {
                "schema_version": "1.0",
                "observer": "agno.learned_knowledge.exact_name",
                "observer_identity_digest": self.observer_identity_digest,
                "candidate_digest": candidate.digest,
                "expected_revision": request.record.revision,
                "kind": request.kind.value,
                "target_key_digest": stable_title_digest,
                "effect_present": effect_present,
            },
            scope=ArtifactScope(
                run_id=candidate.source_run_ids[0],
                tenant_id=candidate.tenant_id,
                user_id=candidate.source_user_id,
            ),
            purpose=RECONCILIATION_EVIDENCE_PURPOSE,
            metadata={
                "schema_version": "1.0",
                "observer": "agno.learned_knowledge.exact_name",
            },
        )
        return ReconciliationObservation(
            candidate_id=candidate.candidate_id,
            kind=request.kind,
            expected_revision=request.record.revision,
            candidate_digest=candidate.digest,
            verdict=(
                ReconciliationVerdict.EFFECT_PRESENT
                if effect_present
                else ReconciliationVerdict.EFFECT_ABSENT
            ),
            evidence_artifacts=(evidence,),
            target_reference=target_reference if effect_present else None,
        )


@dataclass(frozen=True, slots=True)
class ReconciliationItemOutcome:
    candidate_id: str
    kind: ReconciliationKind
    status: ReconciliationItemStatus
    observed_revision: int
    resulting_state: CandidateState | None = None
    resulting_revision: int | None = None
    reconciliation_id: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.candidate_id, field_name="candidate_id")
        object.__setattr__(self, "kind", ReconciliationKind(self.kind))
        object.__setattr__(self, "status", ReconciliationItemStatus(self.status))
        if self.observed_revision < 0:
            raise ValueError("observed_revision cannot be negative")
        if self.resulting_state is not None:
            object.__setattr__(self, "resulting_state", CandidateState(self.resulting_state))
        if self.resulting_revision is not None and self.resulting_revision < 0:
            raise ValueError("resulting_revision cannot be negative")
        if self.reconciliation_id is not None:
            _require_id(self.reconciliation_id, field_name="reconciliation_id")
        if self.error_code is not None:
            _require_id(self.error_code, field_name="error_code")
        if self.status is ReconciliationItemStatus.RECONCILED and (
            self.resulting_state is None
            or self.resulting_revision is None
            or self.reconciliation_id is None
            or self.error_code is not None
        ):
            raise ValueError("reconciled outcomes require exact resulting evidence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "observed_revision": self.observed_revision,
            "resulting_state": (
                self.resulting_state.value if self.resulting_state is not None else None
            ),
            "resulting_revision": self.resulting_revision,
            "reconciliation_id": self.reconciliation_id,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationBatchOutcome:
    owner_digest: str
    items: tuple[ReconciliationItemOutcome, ...]
    next_cursor: ReconciliationCursor | None

    def __post_init__(self) -> None:
        _require_digest(self.owner_digest, field_name="owner_digest")
        object.__setattr__(self, "items", tuple(self.items))
        if any(not isinstance(item, ReconciliationItemOutcome) for item in self.items):
            raise TypeError("items must contain ReconciliationItemOutcome values")
        if self.next_cursor is not None and not isinstance(
            self.next_cursor,
            ReconciliationCursor,
        ):
            raise TypeError("next_cursor must be a ReconciliationCursor")
        if self.next_cursor is not None and self.next_cursor.owner_digest != self.owner_digest:
            raise ValueError("next_cursor must be bound to owner_digest")

    @property
    def reconciled_count(self) -> int:
        return sum(item.status is ReconciliationItemStatus.RECONCILED for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_digest": self.owner_digest,
            "items": [item.to_dict() for item in self.items],
            "next_cursor": (self.next_cursor.to_dict() if self.next_cursor is not None else None),
        }


class LearningReconciliationCoordinator:
    """Observe one bounded page and CAS-commit only verified results."""

    def __init__(
        self,
        gateway: LearningGateway,
        observer: LearningReconciliationObserver,
        *,
        reconciler_digest: str,
        reconciled_by: PromotionActor = PromotionActor.HOST,
        max_concurrency: int = 4,
    ) -> None:
        if not isinstance(gateway, LearningGateway):
            raise TypeError("gateway must be a LearningGateway")
        if not callable(getattr(observer, "observe", None)):
            raise TypeError("observer must provide an async observe method")
        _require_digest(reconciler_digest, field_name="reconciler_digest")
        if not 1 <= max_concurrency <= 32:
            raise ValueError("max_concurrency must be between 1 and 32")
        self.gateway = gateway
        self.observer = observer
        self.reconciler_digest = reconciler_digest
        self.reconciled_by = PromotionActor(reconciled_by)
        self.max_concurrency = max_concurrency

    @staticmethod
    def _outcome(
        request: ReconciliationRequest,
        status: ReconciliationItemStatus,
        *,
        error_code: str | None = None,
    ) -> ReconciliationItemOutcome:
        return ReconciliationItemOutcome(
            candidate_id=request.record.candidate.candidate_id,
            kind=request.kind,
            status=status,
            observed_revision=request.record.revision,
            error_code=error_code,
        )

    async def _verify_observation(
        self,
        request: ReconciliationRequest,
        observation: ReconciliationObservation,
    ) -> None:
        candidate = request.record.candidate
        if (
            observation.candidate_id != candidate.candidate_id
            or observation.kind is not request.kind
            or observation.expected_revision != request.record.revision
            or observation.candidate_digest != candidate.digest
        ):
            raise HarnessError(
                code="LEARNING_RECONCILIATION_OBSERVATION_MISMATCH",
                category="learning",
                message="The observation is not bound to the discovered candidate revision.",
                retryable=False,
                details={"candidate_id": candidate.candidate_id},
            )
        for reference in observation.evidence_artifacts:
            if (
                reference.scope.tenant_id != candidate.tenant_id
                or reference.scope.user_id != candidate.source_user_id
                or reference.purpose != RECONCILIATION_EVIDENCE_PURPOSE
            ):
                raise HarnessError(
                    code="LEARNING_RECONCILIATION_EVIDENCE_SCOPE_MISMATCH",
                    category="learning",
                    message="Reconciliation evidence is outside the candidate owner scope.",
                    retryable=False,
                    details={"candidate_id": candidate.candidate_id},
                )
            await self.gateway.artifact_store.read(reference, offset=0, limit=1)

    async def _process(self, request: ReconciliationRequest) -> ReconciliationItemOutcome:
        candidate_id = request.record.candidate.candidate_id
        try:
            content = await self.gateway.read_content(
                candidate_id,
                owner=request.record.candidate.owner,
            )
            observation = await self.observer.observe(request, content)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, HarnessError)
                else "LEARNING_RECONCILIATION_OBSERVER_FAILED"
            )
            return self._outcome(
                request,
                ReconciliationItemStatus.FAILED,
                error_code=code,
            )
        if observation is None:
            return self._outcome(request, ReconciliationItemStatus.DEFERRED)
        if not isinstance(observation, ReconciliationObservation):
            return self._outcome(
                request,
                ReconciliationItemStatus.REJECTED,
                error_code="LEARNING_RECONCILIATION_OBSERVATION_INVALID",
            )
        try:
            await self._verify_observation(request, observation)
            digest_suffix = observation.digest.removeprefix("sha256:")
            reconciliation_id = f"lrec_observed_{digest_suffix[:40]}"
            reconciliation = CandidateReconciliation(
                reconciliation_id=reconciliation_id,
                candidate_id=candidate_id,
                kind=request.kind,
                verdict=observation.verdict,
                reconciler_digest=self.reconciler_digest,
                evidence_artifact_ids=tuple(
                    item.artifact_id for item in observation.evidence_artifacts
                ),
                reconciled_by=self.reconciled_by,
                target_reference=observation.target_reference,
                notes=observation.notes,
            )
            updated = await asyncio.to_thread(
                self.gateway.ledger.record_reconciliation,
                reconciliation,
                owner=request.record.candidate.owner,
                expected_revision=request.record.revision,
                mutation_id=f"observer:{reconciliation_id}",
            )
        except asyncio.CancelledError:
            raise
        except (CandidateNotFoundError, CandidateRevisionError, CandidateTransitionError):
            return self._outcome(request, ReconciliationItemStatus.STALE)
        except HarnessError as exc:
            return self._outcome(
                request,
                ReconciliationItemStatus.REJECTED,
                error_code=exc.code,
            )
        except Exception:
            return self._outcome(
                request,
                ReconciliationItemStatus.FAILED,
                error_code="LEARNING_RECONCILIATION_COMMIT_FAILED",
            )
        return ReconciliationItemOutcome(
            candidate_id=candidate_id,
            kind=request.kind,
            status=ReconciliationItemStatus.RECONCILED,
            observed_revision=request.record.revision,
            resulting_state=updated.state,
            resulting_revision=updated.revision,
            reconciliation_id=reconciliation_id,
        )

    async def run_page(
        self,
        *,
        owner: LearningOwner,
        limit: int = 100,
        cursor: ReconciliationCursor | None = None,
    ) -> ReconciliationBatchOutcome:
        """Observe a page in discovery order while bounding external concurrency."""
        if not isinstance(owner, LearningOwner):
            raise TypeError("owner must be a LearningOwner")
        page = await self.gateway.scan_reconciliation_required(
            owner=owner,
            limit=limit,
            cursor=cursor,
        )
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def bounded(request: ReconciliationRequest) -> ReconciliationItemOutcome:
            async with semaphore:
                return await self._process(request)

        outcomes = await asyncio.gather(*(bounded(request) for request in page.items))
        return ReconciliationBatchOutcome(
            owner_digest=owner.digest,
            items=tuple(outcomes),
            next_cursor=page.next_cursor,
        )


__all__ = [
    "AgnoLearnedKnowledgeReconciliationObserver",
    "LearningReconciliationCoordinator",
    "LearningReconciliationObserver",
    "RECONCILIATION_EVIDENCE_PURPOSE",
    "ReconciliationBatchOutcome",
    "ReconciliationItemOutcome",
    "ReconciliationItemStatus",
    "ReconciliationObservation",
]
