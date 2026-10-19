"""Scoped feedback attribution; capture is separate from learning promotion."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agnoclaw.runtime.context import ExecutionContext

from ._contracts import Record, digest_string, fail, frozen_json, identifier
from .evaluation import EvaluationCase
from .objects import ObjectRef
from .outputs import WorkProduct


class FeedbackKind(StrEnum):
    CORRECTION = "correction"
    PREFERENCE = "preference"
    OUTCOME = "outcome"


@dataclass(frozen=True)
class FeedbackEvent(Record):
    target: ObjectRef
    product_digest: str
    run_id: str
    procedure_reference: str
    procedure_digest: str
    actor_id: str
    kind: FeedbackKind
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        digest_string(self.product_digest)
        digest_string(self.procedure_digest)
        for name in ("run_id", "procedure_reference", "actor_id"):
            identifier(getattr(self, name), name)
        object.__setattr__(self, "kind", FeedbackKind(self.kind))
        object.__setattr__(self, "payload", frozen_json(self.payload))

    @classmethod
    def capture(
        cls,
        product: WorkProduct,
        *,
        context: ExecutionContext,
        procedure_reference: str,
        procedure_digest: str,
        kind: FeedbackKind,
        payload: Mapping[str, Any],
    ) -> FeedbackEvent:
        """Host authenticates the actor and persists this event in its own transaction."""
        product.target.require_scope(context)
        if not context.user_id:
            fail("FEEDBACK_ACTOR_REQUIRED", "Feedback requires an authenticated actor.")
        return cls(
            product.target,
            product.digest,
            product.run_id,
            procedure_reference,
            procedure_digest,
            context.user_id,
            kind,
            payload,
        )

    def regression_case(self, inputs: Mapping[str, Any]) -> EvaluationCase:
        """Host explicitly curates corrected expected output; no automatic training/promotion."""
        if self.kind is not FeedbackKind.CORRECTION:
            raise ValueError("only curated corrections can become expected-output fixtures")
        return EvaluationCase(f"feedback-{self.digest.split(':')[1]}", inputs, self.payload)
