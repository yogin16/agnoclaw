"""Agno adapter contracts for immutable governed capabilities."""

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
from agnoclaw.capability_adapters import build_agno_capability_bindings
from agnoclaw.runtime import EffectClass, HarnessError


def _spec(
    name: str,
    *,
    version: str = "1.0.0",
    kind: CapabilityKind = CapabilityKind.TOOL,
    schema=None,
    trust: CapabilityTrust | None = None,
) -> CapabilitySpec:
    child = kind is CapabilityKind.CHILD_RUN
    return CapabilitySpec(
        name=name,
        version=version,
        kind=kind,
        effect_class=EffectClass.IDEMPOTENT if child else EffectClass.READ_ONLY,
        trust=trust or (CapabilityTrust.HOST_MANAGED if child else CapabilityTrust.VERIFIED),
        lifetime=CapabilityLifetime.RUN,
        concurrency=CapabilityConcurrency.ISOLATED,
        recovery=CapabilityRecovery.RECONCILABLE if child else CapabilityRecovery.RECREATABLE,
        implementation_digest=f"sha256:{name}:{version}",
        description="Look up one record",
        input_schema=(
            schema
            if schema is not None
            else {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "maxLength": 60_000},
                    "delegation_id": {"type": "string", "maxLength": 256},
                },
                "required": ["task", "delegation_id"],
                "additionalProperties": False,
            }
            if child
            else {"type": "object"}
        ),
        supports_idempotency_key=child,
    )


@pytest.mark.asyncio
async def test_adapter_pins_reference_and_uses_provider_safe_name():
    calls = []

    async def invoke(reference, arguments):
        calls.append((reference, dict(arguments)))
        return {"ok": True}

    binding = build_agno_capability_bindings(
        [_spec("inventory.lookup")],
        invoke=invoke,
    )[0]

    assert binding.tool_name.startswith("inventory_lookup_")
    assert binding.reference == "inventory.lookup@1.0.0"
    assert binding.function.parameters == {"type": "object"}
    assert await binding.function.entrypoint(sku="one") == {"ok": True}
    assert calls == [("inventory.lookup@1.0.0", {"sku": "one"})]


def test_adapter_exposes_only_model_callable_kinds():
    bindings = build_agno_capability_bindings(
        [
            _spec("model.tool"),
            _spec("context.source", kind=CapabilityKind.CONTEXT_PROVIDER),
            _spec("child.launch", kind=CapabilityKind.CHILD_RUN),
        ],
        invoke=lambda _reference, _arguments: None,  # type: ignore[arg-type]
    )

    assert [binding.spec.name for binding in bindings] == [
        "model.tool",
        "context.source",
        "child.launch",
    ]


def test_adapter_rejects_ambiguous_versions_and_non_object_schema():
    async def invoke(_reference, _arguments):
        return None

    with pytest.raises(HarnessError) as versions:
        build_agno_capability_bindings(
            [_spec("inventory"), _spec("inventory", version="2.0.0")],
            invoke=invoke,
        )
    assert versions.value.code == "CAPABILITY_MODEL_VERSION_AMBIGUOUS"

    with pytest.raises(HarnessError) as schema:
        build_agno_capability_bindings(
            [_spec("inventory", schema={"type": "array"})],
            invoke=invoke,
        )
    assert schema.value.code == "CAPABILITY_INPUT_SCHEMA_INVALID"

    with pytest.raises(HarnessError) as child:
        build_agno_capability_bindings(
            [
                _spec(
                    "unsafe.child",
                    kind=CapabilityKind.CHILD_RUN,
                    trust=CapabilityTrust.VERIFIED,
                )
            ],
            invoke=invoke,
        )
    assert child.value.code == "CHILD_CAPABILITY_DECLARATION_INVALID"
