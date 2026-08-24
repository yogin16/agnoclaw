"""Lazy capability discovery, selection, scope, and prompt-budget contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from agnoclaw.capabilities import (
    CapabilityConcurrency,
    CapabilityKind,
    CapabilityLifetime,
    CapabilityRecovery,
    CapabilityRegistry,
    CapabilitySpec,
    CapabilityTrust,
)
from agnoclaw.runtime import EffectClass


def _spec(
    name: str,
    *,
    version: str = "1.0.0",
    description: str = "Read an inventory record",
    scopes: tuple[str, ...] = (),
    tags: tuple[str, ...] = ("inventory", "read"),
    schema: dict | None = None,
    factory=None,
) -> CapabilitySpec:
    return CapabilitySpec(
        name=name,
        version=version,
        kind=CapabilityKind.TOOL,
        effect_class=EffectClass.READ_ONLY,
        trust=CapabilityTrust.VERIFIED,
        lifetime=CapabilityLifetime.RUN,
        concurrency=CapabilityConcurrency.ISOLATED,
        recovery=CapabilityRecovery.RECREATABLE,
        implementation_digest=f"sha256:{name}:{version}",
        description=description,
        tags=tags,
        input_schema=schema or {"type": "object", "properties": {}},
        required_scopes=scopes,
        factory=factory,
    )


def test_registration_is_idempotent_but_version_reuse_cannot_change_manifest():
    registry = CapabilityRegistry()
    spec = _spec("inventory.lookup")

    assert registry.register(spec)
    assert not registry.register(_spec("inventory.lookup"))
    with pytest.raises(Exception) as conflict:
        registry.register(
            _spec("inventory.lookup", description="A different implementation contract")
        )
    assert conflict.value.code == "CAPABILITY_VERSION_CONFLICT"


def test_explicit_default_version_and_exact_resolution():
    registry = CapabilityRegistry()
    one = _spec("inventory.lookup", version="1.0.0")
    two = _spec("inventory.lookup", version="2.0.0")
    registry.register(one)
    registry.register(two, default=True)

    assert registry.resolve("inventory.lookup") is two
    assert registry.resolve("inventory.lookup@1.0.0") is one
    with pytest.raises(Exception) as missing:
        registry.resolve("inventory.lookup@3.0.0")
    assert missing.value.code == "CAPABILITY_NOT_FOUND"


def test_registry_snapshot_is_stable_and_does_not_expose_mutable_storage():
    registry = CapabilityRegistry()
    second = _spec("zeta.tool", version="2.0.0")
    first = _spec("alpha.tool", version="1.0.0")
    registry.register(second)
    registry.register(first)

    assert registry.snapshot() == (first, second)


def test_search_hides_unauthorized_capabilities_and_never_loads_schema_or_factory():
    registry = CapabilityRegistry()
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return object()

    registry.register(
        _spec(
            "inventory.lookup",
            scopes=("inventory:read",),
            factory=factory,
        )
    )
    registry.register(
        _spec(
            "weather.lookup",
            description="Read a weather forecast",
            tags=("weather", "read"),
        )
    )

    hidden = registry.search("inventory")
    visible = registry.search(
        "inventory",
        granted_scopes={"inventory:read"},
    )

    assert hidden == ()
    assert [entry.name for entry in visible] == ["inventory.lookup"]
    assert calls == 0
    assert "input_schema" not in visible[0].to_dict()


def test_selection_is_scope_checked_deduplicated_and_digest_stable():
    registry = CapabilityRegistry()
    registry.register(_spec("inventory.lookup", scopes=("inventory:read",)))

    first = registry.select(
        ["inventory.lookup", "inventory.lookup@1.0.0"],
        profile="durable",
        granted_scopes={"inventory:read"},
        max_capabilities=1,
    )
    second = registry.select(
        ["inventory.lookup"],
        profile="durable",
        granted_scopes={"inventory:read"},
    )

    assert len(first.specs) == 1
    assert first.digest == second.digest
    with pytest.raises(Exception) as denied:
        registry.select(["inventory.lookup"], profile="durable")
    assert denied.value.code == "CAPABILITY_SCOPE_REQUIRED"


def test_selection_enforces_count_and_canonical_schema_byte_budgets():
    registry = CapabilityRegistry()
    registry.register(_spec("inventory.one"))
    registry.register(
        _spec(
            "inventory.two",
            schema={"type": "object", "description": "x" * 200},
        )
    )

    with pytest.raises(Exception) as count:
        registry.select(
            ["inventory.one", "inventory.two"],
            profile="durable",
            max_capabilities=1,
        )
    assert count.value.code == "CAPABILITY_SELECTION_BUDGET_EXCEEDED"

    with pytest.raises(Exception) as schema:
        registry.select(
            ["inventory.two"],
            profile="durable",
            max_schema_bytes=32,
        )
    assert schema.value.code == "CAPABILITY_SCHEMA_BUDGET_EXCEEDED"


def test_thousand_capabilities_stay_out_of_bounded_catalog_prompt():
    registry = CapabilityRegistry()
    for index in range(1_000):
        registry.register(
            _spec(
                f"catalog.tool{index:04d}",
                description=f"Capability number {index} for bounded catalog discovery",
            )
        )

    prompt = registry.catalog_prompt("catalog capability", limit=20, max_chars=1_500)

    assert len(registry) == 1_000
    assert len(prompt) <= 1_500
    assert prompt.count("\n-") <= 20
    assert "properties" not in prompt


def test_copy_on_write_registration_is_safe_for_concurrent_readers():
    registry = CapabilityRegistry()

    def register(index: int) -> None:
        registry.register(_spec(f"parallel.tool{index:03d}"))
        registry.catalog()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(register, range(100)))

    assert len(registry) == 100
    assert len(registry.catalog()) == 100


def test_selected_factory_remains_lazy_until_explicit_materialization():
    registry = CapabilityRegistry()
    produced: list[str] = []
    spec = _spec(
        "inventory.lookup",
        factory=lambda: produced.append("created") or "tool",
    )
    registry.register(spec)
    selection = registry.select(["inventory.lookup"], profile="durable")

    assert produced == []
    assert selection.specs[0].materialize() == "tool"
    assert produced == ["created"]


def test_capability_operation_intent_binds_manifest_and_profile():
    spec = _spec("inventory.lookup")
    intent = spec.operation_intent(
        operation_id="run-1:tool:1",
        run_id="run-1",
        attempt_id="attempt-1",
        request_digest="sha256:request",
        profile="durable",
    )

    assert intent.target == "inventory.lookup@1.0.0"
    assert intent.metadata["capability_digest"] == spec.digest
    assert intent.metadata["profile"] == "durable"
