"""One immutable capability descriptor for tools and other executable surfaces."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .capability_schema import validate_capability_input_schema
from .runtime.errors import HarnessError
from .runtime.operations import EffectClass, OperationIntent, OperationKind
from .runtime.security import freeze_data, thaw_data

_NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:-]{0,127}$")
_SEARCH_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9]+")


class CapabilityKind(StrEnum):
    TOOL = "tool"
    MODEL = "model"
    CONTEXT_PROVIDER = "context_provider"
    MCP_TOOL = "mcp_tool"
    SKILL_COMMAND = "skill_command"
    ELEVATED_COMMAND = "elevated_command"
    CHILD_RUN = "child_run"
    SCHEDULED_JOB = "scheduled_job"


class CapabilityTrust(StrEnum):
    BUILTIN = "builtin"
    VERIFIED = "verified"
    HOST_MANAGED = "host_managed"
    OPAQUE_LEGACY = "opaque_legacy"


class CapabilityLifetime(StrEnum):
    RUN = "run"
    SESSION = "session"
    PROCESS_POOL = "process_pool"


class CapabilityConcurrency(StrEnum):
    ISOLATED = "isolated"
    IMMUTABLE_SHARED = "immutable_shared"
    HOST_MANAGED_SHARED = "host_managed_shared"
    SERIALIZED = "serialized"


class CapabilityRecovery(StrEnum):
    RECREATABLE = "recreatable"
    RECONCILABLE = "reconcilable"
    COMPENSATABLE = "compensatable"
    LIVE_ONLY = "live_only"


@dataclass(frozen=True)
class CapabilitySpec:
    """Descriptor plus optional factory; only the descriptor enters persistence."""

    name: str
    version: str
    kind: CapabilityKind
    effect_class: EffectClass
    trust: CapabilityTrust
    lifetime: CapabilityLifetime
    concurrency: CapabilityConcurrency
    recovery: CapabilityRecovery
    implementation_digest: str
    description: str = ""
    tags: tuple[str, ...] = ()
    input_schema: Any = field(default_factory=dict)
    required_scopes: tuple[str, ...] = ()
    supports_idempotency_key: bool = False
    factory: Callable[[], Any] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not _NAME_PATTERN.fullmatch(self.name):
            raise ValueError("capability name has an invalid format")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("capability version must be a non-empty string")
        object.__setattr__(self, "kind", CapabilityKind(self.kind))
        object.__setattr__(self, "effect_class", EffectClass(self.effect_class))
        object.__setattr__(self, "trust", CapabilityTrust(self.trust))
        object.__setattr__(self, "lifetime", CapabilityLifetime(self.lifetime))
        object.__setattr__(self, "concurrency", CapabilityConcurrency(self.concurrency))
        object.__setattr__(self, "recovery", CapabilityRecovery(self.recovery))
        if not self.implementation_digest.startswith("sha256:"):
            raise ValueError("implementation_digest must be a sha256: digest")
        if not isinstance(self.description, str) or len(self.description) > 1024:
            raise ValueError("capability description must be a string up to 1024 characters")
        tags = tuple(sorted(set(self.tags)))
        if any(not isinstance(tag, str) or not tag.strip() or len(tag) > 64 for tag in tags):
            raise ValueError("capability tags must be non-empty strings up to 64 characters")
        object.__setattr__(self, "tags", tags)
        scopes = tuple(sorted(set(self.required_scopes)))
        if any(not isinstance(scope, str) or not scope.strip() for scope in scopes):
            raise ValueError("required_scopes must contain non-empty strings")
        object.__setattr__(self, "required_scopes", scopes)
        validate_capability_input_schema(
            self.input_schema,
            capability=self.name,
        )
        frozen_schema = freeze_data(self.input_schema)
        object.__setattr__(self, "input_schema", frozen_schema)
        if self.effect_class is EffectClass.IDEMPOTENT and not self.supports_idempotency_key:
            raise ValueError("idempotent capability must declare supports_idempotency_key=True")
        if self.factory is not None and not callable(self.factory):
            raise ValueError("capability factory must be callable")

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "kind": self.kind.value,
            "effect_class": self.effect_class.value,
            "trust": self.trust.value,
            "lifetime": self.lifetime.value,
            "concurrency": self.concurrency.value,
            "recovery": self.recovery.value,
            "implementation_digest": self.implementation_digest,
            "description": self.description,
            "tags": list(self.tags),
            "input_schema": thaw_data(self.input_schema),
            "required_scopes": list(self.required_scopes),
            "supports_idempotency_key": self.supports_idempotency_key,
        }

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            self.manifest(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    def require_profile(self, profile: str) -> None:
        if profile not in {"quick", "durable", "service", "legacy"}:
            raise ValueError(f"unknown runtime profile '{profile}'")
        if profile in {"durable", "service"} and (
            self.trust is CapabilityTrust.OPAQUE_LEGACY
            or self.recovery is CapabilityRecovery.LIVE_ONLY
        ):
            raise HarnessError(
                code="CAPABILITY_NOT_DURABLE",
                category="capability",
                message=(f"Capability '{self.name}' lacks classified durable recovery."),
                retryable=False,
                details={
                    "capability": self.name,
                    "profile": profile,
                    "trust": self.trust.value,
                    "recovery": self.recovery.value,
                },
            )
        if (
            profile in {"durable", "service"}
            and self.effect_class is EffectClass.NON_REPEATABLE
            and self.recovery is not CapabilityRecovery.RECONCILABLE
        ):
            raise HarnessError(
                code="CAPABILITY_EFFECT_UNRECOVERABLE",
                category="capability",
                message=(f"Non-repeatable capability '{self.name}' needs reconciliation."),
                retryable=False,
                details={"capability": self.name, "profile": profile},
            )

    def materialize(self) -> Any:
        if self.factory is None:
            raise HarnessError(
                code="CAPABILITY_FACTORY_MISSING",
                category="capability",
                message=f"Capability '{self.name}' has no executable factory.",
                retryable=False,
                details={"capability": self.name},
            )
        return self.factory()

    def operation_intent(
        self,
        *,
        operation_id: str,
        run_id: str,
        attempt_id: str,
        request_digest: str,
        profile: str,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OperationIntent:
        """Bind this descriptor to one persisted operation without live objects."""
        self.require_profile(profile)
        supplied_metadata = dict(metadata or {})
        reserved = tuple(sorted({"capability_digest", "profile"}.intersection(supplied_metadata)))
        if reserved:
            raise HarnessError(
                code="CAPABILITY_METADATA_RESERVED",
                category="capability",
                message="Capability operation metadata contains reserved fields.",
                retryable=False,
                details={"reserved_fields": reserved},
            )
        if idempotency_key is not None and not self.supports_idempotency_key:
            raise HarnessError(
                code="CAPABILITY_IDEMPOTENCY_UNSUPPORTED",
                category="capability",
                message=f"Capability '{self.name}' does not accept idempotency keys.",
                retryable=False,
                details={"capability": self.name},
            )
        if self.effect_class is EffectClass.IDEMPOTENT and idempotency_key is None:
            raise HarnessError(
                code="CAPABILITY_IDEMPOTENCY_KEY_REQUIRED",
                category="capability",
                message=f"Idempotent capability '{self.name}' requires a key.",
                retryable=False,
                details={"capability": self.name},
            )
        return OperationIntent(
            operation_id=operation_id,
            run_id=run_id,
            attempt_id=attempt_id,
            kind=(
                OperationKind.MODEL
                if self.kind is CapabilityKind.MODEL
                else OperationKind.CAPABILITY
            ),
            target=f"{self.name}@{self.version}",
            request_digest=request_digest,
            effect_class=self.effect_class,
            idempotency_key=idempotency_key,
            timeout_seconds=timeout_seconds,
            metadata={
                **supplied_metadata,
                "capability_digest": self.digest,
                "profile": profile,
            },
        )


@dataclass(frozen=True)
class CapabilityCatalogEntry:
    name: str
    version: str
    kind: CapabilityKind
    effect_class: EffectClass
    trust: CapabilityTrust
    description: str
    tags: tuple[str, ...]
    digest: str

    @classmethod
    def from_spec(cls, spec: CapabilitySpec) -> CapabilityCatalogEntry:
        return cls(
            name=spec.name,
            version=spec.version,
            kind=spec.kind,
            effect_class=spec.effect_class,
            trust=spec.trust,
            description=spec.description,
            tags=spec.tags,
            digest=spec.digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "kind": self.kind.value,
            "effect_class": self.effect_class.value,
            "trust": self.trust.value,
            "description": self.description,
            "tags": list(self.tags),
            "digest": self.digest,
        }


@dataclass(frozen=True)
class CapabilitySelection:
    specs: tuple[CapabilitySpec, ...]
    schema_bytes: int
    digest: str

    def manifest(self) -> list[dict[str, Any]]:
        return [spec.manifest() for spec in self.specs]


class CapabilityRegistry:
    """Copy-on-write registry with bounded discovery and explicit selection."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._specs: dict[tuple[str, str], CapabilitySpec] = {}
        self._defaults: dict[str, str] = {}

    def register(self, spec: CapabilitySpec, *, default: bool | None = None) -> bool:
        if not isinstance(spec, CapabilitySpec):
            raise TypeError("registry entries must be CapabilitySpec instances")
        key = (spec.name, spec.version)
        with self._lock:
            existing = self._specs.get(key)
            if existing is not None:
                if existing.digest == spec.digest:
                    return False
                raise HarnessError(
                    code="CAPABILITY_VERSION_CONFLICT",
                    category="capability",
                    message=(
                        f"Capability '{spec.name}@{spec.version}' was reused with a "
                        "different immutable manifest."
                    ),
                    retryable=False,
                    details={"capability": spec.name, "version": spec.version},
                )
            updated = dict(self._specs)
            updated[key] = spec
            defaults = dict(self._defaults)
            if default is True or spec.name not in defaults:
                defaults[spec.name] = spec.version
            self._specs = updated
            self._defaults = defaults
        return True

    @staticmethod
    def _split_reference(reference: str) -> tuple[str, str | None]:
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("capability reference must be a non-empty string")
        value = reference.strip()
        if "@" not in value:
            return value, None
        name, version = value.rsplit("@", 1)
        if not name or not version:
            raise ValueError("capability reference must use name@version")
        return name, version

    def resolve(
        self,
        reference: str,
        *,
        version: str | None = None,
    ) -> CapabilitySpec:
        name, embedded_version = self._split_reference(reference)
        if version is not None and embedded_version is not None:
            raise ValueError("capability version was supplied twice")
        selected_version = version or embedded_version or self._defaults.get(name)
        if selected_version is None:
            raise HarnessError(
                code="CAPABILITY_NOT_FOUND",
                category="capability",
                message=f"Capability '{name}' is not registered.",
                retryable=False,
                details={"capability": name},
            )
        spec = self._specs.get((name, selected_version))
        if spec is None:
            raise HarnessError(
                code="CAPABILITY_NOT_FOUND",
                category="capability",
                message=f"Capability '{name}@{selected_version}' is not registered.",
                retryable=False,
                details={"capability": name, "version": selected_version},
            )
        return spec

    def catalog(
        self,
        *,
        granted_scopes: tuple[str, ...] | set[str] = (),
        kinds: tuple[CapabilityKind, ...] | None = None,
    ) -> tuple[CapabilityCatalogEntry, ...]:
        granted = frozenset(granted_scopes)
        allowed_kinds = frozenset(kinds) if kinds is not None else None
        entries = (
            CapabilityCatalogEntry.from_spec(spec)
            for spec in self._specs.values()
            if set(spec.required_scopes).issubset(granted)
            and (allowed_kinds is None or spec.kind in allowed_kinds)
        )
        return tuple(sorted(entries, key=lambda item: (item.name, item.version)))

    def snapshot(self) -> tuple[CapabilitySpec, ...]:
        """Return one immutable, deterministically ordered registry snapshot."""
        specs = self._specs
        return tuple(specs[key] for key in sorted(specs))

    @staticmethod
    def _search_score(entry: CapabilityCatalogEntry, tokens: tuple[str, ...]) -> int:
        if not tokens:
            return 1
        name = entry.name.lower()
        tags = " ".join(entry.tags).lower()
        description = entry.description.lower()
        score = 0
        for token in tokens:
            if token == name:
                score += 100
            elif name.startswith(token):
                score += 40
            elif token in name:
                score += 20
            if token in tags:
                score += 10
            if token in description:
                score += 3
        return score

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        granted_scopes: tuple[str, ...] | set[str] = (),
        kinds: tuple[CapabilityKind, ...] | None = None,
    ) -> tuple[CapabilityCatalogEntry, ...]:
        if limit <= 0 or limit > 100:
            raise ValueError("capability search limit must be between 1 and 100")
        if not isinstance(query, str):
            raise TypeError("capability search query must be a string")
        tokens = tuple(sorted(set(token.lower() for token in _SEARCH_TOKEN_PATTERN.findall(query))))
        scored = (
            (self._search_score(entry, tokens), entry)
            for entry in self.catalog(granted_scopes=granted_scopes, kinds=kinds)
        )
        matches = (item for item in scored if item[0] > 0)
        ordered = sorted(
            matches,
            key=lambda item: (-item[0], item[1].name, item[1].version),
        )
        return tuple(entry for _score, entry in ordered[:limit])

    def select(
        self,
        references: tuple[str, ...] | list[str],
        *,
        profile: str,
        granted_scopes: tuple[str, ...] | set[str] = (),
        max_capabilities: int = 32,
        max_schema_bytes: int = 65_536,
    ) -> CapabilitySelection:
        if max_capabilities <= 0 or max_schema_bytes <= 0:
            raise ValueError("capability and schema budgets must be positive")
        granted = frozenset(granted_scopes)
        specs: list[CapabilitySpec] = []
        seen: set[tuple[str, str]] = set()
        schema_bytes = 0
        for reference in references:
            spec = self.resolve(reference)
            key = (spec.name, spec.version)
            if key in seen:
                continue
            seen.add(key)
            if len(specs) >= max_capabilities:
                raise HarnessError(
                    code="CAPABILITY_SELECTION_BUDGET_EXCEEDED",
                    category="capability",
                    message="Selected capability count exceeds the configured budget.",
                    retryable=False,
                    details={
                        "count": len(specs) + 1,
                        "maximum": max_capabilities,
                    },
                )
            spec.require_profile(profile)
            missing = tuple(sorted(set(spec.required_scopes) - granted))
            if missing:
                raise HarnessError(
                    code="CAPABILITY_SCOPE_REQUIRED",
                    category="authorization",
                    message=f"Capability '{spec.name}' requires additional scopes.",
                    retryable=False,
                    details={"capability": spec.name, "missing_scopes": missing},
                )
            schema_bytes += len(
                json.dumps(
                    thaw_data(spec.input_schema),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            )
            if schema_bytes > max_schema_bytes:
                raise HarnessError(
                    code="CAPABILITY_SCHEMA_BUDGET_EXCEEDED",
                    category="capability",
                    message="Selected capability schemas exceed the configured budget.",
                    retryable=False,
                    details={
                        "schema_bytes": schema_bytes,
                        "maximum": max_schema_bytes,
                    },
                )
            specs.append(spec)
        manifests = [spec.manifest() for spec in specs]
        canonical = json.dumps(
            manifests,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
        return CapabilitySelection(
            specs=tuple(specs),
            schema_bytes=schema_bytes,
            digest=digest,
        )

    def catalog_prompt(
        self,
        query: str,
        *,
        granted_scopes: tuple[str, ...] | set[str] = (),
        limit: int = 20,
        max_chars: int = 4_000,
    ) -> str:
        if max_chars < 128:
            raise ValueError("catalog prompt budget must be at least 128 characters")
        lines = ["Available capabilities (select explicitly; schemas load on selection):"]
        for entry in self.search(
            query,
            limit=limit,
            granted_scopes=granted_scopes,
        ):
            description = " ".join(entry.description.split())
            line = (
                f"- {entry.name}@{entry.version} [{entry.kind.value}; "
                f"{entry.effect_class.value}] {description}"
            ).rstrip()
            if len("\n".join((*lines, line))) > max_chars:
                break
            lines.append(line)
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self._specs)


__all__ = [
    "CapabilityCatalogEntry",
    "CapabilityConcurrency",
    "CapabilityKind",
    "CapabilityLifetime",
    "CapabilityRecovery",
    "CapabilityRegistry",
    "CapabilitySelection",
    "CapabilitySpec",
    "CapabilityTrust",
    "EffectClass",
]
