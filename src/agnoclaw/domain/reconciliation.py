"""Entity matching and attributed claim resolution without implicit business writes."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, ClassVar

from ._contracts import Record, fail, frozen_json, identifier
from .components import DomainComponent
from .evidence import EvidenceRef
from .identity import ContractRef
from .objects import ObjectId, ObjectRef, ObjectSnapshot


@dataclass(frozen=True)
class MatchCandidate(Record):
    target: ObjectRef
    score: float


@dataclass(frozen=True)
class MatchResult(Record):
    subject_digest: str
    policy_digest: str
    status: str
    candidates: tuple[MatchCandidate, ...]


@dataclass(frozen=True)
class MatchPolicy(DomainComponent):
    name: str
    compare: Callable[[ObjectSnapshot, ObjectSnapshot], float | None] = dc_field(repr=False)
    implementation_id: str = dc_field(kw_only=True)
    review_threshold: float = dc_field(default=0.5, kw_only=True)
    match_threshold: float = dc_field(default=1.0, kw_only=True)
    version: str = dc_field(default="1", kw_only=True)
    max_candidates: int = dc_field(default=1000, kw_only=True)
    kind: ClassVar[str] = "matching"

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        identifier(self.implementation_id, "implementation_id")
        if not 0 <= self.review_threshold <= self.match_threshold <= 1:
            raise ValueError("matching thresholds must satisfy 0 <= review <= match <= 1")
        if type(self.max_candidates) is not int or self.max_candidates < 1:
            raise ValueError("max_candidates must be positive")
        if not callable(self.compare):
            raise TypeError("compare must be a short pure callable")

    def manifest(self) -> dict[str, Any]:
        return {
            "implementation_id": self.implementation_id,
            "review_threshold": self.review_threshold,
            "match_threshold": self.match_threshold,
            "max_candidates": self.max_candidates,
        }

    def match(self, subject: ObjectSnapshot, candidates: Sequence[ObjectSnapshot]) -> MatchResult:
        if len(candidates) > self.max_candidates:
            raise ValueError("candidate set exceeds the matching budget")
        ids = [c.reference.identity for c in candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate set must contain one revision per logical object")
        if any(
            c.reference.tenant_id != subject.reference.tenant_id
            or c.reference.kind != subject.reference.kind
            for c in candidates
        ):
            fail("SCOPE_MISMATCH", "Matching candidates must share the subject tenant and kind.")
        found = []
        try:
            for candidate in candidates:
                score = self.compare(subject, candidate)
                if score is None:
                    continue
                if (
                    type(score) not in {float, int}
                    or not math.isfinite(score)
                    or not 0 <= score <= 1
                ):
                    raise ValueError("comparator must return a score in [0, 1] or None")
                if score >= self.review_threshold:
                    found.append(MatchCandidate(candidate.reference, float(score)))
        except Exception:
            return MatchResult(subject.digest, self.digest, "error", ())
        found.sort(key=lambda c: (-c.score, c.target.object_id))
        status = "no_match"
        if found:
            status = (
                "match" if len(found) == 1 and found[0].score >= self.match_threshold else "review"
            )
        return MatchResult(subject.digest, self.digest, status, tuple(found))


@dataclass(frozen=True)
class FieldClaim(Record):
    target: ObjectId
    field: str
    value: Any
    source: str
    evidence: tuple[EvidenceRef, ...]
    qualifiers: Mapping[str, Any] = dc_field(default_factory=dict)

    def __post_init__(self) -> None:
        identifier(self.field, "field")
        identifier(self.source, "source")
        object.__setattr__(self, "value", frozen_json(self.value))
        object.__setattr__(self, "qualifiers", frozen_json(self.qualifiers))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not self.evidence:
            raise ValueError("claims require attributed evidence")
        if any(e.source.tenant_id != self.target.tenant_id for e in self.evidence):
            fail("SCOPE_MISMATCH", "Claim evidence belongs to another tenant.")


@dataclass(frozen=True)
class ClaimResolution(Record):
    policy_digest: str
    status: str
    preferred: FieldClaim | None
    claims: tuple[FieldClaim, ...]


@dataclass(frozen=True)
class ResolutionPolicy(DomainComponent):
    """Explicit source priority; tied contradictory values remain unresolved."""

    name: str
    source_priority: tuple[str, ...]
    version: str = dc_field(default="1", kw_only=True)
    kind: ClassVar[str] = "resolution"

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        object.__setattr__(self, "source_priority", tuple(self.source_priority))
        if not self.source_priority or len(set(self.source_priority)) != len(self.source_priority):
            raise ValueError("source priority requires non-empty unique sources")

    def manifest(self) -> dict[str, Any]:
        return {"source_priority": list(self.source_priority)}

    def resolve(self, claims: Sequence[FieldClaim]) -> ClaimResolution:
        from agnoclaw.runtime.security import canonical_json_digest

        selected = tuple({claim.digest: claim for claim in claims}.values())
        if not selected:
            raise ValueError("resolution requires claims")
        first = selected[0]
        if any((claim.target, claim.field) != (first.target, first.field) for claim in selected):
            fail("CLAIM_SCOPE_MISMATCH", "Resolve one object field at a time.")
        if any(claim.qualifiers != first.qualifiers for claim in selected):
            return ClaimResolution(self.digest, "incomparable", None, selected)
        rank = {source: index for index, source in enumerate(self.source_priority)}
        known = [claim for claim in selected if claim.source in rank]
        if not known:
            return ClaimResolution(self.digest, "unresolved", None, selected)
        best = min(rank[claim.source] for claim in known)
        top = [claim for claim in known if rank[claim.source] == best]
        if len({canonical_json_digest(claim.value) for claim in top}) != 1:
            return ClaimResolution(self.digest, "conflict", None, selected)
        return ClaimResolution(self.digest, "preferred", top[0], selected)
