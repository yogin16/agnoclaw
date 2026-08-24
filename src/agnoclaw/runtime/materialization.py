"""Profile resolution and reusable run-resource materializer construction."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..config import HarnessConfig, RuntimeProfile
from .errors import HarnessError
from .postgres_store import PostgresRuntimeStore
from .spec import (
    FactoryMaterializer,
    MaterializationTrust,
    MaterializerDescriptor,
    ResourceConcurrency,
    ResourceLifetime,
    ResourceRecovery,
)


def resolve_runtime_profile(
    source_config: HarnessConfig,
    requested: str | RuntimeProfile | None,
) -> tuple[HarnessConfig, RuntimeProfile]:
    """Resolve one profile and reject competing constructor/config authority."""
    if requested is None:
        return source_config, RuntimeProfile(source_config.profile)
    try:
        profile = RuntimeProfile(requested)
    except ValueError:
        raise HarnessError(
            code="RUNTIME_PROFILE_INVALID",
            category="configuration",
            message="profile must be one of: quick, durable, service, or legacy.",
            retryable=False,
            details={"profile": str(requested)},
        ) from None
    if "profile" in source_config.model_fields_set and source_config.profile is not profile:
        raise HarnessError(
            code="RUNTIME_PROFILE_CONFLICT",
            category="configuration",
            message="AgentHarness profile conflicts with config.profile.",
            retryable=False,
            details={
                "argument_profile": profile.value,
                "config_profile": source_config.profile.value,
            },
        )
    return source_config.with_profile(profile), profile


def validate_profile_resources(
    *,
    profile: RuntimeProfile,
    config: HarnessConfig,
    runtime_store: Any,
    artifact_store: Any,
    agno_db: Any,
) -> None:
    """Fail explicit durable/service profiles before workspace or model side effects."""
    if profile in {RuntimeProfile.DURABLE, RuntimeProfile.SERVICE}:
        missing: list[str] = []
        if runtime_store is None:
            missing.append("runtime_store")
        if artifact_store is None:
            missing.append("artifact_store")
        if missing:
            raise HarnessError(
                code="RUNTIME_PROFILE_STORE_REQUIRED",
                category="configuration",
                message=(
                    f"The {profile.value} profile requires explicit "
                    f"{', '.join(missing)} configuration."
                ),
                retryable=False,
                details={"profile": profile.value, "missing": missing},
            )
    if profile is not RuntimeProfile.SERVICE:
        return
    if not isinstance(runtime_store, PostgresRuntimeStore):
        raise HarnessError(
            code="SERVICE_POSTGRES_RUNTIME_STORE_REQUIRED",
            category="configuration",
            message="The service profile requires PostgresRuntimeStore.",
            retryable=False,
            details={"profile": profile.value},
        )
    if agno_db is None and (
        config.storage.backend != "postgres" or not config.storage.postgres_url
    ):
        raise HarnessError(
            code="SERVICE_AGNO_POSTGRES_REQUIRED",
            category="configuration",
            message=(
                "The service profile requires an injected Agno PostgreSQL DB or "
                "storage.backend='postgres' with postgres_url."
            ),
            retryable=False,
            details={"profile": profile.value},
        )


def run_factory_materializer(
    *,
    resource_id: str,
    resource_type: str,
    factory: Callable[[Any], Any],
) -> FactoryMaterializer:
    """Declare one recreatable, isolated, run-lifetime resource factory."""
    return FactoryMaterializer(
        descriptor=MaterializerDescriptor(
            resource_id=resource_id,
            resource_type=resource_type,
            trust=MaterializationTrust.FACTORY,
            lifetime=ResourceLifetime.RUN,
            concurrency=ResourceConcurrency.ISOLATED,
            recovery=ResourceRecovery.RECREATABLE,
        ),
        factory=factory,
    )


def callable_resource_type(factory: Callable[..., Any]) -> str:
    """Return stable type evidence for classes and ``functools.partial`` factories."""
    target = getattr(factory, "func", factory)
    module = getattr(target, "__module__", type(target).__module__)
    qualname = getattr(target, "__qualname__", type(target).__qualname__)
    return f"{module}.{qualname}"


def materialize_factory_value(factory: Callable[[], Any], context: Any) -> Any:
    """Adapt a zero-argument first-party constructor to ResourceMaterializer."""
    del context
    return factory()


__all__ = [
    "callable_resource_type",
    "materialize_factory_value",
    "resolve_runtime_profile",
    "run_factory_materializer",
    "validate_profile_resources",
]
