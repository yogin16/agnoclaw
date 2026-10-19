"""Bind checked proposals and authoritative review lookup to existing execution."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

from agnoclaw.capabilities import CapabilitySpec
from agnoclaw.capability_execution import CapabilityExecution, CapabilityExecutor
from agnoclaw.runtime.context import ExecutionContext
from agnoclaw.runtime.security import AdmissionEnvelope, canonical_json_digest, thaw_data

from ._contracts import Record, digest_string, fail
from .actions import ActionProposal, ActionSpec
from .authoring import Check, _checks, _report
from .identity import ContractRef, OperationIdentity
from .interaction import ReviewDecision, ReviewRequest
from .objects import ObjectId, ObjectRef
from .outputs import WorkProduct
from .verification import VerificationReport

T = TypeVar("T")


async def _resolve(value: T | Awaitable[T]) -> T:
    return await value if inspect.isawaitable(value) else value


@dataclass(frozen=True)
class PreparedAction(Record):
    proposal: ActionProposal
    report: VerificationReport
    policy_digest: str

    def __post_init__(self) -> None:
        digest_string(self.policy_digest)
        if self.proposal.digest != self.report.subject_digest:
            fail("CHECK_SUBJECT_MISMATCH", "Action report describes a different proposal.")

    @property
    def review_key(self) -> str:
        """Host queue lookup key, never a review authorization token."""
        return canonical_json_digest(
            ["action-review", self.proposal.digest, self.report.digest, self.policy_digest]
        )

    def request_review(
        self,
        *,
        summary: str,
        required_scope: str,
        expires_at: datetime,
        changes: Mapping[str, Any] | None = None,
    ) -> ReviewRequest:
        return ReviewRequest(
            self.proposal.target,
            self.proposal.digest,
            self.report.digest,
            expires_at,
            required_scope,
            summary,
            changes if changes is not None else {},
        )

    def operation_identity(self, request_id: str) -> OperationIdentity:
        """Bind a host request to this exact reviewed intent, independent of attempts.

        Keep the prepared record when retrying. Regenerating it can produce a different
        intent and must not silently reuse the original request's business receipt.
        """
        return OperationIdentity(
            self.proposal.target.tenant_id,
            ContractRef.parse(self.proposal.capability_reference).name,
            request_id,
            canonical_json_digest([self.proposal.digest, self.report.digest, self.policy_digest]),
        )


@dataclass(frozen=True)
class Action:
    """A business capability with executable checks and bound reviewed dispatch.

    The host must bind object-identifying arguments before prepare(), load reviews
    authoritatively, and enforce RBAC/CAS/idempotency in its committed mutation.
    execute() selects the exact prepared arguments; callers cannot substitute them.
    """

    capability: CapabilitySpec
    checks: tuple[Check[ActionProposal], ...] = field(kw_only=True)
    review_required: bool = field(default=True, kw_only=True)

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", _checks(self.checks))
        if not isinstance(self.capability, CapabilitySpec):
            raise TypeError("action capability must be a CapabilitySpec")
        ContractRef(self.capability.name, self.capability.version)

    @property
    def spec(self) -> ActionSpec:
        return ActionSpec(
            self.capability, tuple(str(check.ref) for check in self.checks), self.review_required
        )

    @property
    def digest(self) -> str:
        return canonical_json_digest(
            [self.capability.digest, self.review_required, [check.digest for check in self.checks]]
        )

    async def prepare(
        self,
        product: WorkProduct,
        arguments: Mapping[str, Any],
        *,
        context: ExecutionContext,
    ) -> PreparedAction:
        proposal = self.spec.propose(product, arguments, context=context)
        return PreparedAction(proposal, await _report(proposal, self.checks), self.digest)

    async def execute(
        self,
        executor: CapabilityExecutor,
        prepared: PreparedAction,
        *,
        run_id: str,
        attempt_id: str,
        operation_id: str,
        admission: AdmissionEnvelope,
        load_current: Callable[[ObjectId], ObjectRef | Awaitable[ObjectRef]],
        load_review: Callable[
            [PreparedAction], ReviewDecision | None | Awaitable[ReviewDecision | None]
        ],
        idempotency_key: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> CapabilityExecution:
        if prepared.policy_digest != self.digest:
            fail(
                "ACTION_POLICY_CHANGED", "Prepare and review again under the current action policy."
            )
        # One identity boundary for both the domain checks and executor admission.
        principal = admission.identity
        context = ExecutionContext.create(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            session_id=principal.session_id,
            workspace_id=principal.workspace_id,
            org_id=principal.org_id,
            team_id=principal.team_id,
            roles=principal.roles,
            scopes=principal.scopes,
            admission=admission,
        )
        prepared.proposal.target.require_scope(context)
        if (
            prepared.proposal.capability_reference != self.spec.reference
            or prepared.proposal.capability_digest != self.capability.digest
        ):
            fail("CAPABILITY_CHANGED", "Prepared action belongs to another capability.")

        async def preflight() -> None:
            current = await _resolve(load_current(prepared.proposal.target.identity))
            review = await _resolve(load_review(prepared))
            self.spec.preflight(
                prepared.proposal,
                context=context,
                current_target=current,
                report=prepared.report,
                review=review,
                now=clock(),
            )

        return await executor.execute(
            self.spec.reference,
            operation_id=operation_id,
            run_id=run_id,
            attempt_id=attempt_id,
            admission=admission,
            arguments=thaw_data(prepared.proposal.arguments),
            idempotency_key=idempotency_key,
            pre_dispatch=preflight,
            expected_capability_digest=self.capability.digest,
        )
