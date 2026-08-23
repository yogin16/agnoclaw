"""Live coordination over the durable approval ledger for capability calls."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from .capabilities import CapabilitySpec
from .runtime.approvals import (
    ApprovalAlreadySettledError,
    ApprovalDecision,
    ApprovalNotFoundError,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalState,
)
from .runtime.context import ExecutionContext
from .runtime.errors import HarnessError
from .runtime.hooks import ToolCallRequest
from .runtime.lifecycle import LifecycleTransition, RunState, TransitionKind
from .runtime.permissions import PermissionController
from .runtime.policy import PolicyDecision
from .runtime.security import (
    AuthorizationGrant,
    GrantScope,
    canonical_json_digest,
)
from .runtime.store import RunOwner, RuntimeStore


def _digest(value: Any) -> str:
    return canonical_json_digest(value)


@dataclass(frozen=True)
class DurableAuthorization:
    """Settled policy projection plus its immutable approval evidence."""

    decision: PolicyDecision
    record: ApprovalRecord


class DurableApprovalCoordinator:
    """Persist, await, and consume exact approvals without owning policy."""

    def __init__(
        self,
        store: RuntimeStore,
        controller: PermissionController,
        *,
        ttl_seconds: int = 900,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("approval ttl_seconds must be positive")
        if poll_interval_seconds <= 0 or poll_interval_seconds > 5:
            raise ValueError("approval poll interval must be in (0, 5] seconds")
        for method_name in (
            "apply_transition",
            "get_approval",
            "list_approvals",
            "settle_approval",
            "expire_approval",
        ):
            if not callable(getattr(store, method_name, None)):
                raise HarnessError(
                    code="RUNTIME_STORE_APPROVALS_REQUIRED",
                    category="configuration",
                    message="Durable approvals require a schema-v7 RuntimeStore.",
                    retryable=False,
                    details={"missing_method": method_name},
                )
        self.store = store
        self.controller = controller
        self.ttl_seconds = ttl_seconds
        self.poll_interval_seconds = poll_interval_seconds

    @staticmethod
    def _owner(context: ExecutionContext) -> RunOwner:
        return RunOwner(tenant_id=context.tenant_id, user_id=context.user_id)

    @staticmethod
    def _require_identity(context: ExecutionContext) -> tuple[str, str, str, str]:
        admission = context.admission
        values = {
            "tenant_id": context.tenant_id,
            "principal_id": context.user_id,
            "session_id": context.session_id,
            "authority_digest": (admission.authority_digest if admission is not None else None),
        }
        missing = next((name for name, value in values.items() if not value), None)
        if missing is not None:
            raise HarnessError(
                code="APPROVAL_IDENTITY_REQUIRED",
                category="authorization",
                message="Durable approval requires complete trusted run identity.",
                retryable=False,
                details={"field": missing},
            )
        return (
            str(values["tenant_id"]),
            str(values["principal_id"]),
            str(values["session_id"]),
            str(values["authority_digest"]),
        )

    @staticmethod
    async def _store_call(call: Callable[[], Any]) -> Any:
        """Observe a transaction through commit/rollback despite task cancellation."""

        def captured() -> tuple[bool, Any]:
            try:
                return True, call()
            except BaseException as exc:
                return False, exc

        task = asyncio.create_task(asyncio.to_thread(captured))
        cancelled = False
        while True:
            try:
                succeeded, value = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        if succeeded:
            return value
        raise value

    @staticmethod
    def _verify_request_authority(
        record: ApprovalRecord,
        context: ExecutionContext,
    ) -> None:
        tenant, principal, session, authority = DurableApprovalCoordinator._require_identity(
            context
        )
        expected = (
            record.request.tenant_id,
            record.request.principal_id,
            record.request.session_id,
            record.request.authority_digest,
        )
        if expected != (tenant, principal, session, authority):
            raise HarnessError(
                code="APPROVAL_AUTHORITY_MISMATCH",
                category="authorization",
                message="Approval authority differs from the requesting run.",
                retryable=False,
                details={"request_id": record.request.request_id},
            )

    def list_for_run(
        self,
        run_id: str,
        *,
        context: ExecutionContext,
        states: tuple[ApprovalState, ...] | None = None,
        limit: int = 100,
    ) -> tuple[ApprovalRecord, ...]:
        self._require_identity(context)
        records = self.store.list_approvals(
            run_id,
            states=states,
            limit=limit,
            owner=self._owner(context),
        )
        for record in records:
            self._verify_request_authority(record, context)
        return tuple(records)

    def decide(
        self,
        request_id: str,
        *,
        approved: bool,
        issuer: str,
        reason_code: str,
        context: ExecutionContext,
        grant_scope: GrantScope = GrantScope.RUN,
    ) -> ApprovalRecord:
        """Settle one request through the trusted host API; model data is not authority."""
        owner = self._owner(context)
        record = self.store.get_approval(request_id, owner=owner)
        self._verify_request_authority(record, context)
        replayed = self._exact_decision_replay(
            record,
            approved=approved,
            issuer=issuer,
            reason_code=reason_code,
            grant_scope=grant_scope,
        )
        if replayed is not None:
            return replayed
        decision = ApprovalDecision(
            decision_id=f"approval_decision_{uuid4().hex}",
            request_id=request_id,
            request_digest=record.request.digest,
            request_nonce=record.request.nonce,
            approved=approved,
            issuer=issuer,
            reason_code=reason_code,
            grant_scope=grant_scope,
        )
        grant = None
        if approved:
            grant = AuthorizationGrant(
                grant_id=f"authorization_grant_{uuid4().hex}",
                scope=grant_scope,
                tenant_id=record.request.tenant_id,
                principal_id=record.request.principal_id,
                session_id=record.request.session_id,
                run_id=(record.request.run_id if grant_scope is GrantScope.RUN else None),
                capability_ids=(record.request.capability_id,),
                capability_digests=(record.request.capability_digest,),
                effect_categories=(record.request.effect_category,),
                argument_digest=record.request.argument_digest,
                policy_version=record.request.policy_version,
                authority_digest=record.request.authority_digest,
                issuer=issuer,
                issued_at=decision.decided_at,
                expires_at=record.request.expires_at,
                nonce=f"authorization_nonce_{uuid4().hex}",
            )
        try:
            settled = self.store.settle_approval(
                decision,
                expected_revision=record.revision,
                grant=grant,
                owner=owner,
            ).record
        except HarnessError as exc:
            if exc.code not in {
                "APPROVAL_ALREADY_SETTLED",
                "APPROVAL_REVISION_CONFLICT",
                "APPROVAL_RUN_NOT_WAITING",
            }:
                raise
            current = self.store.get_approval(request_id, owner=owner)
            replayed = self._exact_decision_replay(
                current,
                approved=approved,
                issuer=issuer,
                reason_code=reason_code,
                grant_scope=grant_scope,
            )
            if replayed is None:
                raise
            return replayed
        self._resume_run_sync(settled, context=context)
        return settled

    @staticmethod
    def _exact_decision_replay(
        record: ApprovalRecord,
        *,
        approved: bool,
        issuer: str,
        reason_code: str,
        grant_scope: GrantScope,
    ) -> ApprovalRecord | None:
        """Return an exact terminal decision replay; reject conflicting retries."""
        if record.state is ApprovalState.PENDING:
            return None
        decision = record.decision
        if decision is not None and (
            decision.approved,
            decision.issuer,
            decision.reason_code,
            decision.grant_scope,
        ) == (approved, issuer, reason_code, grant_scope):
            return record
        raise ApprovalAlreadySettledError(
            request_id=record.request.request_id,
            state=record.state,
        )

    async def _expire(self, record: ApprovalRecord, context: ExecutionContext) -> ApprovalRecord:
        decision = ApprovalDecision(
            decision_id=f"approval_expiry_{uuid4().hex}",
            request_id=record.request.request_id,
            request_digest=record.request.digest,
            request_nonce=record.request.nonce,
            approved=False,
            issuer="agnoclaw:expiry-v1",
            reason_code="APPROVAL_EXPIRED",
        )
        result = await self._store_call(
            lambda: self.store.expire_approval(
                decision,
                expected_revision=record.revision,
                owner=self._owner(context),
            )
        )
        return result.record

    async def _resume_run(
        self,
        record: ApprovalRecord,
        *,
        context: ExecutionContext,
    ) -> None:
        await self._store_call(lambda: self._resume_run_sync(record, context=context))

    def _resume_run_sync(
        self,
        record: ApprovalRecord,
        *,
        context: ExecutionContext,
    ) -> None:
        """Idempotently leave an exact approval wait after durable settlement."""
        snapshot = self.store.get_run(
            record.request.run_id,
            owner=self._owner(context),
        )
        if snapshot.state is RunState.RUNNING and snapshot.pending_request_id is None:
            return
        if (
            snapshot.state is not RunState.WAITING_FOR_APPROVAL
            or snapshot.pending_request_id != record.request.request_id
        ):
            raise HarnessError(
                code="APPROVAL_RUN_NOT_WAITING",
                category="approval",
                message="The run left its approval wait before continuation.",
                retryable=False,
                details={"request_id": record.request.request_id},
            )
        self.store.apply_transition(
            LifecycleTransition(
                run_id=record.request.run_id,
                kind=TransitionKind.RESPOND,
                transition_id=(
                    f"{record.request.run_id}:approval:{record.request.request_id}:respond"
                ),
                pending_request_id=record.request.request_id,
                reason_code=(
                    record.decision.reason_code
                    if record.decision is not None
                    else "APPROVAL_SETTLED"
                ),
                payload={
                    "approval_state": record.state.value,
                    "decision_digest": (
                        record.decision.digest if record.decision is not None else None
                    ),
                },
            ),
            expected_revision=snapshot.revision,
        )

    def validate_grant(
        self,
        authorization: DurableAuthorization,
        *,
        context: ExecutionContext,
        spec: CapabilitySpec,
        category: str,
        arguments: dict[str, Any],
        policy_version: str,
    ) -> None:
        """Reauthorize an approved grant at the exact effect boundary."""
        record = authorization.record
        grant = record.grant
        if record.state is not ApprovalState.APPROVED or grant is None:
            raise HarnessError(
                code="AUTHORIZATION_GRANT_REQUIRED",
                category="authorization",
                message="Capability dispatch requires an approved authorization grant.",
                retryable=False,
                details={"request_id": record.request.request_id},
            )
        tenant, principal, session, authority = self._require_identity(context)
        expected_run = record.request.run_id if grant.scope is GrantScope.RUN else None
        bindings: dict[str, tuple[Any, Any]] = {
            "tenant_id": (tenant, grant.tenant_id),
            "principal_id": (principal, grant.principal_id),
            "session_id": (session, grant.session_id),
            "run_id": (expected_run, grant.run_id),
            "capability_ids": ((f"{spec.name}@{spec.version}",), grant.capability_ids),
            "capability_digests": ((spec.digest,), grant.capability_digests),
            "effect_categories": ((category,), grant.effect_categories),
            "argument_digest": (_digest(arguments), grant.argument_digest),
            "policy_version": (policy_version, grant.policy_version),
            "authority_digest": (authority, grant.authority_digest),
        }
        mismatch = next(
            (field_name for field_name, values in bindings.items() if values[0] != values[1]),
            None,
        )
        if mismatch is not None or grant.is_expired():
            raise HarnessError(
                code=(
                    "AUTHORIZATION_GRANT_EXPIRED"
                    if mismatch is None
                    else "AUTHORIZATION_GRANT_INVALID"
                ),
                category="authorization",
                message="Authorization grant is no longer valid for this dispatch.",
                retryable=False,
                details={
                    "request_id": record.request.request_id,
                    "field": mismatch or "expires_at",
                },
            )

    def pre_dispatch(
        self,
        renewal: Callable[[], Any],
        authorization: DurableAuthorization,
        *,
        context: ExecutionContext,
        spec: CapabilitySpec,
        category: str,
        arguments: dict[str, Any],
        policy_version: Callable[[], str],
    ) -> Callable[[], Awaitable[None]]:
        """Compose lease renewal and exact grant reauthorization."""

        async def verify() -> None:
            renewed = renewal()
            if inspect.isawaitable(renewed):
                await renewed
            self.validate_grant(
                authorization,
                context=context,
                spec=spec,
                category=category,
                arguments=arguments,
                policy_version=policy_version(),
            )

        return verify

    async def authorize(
        self,
        *,
        request: ToolCallRequest,
        context: ExecutionContext,
        spec: CapabilitySpec,
        category: str,
        arguments: dict[str, Any],
        policy_version: str,
        resolve_async_value: Callable[[Any], Awaitable[Any]],
    ) -> DurableAuthorization:
        """Persist before asking, wait durably, then resume with exact evidence."""
        tenant, principal, session, authority = self._require_identity(context)
        call_id = (
            str(request.metadata.get("call_id"))
            if request.metadata.get("call_id")
            else None
        )
        if call_id is None:
            raise HarnessError(
                code="APPROVAL_CALL_ID_REQUIRED",
                category="approval",
                message="Durable approval requires a stable provider tool-call identity.",
                retryable=False,
                details={"run_id": request.run_id},
            )
        argument_digest = _digest(arguments)
        identity_digest = _digest(
            {
                "schema": "agnoclaw.approval.identity.v1",
                "run_id": request.run_id,
                "call_id": call_id,
                "capability_id": f"{spec.name}@{spec.version}",
                "capability_digest": spec.digest,
                "effect_category": category,
                "argument_digest": argument_digest,
                "policy_version": policy_version,
                "authority_digest": authority,
                "tenant_id": tenant,
                "principal_id": principal,
                "session_id": session,
            }
        )
        request_id = f"approval_{identity_digest.split(':', 1)[1][:40]}"
        owner = self._owner(context)
        created = False
        try:
            record = await self._store_call(
                lambda: self.store.get_approval(request_id, owner=owner)
            )
        except ApprovalNotFoundError:
            requested_at = datetime.now(UTC)
            approval = ApprovalRequest(
                request_id=request_id,
                run_id=request.run_id,
                call_id=call_id,
                capability_id=f"{spec.name}@{spec.version}",
                capability_digest=spec.digest,
                effect_category=category,
                argument_digest=argument_digest,
                policy_version=policy_version,
                authority_digest=authority,
                tenant_id=tenant,
                principal_id=principal,
                session_id=session,
                requested_at=requested_at.isoformat(),
                expires_at=(requested_at + timedelta(seconds=self.ttl_seconds)).isoformat(),
                nonce=f"approval_nonce_{uuid4().hex}",
            )
            record = ApprovalRecord(request=approval)
            created = True
        else:
            approval = record.request
            expected = (
                request.run_id,
                call_id,
                f"{spec.name}@{spec.version}",
                spec.digest,
                category,
                argument_digest,
                policy_version,
                authority,
                tenant,
                principal,
                session,
            )
            actual = (
                approval.run_id,
                approval.call_id,
                approval.capability_id,
                approval.capability_digest,
                approval.effect_category,
                approval.argument_digest,
                approval.policy_version,
                approval.authority_digest,
                approval.tenant_id,
                approval.principal_id,
                approval.session_id,
            )
            if actual != expected:
                raise HarnessError(
                    code="APPROVAL_REPLAY_MISMATCH",
                    category="approval",
                    message="The persisted approval differs from the replayed tool call.",
                    retryable=False,
                    details={"request_id": request_id},
                )
        snapshot = await self._store_call(
            lambda: self.store.get_run(request.run_id, owner=owner)
        )
        if created or (
            record.state is ApprovalState.PENDING
            and snapshot.state is not RunState.WAITING_FOR_APPROVAL
        ):
            await self._store_call(
                lambda: self.store.apply_transition(
                    LifecycleTransition(
                        run_id=request.run_id,
                        kind=TransitionKind.WAIT_FOR_APPROVAL,
                        transition_id=f"{request.run_id}:{approval.request_id}:wait",
                        occurred_at=approval.requested_at,
                        pending_request_id=approval.request_id,
                        reason_code=approval.reason_code,
                    ),
                    expected_revision=snapshot.revision,
                    approval_request=approval,
                )
            )

        approver = self.controller.approver
        if created and approver is not None:
            allowed = await resolve_async_value(
                approver.approve(
                    self.controller.permission_request(request, category=category),
                    context,
                )
            )
            approval_version = str(getattr(approver, "approval_version", "unknown"))
            issuer = (
                f"{approver.__class__.__module__}."
                f"{approver.__class__.__qualname__}:{approval_version}"
            )
            await self._store_call(
                lambda: self.decide(
                    approval.request_id,
                    approved=bool(allowed),
                    issuer=issuer,
                    reason_code=("PERMISSION_APPROVED" if bool(allowed) else "PERMISSION_REJECTED"),
                    context=context,
                )
            )

        while True:
            record = await self._store_call(
                lambda: self.store.get_approval(
                    approval.request_id,
                    owner=self._owner(context),
                )
            )
            self._verify_request_authority(record, context)
            if record.state is not ApprovalState.PENDING:
                break
            if record.request.is_expired():
                record = await self._expire(record, context)
                break
            await asyncio.sleep(self.poll_interval_seconds)

        await self._resume_run(record, context=context)
        if record.state is ApprovalState.APPROVED:
            authorization = DurableAuthorization(
                decision=self.controller.decision_from_approval(
                    allowed=True,
                    tool_name=request.tool_name,
                ),
                record=record,
            )
            self.validate_grant(
                authorization,
                context=context,
                spec=spec,
                category=category,
                arguments=arguments,
                policy_version=policy_version,
            )
            return authorization
        reason_code = (
            record.decision.reason_code if record.decision is not None else "PERMISSION_REJECTED"
        )
        return DurableAuthorization(
            decision=PolicyDecision.deny(
                reason_code=reason_code,
                message=f"Permission rejected for tool '{request.tool_name}'.",
            ),
            record=record,
        )


__all__ = ["DurableApprovalCoordinator", "DurableAuthorization"]
