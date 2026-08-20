"""AgentHarness capability governance composition outside the public facade."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from .capabilities import EffectClass
from .capability_execution import CapabilityExecution
from .capability_governance import (
    apply_capability_policy_constraints,
    apply_capability_result_policy,
    capability_lease_evidence,
    capability_permission_category,
    capability_policy_record,
    capability_policy_version,
    enforce_capability_scopes,
)
from .runtime.context import ExecutionContext
from .runtime.errors import HarnessError
from .runtime.hooks import ToolCallRequest

if TYPE_CHECKING:
    from .agent import AgentHarness


_CAPABILITY_METADATA_FIELDS = frozenset(
    {
        "capability_kind",
        "effect_class",
        "lease_evidence",
        "policy_evidence",
    }
)


async def execute_harness_capability(
    harness: AgentHarness,
    reference: str,
    *,
    operation_id: str,
    arguments: dict[str, Any] | None = None,
    run_id: str | None = None,
    attempt_id: str | None = None,
    context: ExecutionContext | None = None,
    idempotency_key: str | None = None,
    timeout_seconds: float | None = None,
    safe_metadata: dict[str, Any] | None = None,
    policy_evidence: tuple[dict[str, Any], ...] | None = None,
) -> CapabilityExecution:
    """Execute one registered capability through governance, approval, and effect gates."""
    if harness._closed:
        raise HarnessError(
            code="HARNESS_CLOSED",
            category="lifecycle",
            message="This harness no longer accepts capability calls.",
            retryable=False,
        )
    active_run_id = harness._active_runtime_run_id.get()
    resolved_run_id = run_id or active_run_id
    if resolved_run_id is None or active_run_id != resolved_run_id:
        raise HarnessError(
            code="CAPABILITY_ACTIVE_RUN_REQUIRED",
            category="lifecycle",
            message="Capability execution requires the currently leased lifecycle run.",
            retryable=False,
        )
    claim = harness._active_runtime_claim.get()
    if claim is None or claim.run_id != resolved_run_id:
        raise HarnessError(
            code="CAPABILITY_ACTIVE_LEASE_REQUIRED",
            category="runtime_lease",
            message="Capability execution requires the active run/session lease claim.",
            retryable=False,
            details={"run_id": resolved_run_id},
        )
    resolved_context = context or harness._active_runtime_context.get()
    if resolved_context is None or resolved_context.admission is None:
        raise HarnessError(
            code="CAPABILITY_ADMISSION_REQUIRED",
            category="authorization",
            message="Capability execution requires trusted run admission.",
            retryable=False,
        )
    active_context = harness._active_runtime_context.get()
    if (
        active_context is None
        or active_context.admission is None
        or active_context.admission.authority_digest != resolved_context.admission.authority_digest
    ):
        raise HarnessError(
            code="CAPABILITY_ACTIVE_AUTHORITY_CONFLICT",
            category="authorization",
            message="Capability authority differs from the active leased run.",
            retryable=False,
        )
    owner = harness._runtime_owner(resolved_context)
    await asyncio.to_thread(
        harness._get_runtime_store().get_run,
        resolved_run_id,
        owner=owner,
    )
    spec = harness._capability_registry.resolve(reference)
    enforce_capability_scopes(spec, resolved_context.admission)
    supplied_metadata = dict(safe_metadata or {})
    reserved = tuple(sorted(_CAPABILITY_METADATA_FIELDS.intersection(supplied_metadata)))
    if reserved:
        raise HarnessError(
            code="CAPABILITY_METADATA_RESERVED",
            category="capability",
            message="Capability metadata contains harness-owned evidence fields.",
            retryable=False,
            details={"reserved_fields": reserved},
        )
    governed_arguments = dict(arguments or {})
    evidence = list(policy_evidence or ())
    durable_authorization = None
    if policy_evidence is None:
        request = ToolCallRequest(
            run_id=resolved_run_id,
            tool_name=spec.name,
            arguments=dict(governed_arguments),
            metadata={
                "call_id": operation_id,
                "capability_digest": spec.digest,
                "capability_kind": spec.kind.value,
                "effect_class": spec.effect_class.value,
            },
        )
        violations = harness._guardrails.check(request)
        if violations:
            raise HarnessError(
                code="GUARDRAIL_DENIED",
                category="guardrail",
                message=f"Guardrail denied capability call: {spec.name}",
                retryable=False,
                details={"capability": spec.name},
            )
        permission_category = capability_permission_category(spec)
        durable_approvals = bool(harness.config.permission_durable_approvals)
        if durable_approvals:
            permission = harness._permission_controller.decision_before_approval(
                request,
                resolved_context,
                category=permission_category,
                is_read_only=spec.effect_class is EffectClass.READ_ONLY,
                defer_required_without_approver=True,
            )
        else:
            permission = await harness._permission_controller.acheck_capability_call(
                request,
                resolved_context,
                category=permission_category,
                is_read_only=spec.effect_class is EffectClass.READ_ONLY,
                resolve_async_value=harness._resolve_async_value,
            )
        deferred_approval = permission is None
        if permission is not None:
            await harness._enforce_policy_decision_async(
                decision=permission,
                checkpoint="permission.before_tool_call",
                run_id=resolved_run_id,
                context=resolved_context,
            )
            evidence.append(
                capability_policy_record(
                    checkpoint="permission.before_tool_call",
                    decision=permission,
                    spec=spec,
                    arguments=governed_arguments,
                    admission=resolved_context.admission,
                    policy_engines=harness._policy_engines,
                    permission_controller=harness._permission_controller,
                )
            )
        decision = await harness._run_policy_async(
            method_name="before_tool_call",
            payload=request,
            run_input=None,
            context=resolved_context,
        )
        await harness._enforce_policy_decision_async(
            decision=decision,
            checkpoint="before_tool_call",
            run_id=resolved_run_id,
            context=resolved_context,
        )
        evidence.append(
            capability_policy_record(
                checkpoint="before_tool_call",
                decision=decision,
                spec=spec,
                arguments=governed_arguments,
                admission=resolved_context.admission,
                policy_engines=harness._policy_engines,
                permission_controller=harness._permission_controller,
            )
        )
        governed_arguments, idempotency_key, timeout_seconds = apply_capability_policy_constraints(
            decision,
            arguments=governed_arguments,
            idempotency_key=idempotency_key,
            timeout_seconds=timeout_seconds,
        )
        request.arguments = dict(governed_arguments)
        violations = harness._guardrails.check(request)
        if violations:
            raise HarnessError(
                code="GUARDRAIL_DENIED",
                category="guardrail",
                message=f"Guardrail denied constrained capability: {spec.name}",
                retryable=False,
                details={"capability": spec.name},
            )
        if deferred_approval:
            request.arguments = dict(governed_arguments)
            durable_authorization = await harness._get_capability_approval_coordinator().authorize(
                request=request,
                context=resolved_context,
                spec=spec,
                category=permission_category,
                arguments=governed_arguments,
                policy_version=capability_policy_version(
                    harness._policy_engines,
                    harness._permission_controller,
                ),
                resolve_async_value=harness._resolve_async_value,
            )
            permission = durable_authorization.decision
            await harness._enforce_policy_decision_async(
                decision=permission,
                checkpoint="permission.before_tool_call",
                run_id=resolved_run_id,
                context=resolved_context,
            )
            evidence.insert(
                0,
                capability_policy_record(
                    checkpoint="permission.before_tool_call",
                    decision=permission,
                    spec=spec,
                    arguments=governed_arguments,
                    admission=resolved_context.admission,
                    policy_engines=harness._policy_engines,
                    permission_controller=harness._permission_controller,
                ),
            )
    lease_evidence = capability_lease_evidence(claim)

    def pre_dispatch() -> Any:
        return harness._renew_capability_lease(claim)

    if durable_authorization is not None:
        pre_dispatch = harness._get_capability_approval_coordinator().pre_dispatch(
            pre_dispatch,
            durable_authorization,
            context=resolved_context,
            spec=spec,
            category=capability_permission_category(spec),
            arguments=governed_arguments,
            policy_version=lambda: capability_policy_version(
                harness._policy_engines,
                harness._permission_controller,
            ),
        )
    return await harness._get_capability_executor().execute(
        f"{spec.name}@{spec.version}",
        operation_id=operation_id,
        run_id=resolved_run_id,
        attempt_id=(attempt_id or f"{resolved_run_id}:attempt:fence:{claim.run.fence_token}"),
        arguments=governed_arguments,
        profile="durable",
        admission=resolved_context.admission,
        session_id=resolved_context.session_id,
        idempotency_key=idempotency_key,
        timeout_seconds=timeout_seconds,
        safe_metadata={
            **supplied_metadata,
            "capability_kind": spec.kind.value,
            "effect_class": spec.effect_class.value,
            "lease_evidence": lease_evidence,
            "policy_evidence": evidence,
        },
        pre_dispatch=pre_dispatch,
        result_transformer=lambda result: apply_capability_result_policy(
            spec=spec,
            run_id=resolved_run_id,
            arguments=governed_arguments,
            context=resolved_context,
            result=result,
            run_policy=harness._run_policy_async,
            enforce_policy=harness._enforce_policy_decision_async,
        ),
    )


__all__ = ["execute_harness_capability"]
