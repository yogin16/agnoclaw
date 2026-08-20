"""Execution context contract for v0.2 harness runs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .security import AdmissionEnvelope, IdentitySource


def _normalize_sequence(values: Iterable[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    return tuple(v for v in values if v)


def _normalize_metadata(values: Mapping[str, Any] | None) -> dict[str, Any]:
    if not values:
        return {}
    return {str(k): v for k, v in values.items()}


@dataclass(frozen=True)
class ExecutionContext:
    """Immutable run context passed across runtime boundaries."""

    user_id: str | None
    session_id: str | None
    workspace_id: str | None
    tenant_id: str | None = None
    org_id: str | None = None
    team_id: str | None = None
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    request_id: str | None = None
    trace_id: str | None = None
    trusted_permission_tools: tuple[str, ...] = ()
    trusted_permission_categories: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    identity_source: IdentitySource = IdentitySource.TRUSTED_HOST
    admission: AdmissionEnvelope | None = None

    def with_metadata(self, updates: Mapping[str, Any] | None) -> ExecutionContext:
        """Return a new context with merged metadata."""
        if not updates:
            return self
        merged = dict(self.metadata)
        merged.update({str(k): v for k, v in updates.items()})
        return replace(self, metadata=merged)

    def with_identity(
        self,
        *,
        user_id: str | None,
        session_id: str | None,
    ) -> ExecutionContext:
        """Return a context with its canonical run identity resolved."""
        return replace(self, user_id=user_id, session_id=session_id)

    @classmethod
    def create(
        cls,
        *,
        user_id: str | None,
        session_id: str | None,
        workspace_id: str | None,
        tenant_id: str | None = None,
        org_id: str | None = None,
        team_id: str | None = None,
        roles: Iterable[str] | None = None,
        scopes: Iterable[str] | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        trusted_permission_tools: Iterable[str] | None = None,
        trusted_permission_categories: Iterable[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        identity_source: IdentitySource = IdentitySource.TRUSTED_HOST,
        admission: AdmissionEnvelope | None = None,
    ) -> ExecutionContext:
        """Build a normalized execution context."""
        return cls(
            user_id=user_id,
            session_id=session_id,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            org_id=org_id,
            team_id=team_id,
            roles=_normalize_sequence(roles),
            scopes=_normalize_sequence(scopes),
            request_id=request_id,
            trace_id=trace_id,
            trusted_permission_tools=_normalize_sequence(trusted_permission_tools),
            trusted_permission_categories=_normalize_sequence(trusted_permission_categories),
            metadata=_normalize_metadata(metadata),
            identity_source=identity_source,
            admission=admission,
        )
