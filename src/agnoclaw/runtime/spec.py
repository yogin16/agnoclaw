"""Immutable harness intent and explicit live-resource materialization contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from .errors import HarnessError
from .security import AdmissionEnvelope, freeze_data, thaw_data


class ResourceLifetime(StrEnum):
    RUN = "run"
    SESSION = "session"
    PROCESS_POOL = "process_pool"


class ResourceConcurrency(StrEnum):
    ISOLATED = "isolated"
    IMMUTABLE_SHARED = "immutable_shared"
    HOST_MANAGED_SHARED = "host_managed_shared"
    SERIALIZED = "serialized"


class ResourceRecovery(StrEnum):
    RECREATABLE = "recreatable"
    CHECKPOINTABLE = "checkpointable"
    RECONCILABLE = "reconcilable"
    LIVE_ONLY = "live_only"


class MaterializationTrust(StrEnum):
    FACTORY = "factory"
    EXPLICIT_IMMUTABLE = "explicit_immutable"
    HOST_MANAGED = "host_managed"
    LEGACY_SERIALIZED = "legacy_serialized"


@dataclass(frozen=True)
class MaterializerDescriptor:
    """Serializable declaration of a live resource's runtime guarantees."""

    resource_id: str
    resource_type: str
    trust: MaterializationTrust
    lifetime: ResourceLifetime
    concurrency: ResourceConcurrency
    recovery: ResourceRecovery


@dataclass(frozen=True)
class RunMaterializationContext:
    run_id: str
    attempt_id: str
    profile: str
    admission: AdmissionEnvelope


@runtime_checkable
class ResourceMaterializer(Protocol):
    """Construct and release one classified runtime resource."""

    @property
    def descriptor(self) -> MaterializerDescriptor: ...

    def materialize(self, context: RunMaterializationContext) -> Any | Awaitable[Any]: ...

    def release(
        self, resource: Any, context: RunMaterializationContext
    ) -> None | Awaitable[None]: ...


@dataclass(frozen=True)
class FactoryMaterializer:
    """Run/session resource produced by an explicit host or first-party factory."""

    descriptor: MaterializerDescriptor
    factory: Callable[[RunMaterializationContext], Any | Awaitable[Any]] = field(
        repr=False, compare=False
    )
    closer: Callable[[Any, RunMaterializationContext], None | Awaitable[None]] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.descriptor.trust != MaterializationTrust.FACTORY:
            raise ValueError("FactoryMaterializer requires trust=factory")
        if self.descriptor.concurrency != ResourceConcurrency.ISOLATED:
            raise ValueError("factory materializers must declare isolated concurrency")

    def materialize(self, context: RunMaterializationContext) -> Any | Awaitable[Any]:
        return self.factory(context)

    def release(self, resource: Any, context: RunMaterializationContext) -> None | Awaitable[None]:
        if self.closer is None:
            return None
        return self.closer(resource, context)


@dataclass(frozen=True)
class SharedResourceMaterializer:
    """Explicitly immutable or host-managed shared resource."""

    descriptor: MaterializerDescriptor
    resource: Any = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        allowed = {
            MaterializationTrust.EXPLICIT_IMMUTABLE,
            MaterializationTrust.HOST_MANAGED,
        }
        if self.descriptor.trust not in allowed:
            raise ValueError("shared materializer requires explicit immutable/host trust")
        shared = {
            ResourceConcurrency.IMMUTABLE_SHARED,
            ResourceConcurrency.HOST_MANAGED_SHARED,
        }
        if self.descriptor.concurrency not in shared:
            raise ValueError("shared materializer requires an explicit shared concurrency class")

    def materialize(self, context: RunMaterializationContext) -> Any:
        del context
        return self.resource

    def release(self, resource: Any, context: RunMaterializationContext) -> None:
        del resource, context


@dataclass(frozen=True)
class LegacySerializedMaterializer:
    """Compatibility-only opaque resource; the coordinator must serialize its use."""

    descriptor: MaterializerDescriptor
    resource: Any = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.descriptor.trust != MaterializationTrust.LEGACY_SERIALIZED:
            raise ValueError("legacy materializer requires trust=legacy_serialized")
        if self.descriptor.concurrency != ResourceConcurrency.SERIALIZED:
            raise ValueError("legacy materializer requires serialized concurrency")
        if self.descriptor.recovery != ResourceRecovery.LIVE_ONLY:
            raise ValueError("legacy materializer must declare live-only recovery")

    def materialize(self, context: RunMaterializationContext) -> Any:
        del context
        return self.resource

    def release(self, resource: Any, context: RunMaterializationContext) -> None:
        del resource, context


def _resource_type(value: Any) -> str:
    value_type = value.__class__
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _deeply_immutable(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return True
    if isinstance(value, tuple):
        return all(_deeply_immutable(item) for item in value)
    if isinstance(value, frozenset):
        return all(_deeply_immutable(item) for item in value)
    return False


def classify_resource(
    resource_id: str,
    resource: Any,
    *,
    profile: str,
    declared_immutable: bool = False,
) -> ResourceMaterializer:
    """Classify a supplied object without guessing that opaque state is safe."""
    if declared_immutable or _deeply_immutable(resource):
        return SharedResourceMaterializer(
            descriptor=MaterializerDescriptor(
                resource_id=resource_id,
                resource_type=_resource_type(resource),
                trust=MaterializationTrust.EXPLICIT_IMMUTABLE,
                lifetime=ResourceLifetime.PROCESS_POOL,
                concurrency=ResourceConcurrency.IMMUTABLE_SHARED,
                recovery=ResourceRecovery.RECREATABLE,
            ),
            resource=resource,
        )
    if profile in {"durable", "service"}:
        raise HarnessError(
            code="UNCLASSIFIED_RUNTIME_RESOURCE",
            category="configuration",
            message=(
                f"Resource '{resource_id}' is opaque. Supply a run factory or declare a "
                "certified immutable/host-managed resource for this profile."
            ),
            retryable=False,
            details={"parameter": resource_id, "profile": profile},
        )
    return LegacySerializedMaterializer(
        descriptor=MaterializerDescriptor(
            resource_id=resource_id,
            resource_type=_resource_type(resource),
            trust=MaterializationTrust.LEGACY_SERIALIZED,
            lifetime=ResourceLifetime.PROCESS_POOL,
            concurrency=ResourceConcurrency.SERIALIZED,
            recovery=ResourceRecovery.LIVE_ONLY,
        ),
        resource=resource,
    )


def host_managed_resource(
    resource_id: str,
    resource: Any,
    *,
    lifetime: ResourceLifetime,
    recovery: ResourceRecovery,
) -> SharedResourceMaterializer:
    """Declare a shared host service with externally enforced concurrency."""
    return SharedResourceMaterializer(
        descriptor=MaterializerDescriptor(
            resource_id=resource_id,
            resource_type=_resource_type(resource),
            trust=MaterializationTrust.HOST_MANAGED,
            lifetime=lifetime,
            concurrency=ResourceConcurrency.HOST_MANAGED_SHARED,
            recovery=recovery,
        ),
        resource=resource,
    )


_SECRET_SETTING_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "auth_token",
        "client_secret",
        "credential",
        "password",
        "private_key",
        "secret",
    }
)


def _assert_no_inline_secrets(value: Any, *, path: str = "settings") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in _SECRET_SETTING_KEYS and item not in (None, ""):
                raise HarnessError(
                    code="HARNESS_SPEC_SECRET_FORBIDDEN",
                    category="configuration",
                    message=f"Harness spec field '{path}.{key}' must use a secret reference.",
                    retryable=False,
                    details={"parameter": f"{path}.{key}"},
                )
            _assert_no_inline_secrets(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_inline_secrets(item, path=f"{path}[{index}]")


@dataclass(frozen=True)
class _HarnessSpec:
    """Private serializable reusable intent; no live runtime objects."""

    schema_version: str
    harness_name: str
    agent_id: str | None
    profile: str
    settings: Mapping[str, Any]
    settings_digest: str
    resources: tuple[MaterializerDescriptor, ...]

    def __post_init__(self) -> None:
        frozen = freeze_data(self.settings)
        object.__setattr__(self, "settings", frozen)
        object.__setattr__(self, "resources", tuple(self.resources))

    def public_manifest(self) -> HarnessRuntimeManifest:
        return HarnessRuntimeManifest(
            schema_version=self.schema_version,
            harness_name=self.harness_name,
            agent_id=self.agent_id,
            profile=self.profile,
            spec_digest=self.settings_digest,
            resources=self.resources,
        )


@dataclass(frozen=True)
class HarnessRuntimeManifest:
    """Content-minimized public evidence for one compiled harness configuration."""

    schema_version: str
    harness_name: str
    agent_id: str | None
    profile: str
    spec_digest: str
    resources: tuple[MaterializerDescriptor, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "resources", tuple(self.resources))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "harness_name": self.harness_name,
            "agent_id": self.agent_id,
            "profile": self.profile,
            "spec_digest": self.spec_digest,
            "resources": [
                {
                    "resource_id": item.resource_id,
                    "resource_type": item.resource_type,
                    "trust": item.trust.value,
                    "lifetime": item.lifetime.value,
                    "concurrency": item.concurrency.value,
                    "recovery": item.recovery.value,
                }
                for item in self.resources
            ],
        }


def compile_harness_spec(
    *,
    harness_name: str,
    agent_id: str | None,
    profile: str,
    settings: Mapping[str, Any],
    materializers: tuple[ResourceMaterializer, ...] = (),
) -> _HarnessSpec:
    """Compile immutable intent and deterministic resource declarations."""
    _assert_no_inline_secrets(settings)
    frozen = freeze_data(settings)
    descriptors = tuple(item.descriptor for item in materializers)
    schema_version = "0.12a2"
    canonical = json.dumps(
        {
            "schema_version": schema_version,
            "harness_name": harness_name,
            "agent_id": agent_id,
            "profile": profile,
            "settings": thaw_data(frozen),
            "resources": [
                {
                    "resource_id": item.resource_id,
                    "resource_type": item.resource_type,
                    "trust": item.trust.value,
                    "lifetime": item.lifetime.value,
                    "concurrency": item.concurrency.value,
                    "recovery": item.recovery.value,
                }
                for item in descriptors
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    return _HarnessSpec(
        schema_version=schema_version,
        harness_name=harness_name,
        agent_id=agent_id,
        profile=profile,
        settings=frozen,
        settings_digest=digest,
        resources=descriptors,
    )


__all__ = [
    "FactoryMaterializer",
    "HarnessRuntimeManifest",
    "LegacySerializedMaterializer",
    "MaterializationTrust",
    "MaterializerDescriptor",
    "ResourceConcurrency",
    "ResourceLifetime",
    "ResourceMaterializer",
    "ResourceRecovery",
    "RunMaterializationContext",
    "SharedResourceMaterializer",
    "classify_resource",
    "compile_harness_spec",
    "host_managed_resource",
]
