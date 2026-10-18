"""Portable, exact-subject human review records for host-owned review queues."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from agnoclaw.runtime.context import ExecutionContext

from ._contracts import Record, aware, digest_string, fail, frozen_json, identifier
from .objects import ObjectRef
from .verification import VerificationReport


class ReviewVerdict(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True)
class ReviewRequest(Record):
    target: ObjectRef
    subject_digest: str
    report_digest: str
    expires_at: datetime
    required_scope: str
    summary: str
    changes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        digest_string(self.subject_digest)
        digest_string(self.report_digest)
        aware(self.expires_at)
        identifier(self.required_scope, "required_scope")
        identifier(self.summary, "summary")
        object.__setattr__(self, "changes", frozen_json(self.changes))

    def decide(
        self,
        verdict: ReviewVerdict,
        *,
        context: ExecutionContext,
        now: datetime,
    ) -> ReviewDecision:
        """Host authenticates context and stores this record; never trust a client receipt."""
        aware(now)
        self.target.require_scope(context)
        if not context.user_id or self.required_scope not in context.scopes:
            fail("REVIEW_SCOPE_REQUIRED", "Reviewer lacks the required authenticated scope.")
        if now >= self.expires_at:
            fail("REVIEW_EXPIRED", "The review request has expired.")
        return ReviewDecision(self, ReviewVerdict(verdict), context.user_id, now)


@dataclass(frozen=True)
class ReviewDecision(Record):
    request: ReviewRequest
    verdict: ReviewVerdict
    reviewer_id: str
    decided_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdict", ReviewVerdict(self.verdict))
        identifier(self.reviewer_id, "reviewer_id")
        aware(self.decided_at)
        if self.decided_at >= self.request.expires_at:
            raise ValueError("review decision must precede request expiry")

    def require_approved(
        self,
        *,
        target: ObjectRef,
        subject_digest: str,
        report: VerificationReport,
        now: datetime,
    ) -> None:
        aware(now)
        if self.verdict is not ReviewVerdict.APPROVE:
            fail("REVIEW_REJECTED", "The review did not approve this operation.")
        if not self.decided_at <= now < self.request.expires_at:
            fail("REVIEW_EXPIRED", "The review is expired or not yet valid.")
        if (
            self.request.target != target
            or self.request.subject_digest != subject_digest
            or self.request.report_digest != report.digest
        ):
            fail("REVIEW_MISMATCH", "Review does not cover the exact target, proposal and checks.")
