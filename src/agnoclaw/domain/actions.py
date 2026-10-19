"""Domain proposals and preflight checks over the existing capability executor."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agnoclaw.capabilities import CapabilitySpec
from agnoclaw.capability_schema import validate_capability_arguments
from agnoclaw.runtime.context import ExecutionContext

from ._contracts import Record, digest_string, fail, frozen_json, identifier
from .interaction import ReviewDecision
from .objects import ObjectRef
from .outputs import WorkProduct
from .verification import VerificationReport


@dataclass(frozen=True)
class ActionProposal(Record):
    target: ObjectRef
    capability_reference: str
    capability_digest: str
    product_digest: str
    arguments: Mapping[str, Any]
    evidence_digests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        identifier(self.capability_reference, "capability_reference")
        digest_string(self.capability_digest)
        digest_string(self.product_digest)
        object.__setattr__(self, "arguments", frozen_json(self.arguments, max_bytes=65_536))
        object.__setattr__(self, "evidence_digests", tuple(self.evidence_digests))
        for digest in self.evidence_digests:
            digest_string(digest)


@dataclass(frozen=True)
class ActionSpec:
    capability: CapabilitySpec
    required_checks: tuple[str, ...]
    review_required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_checks", tuple(self.required_checks))
        if not self.required_checks or any("@" not in ref for ref in self.required_checks):
            raise ValueError("actions require explicit versioned checks")
        if len(set(self.required_checks)) != len(self.required_checks):
            raise ValueError("action check requirements must be unique")

    @property
    def reference(self) -> str:
        return f"{self.capability.name}@{self.capability.version}"

    def propose(
        self,
        product: WorkProduct,
        arguments: Mapping[str, Any],
        *,
        context: ExecutionContext,
    ) -> ActionProposal:
        product.target.require_scope(context)
        validate_capability_arguments(
            self.capability.input_schema, arguments, capability=self.capability.name
        )
        return ActionProposal(
            product.target,
            self.reference,
            self.capability.digest,
            product.digest,
            arguments,
            tuple(item.digest for item in product.evidence),
        )

    def preflight(
        self,
        proposal: ActionProposal,
        *,
        context: ExecutionContext,
        current_target: ObjectRef,
        report: VerificationReport,
        now: datetime,
        review: ReviewDecision | None = None,
    ) -> None:
        """Use inside a registered capability or executor pre_dispatch callback.

        The host loads the authoritative review and current revision. Its mutation
        must recheck the revision atomically, enforce RBAC, and commit before returning.
        This check neither executes nor authorizes a capability on its own.
        """
        proposal.target.require_scope(context)
        proposal.target.require_current(current_target)
        if (
            proposal.capability_reference != self.reference
            or proposal.capability_digest != self.capability.digest
        ):
            fail("CAPABILITY_CHANGED", "Proposal capability version or implementation changed.")
        validate_capability_arguments(
            self.capability.input_schema, proposal.arguments, capability=self.capability.name
        )
        report.require_pass(proposal.digest, self.required_checks)
        if self.review_required or review is not None:
            if review is None:
                fail("REVIEW_REQUIRED", "An authoritative review is required before dispatch.")
            assert review is not None
            review.require_approved(
                target=proposal.target, subject_digest=proposal.digest, report=report, now=now
            )
