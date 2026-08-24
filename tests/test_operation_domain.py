"""Pure contracts for effect-aware operation dispatch and crash recovery."""

from __future__ import annotations

import pytest

from agnoclaw.capabilities import (
    CapabilityConcurrency,
    CapabilityKind,
    CapabilityLifetime,
    CapabilityRecovery,
    CapabilitySpec,
    CapabilityTrust,
)
from agnoclaw.runtime.operations import (
    EffectClass,
    OperationFenceError,
    OperationIntent,
    OperationKind,
    OperationRecord,
    OperationResultSlotMismatchError,
    OperationSettlement,
    OperationState,
    OperationTransitionError,
    RecoveryAction,
    begin_operation_dispatch,
    operation_result_slot_id,
    recovery_action,
    reset_operation_for_recovery,
    settle_operation,
)


def _intent(effect: EffectClass, *, key: str | None = None) -> OperationIntent:
    return OperationIntent(
        operation_id=f"operation-{effect.value}",
        run_id="run-1",
        attempt_id="attempt-1",
        kind=OperationKind.CAPABILITY,
        target="example.tool",
        request_digest="sha256:request",
        effect_class=effect,
        idempotency_key=key,
        metadata={"classification": ["internal"]},
    )


def test_intent_is_deep_frozen_and_digest_is_deterministic():
    metadata = {"classification": ["internal"]}
    first = _intent(EffectClass.READ_ONLY)
    second = OperationIntent(**first.to_dict())
    mutable = OperationIntent(
        operation_id="operation-mutable",
        run_id="run-1",
        attempt_id="attempt-1",
        kind=OperationKind.MODEL,
        target="provider.model",
        request_digest="sha256:request",
        effect_class=EffectClass.READ_ONLY,
        metadata=metadata,
    )
    metadata["classification"].append("secret")

    assert first.digest == second.digest
    assert mutable.to_dict()["metadata"] == {"classification": ["internal"]}


def test_intent_preprovisions_a_canonical_result_identity_and_rejects_tampering():
    intent = _intent(EffectClass.READ_ONLY)
    serialized = intent.to_dict()

    assert intent.result_slot_id == operation_result_slot_id(intent.operation_id)
    assert OperationIntent.from_dict(serialized) == intent
    legacy = dict(serialized)
    legacy.pop("result_slot_id")
    assert OperationIntent.from_dict(legacy).result_slot_id == intent.result_slot_id
    with pytest.raises(ValueError, match="result_slot_id"):
        OperationIntent(**{**serialized, "result_slot_id": "operation-result:v1:wrong"})


def test_idempotent_intent_requires_a_real_key():
    with pytest.raises(ValueError, match="idempotency_key"):
        _intent(EffectClass.IDEMPOTENT)


@pytest.mark.parametrize(
    ("effect", "key", "expected"),
    [
        (EffectClass.READ_ONLY, None, RecoveryAction.RETRY),
        (EffectClass.IDEMPOTENT, "provider-key", RecoveryAction.RETRY),
        (EffectClass.COMPENSATABLE, None, RecoveryAction.RECONCILE),
        (EffectClass.NON_REPEATABLE, None, RecoveryAction.RECONCILE),
    ],
)
def test_interrupted_dispatch_has_effect_aware_recovery(effect, key, expected):
    dispatching = begin_operation_dispatch(
        OperationRecord(intent=_intent(effect, key=key)),
        worker_id="worker-1",
        fence_token=1,
    )

    assert recovery_action(dispatching) is expected


def test_planned_and_terminal_recovery_are_not_ambiguous():
    planned = OperationRecord(intent=_intent(EffectClass.READ_ONLY))
    dispatching = begin_operation_dispatch(
        planned,
        worker_id="worker-1",
        fence_token=1,
    )
    completed = settle_operation(
        dispatching,
        OperationSettlement(
            state=OperationState.SUCCEEDED,
            result_reference="artifact:sha256:result",
        ),
        fence_token=1,
    )

    assert recovery_action(planned) is RecoveryAction.DISPATCH
    assert recovery_action(completed) is RecoveryAction.DO_NOTHING


def test_fence_tokens_are_monotonic_and_exact_at_settlement():
    planned = OperationRecord(intent=_intent(EffectClass.READ_ONLY))
    with pytest.raises(OperationFenceError):
        begin_operation_dispatch(planned, worker_id="worker-1", fence_token=0)
    dispatching = begin_operation_dispatch(
        planned,
        worker_id="worker-1",
        fence_token=2,
    )
    with pytest.raises(OperationFenceError):
        settle_operation(
            dispatching,
            OperationSettlement(state=OperationState.FAILED, safe_error={"code": "x"}),
            fence_token=1,
        )


def test_safe_recovery_advances_fence_before_redispatch():
    dispatching = begin_operation_dispatch(
        OperationRecord(intent=_intent(EffectClass.IDEMPOTENT, key="provider-key")),
        worker_id="worker-1",
        fence_token=4,
    )

    planned = reset_operation_for_recovery(dispatching, next_fence_token=5)
    reclaimed = begin_operation_dispatch(
        planned,
        worker_id="worker-2",
        fence_token=6,
    )

    assert planned.state is OperationState.PLANNED
    assert reclaimed.worker_id == "worker-2"
    assert reclaimed.dispatch_attempt == 2
    assert reclaimed.fence_token == 6


def test_non_repeatable_dispatch_cannot_be_reset_for_retry():
    dispatching = begin_operation_dispatch(
        OperationRecord(intent=_intent(EffectClass.NON_REPEATABLE)),
        worker_id="worker-1",
        fence_token=1,
    )

    with pytest.raises(OperationTransitionError):
        reset_operation_for_recovery(dispatching, next_fence_token=2)


def test_terminal_operation_is_immutable():
    dispatching = begin_operation_dispatch(
        OperationRecord(intent=_intent(EffectClass.READ_ONLY)),
        worker_id="worker-1",
        fence_token=1,
    )
    completed = settle_operation(
        dispatching,
        OperationSettlement(
            state=OperationState.SUCCEEDED,
            result_reference="artifact:sha256:result",
        ),
        fence_token=1,
    )

    with pytest.raises(OperationTransitionError):
        settle_operation(
            completed,
            OperationSettlement(state=OperationState.FAILED, safe_error={"code": "late"}),
            fence_token=1,
        )


def test_success_fulfills_exact_preprovisioned_result_identity():
    intent = _intent(EffectClass.READ_ONLY)
    dispatching = begin_operation_dispatch(
        OperationRecord(intent=intent),
        worker_id="worker-1",
        fence_token=1,
    )
    completed = settle_operation(
        dispatching,
        OperationSettlement(
            state=OperationState.SUCCEEDED,
            result_reference="artifact:sha256:result",
        ),
        fence_token=1,
    )

    assert completed.settlement is not None
    assert completed.settlement.result_slot_id == intent.result_slot_id
    with pytest.raises(OperationResultSlotMismatchError) as error:
        settle_operation(
            dispatching,
            OperationSettlement(
                state=OperationState.SUCCEEDED,
                result_reference="artifact:sha256:result",
                result_slot_id="operation-result:v1:" + "0" * 64,
            ),
            fence_token=1,
        )
    assert error.value.code == "OPERATION_RESULT_SLOT_MISMATCH"


def test_planned_operation_can_only_settle_as_pre_dispatch_cancel():
    planned = OperationRecord(intent=_intent(EffectClass.READ_ONLY))
    cancelled = settle_operation(
        planned,
        OperationSettlement(
            state=OperationState.CANCELLED,
            safe_error={"code": "OPERATION_CANCELLED_BEFORE_DISPATCH"},
        ),
        fence_token=0,
    )

    assert cancelled.state is OperationState.CANCELLED
    with pytest.raises(OperationTransitionError):
        settle_operation(
            planned,
            OperationSettlement(state=OperationState.SUCCEEDED),
            fence_token=0,
        )


def _capability(**overrides):
    values = {
        "name": "payments.lookup",
        "version": "1.0.0",
        "kind": CapabilityKind.TOOL,
        "effect_class": EffectClass.READ_ONLY,
        "trust": CapabilityTrust.VERIFIED,
        "lifetime": CapabilityLifetime.RUN,
        "concurrency": CapabilityConcurrency.ISOLATED,
        "recovery": CapabilityRecovery.RECREATABLE,
        "implementation_digest": "sha256:implementation",
        "input_schema": {"type": "object", "required": ["payment_id"]},
        "required_scopes": ("payments:read",),
        "factory": lambda: object(),
    }
    values.update(overrides)
    return CapabilitySpec(**values)


def test_capability_manifest_excludes_live_factory_and_is_stable():
    first = _capability()
    second = _capability(factory=lambda: "different live object")

    assert first.manifest() == second.manifest()
    assert first.digest == second.digest
    assert "factory" not in first.manifest()


def test_capability_schema_and_scope_are_canonical_and_frozen():
    schema = {"required": ["payment_id"]}
    spec = _capability(
        input_schema=schema,
        required_scopes=("payments:read", "payments:read", "tenant:read"),
    )
    schema["required"].append("secret")

    assert spec.manifest()["input_schema"] == {"required": ["payment_id"]}
    assert spec.required_scopes == ("payments:read", "tenant:read")


def test_idempotent_capability_must_accept_key():
    with pytest.raises(ValueError, match="supports_idempotency_key"):
        _capability(effect_class=EffectClass.IDEMPOTENT)


@pytest.mark.parametrize("profile", ["durable", "service"])
def test_opaque_or_live_only_capability_fails_durable_profiles(profile):
    with pytest.raises(Exception) as opaque:
        _capability(trust=CapabilityTrust.OPAQUE_LEGACY).require_profile(profile)
    assert opaque.value.code == "CAPABILITY_NOT_DURABLE"

    with pytest.raises(Exception) as live_only:
        _capability(recovery=CapabilityRecovery.LIVE_ONLY).require_profile(profile)
    assert live_only.value.code == "CAPABILITY_NOT_DURABLE"


def test_non_repeatable_capability_needs_reconciliation_in_durable_profile():
    with pytest.raises(Exception) as unsafe:
        _capability(
            effect_class=EffectClass.NON_REPEATABLE,
            recovery=CapabilityRecovery.RECREATABLE,
        ).require_profile("durable")
    assert unsafe.value.code == "CAPABILITY_EFFECT_UNRECOVERABLE"

    _capability(
        effect_class=EffectClass.NON_REPEATABLE,
        recovery=CapabilityRecovery.RECONCILABLE,
    ).require_profile("durable")


def test_missing_factory_has_typed_failure():
    with pytest.raises(Exception) as missing:
        _capability(factory=None).materialize()
    assert missing.value.code == "CAPABILITY_FACTORY_MISSING"
