"""Pure governance helpers shared by AgentHarness capability ingress."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from .capabilities import CapabilityKind, CapabilitySpec
from .runtime.errors import HarnessError
from .runtime.hooks import ToolCallResult
from .runtime.leases import RunLeaseClaim
from .runtime.permissions import PermissionController
from .runtime.policy import (
    AllowAllPolicyEngine,
    PolicyAction,
    PolicyDecision,
    RedactionRule,
)
from .runtime.security import AdmissionEnvelope, freeze_data, thaw_data


def capability_digest(value: Any) -> str:
    """Digest bounded JSON-like authority data without retaining its content."""
    canonical = json.dumps(
        thaw_data(freeze_data(value)),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def capability_permission_category(spec: CapabilitySpec) -> str:
    """Map explicit capability semantics into the compatibility permission grammar."""
    if spec.effect_class.value == "read_only":
        return "read"
    if spec.kind is CapabilityKind.ELEVATED_COMMAND:
        return "elevated_exec"
    if spec.kind is CapabilityKind.CHILD_RUN:
        return "subagent"
    return f"capability:{spec.kind.value}"


def enforce_capability_scopes(
    spec: CapabilitySpec,
    admission: AdmissionEnvelope,
) -> None:
    """Reject missing declared authority before calling governance extensions."""
    missing = tuple(sorted(set(spec.required_scopes) - set(admission.identity.scopes)))
    if missing:
        raise HarnessError(
            code="CAPABILITY_SCOPE_REQUIRED",
            category="authorization",
            message=f"Capability '{spec.name}' requires additional scopes.",
            retryable=False,
            details={"capability": spec.name, "missing_scopes": missing},
        )


def _authority_version(value: Any, *, code: str, authority: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise HarnessError(
            code=code,
            category="policy",
            message=(
                f"{authority} must declare a non-empty version up to 256 characters "
                "for durable capability evidence."
            ),
            retryable=False,
            details={"authority": authority},
        )
    return value.strip()


def capability_policy_version(
    policy_engines: Sequence[tuple[str, Any]],
    permission_controller: PermissionController,
) -> str:
    """Return a stable digest of every policy and permission authority input."""
    engines = []
    for name, engine in policy_engines:
        engine_type = engine.__class__
        version = getattr(engine, "policy_version", None)
        if isinstance(engine, AllowAllPolicyEngine):
            version = "allow-all-v1"
        else:
            version = _authority_version(
                version,
                code="CAPABILITY_POLICY_VERSION_REQUIRED",
                authority=f"Policy engine '{name}' policy_version",
            )
        engines.append(
            {
                "name": name,
                "type": f"{engine_type.__module__}.{engine_type.__qualname__}",
                "version": version,
            }
        )
    approver = permission_controller.approver
    approval_version = None
    if approver is not None:
        approval_version = _authority_version(
            getattr(approver, "approval_version", None),
            code="CAPABILITY_APPROVAL_VERSION_REQUIRED",
            authority="Permission approver approval_version",
        )
    permission = {
        "mode": permission_controller.current_mode().value,
        "require_approver": permission_controller.require_approver,
        "approver_type": (
            None
            if approver is None
            else f"{approver.__class__.__module__}.{approver.__class__.__qualname__}"
        ),
        "approval_version": approval_version,
        "preapproved_tools": sorted(permission_controller._approved_tools),
        "preapproved_categories": sorted(permission_controller._approved_categories),
    }
    return capability_digest({"engines": engines, "permission": permission})


def capability_policy_record(
    *,
    checkpoint: str,
    decision: PolicyDecision,
    spec: CapabilitySpec,
    arguments: dict[str, Any],
    admission: AdmissionEnvelope,
    policy_engines: Sequence[tuple[str, Any]],
    permission_controller: PermissionController,
) -> dict[str, Any]:
    """Build content-minimized durable evidence for one policy decision."""
    policy_version = capability_policy_version(
        policy_engines,
        permission_controller,
    )
    input_digest = capability_digest(
        {
            "arguments": arguments,
            "authority_digest": admission.authority_digest,
            "capability_digest": spec.digest,
            "checkpoint": checkpoint,
        }
    )
    constraint_digest = capability_digest(decision.constraints) if decision.constraints else None
    return {
        "decision_id": capability_digest(
            [
                checkpoint,
                decision.action.value,
                decision.reason_code,
                policy_version,
                input_digest,
                constraint_digest,
            ]
        ),
        "checkpoint": checkpoint,
        "action": decision.action.value,
        "reason_code": decision.reason_code,
        "policy_version": policy_version,
        "input_digest": input_digest,
        "principal_digest": admission.authority_digest,
        "constraint_digest": constraint_digest,
        "redaction_count": len(decision.redactions),
    }


def _apply_redactions(value: Any, redactions: tuple[RedactionRule, ...]) -> Any:
    if isinstance(value, str):
        updated = value
        for rule in redactions:
            if rule.target:
                updated = updated.replace(rule.target, rule.replacement)
        return updated
    if isinstance(value, list):
        return [_apply_redactions(item, redactions) for item in value]
    if isinstance(value, tuple):
        return tuple(_apply_redactions(item, redactions) for item in value)
    if isinstance(value, dict):
        return {key: _apply_redactions(item, redactions) for key, item in value.items()}
    return value


def apply_capability_policy_constraints(
    decision: PolicyDecision,
    *,
    arguments: dict[str, Any],
    idempotency_key: str | None,
    timeout_seconds: float | None,
) -> tuple[dict[str, Any], str | None, float | None]:
    """Apply the small, fail-closed constraint grammar before operation intent."""
    updated_arguments = dict(arguments)
    if decision.action is PolicyAction.ALLOW_WITH_REDACTION:
        updated_arguments = _apply_redactions(updated_arguments, decision.redactions)
    if decision.action is not PolicyAction.ALLOW_WITH_CONSTRAINTS:
        return updated_arguments, idempotency_key, timeout_seconds
    constraints = dict(decision.constraints)
    supported = {
        "arguments",
        "max_timeout_seconds",
        "require_idempotency_key",
    }
    unsupported = tuple(sorted(set(constraints) - supported))
    if unsupported:
        raise HarnessError(
            code="CAPABILITY_POLICY_CONSTRAINT_UNSUPPORTED",
            category="policy",
            message="Capability policy returned unsupported constraints.",
            retryable=False,
            details={"unsupported_constraints": unsupported},
        )
    forced_arguments = constraints.get("arguments")
    if forced_arguments is not None:
        if not isinstance(forced_arguments, dict):
            raise HarnessError(
                code="CAPABILITY_POLICY_CONSTRAINT_INVALID",
                category="policy",
                message="The capability arguments constraint must be an object.",
                retryable=False,
            )
        updated_arguments.update(forced_arguments)
    maximum = constraints.get("max_timeout_seconds")
    if maximum is not None:
        if isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or maximum <= 0:
            raise HarnessError(
                code="CAPABILITY_POLICY_CONSTRAINT_INVALID",
                category="policy",
                message="max_timeout_seconds must be a positive number.",
                retryable=False,
            )
        timeout_seconds = (
            float(maximum) if timeout_seconds is None else min(timeout_seconds, float(maximum))
        )
    required_key = constraints.get("require_idempotency_key")
    if required_key is not None and not isinstance(required_key, bool):
        raise HarnessError(
            code="CAPABILITY_POLICY_CONSTRAINT_INVALID",
            category="policy",
            message="require_idempotency_key must be a boolean.",
            retryable=False,
        )
    if required_key and not idempotency_key:
        raise HarnessError(
            code="CAPABILITY_POLICY_IDEMPOTENCY_REQUIRED",
            category="policy",
            message="Policy requires an idempotency key for this capability.",
            retryable=False,
        )
    return updated_arguments, idempotency_key, timeout_seconds


async def apply_capability_result_policy(
    *,
    spec: CapabilitySpec,
    run_id: str,
    arguments: dict[str, Any],
    context: Any,
    result: Any,
    run_policy: Callable[..., Awaitable[PolicyDecision]],
    enforce_policy: Callable[..., Awaitable[None]],
) -> Any:
    """Apply post-call policy before a result can enter durable settlement."""
    decision = await run_policy(
        method_name="after_tool_call",
        payload=ToolCallResult(
            run_id=run_id,
            tool_name=spec.name,
            arguments=dict(arguments),
            output=result,
            metadata={
                "capability_digest": spec.digest,
                "capability_kind": spec.kind.value,
                "effect_class": spec.effect_class.value,
            },
        ),
        run_input=None,
        context=context,
    )
    await enforce_policy(
        decision=decision,
        checkpoint="after_tool_call",
        run_id=run_id,
        context=context,
    )
    if decision.action is PolicyAction.ALLOW_WITH_REDACTION:
        return _apply_redactions(result, decision.redactions)
    if decision.action is PolicyAction.ALLOW_WITH_CONSTRAINTS and decision.constraints:
        raise HarnessError(
            code="CAPABILITY_RESULT_CONSTRAINT_UNSUPPORTED",
            category="policy",
            message="Result constraints require an explicit output adapter.",
            retryable=False,
        )
    return result


def capability_lease_evidence(claim: RunLeaseClaim) -> dict[str, Any]:
    """Bind operation evidence to opaque run/session ownership without raw tokens."""
    return {
        "claim_digest": capability_digest(
            [claim.claim_id, claim.run.lease_token, claim.session.lease_token]
        ),
        "run_fence_token": claim.run.fence_token,
        "session_fence_token": claim.session.fence_token,
    }


__all__ = [
    "apply_capability_policy_constraints",
    "apply_capability_result_policy",
    "capability_digest",
    "capability_lease_evidence",
    "capability_permission_category",
    "capability_policy_record",
    "capability_policy_version",
    "enforce_capability_scopes",
]
