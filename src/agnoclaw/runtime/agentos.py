"""AgentOS compatibility adapters for AgentHarness."""

from __future__ import annotations

import inspect
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

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


def _agentos_scheduler_metadata(values: Mapping[str, Any]) -> dict[str, Any]:
    scheduler_keys = (
        "schedule_id",
        "schedule_run_id",
        "schedule_name",
        "scheduled_at",
    )
    return {key: values[key] for key in scheduler_keys if key in values and values[key] is not None}


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
        call_kwargs = dict(kwargs)
        call_kwargs.pop("background_tasks", None)
        call_kwargs.pop("background", None)
        scheduler_metadata = _agentos_scheduler_metadata(call_kwargs)
        for key in scheduler_metadata:
            call_kwargs.pop(key, None)
        run_metadata = dict(metadata or {})
        if scheduler_metadata:
            run_metadata.setdefault("agentos", {})
            if isinstance(run_metadata["agentos"], dict):
                run_metadata["agentos"].setdefault("scheduler", scheduler_metadata)

        context = self._context_from_agentos_kwargs(
            session_id=session_id,
            user_id=user_id,
            metadata=run_metadata,
            dependencies=dependencies,
        )
        should_stream = bool(stream)
        should_stream_events = bool(stream_events) if stream_events is not None else should_stream

        if should_stream:
            async def _stream():
                result = await self.harness.arun(
                    str(input),
                    stream=True,
                    stream_events=should_stream_events,
                    context=context,
                    metadata=run_metadata,
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
            metadata=run_metadata,
            **call_kwargs,
        )


class AgentOSPermissionApprover:
    """
    Bridge agnoclaw permission requests into AgentOS approval records.

    The bridge is intentionally conservative: an already-approved matching record
    allows the tool call; otherwise a pending approval is persisted and the current
    permission request is rejected so the caller can resolve it through AgentOS.
    """

    def __init__(self, db: Any, *, agent_id: str, source_name: str = "agnoclaw") -> None:
        self.db = db
        self.agent_id = agent_id
        self.source_name = source_name

    def approve(self, request: Any, context: ExecutionContext) -> bool:
        if self.db is None:
            return False
        approved = self._find_matching_approval(
            request,
            context,
            status="approved",
        )
        if approved is not None:
            return True
        if self._find_matching_approval(request, context, status="pending") is None:
            self._create_pending_approval(request, context)
        return False

    def _find_matching_approval(
        self,
        request: Any,
        context: ExecutionContext,
        *,
        status: str,
    ) -> Mapping[str, Any] | None:
        getter = getattr(self.db, "get_approvals", None)
        if not callable(getter):
            return None
        result = getter(
            status=status,
            source_type="agnoclaw",
            approval_type="required",
            pause_type="permission",
            agent_id=self.agent_id,
            user_id=context.user_id,
            run_id=request.run_id,
            limit=100,
            page=1,
        )
        if inspect.isawaitable(result):
            closer = getattr(result, "close", None)
            if callable(closer):
                closer()
            return None
        approvals = result[0] if isinstance(result, tuple) else result
        if not isinstance(approvals, list):
            return None
        for approval in approvals:
            if not isinstance(approval, Mapping):
                continue
            if approval.get("tool_name") != request.tool_name:
                continue
            tool_args = approval.get("tool_args")
            if isinstance(tool_args, Mapping) and dict(tool_args) != dict(request.arguments):
                continue
            return approval
        return None

    def _create_pending_approval(self, request: Any, context: ExecutionContext) -> None:
        creator = getattr(self.db, "create_approval", None)
        if not callable(creator):
            return
        now = int(time.time())
        approval = {
            "id": f"agnoclaw_perm_{uuid4().hex}",
            "run_id": request.run_id,
            "session_id": context.session_id or "",
            "status": "pending",
            "source_type": "agnoclaw",
            "approval_type": "required",
            "pause_type": "permission",
            "tool_name": request.tool_name,
            "tool_args": dict(request.arguments),
            "agent_id": self.agent_id,
            "user_id": context.user_id,
            "source_name": self.source_name,
            "requirements": [
                {
                    "type": "permission",
                    "tool_name": request.tool_name,
                    "category": getattr(request, "category", None),
                }
            ],
            "context": {
                "tenant_id": context.tenant_id,
                "org_id": context.org_id,
                "team_id": context.team_id,
                "workspace_id": context.workspace_id,
                "request_id": context.request_id,
                "trace_id": context.trace_id,
                "roles": list(context.roles),
                "scopes": list(context.scopes),
                "metadata": context.metadata,
            },
            "created_at": now,
            "updated_at": now,
        }
        result = creator(approval)
        if inspect.isawaitable(result):
            closer = getattr(result, "close", None)
            if callable(closer):
                closer()
            return


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


def _attach_agentos_approval_bridge(
    harnesses: Sequence[Any],
    agents: Sequence[Any],
    db: Any,
) -> None:
    if db is None:
        return
    for harness, agent in zip(harnesses, agents, strict=True):
        controller = getattr(harness, "_permission_controller", None)
        if controller is None or getattr(controller, "approver", None) is not None:
            continue
        controller.approver = AgentOSPermissionApprover(db, agent_id=agent.id)
        harness._agentos_approvals_enabled = True


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
    if approvals:
        _attach_agentos_approval_bridge(harnesses, agents, db)
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
    app.state.agnoclaw_scheduler_requested = scheduler
    app.state.agnoclaw_mcp_requested = enable_mcp_server
    for harness in harnesses:
        harness._agentos_scheduler_enabled = scheduler
        harness._agentos_mcp_enabled = enable_mcp_server
    if include_agnoclaw_admin:
        app.include_router(
            _build_agnoclaw_admin_router(
                app.state.agnoclaw_harnesses,
                settings=getattr(agent_os, "settings", None),
            )
        )
    return app


def _build_agnoclaw_admin_router(harnesses: Mapping[str, Any], *, settings: Any = None) -> Any:
    from agno.os.auth import get_authentication_dependency
    from agno.os.scopes import has_required_scopes
    from fastapi import APIRouter, Depends, HTTPException, Request
    from fastapi.responses import FileResponse

    auth_dependency = get_authentication_dependency(settings)

    async def _require_agnoclaw_admin(request: Request) -> None:
        if not getattr(request.state, "authorization_enabled", False):
            return
        scopes = list(getattr(request.state, "scopes", []) or [])
        if has_required_scopes(scopes, ["agnoclaw:debug"]):
            return
        if has_required_scopes(scopes, ["agnoclaw:admin"]):
            return
        raise HTTPException(status_code=403, detail="agnoclaw admin scope required")

    router = APIRouter(
        prefix="/agnoclaw",
        tags=["agnoclaw"],
        dependencies=[Depends(auth_dependency), Depends(_require_agnoclaw_admin)],
    )

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
        lister = getattr(harness, "admin_harness_capabilities", None)
        if callable(lister):
            return dict(lister())
        return {"permission_mode": getattr(harness, "permission_mode", None)}

    @router.get("/harnesses/{harness_id}/runtime")
    async def harness_runtime(harness_id: str) -> dict[str, Any]:
        harness = _get_harness(harness_id)
        info = getattr(harness, "admin_runtime_info", None)
        if callable(info):
            return dict(info())
        return {"session_id": getattr(harness, "session_id", None)}

    @router.get("/harnesses/{harness_id}/skills")
    async def harness_skills(harness_id: str) -> list[Any]:
        harness = _get_harness(harness_id)
        lister = getattr(harness, "admin_list_skills", None)
        if callable(lister):
            return list(lister())
        lister = getattr(getattr(harness, "skills", None), "list_skills", None)
        return list(lister() if callable(lister) else [])

    @router.get("/harnesses/{harness_id}/packs")
    async def harness_packs(harness_id: str) -> list[dict[str, Any]]:
        harness = _get_harness(harness_id)
        lister = getattr(harness, "admin_list_packs", None)
        return list(lister() if callable(lister) else [])

    @router.get("/harnesses/{harness_id}/policies")
    async def harness_policies(harness_id: str) -> dict[str, Any]:
        harness = _get_harness(harness_id)
        lister = getattr(harness, "admin_list_policies", None)
        return dict(lister() if callable(lister) else {})

    @router.get("/harnesses/{harness_id}/permissions")
    async def harness_permissions(harness_id: str) -> dict[str, Any]:
        harness = _get_harness(harness_id)
        lister = getattr(harness, "admin_list_permissions", None)
        return dict(lister() if callable(lister) else {})

    @router.get("/harnesses/{harness_id}/events")
    async def harness_events(harness_id: str) -> list[dict[str, Any]]:
        harness = _get_harness(harness_id)
        lister = getattr(harness, "admin_list_events", None)
        return list(lister() if callable(lister) else [])

    @router.get("/harnesses/{harness_id}/events/{run_id}")
    async def harness_run_events(harness_id: str, run_id: str) -> list[dict[str, Any]]:
        harness = _get_harness(harness_id)
        lister = getattr(harness, "admin_list_events", None)
        return list(lister(run_id=run_id) if callable(lister) else [])

    @router.get("/harnesses/{harness_id}/sandboxes/{session_id}")
    async def harness_sandbox(harness_id: str, session_id: str) -> dict[str, Any]:
        harness = _get_harness(harness_id)
        info = getattr(harness, "admin_sandbox_info", None)
        if not callable(info):
            raise HTTPException(status_code=404, detail="Sandbox admin is unavailable")
        return dict(info(session_id=session_id))

    @router.get("/harnesses/{harness_id}/sandboxes/{session_id}/files")
    async def harness_sandbox_files(harness_id: str, session_id: str) -> list[dict[str, Any]]:
        harness = _get_harness(harness_id)
        lister = getattr(harness, "admin_list_sandbox_files", None)
        return list(lister(session_id=session_id) if callable(lister) else [])

    @router.post("/harnesses/{harness_id}/sandboxes/{session_id}/snapshot")
    async def harness_sandbox_snapshot(harness_id: str, session_id: str) -> dict[str, Any]:
        harness = _get_harness(harness_id)
        snapshot = getattr(harness, "admin_snapshot_sandbox", None)
        if not callable(snapshot):
            raise HTTPException(status_code=404, detail="Sandbox snapshot is unavailable")
        return dict(snapshot(session_id=session_id))

    @router.post("/harnesses/{harness_id}/sandboxes/{session_id}/reset")
    async def harness_sandbox_reset(harness_id: str, session_id: str) -> dict[str, Any]:
        harness = _get_harness(harness_id)
        reset = getattr(harness, "admin_reset_sandbox", None)
        if not callable(reset):
            raise HTTPException(status_code=404, detail="Sandbox reset is unavailable")
        return dict(reset(session_id=session_id))

    @router.get("/harnesses/{harness_id}/sandboxes/{session_id}/artifacts/{artifact_path:path}")
    async def harness_sandbox_artifact(
        harness_id: str,
        session_id: str,
        artifact_path: str,
    ) -> FileResponse:
        del session_id
        harness = _get_harness(harness_id)
        resolver = getattr(harness, "admin_sandbox_artifact_path", None)
        path = resolver(artifact_path) if callable(resolver) else None
        if path is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return FileResponse(path)

    @router.get("/policies")
    async def policies() -> dict[str, Any]:
        return {
            harness_id: harness.admin_list_policies()
            for harness_id, harness in harnesses.items()
            if callable(getattr(harness, "admin_list_policies", None))
        }

    @router.get("/permissions")
    async def permissions() -> dict[str, Any]:
        return {
            harness_id: harness.admin_list_permissions()
            for harness_id, harness in harnesses.items()
            if callable(getattr(harness, "admin_list_permissions", None))
        }

    return router
