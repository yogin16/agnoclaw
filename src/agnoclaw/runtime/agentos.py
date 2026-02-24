"""AgentOS compatibility adapter skeleton."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .context import ExecutionContext


def _first_non_empty(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _normalize_multi(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        chunks: list[str] = []
        for part in value.replace(",", " ").split():
            token = part.strip()
            if token:
                chunks.append(token)
        return tuple(chunks)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),) if str(value).strip() else ()


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class AgentOSClaimKeys:
    """Claim key mapping for AgentOS/JWT payload normalization."""

    user_id: tuple[str, ...] = ("user_id", "sub")
    session_id: tuple[str, ...] = ("session_id", "sid")
    tenant_id: tuple[str, ...] = ("tenant_id", "tenant")
    org_id: tuple[str, ...] = ("org_id", "organization_id", "org")
    team_id: tuple[str, ...] = ("team_id", "team")
    roles: tuple[str, ...] = ("roles", "role")
    scopes: tuple[str, ...] = ("scopes", "scope", "permissions")
    request_id: tuple[str, ...] = ("request_id", "x_request_id")
    trace_id: tuple[str, ...] = ("trace_id", "x_trace_id", "traceparent")


class AgentOSContextAdapter:
    """
    Convert AgentOS/JWT claims into the harness `ExecutionContext`.

    This keeps AgentOS integration optional while preserving a stable shape
    for policy checks and runtime events.
    """

    def __init__(self, claim_keys: AgentOSClaimKeys | None = None) -> None:
        self.claim_keys = claim_keys or AgentOSClaimKeys()

    def to_execution_context(
        self,
        claims: Mapping[str, Any],
        *,
        workspace_id: str | None,
        user_id: str | None = None,
        session_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExecutionContext:
        payload = dict(metadata or {})
        payload.setdefault("agentos", {})
        payload["agentos"].update(
            {
                "claims_present": True,
                "claim_keys_used": {
                    "user_id": self.claim_keys.user_id,
                    "session_id": self.claim_keys.session_id,
                    "tenant_id": self.claim_keys.tenant_id,
                    "org_id": self.claim_keys.org_id,
                    "team_id": self.claim_keys.team_id,
                    "roles": self.claim_keys.roles,
                    "scopes": self.claim_keys.scopes,
                },
            }
        )

        resolved_user = user_id or _as_str(_first_non_empty(claims, self.claim_keys.user_id))
        resolved_session = session_id or _as_str(_first_non_empty(claims, self.claim_keys.session_id))
        resolved_tenant = _as_str(_first_non_empty(claims, self.claim_keys.tenant_id))
        resolved_org = _as_str(_first_non_empty(claims, self.claim_keys.org_id))
        resolved_team = _as_str(_first_non_empty(claims, self.claim_keys.team_id))
        resolved_roles = _normalize_multi(_first_non_empty(claims, self.claim_keys.roles))
        resolved_scopes = _normalize_multi(_first_non_empty(claims, self.claim_keys.scopes))
        resolved_request = _as_str(_first_non_empty(claims, self.claim_keys.request_id))
        resolved_trace = _as_str(_first_non_empty(claims, self.claim_keys.trace_id))

        return ExecutionContext.create(
            user_id=resolved_user,
            session_id=resolved_session,
            workspace_id=workspace_id,
            tenant_id=resolved_tenant,
            org_id=resolved_org,
            team_id=resolved_team,
            roles=resolved_roles,
            scopes=resolved_scopes,
            request_id=resolved_request,
            trace_id=resolved_trace,
            metadata=payload,
        )
