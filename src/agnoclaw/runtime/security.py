"""Frozen security-foundation contracts for the v0.12 runtime.

These types define trust, classification, policy evidence, key indirection, and safe
diagnostics before the durable schema and isolated run materializer are introduced.
They intentionally do not implement authentication, authorization policy, or crypto.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from .errors import HarnessError
from .policy import PolicyAction


class IdentitySource(StrEnum):
    """Stable trust classes for identity assertions, highest authority first."""

    AUTHENTICATED_CLAIMS = "authenticated_claims"
    TRUSTED_HOST = "trusted_host"
    INTERNAL_PARENT = "internal_parent"
    CALLER_ARGUMENT = "caller_argument"
    REQUEST_PATH_BODY = "request_path_body"

    @property
    def authoritative(self) -> bool:
        return self in {
            IdentitySource.AUTHENTICATED_CLAIMS,
            IdentitySource.TRUSTED_HOST,
            IdentitySource.INTERNAL_PARENT,
        }


_IDENTITY_SOURCE_RANK = {
    IdentitySource.AUTHENTICATED_CLAIMS: 0,
    IdentitySource.TRUSTED_HOST: 1,
    IdentitySource.INTERNAL_PARENT: 2,
    IdentitySource.CALLER_ARGUMENT: 3,
    IdentitySource.REQUEST_PATH_BODY: 4,
}

_IDENTITY_FIELDS = (
    "tenant_id",
    "org_id",
    "team_id",
    "user_id",
    "session_id",
    "workspace_id",
)


def _normalized_strings(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(sorted({value for value in (values or ()) if value}))


def freeze_data(value: Any) -> Any:
    """Deep-freeze JSON-like admission data and reject live/opaque objects."""
    if isinstance(value, float) and not isfinite(value):
        raise TypeError("admission data floats must be finite")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("admission data mapping keys must be strings")
        frozen = {key: freeze_data(item) for key, item in value.items()}
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_data(item) for item in value)
    raise TypeError(
        "admission data must contain only JSON-like scalars, mappings, lists, and tuples"
    )


def thaw_data(value: Any) -> Any:
    """Return mutable JSON-compatible data from :func:`freeze_data` output."""
    if isinstance(value, Mapping):
        return {str(key): thaw_data(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_data(item) for item in value]
    return value


@dataclass(frozen=True)
class IdentityAssertion:
    """Identity supplied by one explicitly classified boundary."""

    source: IdentitySource
    tenant_id: str | None = None
    org_id: str | None = None
    team_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    workspace_id: str | None = None
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in _IDENTITY_FIELDS:
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise HarnessError(
                    code="IDENTITY_VALUE_INVALID",
                    category="authorization",
                    message=f"{field_name} must be a non-empty string when supplied.",
                    retryable=False,
                    details={"field": field_name},
                )
        object.__setattr__(self, "roles", _normalized_strings(self.roles))
        object.__setattr__(self, "scopes", _normalized_strings(self.scopes))
        if not self.source.authoritative and (self.roles or self.scopes):
            raise HarnessError(
                code="IDENTITY_AUTHORITY_REQUIRED",
                category="authorization",
                message="Roles and scopes require authenticated or trusted-host authority.",
                retryable=False,
                details={"source": self.source.value},
            )


@dataclass(frozen=True)
class IdentityProvenance:
    """Winning source for one resolved identity field."""

    field: str
    source: IdentitySource
    authoritative: bool


@dataclass(frozen=True)
class PrincipalIdentity:
    """Canonical identity shared by every consumer after admission."""

    tenant_id: str | None = None
    org_id: str | None = None
    team_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    workspace_id: str | None = None
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()


def _resolve_identity_field(
    field_name: str,
    assertions: tuple[IdentityAssertion, ...],
) -> tuple[str | None, IdentityProvenance | None]:
    supplied = [
        (assertion, getattr(assertion, field_name))
        for assertion in assertions
        if getattr(assertion, field_name) is not None
    ]
    values = {value for _, value in supplied}
    if len(values) > 1:
        raise HarnessError(
            code="IDENTITY_CLAIM_CONFLICT",
            category="authorization",
            message=f"Conflicting identity values were supplied for {field_name}.",
            retryable=False,
            details={"field": field_name},
        )
    if not supplied:
        return None, None
    winner, value = min(supplied, key=lambda item: _IDENTITY_SOURCE_RANK[item[0].source])
    return value, IdentityProvenance(
        field=field_name,
        source=winner.source,
        authoritative=winner.source.authoritative,
    )


def _resolve_authority_set(
    field_name: str,
    assertions: tuple[IdentityAssertion, ...],
) -> tuple[str, ...]:
    supplied = [
        (assertion, getattr(assertion, field_name))
        for assertion in assertions
        if getattr(assertion, field_name)
    ]
    if not supplied:
        return ()
    distinct = {values for _, values in supplied}
    if len(distinct) > 1:
        raise HarnessError(
            code="IDENTITY_CLAIM_CONFLICT",
            category="authorization",
            message=f"Conflicting authority values were supplied for {field_name}.",
            retryable=False,
            details={"field": field_name},
        )
    winner, values = min(supplied, key=lambda item: _IDENTITY_SOURCE_RANK[item[0].source])
    if not winner.source.authoritative:  # guarded by IdentityAssertion; defense in depth
        raise HarnessError(
            code="IDENTITY_AUTHORITY_REQUIRED",
            category="authorization",
            message=f"{field_name} requires a trusted identity source.",
            retryable=False,
            details={"field": field_name},
        )
    return values


def resolve_principal(
    *assertions: IdentityAssertion,
    require_trusted_tenant: bool = False,
    require_user: bool = False,
) -> tuple[PrincipalIdentity, tuple[IdentityProvenance, ...]]:
    """Resolve one canonical identity; any conflicting identity fails closed."""
    normalized = tuple(assertions)
    resolved: dict[str, str | None] = {}
    provenance: list[IdentityProvenance] = []
    for field_name in _IDENTITY_FIELDS:
        value, source = _resolve_identity_field(field_name, normalized)
        resolved[field_name] = value
        if source is not None:
            provenance.append(source)

    by_field = {item.field: item for item in provenance}
    if require_trusted_tenant:
        tenant_source = by_field.get("tenant_id")
        if tenant_source is None or not tenant_source.authoritative:
            raise HarnessError(
                code="TRUSTED_TENANT_REQUIRED",
                category="authorization",
                message="This profile requires tenant identity from a trusted source.",
                retryable=False,
                details={"field": "tenant_id"},
            )
    if require_user and resolved["user_id"] is None:
        raise HarnessError(
            code="USER_ID_REQUIRED",
            category="authorization",
            message="This operation requires a user identity.",
            retryable=False,
            details={"field": "user_id"},
        )

    identity = PrincipalIdentity(
        **resolved,
        roles=_resolve_authority_set("roles", normalized),
        scopes=_resolve_authority_set("scopes", normalized),
    )
    return identity, tuple(provenance)


@dataclass(frozen=True)
class AdmissionEnvelope:
    """Deep-frozen admission result consumed by all future runtime boundaries."""

    identity: PrincipalIdentity
    provenance: tuple[IdentityProvenance, ...]
    request_id: str | None = None
    trace_id: str | None = None
    client_metadata: Mapping[str, Any] = field(default_factory=dict)
    trusted_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_metadata", freeze_data(self.client_metadata))
        object.__setattr__(self, "trusted_metadata", freeze_data(self.trusted_metadata))

    def to_dict(self) -> dict[str, Any]:
        """Serialize the frozen envelope without adding runtime authority."""
        return {
            "identity": {
                "tenant_id": self.identity.tenant_id,
                "org_id": self.identity.org_id,
                "team_id": self.identity.team_id,
                "user_id": self.identity.user_id,
                "session_id": self.identity.session_id,
                "workspace_id": self.identity.workspace_id,
                "roles": list(self.identity.roles),
                "scopes": list(self.identity.scopes),
            },
            "provenance": [
                {
                    "field": item.field,
                    "source": item.source.value,
                    "authoritative": item.authoritative,
                }
                for item in self.provenance
            ],
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "client_metadata": thaw_data(self.client_metadata),
            "trusted_metadata": thaw_data(self.trusted_metadata),
        }

    @property
    def digest(self) -> str:
        """Return a stable content digest without persisting admission values."""
        canonical = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    @property
    def authority_digest(self) -> str:
        """Bind principal authority without request, trace, or client-data churn."""
        payload = self.to_dict()
        authority = {
            "identity": payload["identity"],
            "provenance": sorted(
                payload["provenance"],
                key=lambda item: (item["field"], item["source"]),
            ),
            "trusted_metadata": payload["trusted_metadata"],
        }
        canonical = json.dumps(
            authority,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AdmissionEnvelope:
        """Rehydrate only a previously trusted internal envelope projection."""
        raw_identity = value.get("identity")
        if not isinstance(raw_identity, Mapping):
            raise TypeError("admission identity must be a mapping")
        raw_provenance = value.get("provenance")
        if not isinstance(raw_provenance, (list, tuple)):
            raise TypeError("admission provenance must be a sequence")
        provenance: list[IdentityProvenance] = []
        for item in raw_provenance:
            if not isinstance(item, Mapping):
                raise TypeError("admission provenance entries must be mappings")
            source = IdentitySource(str(item.get("source")))
            provenance.append(
                IdentityProvenance(
                    field=str(item.get("field")),
                    source=source,
                    authoritative=source.authoritative,
                )
            )
        return cls(
            identity=PrincipalIdentity(
                tenant_id=raw_identity.get("tenant_id"),
                org_id=raw_identity.get("org_id"),
                team_id=raw_identity.get("team_id"),
                user_id=raw_identity.get("user_id"),
                session_id=raw_identity.get("session_id"),
                workspace_id=raw_identity.get("workspace_id"),
                roles=_normalized_strings(raw_identity.get("roles")),
                scopes=_normalized_strings(raw_identity.get("scopes")),
            ),
            provenance=tuple(provenance),
            request_id=value.get("request_id"),
            trace_id=value.get("trace_id"),
            client_metadata=value.get("client_metadata") or {},
            trusted_metadata=value.get("trusted_metadata") or {},
        )

    @classmethod
    def resolve(
        cls,
        *assertions: IdentityAssertion,
        request_id: str | None = None,
        trace_id: str | None = None,
        client_metadata: Mapping[str, Any] | None = None,
        trusted_metadata: Mapping[str, Any] | None = None,
        require_trusted_tenant: bool = False,
        require_user: bool = False,
    ) -> AdmissionEnvelope:
        identity, provenance = resolve_principal(
            *assertions,
            require_trusted_tenant=require_trusted_tenant,
            require_user=require_user,
        )
        return cls(
            identity=identity,
            provenance=provenance,
            request_id=request_id,
            trace_id=trace_id,
            client_metadata=client_metadata or {},
            trusted_metadata=trusted_metadata or {},
        )


class DataClassification(StrEnum):
    """Content sensitivity independent of its business/storage type."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"
    CREDENTIAL = "credential"


class ModelAccess(StrEnum):
    ALLOW = "allow"
    REQUIRE_POLICY = "require_policy"
    DENY = "deny"


class PersistenceControl(StrEnum):
    PLAINTEXT_ALLOWED = "plaintext_allowed"
    ENCRYPTION_REQUIRED = "encryption_required"
    REFERENCE_ONLY = "reference_only"


class TelemetryControl(StrEnum):
    CONTENT_ALLOWED = "content_allowed"
    METADATA_ONLY = "metadata_only"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class DataHandlingRule:
    model_access: ModelAccess
    persistence: PersistenceControl
    telemetry: TelemetryControl


_DATA_HANDLING = MappingProxyType(
    {
        DataClassification.PUBLIC: DataHandlingRule(
            ModelAccess.ALLOW,
            PersistenceControl.PLAINTEXT_ALLOWED,
            TelemetryControl.CONTENT_ALLOWED,
        ),
        DataClassification.INTERNAL: DataHandlingRule(
            ModelAccess.ALLOW,
            PersistenceControl.PLAINTEXT_ALLOWED,
            TelemetryControl.METADATA_ONLY,
        ),
        DataClassification.CONFIDENTIAL: DataHandlingRule(
            ModelAccess.REQUIRE_POLICY,
            PersistenceControl.ENCRYPTION_REQUIRED,
            TelemetryControl.METADATA_ONLY,
        ),
        DataClassification.RESTRICTED: DataHandlingRule(
            ModelAccess.DENY,
            PersistenceControl.ENCRYPTION_REQUIRED,
            TelemetryControl.FORBIDDEN,
        ),
        DataClassification.CREDENTIAL: DataHandlingRule(
            ModelAccess.DENY,
            PersistenceControl.REFERENCE_ONLY,
            TelemetryControl.FORBIDDEN,
        ),
    }
)


def data_handling_for(classification: DataClassification) -> DataHandlingRule:
    """Return the non-weakenable default handling rule for a classification."""
    return _DATA_HANDLING[classification]


@dataclass(frozen=True)
class DataLabel:
    """Classification plus scope/retention labels carried with content references."""

    classification: DataClassification
    categories: tuple[str, ...] = ()
    tenant_id: str | None = None
    retention_policy: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "categories", _normalized_strings(self.categories))


@dataclass(frozen=True)
class PolicyDecisionRecord:
    """Safe, immutable evidence for one policy checkpoint decision."""

    decision_id: str
    checkpoint: str
    action: PolicyAction
    reason_code: str
    policy_version: str
    input_digest: str
    principal_digest: str
    constraint_digest: str | None = None
    redaction_count: int = 0


class GrantScope(StrEnum):
    RUN = "run"
    SESSION = "session"


@dataclass(frozen=True)
class AuthorizationGrant:
    """Immutable least-privilege authorization evidence, never a global allowlist."""

    grant_id: str
    scope: GrantScope
    tenant_id: str
    principal_id: str
    session_id: str
    run_id: str | None
    capability_ids: tuple[str, ...]
    capability_digests: tuple[str, ...]
    effect_categories: tuple[str, ...]
    argument_digest: str | None
    policy_version: str
    authority_digest: str
    issuer: str
    expires_at: str
    nonce: str
    issued_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        required = {
            "grant_id": self.grant_id,
            "tenant_id": self.tenant_id,
            "principal_id": self.principal_id,
            "session_id": self.session_id,
            "policy_version": self.policy_version,
            "authority_digest": self.authority_digest,
            "issuer": self.issuer,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
        }
        invalid = next((name for name, value in required.items() if not value), None)
        if invalid is not None:
            raise ValueError(f"authorization grant requires {invalid}")
        if self.schema_version != "1.0":
            raise ValueError(
                f"unsupported authorization grant schema version '{self.schema_version}'"
            )
        object.__setattr__(self, "scope", GrantScope(self.scope))
        if self.scope == GrantScope.RUN and not self.run_id:
            raise ValueError("run-scoped authorization grant requires run_id")
        if self.scope == GrantScope.SESSION and self.run_id is not None:
            raise ValueError("session-scoped authorization grant cannot bind run_id")
        object.__setattr__(self, "capability_ids", _normalized_strings(self.capability_ids))
        object.__setattr__(
            self,
            "capability_digests",
            _normalized_strings(self.capability_digests),
        )
        object.__setattr__(self, "effect_categories", _normalized_strings(self.effect_categories))
        if not self.capability_ids and not self.effect_categories:
            raise ValueError("authorization grant requires a capability or effect category")
        if self.capability_ids and not self.capability_digests:
            raise ValueError("capability-bound authorization grant requires a capability digest")
        for field_name in ("argument_digest", "authority_digest"):
            value = getattr(self, field_name)
            if value is not None and not value.startswith("sha256:"):
                raise ValueError(f"{field_name} must be a sha256: digest")
        if any(not value.startswith("sha256:") for value in self.capability_digests):
            raise ValueError("capability_digests must contain sha256: digests")
        issued = self._parse_timestamp(self.issued_at, field_name="issued_at")
        expires = self._parse_timestamp(self.expires_at, field_name="expires_at")
        if expires <= issued:
            raise ValueError("authorization grant expires_at must be after issued_at")

    @staticmethod
    def _parse_timestamp(value: str, *, field_name: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field_name} must include a UTC offset")
        return parsed.astimezone(UTC)

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    def is_expired(self, *, at: str | None = None) -> bool:
        observed = self._parse_timestamp(
            at or datetime.now(UTC).isoformat(),
            field_name="observed_at",
        )
        return observed >= self._parse_timestamp(
            self.expires_at,
            field_name="expires_at",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "grant_id": self.grant_id,
            "scope": self.scope.value,
            "tenant_id": self.tenant_id,
            "principal_id": self.principal_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "capability_ids": list(self.capability_ids),
            "capability_digests": list(self.capability_digests),
            "effect_categories": list(self.effect_categories),
            "argument_digest": self.argument_digest,
            "policy_version": self.policy_version,
            "authority_digest": self.authority_digest,
            "issuer": self.issuer,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AuthorizationGrant:
        return cls(**value)


class KeyPurpose(StrEnum):
    RUNTIME_CONTENT = "runtime_content"
    ARTIFACT = "artifact"
    LEARNING = "learning"
    AUDIT = "audit"


@dataclass(frozen=True)
class KeyReference:
    """Opaque tenant-bound key reference; never raw key material."""

    key_id: str
    version: str
    tenant_id: str
    purpose: KeyPurpose


@dataclass(frozen=True)
class SealedContent:
    """Serializable encrypted payload returned by a host key provider."""

    key: KeyReference
    algorithm: str
    nonce_b64: str
    ciphertext_b64: str
    aad_digest: str


@runtime_checkable
class KeyProvider(Protocol):
    """Host-owned envelope-encryption/deletion seam; it never exposes raw keys."""

    def seal(
        self,
        plaintext: bytes,
        *,
        tenant_id: str,
        purpose: KeyPurpose,
        aad: bytes,
    ) -> SealedContent | Awaitable[SealedContent]: ...

    def unseal(self, content: SealedContent, *, aad: bytes) -> bytes | Awaitable[bytes]: ...

    def destroy(self, key: KeyReference) -> None | Awaitable[None]: ...


_SAFE_DIAGNOSTIC_KEYS = frozenset(
    {
        "agno_version",
        "attempt_id",
        "checkpoint",
        "exception_type",
        "feature",
        "field",
        "install_extra",
        "operation_id",
        "package",
        "parameter",
        "policy_version",
        "profile",
        "provider",
        "reason",
        "reason_code",
        "run_id",
        "source",
    }
)
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "input",
    "output",
    "password",
    "prompt",
    "secret",
    "token",
)


def sanitize_diagnostic_details(
    details: Mapping[str, Any] | None,
) -> Mapping[str, str | int | float | bool | None]:
    """Allowlist low-risk scalar diagnostics and drop sensitive/structured content."""
    sanitized: dict[str, str | int | float | bool | None] = {}
    for raw_key, value in (details or {}).items():
        key = str(raw_key)
        lowered = key.lower()
        if key not in _SAFE_DIAGNOSTIC_KEYS:
            continue
        if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            sanitized[key] = value
    return MappingProxyType(sanitized)


@dataclass(frozen=True)
class SafeDiagnostic:
    """Persistence/export-safe failure information with no raw exception payload."""

    code: str
    category: str
    safe_message: str
    retryable: bool
    details: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)
    help_actions: tuple[str, ...] = ()
    debug_reference: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", sanitize_diagnostic_details(self.details))
        object.__setattr__(
            self,
            "help_actions",
            tuple(action[:500] for action in self.help_actions if action),
        )

    @classmethod
    def from_error(
        cls,
        error: HarnessError,
        *,
        safe_message: str,
        safe_details: Mapping[str, Any] | None = None,
        help_actions: Sequence[str] = (),
        debug_reference: str | None = None,
    ) -> SafeDiagnostic:
        """Convert a runtime error without copying its possibly sensitive message."""
        return cls(
            code=error.code,
            category=error.category,
            safe_message=safe_message,
            retryable=error.retryable,
            details=safe_details or {},
            help_actions=tuple(help_actions),
            debug_reference=debug_reference,
        )


__all__ = [
    "AdmissionEnvelope",
    "AuthorizationGrant",
    "DataClassification",
    "DataHandlingRule",
    "DataLabel",
    "IdentityAssertion",
    "IdentityProvenance",
    "IdentitySource",
    "GrantScope",
    "KeyProvider",
    "KeyPurpose",
    "KeyReference",
    "ModelAccess",
    "PersistenceControl",
    "PolicyDecisionRecord",
    "PrincipalIdentity",
    "SafeDiagnostic",
    "SealedContent",
    "TelemetryControl",
    "data_handling_for",
    "freeze_data",
    "resolve_principal",
    "sanitize_diagnostic_details",
    "thaw_data",
]
