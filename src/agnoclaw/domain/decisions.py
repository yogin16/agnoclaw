"""Optional business decision rules and bounded inference routing.

Rules, model recommendations, human commitments, and runtime authorization are
separate. This is not a DMN implementation or a model execution engine.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agnoclaw.runtime.security import canonical_json_digest

from ._contracts import Record, digest_string, fail, frozen_json, identifier
from .verification import VerificationReport


@dataclass(frozen=True)
class DecisionRule:
    rule_id: str
    outcome: str
    predicate: Callable[[Mapping[str, Any]], bool] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        identifier(self.rule_id, "rule_id")
        identifier(self.outcome, "outcome")
        if not callable(self.predicate):
            raise TypeError("decision rule predicate must be callable")


class DecisionStatus(StrEnum):
    DECIDED = "decided"
    NO_MATCH = "no_match"
    CONFLICT = "conflict"
    ERROR = "error"


@dataclass(frozen=True)
class DecisionResult(Record):
    model_reference: str
    implementation_digest: str
    input_digest: str
    status: DecisionStatus
    outcome: str | None
    matched_rules: tuple[str, ...]

    def __post_init__(self) -> None:
        identifier(self.model_reference, "model_reference")
        digest_string(self.implementation_digest)
        digest_string(self.input_digest)
        object.__setattr__(self, "status", DecisionStatus(self.status))
        object.__setattr__(self, "matched_rules", tuple(self.matched_rules))
        if (self.status is DecisionStatus.DECIDED) != (self.outcome is not None):
            raise ValueError("only a decided result may have an outcome")


@dataclass(frozen=True)
class DecisionTable:
    """Host-authored pure predicates with unique-hit semantics and explicit abstention."""

    name: str
    version: str
    implementation_digest: str
    rules: tuple[DecisionRule, ...]

    def __post_init__(self) -> None:
        identifier(self.name, "name")
        identifier(self.version, "version")
        digest_string(self.implementation_digest)
        object.__setattr__(self, "rules", tuple(self.rules))
        if not self.rules or len({rule.rule_id for rule in self.rules}) != len(self.rules):
            raise ValueError("decision tables need non-empty, uniquely identified rules")

    def evaluate(self, facts: Mapping[str, Any]) -> DecisionResult:
        facts = frozen_json(facts)
        matched: list[DecisionRule] = []
        status = DecisionStatus.NO_MATCH
        try:
            for rule in self.rules:
                result = rule.predicate(facts)
                if type(result) is not bool:
                    raise TypeError("predicates must return bool, not truthy values")
                if result:
                    matched.append(rule)
            if len(matched) == 1:
                status = DecisionStatus.DECIDED
            elif matched:
                status = DecisionStatus.CONFLICT
        except Exception:
            status = DecisionStatus.ERROR
        return DecisionResult(
            f"{self.name}@{self.version}",
            self.implementation_digest,
            canonical_json_digest(facts),
            status,
            matched[0].outcome if status is DecisionStatus.DECIDED else None,
            tuple(rule.rule_id for rule in matched),
        )


class InferenceRoute(StrEnum):
    FAST = "fast"
    DELIBERATE = "deliberate"
    REVIEW = "review"
    ACCEPT = "accept"


@dataclass(frozen=True)
class RouteDecision(Record):
    route: InferenceRoute
    reason: str


@dataclass(frozen=True)
class InferencePolicy:
    """Optional System-1/System-2 routing hints; host selects Agno model factories.

    A fast path must be explicitly enabled after domain evaluation. Each model tier
    is attempted at most once; uncertainty then routes to human review. ACCEPT means
    inference is sufficient, never that an action is authorized.
    """

    fast_path_validated: bool = False
    requires_review: bool = False

    def choose(
        self,
        *,
        required_checks: Sequence[str],
        report: VerificationReport | None = None,
        subject_digest: str | None = None,
        attempted: Sequence[InferenceRoute] = (),
        ambiguous_evidence: bool = False,
    ) -> RouteDecision:
        if not required_checks:
            raise ValueError("inference routing requires explicit acceptance checks")
        if report is not None and report.subject_digest != subject_digest:
            fail("CHECK_SUBJECT_MISMATCH", "Routing checks must describe the current subject.")
        attempts = {InferenceRoute(route) for route in attempted}
        if report is not None and report.passes(required_checks) and not ambiguous_evidence:
            if self.requires_review:
                return RouteDecision(InferenceRoute.REVIEW, "Domain policy requires human review.")
            return RouteDecision(InferenceRoute.ACCEPT, "Required checks passed.")
        if InferenceRoute.DELIBERATE in attempts:
            return RouteDecision(InferenceRoute.REVIEW, "Unresolved after deliberate inference.")
        if (
            self.fast_path_validated
            and not ambiguous_evidence
            and report is None
            and InferenceRoute.FAST not in attempts
        ):
            return RouteDecision(InferenceRoute.FAST, "Domain-evaluated fast path is available.")
        return RouteDecision(
            InferenceRoute.DELIBERATE, "Use deliberate inference for unresolved work."
        )
