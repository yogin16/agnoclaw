"""Fail-closed policy, permission, scope, and result governance contracts."""

from __future__ import annotations

import pytest

from agnoclaw import (
    CapabilityConcurrency,
    CapabilityKind,
    CapabilityLifetime,
    CapabilityRecovery,
    CapabilitySpec,
    CapabilityTrust,
    EffectClass,
)
from agnoclaw.capability_governance import (
    apply_capability_policy_constraints,
    apply_capability_result_policy,
    capability_policy_version,
    enforce_capability_scopes,
)
from agnoclaw.runtime import (
    AdmissionEnvelope,
    HarnessError,
    IdentityAssertion,
    IdentitySource,
    PolicyAction,
    PolicyDecision,
    RedactionRule,
)
from agnoclaw.runtime.permissions import PermissionController
from agnoclaw.runtime.policy import AllowAllPolicyEngine


def _spec(*, scopes: tuple[str, ...] = ()) -> CapabilitySpec:
    return CapabilitySpec(
        name="inventory.lookup",
        version="1.0.0",
        kind=CapabilityKind.TOOL,
        effect_class=EffectClass.READ_ONLY,
        trust=CapabilityTrust.VERIFIED,
        lifetime=CapabilityLifetime.RUN,
        concurrency=CapabilityConcurrency.ISOLATED,
        recovery=CapabilityRecovery.RECREATABLE,
        implementation_digest="sha256:inventory-lookup-v1",
        required_scopes=scopes,
    )


def _admission(*, scopes: tuple[str, ...] = ()) -> AdmissionEnvelope:
    return AdmissionEnvelope.resolve(
        IdentityAssertion(
            source=IdentitySource.TRUSTED_HOST,
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            scopes=scopes,
        )
    )


def test_scope_authority_fails_before_extension_callbacks():
    with pytest.raises(HarnessError) as failure:
        enforce_capability_scopes(
            _spec(scopes=("inventory:read",)),
            _admission(),
        )

    assert failure.value.code == "CAPABILITY_SCOPE_REQUIRED"
    assert failure.value.details == {
        "capability": "inventory.lookup",
        "missing_scopes": ("inventory:read",),
    }


def test_policy_and_approver_versions_are_required_and_digest_bound():
    class Policy:
        policy_version = "policy-1"

    class Approver:
        approval_version = "approval-1"

        def approve(self, _request, _context):
            return True

    controller = PermissionController(mode="default", approver=Approver())
    first = capability_policy_version([("harness", Policy())], controller)

    Policy.policy_version = "policy-2"
    second = capability_policy_version([("harness", Policy())], controller)

    assert first.startswith("sha256:")
    assert first != second

    class UnversionedPolicy:
        pass

    with pytest.raises(HarnessError) as policy:
        capability_policy_version([("harness", UnversionedPolicy())], controller)
    assert policy.value.code == "CAPABILITY_POLICY_VERSION_REQUIRED"

    class UnversionedApprover:
        def approve(self, _request, _context):
            return True

    with pytest.raises(HarnessError) as approver:
        capability_policy_version(
            [("harness", AllowAllPolicyEngine())],
            PermissionController(mode="default", approver=UnversionedApprover()),
        )
    assert approver.value.code == "CAPABILITY_APPROVAL_VERSION_REQUIRED"


def test_constraint_grammar_forces_arguments_and_only_tightens_timeout():
    decision = PolicyDecision(
        action=PolicyAction.ALLOW_WITH_CONSTRAINTS,
        reason_code="BOUND",
        constraints={
            "arguments": {"region": "eu"},
            "max_timeout_seconds": 5,
            "require_idempotency_key": True,
        },
    )

    arguments, key, timeout = apply_capability_policy_constraints(
        decision,
        arguments={"region": "caller", "sku": "one"},
        idempotency_key="provider-key",
        timeout_seconds=30,
    )

    assert arguments == {"region": "eu", "sku": "one"}
    assert key == "provider-key"
    assert timeout == 5


@pytest.mark.parametrize(
    ("constraints", "code"),
    [
        ({"unknown": True}, "CAPABILITY_POLICY_CONSTRAINT_UNSUPPORTED"),
        ({"arguments": []}, "CAPABILITY_POLICY_CONSTRAINT_INVALID"),
        ({"max_timeout_seconds": True}, "CAPABILITY_POLICY_CONSTRAINT_INVALID"),
        (
            {"require_idempotency_key": "yes"},
            "CAPABILITY_POLICY_CONSTRAINT_INVALID",
        ),
        (
            {"require_idempotency_key": True},
            "CAPABILITY_POLICY_IDEMPOTENCY_REQUIRED",
        ),
    ],
)
def test_malformed_or_unsatisfied_constraints_fail_closed(constraints, code):
    with pytest.raises(HarnessError) as failure:
        apply_capability_policy_constraints(
            PolicyDecision(
                action=PolicyAction.ALLOW_WITH_CONSTRAINTS,
                reason_code="TEST",
                constraints=constraints,
            ),
            arguments={},
            idempotency_key=None,
            timeout_seconds=None,
        )

    assert failure.value.code == code


@pytest.mark.asyncio
async def test_result_redaction_runs_before_returning_settleable_value():
    enforced = []

    async def run_policy(**_kwargs):
        return PolicyDecision(
            action=PolicyAction.ALLOW_WITH_REDACTION,
            reason_code="REDACT",
            redactions=(RedactionRule(target="secret", replacement="[MASKED]"),),
        )

    async def enforce_policy(**kwargs):
        enforced.append(kwargs["checkpoint"])

    result = await apply_capability_result_policy(
        spec=_spec(),
        run_id="run-1",
        arguments={},
        context=object(),
        result={"nested": ["secret-value"]},
        run_policy=run_policy,
        enforce_policy=enforce_policy,
    )

    assert result == {"nested": ["[MASKED]-value"]}
    assert enforced == ["after_tool_call"]


@pytest.mark.asyncio
async def test_nonempty_result_constraints_are_not_silently_ignored():
    async def run_policy(**_kwargs):
        return PolicyDecision(
            action=PolicyAction.ALLOW_WITH_CONSTRAINTS,
            reason_code="BOUND_RESULT",
            constraints={"maximum_bytes": 100},
        )

    async def enforce_policy(**_kwargs):
        return None

    with pytest.raises(HarnessError) as failure:
        await apply_capability_result_policy(
            spec=_spec(),
            run_id="run-1",
            arguments={},
            context=object(),
            result="value",
            run_policy=run_policy,
            enforce_policy=enforce_policy,
        )

    assert failure.value.code == "CAPABILITY_RESULT_CONSTRAINT_UNSUPPORTED"
