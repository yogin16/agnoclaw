from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agnoclaw.runtime import HarnessError
from agnoclaw.runtime.spec import (
    FactoryMaterializer,
    LegacySerializedMaterializer,
    MaterializationTrust,
    MaterializerDescriptor,
    ResourceConcurrency,
    ResourceLifetime,
    ResourceRecovery,
    SharedResourceMaterializer,
    classify_resource,
    compile_harness_spec,
    host_managed_resource,
)


def test_compiled_harness_spec_is_deterministic_and_deep_frozen() -> None:
    settings = {"model": "test:model", "limits": {"tools": ["one", "two"]}}
    first = compile_harness_spec(
        harness_name="test",
        agent_id="agent-1",
        profile="quick",
        settings=settings,
    )
    second = compile_harness_spec(
        harness_name="test",
        agent_id="agent-1",
        profile="quick",
        settings={"limits": {"tools": ["one", "two"]}, "model": "test:model"},
    )
    settings["limits"]["tools"].append("three")

    assert first.settings_digest == second.settings_digest
    assert first.settings["limits"]["tools"] == ("one", "two")
    with pytest.raises(TypeError):
        first.settings["model"] = "other:model"
    with pytest.raises(FrozenInstanceError):
        first.profile = "service"


def test_compiled_spec_digest_binds_profile_and_resource_guarantees() -> None:
    settings = {"model": "test:model"}
    quick = compile_harness_spec(
        harness_name="test",
        agent_id="agent-1",
        profile="quick",
        settings=settings,
    )
    durable = compile_harness_spec(
        harness_name="test",
        agent_id="agent-1",
        profile="durable",
        settings=settings,
    )
    resource_bound = compile_harness_spec(
        harness_name="test",
        agent_id="agent-1",
        profile="quick",
        settings=settings,
        materializers=(classify_resource("model", "test:model", profile="quick"),),
    )

    assert quick.schema_version == "0.12a2"
    assert quick.settings_digest != durable.settings_digest
    assert quick.settings_digest != resource_bound.settings_digest


def test_compiled_harness_spec_rejects_inline_secrets() -> None:
    with pytest.raises(HarnessError) as caught:
        compile_harness_spec(
            harness_name="test",
            agent_id=None,
            profile="service",
            settings={"provider": {"api_key": "do-not-store"}},
        )
    assert caught.value.code == "HARNESS_SPEC_SECRET_FORBIDDEN"
    assert "do-not-store" not in str(caught.value)


def test_immutable_values_are_explicitly_shareable() -> None:
    materializer = classify_resource(
        "model",
        "provider:model",
        profile="quick",
    )
    assert isinstance(materializer, SharedResourceMaterializer)
    assert materializer.descriptor.trust == MaterializationTrust.EXPLICIT_IMMUTABLE
    assert materializer.descriptor.concurrency == ResourceConcurrency.IMMUTABLE_SHARED


def test_opaque_legacy_resource_is_serialized_not_copied() -> None:
    resource = object()
    materializer = classify_resource("tool:custom", resource, profile="legacy")
    assert isinstance(materializer, LegacySerializedMaterializer)
    assert materializer.resource is resource
    assert materializer.descriptor.recovery == ResourceRecovery.LIVE_ONLY


@pytest.mark.parametrize("profile", ["durable", "service"])
def test_opaque_resource_fails_safe_profiles(profile: str) -> None:
    with pytest.raises(HarnessError) as caught:
        classify_resource("tool:custom", object(), profile=profile)
    assert caught.value.code == "UNCLASSIFIED_RUNTIME_RESOURCE"
    assert caught.value.details == {"parameter": "tool:custom", "profile": profile}


def test_factory_materializer_requires_isolated_declaration() -> None:
    descriptor = MaterializerDescriptor(
        resource_id="browser",
        resource_type="tests.Browser",
        trust=MaterializationTrust.FACTORY,
        lifetime=ResourceLifetime.RUN,
        concurrency=ResourceConcurrency.ISOLATED,
        recovery=ResourceRecovery.RECREATABLE,
    )
    materializer = FactoryMaterializer(
        descriptor=descriptor,
        factory=lambda context: {"run_id": context.run_id},
    )
    assert materializer.descriptor.lifetime == ResourceLifetime.RUN


def test_host_managed_resource_requires_explicit_recovery_declaration() -> None:
    database = object()
    materializer = host_managed_resource(
        "database",
        database,
        lifetime=ResourceLifetime.PROCESS_POOL,
        recovery=ResourceRecovery.RECONCILABLE,
    )
    assert materializer.resource is database
    assert materializer.descriptor.trust == MaterializationTrust.HOST_MANAGED
    assert materializer.descriptor.concurrency == ResourceConcurrency.HOST_MANAGED_SHARED


def test_materializer_constructor_rejects_mismatched_claims() -> None:
    descriptor = MaterializerDescriptor(
        resource_id="unsafe",
        resource_type="tests.Unsafe",
        trust=MaterializationTrust.LEGACY_SERIALIZED,
        lifetime=ResourceLifetime.RUN,
        concurrency=ResourceConcurrency.IMMUTABLE_SHARED,
        recovery=ResourceRecovery.LIVE_ONLY,
    )
    with pytest.raises(ValueError, match="serialized"):
        LegacySerializedMaterializer(descriptor=descriptor, resource=object())
