"""AgentOS compatibility adapters for AgentHarness."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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
        resolved_session = session_id or _as_str(
            _first_non_empty(claims, self.claim_keys.session_id)
        )
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


class AgentOSHarnessAgent:
    """
    AgentOS-compatible facade over an AgentHarness.

    This intentionally routes every run through `AgentHarness.arun()` so policy,
    permissions, guardrails, skill handling, event emission, and workspace context
    remain inside the agnoclaw runtime boundary.
    """

    framework = "agnoclaw"

    def __init__(
        self,
        harness: Any,
        *,
        id: str | None = None,
        name: str | None = None,
        context_adapter: AgentOSContextAdapter | None = None,
    ) -> None:
        self.harness = harness
        self._id = (
            id
            or getattr(harness, "agent_id", None)
            or getattr(harness, "name", None)
            or "agnoclaw"
        )
        self._name = name or getattr(harness, "name", None) or self._id
        self.context_adapter = context_adapter or AgentOSContextAdapter()

    @property
    def id(self) -> str:
        return str(self._id)

    @property
    def name(self) -> str:
        return str(self._name)

    @property
    def description(self) -> str:
        return "agnoclaw AgentHarness runtime adapter"

    @property
    def db(self) -> Any:
        agent = getattr(self.harness, "_agent", None)
        return getattr(agent, "db", None)

    @db.setter
    def db(self, value: Any) -> None:
        agent = getattr(self.harness, "_agent", None)
        if agent is not None:
            agent.db = value

    def _context_from_agentos_kwargs(
        self,
        *,
        session_id: str | None,
        user_id: str | None,
        metadata: Mapping[str, Any] | None,
        dependencies: Mapping[str, Any] | None,
    ) -> ExecutionContext:
        metadata_payload = dict(metadata or {})
        claims = metadata_payload.pop("agentos_claims", None)
        if claims is None:
            claims = metadata_payload.pop("claims", None)
        if not isinstance(claims, Mapping):
            claims = {}
        if dependencies:
            metadata_payload["dependencies"] = dict(dependencies)
        return self.context_adapter.to_execution_context(
            claims,
            workspace_id=str(self.harness.workspace.path),
            user_id=user_id,
            session_id=session_id,
            metadata=metadata_payload,
        )

    def arun(
        self,
        input: Any,
        *,
        stream: bool | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        stream_events: bool | None = None,
        metadata: Mapping[str, Any] | None = None,
        dependencies: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        context = self._context_from_agentos_kwargs(
            session_id=session_id,
            user_id=user_id,
            metadata=metadata,
            dependencies=dependencies,
        )
        call_kwargs = dict(kwargs)
        call_kwargs.pop("background_tasks", None)
        call_kwargs.pop("background", None)
        should_stream = bool(stream)
        should_stream_events = bool(stream_events) if stream_events is not None else should_stream

        if should_stream:
            async def _stream():
                result = await self.harness.arun(
                    str(input),
                    stream=True,
                    stream_events=should_stream_events,
                    context=context,
                    metadata=dict(metadata or {}),
                    **call_kwargs,
                )
                async for event in result:
                    yield event

            return _stream()

        return self.harness.arun(
            str(input),
            stream=False,
            stream_events=should_stream_events,
            context=context,
            metadata=dict(metadata or {}),
            **call_kwargs,
        )


def as_agentos_agent(
    harness: Any,
    *,
    agent_id: str | None = None,
    name: str | None = None,
    context_adapter: AgentOSContextAdapter | None = None,
) -> AgentOSHarnessAgent:
    """Build an AgentOS-compatible agent facade for a harness."""
    return AgentOSHarnessAgent(
        harness,
        id=agent_id,
        name=name,
        context_adapter=context_adapter,
    )


def create_agentos_app(
    harnesses: Sequence[Any],
    *,
    include_agnoclaw_admin: bool = False,
    enable_mcp_server: bool = False,
    scheduler: bool = False,
    approvals: bool = False,
    **kwargs: Any,
) -> Any:
    """
    Create a FastAPI app backed by Agno AgentOS and agnoclaw harness adapters.

    AgentOS owns the agent/session/trace/schedule/approval API surface. The
    optional `/agnoclaw` admin routes expose only harness-owned inspection data.
    """
    if not harnesses:
        raise ValueError("create_agentos_app requires at least one harness")
    try:
        from agno.os.app import AgentOS
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise ImportError("Agno AgentOS is required for create_agentos_app()") from exc

    agents = [
        h.as_agentos_agent() if hasattr(h, "as_agentos_agent") else as_agentos_agent(h)
        for h in harnesses
    ]
    db = kwargs.pop("db", None)
    if db is None:
        db = getattr(agents[0], "db", None)
    agent_os = AgentOS(
        agents=agents,
        db=db,
        enable_mcp_server=enable_mcp_server,
        scheduler=scheduler,
        **kwargs,
    )
    app = agent_os.get_app()
    app.state.agnoclaw_harnesses = {
        agent.id: harness for agent, harness in zip(agents, harnesses, strict=True)
    }
    app.state.agnoclaw_approvals_requested = approvals
    if include_agnoclaw_admin:
        app.include_router(_build_agnoclaw_admin_router(app.state.agnoclaw_harnesses))
    return app


def _build_agnoclaw_admin_router(harnesses: Mapping[str, Any]) -> Any:
    from fastapi import APIRouter, HTTPException

    router = APIRouter(prefix="/agnoclaw", tags=["agnoclaw"])

    def _get_harness(harness_id: str) -> Any:
        harness = harnesses.get(harness_id)
        if harness is None:
            raise HTTPException(status_code=404, detail="Harness not found")
        return harness

    @router.get("/harnesses")
    async def list_harnesses() -> list[dict[str, Any]]:
        return [
            {
                "id": harness_id,
                "name": getattr(harness, "name", harness_id),
                "workspace": str(getattr(getattr(harness, "workspace", None), "path", "")),
            }
            for harness_id, harness in harnesses.items()
        ]

    @router.get("/harnesses/{harness_id}/capabilities")
    async def harness_capabilities(harness_id: str) -> dict[str, Any]:
        harness = _get_harness(harness_id)
        return {
            "skills": len(getattr(getattr(harness, "skills", None), "list_skills", lambda: [])()),
            "context_providers": [
                {
                    "id": getattr(provider, "id", None),
                    "name": getattr(provider, "name", None),
                }
                for provider in getattr(harness, "_context_providers", [])
            ],
            "packs": [
                getattr(getattr(pack, "manifest", None), "name", None)
                for pack in getattr(harness, "_loaded_packs", [])
            ],
            "permission_mode": getattr(harness, "permission_mode", None),
        }

    @router.get("/harnesses/{harness_id}/runtime")
    async def harness_runtime(harness_id: str) -> dict[str, Any]:
        harness = _get_harness(harness_id)
        return {
            "model": getattr(harness, "_model", None),
            "session_id": getattr(harness, "session_id", None),
            "workspace_id": str(getattr(getattr(harness, "workspace", None), "path", "")),
            "sandbox_dir": str(getattr(harness, "sandbox_dir", "")),
        }

    @router.get("/harnesses/{harness_id}/skills")
    async def harness_skills(harness_id: str) -> list[Any]:
        harness = _get_harness(harness_id)
        lister = getattr(getattr(harness, "skills", None), "list_skills", None)
        return list(lister() if callable(lister) else [])

    @router.get("/harnesses/{harness_id}/packs")
    async def harness_packs(harness_id: str) -> list[dict[str, Any]]:
        harness = _get_harness(harness_id)
        packs = []
        for loaded in getattr(harness, "_loaded_packs", []):
            manifest = getattr(loaded, "manifest", None)
            if manifest is None:
                continue
            packs.append(
                {
                    "name": manifest.name,
                    "version": manifest.version,
                    "description": manifest.description,
                    "trust": {
                        "default": manifest.trust.default,
                        "requires_code_execution": manifest.trust.requires_code_execution,
                    },
                }
            )
        return packs

    return router
