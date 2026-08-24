"""Authenticated AgentOS/FastAPI adapter for the durable lifecycle protocol."""

import hmac
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from ..commands import command_from_dict
from .agentos import AgentOSContextAdapter
from .child_ingress import DeclaredChildTemplate
from .context import ExecutionContext
from .errors import HarnessError
from .lifecycle_protocol import (
    LIFECYCLE_PROTOCOL_VERSION,
    MAX_LIFECYCLE_EVENT_PAGE_SIZE,
    MAX_LIFECYCLE_MESSAGE_BYTES,
    MAX_LIFECYCLE_METADATA_BYTES,
    MAX_LIFECYCLE_OUTPUT_PAGE_SIZE,
    MAX_LIFECYCLE_REQUEST_BYTES,
    harness_error_to_wire,
    lifecycle_envelope,
    snapshot_to_wire,
)
from .run_handle import RunReconciliationRequiredError, RunWaitError
from .security import IdentitySource, freeze_data, thaw_data
from .store import encode_event_cursor

_START_FIELDS = frozenset(
    {
        "protocol_version",
        "message",
        "idempotency_key",
        "session_id",
        "user_id",
        "metadata",
        "learning_consent",
        "persist_output",
        "options",
    }
)
_PROTECTED_OPTIONS = frozenset(
    {
        "context",
        "idempotency_key",
        "learning_consent",
        "metadata",
        "persist_output",
        "session_id",
        "stream",
        "user_id",
    }
)
_PROTECTED_METADATA = frozenset({"_agnoclaw_context", "agentos", "agentos_claims", "claims"})
_CHILD_START_FIELDS = frozenset(
    {"protocol_version", "template", "task", "delegation_id", "user_id"}
)


class LifecycleRequestError(HarnessError):
    def __init__(self, message: str) -> None:
        super().__init__(
            code="LIFECYCLE_REQUEST_INVALID",
            category="validation",
            message=message,
            retryable=False,
        )


class LifecycleAuthorizationError(HarnessError):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(
            code=code,
            category="authorization",
            message=message,
            retryable=False,
        )


def build_lifecycle_router(
    harnesses: Mapping[str, Any],
    *,
    settings: Any = None,
    child_templates: Mapping[str, Mapping[str, DeclaredChildTemplate]] | None = None,
) -> Any:
    """Build the versioned lifecycle router without importing FastAPI at package import."""
    from agno.os.auth import get_authentication_dependency
    from agno.os.scopes import has_required_scopes
    from fastapi import APIRouter, Depends, HTTPException, Request
    from fastapi.responses import JSONResponse
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    auth_dependency = get_authentication_dependency(settings)
    security = HTTPBearer(auto_error=False)
    router = APIRouter(
        prefix="/agnoclaw/v1",
        tags=["agnoclaw-lifecycle"],
    )
    declared_templates: dict[str, dict[str, DeclaredChildTemplate]] = {}
    for parent_id, catalog in (child_templates or {}).items():
        if parent_id not in harnesses or not isinstance(catalog, Mapping):
            raise ValueError("child template catalogs require a registered parent harness")
        normalized: dict[str, DeclaredChildTemplate] = {}
        for name, template in catalog.items():
            if not isinstance(template, DeclaredChildTemplate) or name != template.name:
                raise ValueError("child template catalog keys must match declared template names")
            normalized[name] = template
        declared_templates[parent_id] = normalized

    def response(value: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
        return JSONResponse(value, status_code=status_code)

    def error_response(error: Exception) -> JSONResponse:
        if isinstance(error, HarnessError):
            status = _harness_error_status(error)
            safe_error = error
        elif isinstance(error, (TypeError, ValueError, json.JSONDecodeError)):
            status = 400
            safe_error = LifecycleRequestError("The lifecycle request is invalid.")
        else:
            status = 500
            safe_error = HarnessError(
                code="LIFECYCLE_INTERNAL_ERROR",
                category="internal",
                message="The lifecycle request could not be completed.",
                retryable=True,
            )
        return response(
            lifecycle_envelope("error", error=harness_error_to_wire(safe_error)),
            status_code=status,
        )

    def harness_for(harness_id: str) -> Any:
        harness = harnesses.get(harness_id)
        if harness is None:
            raise LifecycleAuthorizationError(
                code="HARNESS_NOT_FOUND",
                message="The requested harness does not exist or is not visible.",
            )
        return harness

    def child_template_for(harness_id: str, name: str) -> DeclaredChildTemplate:
        template = declared_templates.get(harness_id, {}).get(name)
        if template is None:
            raise LifecycleAuthorizationError(
                code="CHILD_TEMPLATE_NOT_FOUND",
                message="The requested child template does not exist or is not visible.",
            )
        return template

    def authorize(request: Request, *, required_scope: str) -> None:
        state = request.state
        authorization_enabled = getattr(state, "authorization_enabled", False) is True
        authenticated = getattr(state, "authenticated", False) is True
        scopes = [str(item) for item in (getattr(state, "scopes", None) or ())]
        if not authenticated:
            raise LifecycleAuthorizationError(
                code="LIFECYCLE_AUTHENTICATION_REQUIRED",
                message="The lifecycle protocol requires an authenticated AgentOS request.",
            )
        # A validated OS security key is a trusted root and intentionally has no ACL
        # scope list. JWT and service-account identities are always scope checked.
        if authorization_enabled or scopes:
            admin_scope = getattr(state, "admin_scope", None)
            if isinstance(admin_scope, str) and has_required_scopes(scopes, [admin_scope]):
                return
            if not has_required_scopes(scopes, [required_scope]):
                raise LifecycleAuthorizationError(
                    code="LIFECYCLE_SCOPE_REQUIRED",
                    message=f"The lifecycle request requires '{required_scope}'.",
                )

    async def authenticate(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None,
    ) -> None:
        try:
            await auth_dependency(request, credentials)
        except HTTPException as exc:
            raise LifecycleAuthorizationError(
                code="LIFECYCLE_AUTHENTICATION_REQUIRED",
                message="The lifecycle protocol requires valid AgentOS authentication.",
            ) from exc
        # Agno 2.8+ marks a verified OS key as authenticated. Older supported
        # releases returned success without setting request state, so preserve
        # the same trust result only after an exact local comparison.
        configured_key = getattr(settings, "os_security_key", None)
        if (
            getattr(request.state, "authenticated", False) is not True
            and isinstance(configured_key, str)
            and credentials is not None
            and hmac.compare_digest(credentials.credentials, configured_key)
        ):
            request.state.authenticated = True

    def context_for(
        request: Request,
        harness: Any,
        *,
        user_id: str | None,
        session_id: str | None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExecutionContext:
        state = request.state
        claims_value = getattr(state, "claims", None)
        claims = dict(claims_value) if isinstance(claims_value, Mapping) else {}
        state_user = _optional_identifier(getattr(state, "user_id", None), field_name="user_id")
        if state_user is not None:
            existing_user = claims.get("user_id", claims.get("sub"))
            if existing_user is not None and str(existing_user) != state_user:
                raise LifecycleAuthorizationError(
                    code="IDENTITY_CLAIM_CONFLICT",
                    message="Authenticated user identity is internally inconsistent.",
                )
            claims.setdefault("user_id", state_user)
        state_scopes = tuple(str(item) for item in (getattr(state, "scopes", None) or ()))
        if state_scopes:
            claims["scopes"] = state_scopes
        state_roles = tuple(str(item) for item in (getattr(state, "roles", None) or ()))
        if state_roles:
            claims["roles"] = state_roles

        context_metadata = dict(metadata or {})
        for protected_key in _PROTECTED_METADATA:
            context_metadata.pop(protected_key, None)
        context = AgentOSContextAdapter().to_execution_context(
            claims,
            workspace_id=str(harness.workspace.path),
            user_id=user_id,
            session_id=session_id,
            metadata={"source": "agnoclaw_lifecycle", **context_metadata},
        )
        authorization_enabled = bool(getattr(state, "authorization_enabled", False))
        if authorization_enabled and context.user_id is None:
            raise LifecycleAuthorizationError(
                code="LIFECYCLE_PRINCIPAL_REQUIRED",
                message="JWT lifecycle requests require an authenticated user principal.",
            )
        if context.tenant_id is None:
            context = replace(context, tenant_id=getattr(harness, "_tenant_id", None))
        if context.user_id is None:
            context = replace(context, user_id=getattr(harness, "user_id", None))
        if not authorization_enabled and not claims:
            context = replace(context, identity_source=IdentitySource.TRUSTED_HOST)
        return context

    async def reattach(
        request: Request,
        *,
        harness_id: str,
        run_id: str,
        user_id: str | None,
        required_scope: str,
        credentials: HTTPAuthorizationCredentials | None,
    ) -> Any:
        await authenticate(request, credentials)
        authorize(request, required_scope=required_scope)
        harness = harness_for(_required_identifier(harness_id, field_name="harness_id"))
        run_id = _required_identifier(run_id, field_name="run_id")
        context = context_for(request, harness, user_id=user_id, session_id=None)
        return harness.get_run(run_id, context=context)

    @router.post("/harnesses/{harness_id}/runs")
    async def start_run(
        request: Request,
        harness_id: str,
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
    ) -> JSONResponse:
        try:
            await authenticate(request, credentials)
            authorize(request, required_scope="agents:run")
            harness = harness_for(_required_identifier(harness_id, field_name="harness_id"))
            payload = await _read_json_body(request)
            start = _parse_start_request(payload)
            context = context_for(
                request,
                harness,
                user_id=start["user_id"],
                session_id=start["session_id"],
                metadata=start["metadata"],
            )
            run = await harness.start(
                start["message"],
                idempotency_key=start["idempotency_key"],
                context=context,
                metadata=start["metadata"],
                learning_consent=start["learning_consent"],
                persist_output=start["persist_output"],
                **start["options"],
            )
            snapshot = await run.status()
            return response(
                lifecycle_envelope("run", run=snapshot_to_wire(snapshot)),
                status_code=202,
            )
        except Exception as exc:
            return error_response(exc)

    @router.get("/harnesses/{harness_id}/runs/{run_id}")
    async def get_run(
        request: Request,
        harness_id: str,
        run_id: str,
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
        user_id: str | None = None,
    ) -> JSONResponse:
        try:
            run = await reattach(
                request,
                harness_id=harness_id,
                run_id=run_id,
                user_id=user_id,
                required_scope="agents:read",
                credentials=credentials,
            )
            return response(lifecycle_envelope("run", run=snapshot_to_wire(await run.status())))
        except Exception as exc:
            return error_response(exc)

    @router.post("/harnesses/{harness_id}/runs/{run_id}/children")
    async def start_child(
        request: Request,
        harness_id: str,
        run_id: str,
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
    ) -> JSONResponse:
        try:
            await authenticate(request, credentials)
            authorize(request, required_scope="agents:run")
            harness_id = _required_identifier(harness_id, field_name="harness_id")
            run_id = _required_identifier(run_id, field_name="run_id")
            harness = harness_for(harness_id)
            payload = _parse_child_start_request(await _read_json_body(request))
            context = context_for(
                request,
                harness,
                user_id=payload["user_id"],
                session_id=None,
            )
            parent = harness.get_run(run_id, context=context)
            template = child_template_for(harness_id, payload["template"])
            for required_scope in template.required_scopes:
                authorize(request, required_scope=required_scope)
            child = await template.start(
                harness,
                parent,
                payload["task"],
                context=context,
                delegation_id=payload["delegation_id"],
            )
            return response(
                lifecycle_envelope(
                    "child",
                    parent=snapshot_to_wire(await parent.status()),
                    child=snapshot_to_wire(await child.status()),
                    template=template.name,
                    template_digest=template.digest,
                ),
                status_code=202,
            )
        except Exception as exc:
            return error_response(exc)

    @router.get("/harnesses/{harness_id}/runs/{run_id}/children")
    async def list_children(
        request: Request,
        harness_id: str,
        run_id: str,
        limit: str = "64",
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
        user_id: str | None = None,
    ) -> JSONResponse:
        try:
            page_limit = _parse_bounded_int(limit, field_name="limit", minimum=1, maximum=64)
            run = await reattach(
                request,
                harness_id=harness_id,
                run_id=run_id,
                user_id=user_id,
                required_scope="agents:read",
                credentials=credentials,
            )
            parent = await run.status()
            children = await run.children(limit=page_limit)
            return response(
                lifecycle_envelope(
                    "children",
                    parent=snapshot_to_wire(parent),
                    children=[snapshot_to_wire(item) for item in children],
                )
            )
        except Exception as exc:
            return error_response(exc)

    @router.get("/harnesses/{harness_id}/runs/{run_id}/child-results")
    async def get_child_results(
        request: Request,
        harness_id: str,
        run_id: str,
        limit: str = "64",
        artifact_limit: str = "16",
        max_inline_result_chars: str = "8000",
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
        user_id: str | None = None,
    ) -> JSONResponse:
        try:
            result_limit = _parse_bounded_int(
                limit, field_name="limit", minimum=1, maximum=64
            )
            result_artifact_limit = _parse_bounded_int(
                artifact_limit,
                field_name="artifact_limit",
                minimum=0,
                maximum=100,
            )
            inline_limit = _parse_bounded_int(
                max_inline_result_chars,
                field_name="max_inline_result_chars",
                minimum=256,
                maximum=1_000_000,
            )
            run = await reattach(
                request,
                harness_id=harness_id,
                run_id=run_id,
                user_id=user_id,
                required_scope="agents:read",
                credentials=credentials,
            )
            parent = await run.status()
            results = await run.child_results(
                limit=result_limit,
                artifact_limit=result_artifact_limit,
            )
            return response(
                lifecycle_envelope(
                    "child_results",
                    parent=snapshot_to_wire(parent),
                    results=results.synthesis_payload(
                        max_inline_result_chars=inline_limit
                    ),
                )
            )
        except Exception as exc:
            return error_response(exc)

    @router.get("/harnesses/{harness_id}/runs/{run_id}/result")
    async def get_result(
        request: Request,
        harness_id: str,
        run_id: str,
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
        user_id: str | None = None,
    ) -> JSONResponse:
        try:
            run = await reattach(
                request,
                harness_id=harness_id,
                run_id=run_id,
                user_id=user_id,
                required_scope="agents:read",
                credentials=credentials,
            )
            snapshot = await run.status()
            if snapshot.state.value == "waiting_for_reconciliation":
                blocked = RunReconciliationRequiredError(snapshot)
                return response(
                    lifecycle_envelope(
                        "result",
                        run=snapshot_to_wire(snapshot),
                        ready=False,
                        blocked=True,
                        result=None,
                        error=harness_error_to_wire(blocked),
                    )
                )
            if not snapshot.terminal:
                return response(
                    lifecycle_envelope(
                        "result",
                        run=snapshot_to_wire(snapshot),
                        ready=False,
                        blocked=False,
                        result=None,
                        error=None,
                    )
                )
            try:
                result = await run.wait()
            except RunWaitError as exc:
                return response(
                    lifecycle_envelope(
                        "result",
                        run=snapshot_to_wire(exc.snapshot),
                        ready=True,
                        blocked=False,
                        result=None,
                        error={
                            **harness_error_to_wire(exc),
                            "safe_error": thaw_data(exc.safe_error),
                        },
                    )
                )
            return response(
                lifecycle_envelope(
                    "result",
                    run=snapshot_to_wire(snapshot),
                    ready=True,
                    blocked=False,
                    result=result,
                    error=None,
                )
            )
        except Exception as exc:
            return error_response(exc)

    @router.get("/harnesses/{harness_id}/runs/{run_id}/events")
    async def get_events(
        request: Request,
        harness_id: str,
        run_id: str,
        after: str | None = None,
        limit: str = "100",
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
        user_id: str | None = None,
    ) -> JSONResponse:
        try:
            page_limit = _parse_event_limit(limit)
            if after is not None and (not after or len(after) > 2_048):
                raise LifecycleRequestError("after must be a valid bounded event cursor")
            run = await reattach(
                request,
                harness_id=harness_id,
                run_id=run_id,
                user_id=user_id,
                required_scope="agents:read",
                credentials=credentials,
            )
            items = []
            stream = run.events(after=after, follow=False)
            try:
                async for event in stream:
                    items.append(event)
                    if len(items) == page_limit:
                        break
            finally:
                await stream.aclose()
            snapshot = await run.status()
            next_cursor = (
                encode_event_cursor(run_id=run_id, sequence=items[-1].sequence) if items else after
            )
            return response(
                lifecycle_envelope(
                    "events",
                    run=snapshot_to_wire(snapshot),
                    events=[item.to_dict() for item in items],
                    next_cursor=next_cursor,
                )
            )
        except Exception as exc:
            return error_response(exc)

    @router.get("/harnesses/{harness_id}/runs/{run_id}/output")
    async def get_output(
        request: Request,
        harness_id: str,
        run_id: str,
        after: str | None = None,
        limit: str = "50",
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
        user_id: str | None = None,
    ) -> JSONResponse:
        try:
            page_limit = _parse_output_limit(limit)
            if after is not None and (not after or len(after) > 2_048):
                raise LifecycleRequestError("after must be a valid bounded event cursor")
            run = await reattach(
                request,
                harness_id=harness_id,
                run_id=run_id,
                user_id=user_id,
                required_scope="agents:read",
                credentials=credentials,
            )
            items = []
            stream = run.output(after=after, follow=False)
            try:
                async for segment in stream:
                    items.append(segment)
                    if len(items) == page_limit:
                        break
            finally:
                await stream.aclose()
            snapshot = await run.status()
            next_cursor = items[-1].cursor if items else after
            return response(
                lifecycle_envelope(
                    "output",
                    run=snapshot_to_wire(snapshot),
                    segments=[item.to_dict() for item in items],
                    next_cursor=next_cursor,
                )
            )
        except Exception as exc:
            return error_response(exc)

    @router.post("/harnesses/{harness_id}/runs/{run_id}/cancel")
    async def cancel_run(
        request: Request,
        harness_id: str,
        run_id: str,
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
        user_id: str | None = None,
    ) -> JSONResponse:
        try:
            run = await reattach(
                request,
                harness_id=harness_id,
                run_id=run_id,
                user_id=user_id,
                required_scope="agents:run",
                credentials=credentials,
            )
            return response(lifecycle_envelope("run", run=snapshot_to_wire(await run.cancel())))
        except Exception as exc:
            return error_response(exc)

    @router.post("/harnesses/{harness_id}/runs/{run_id}/commands")
    async def command_run(
        request: Request,
        harness_id: str,
        run_id: str,
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
        user_id: str | None = None,
    ) -> JSONResponse:
        try:
            run = await reattach(
                request,
                harness_id=harness_id,
                run_id=run_id,
                user_id=user_id,
                required_scope="agents:run",
                credentials=credentials,
            )
            payload = await _read_json_body(request)
            await run.command(command_from_dict(dict(payload)))
            return response(lifecycle_envelope("run", run=snapshot_to_wire(await run.status())))
        except Exception as exc:
            return error_response(exc)

    return router


async def _read_json_body(request: Any) -> Mapping[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            parsed_content_length = int(content_length)
            if parsed_content_length < 0:
                raise LifecycleRequestError("Content-Length is invalid.")
            if parsed_content_length > MAX_LIFECYCLE_REQUEST_BYTES:
                raise LifecycleRequestError("The lifecycle request body is too large.")
        except ValueError as exc:
            raise LifecycleRequestError("Content-Length is invalid.") from exc
    body = await request.body()
    if len(body) > MAX_LIFECYCLE_REQUEST_BYTES:
        raise LifecycleRequestError("The lifecycle request body is too large.")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleRequestError("The lifecycle request body must be valid JSON.") from exc
    if not isinstance(value, Mapping):
        raise LifecycleRequestError("The lifecycle request body must be an object.")
    return value


def _parse_start_request(value: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(value) - _START_FIELDS
    if unknown:
        raise LifecycleRequestError(
            f"Unknown lifecycle request field(s): {', '.join(sorted(unknown))}."
        )
    if value.get("protocol_version") != "1.0":
        raise LifecycleRequestError("The lifecycle protocol version is unsupported.")
    message = value.get("message")
    if not isinstance(message, str) or not message.strip():
        raise LifecycleRequestError("message must be a non-empty string")
    if len(message.encode("utf-8")) > MAX_LIFECYCLE_MESSAGE_BYTES:
        raise LifecycleRequestError("message is too large")
    metadata = value.get("metadata") or {}
    options = value.get("options") or {}
    if not isinstance(metadata, Mapping) or not isinstance(options, Mapping):
        raise LifecycleRequestError("metadata and options must be objects")
    metadata = thaw_data(freeze_data(metadata))
    options = thaw_data(freeze_data(options))
    if len(_canonical_json(metadata)) > MAX_LIFECYCLE_METADATA_BYTES:
        raise LifecycleRequestError("metadata is too large")
    protected_metadata = set(metadata) & _PROTECTED_METADATA
    if protected_metadata:
        raise LifecycleRequestError(
            f"Protected metadata field(s): {', '.join(sorted(protected_metadata))}."
        )
    protected = set(options) & _PROTECTED_OPTIONS
    if protected:
        raise LifecycleRequestError(
            f"Protected lifecycle option(s): {', '.join(sorted(protected))}."
        )
    learning_consent = value.get("learning_consent", False)
    if not isinstance(learning_consent, bool):
        raise LifecycleRequestError("learning_consent must be a boolean")
    persist_output = value.get("persist_output", True)
    if not isinstance(persist_output, bool):
        raise LifecycleRequestError("persist_output must be a boolean")
    return {
        "message": message,
        "idempotency_key": _optional_identifier(
            value.get("idempotency_key"), field_name="idempotency_key"
        ),
        "session_id": _optional_identifier(value.get("session_id"), field_name="session_id"),
        "user_id": _optional_identifier(value.get("user_id"), field_name="user_id"),
        "metadata": metadata,
        "learning_consent": learning_consent,
        "persist_output": persist_output,
        "options": options,
    }


def _parse_child_start_request(value: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(value) - _CHILD_START_FIELDS
    if unknown:
        raise LifecycleRequestError(
            f"Unknown child request field(s): {', '.join(sorted(unknown))}."
        )
    if value.get("protocol_version") != LIFECYCLE_PROTOCOL_VERSION:
        raise LifecycleRequestError("The lifecycle protocol version is unsupported.")
    task = value.get("task")
    if not isinstance(task, str) or not task.strip():
        raise LifecycleRequestError("task must be a non-empty string")
    if len(task.encode("utf-8")) > MAX_LIFECYCLE_MESSAGE_BYTES:
        raise LifecycleRequestError("task is too large")
    template = _optional_identifier(value.get("template"), field_name="template")
    delegation_id = _optional_identifier(
        value.get("delegation_id"), field_name="delegation_id"
    )
    if template is None or delegation_id is None:
        raise LifecycleRequestError("template and delegation_id are required")
    return {
        "template": template,
        "task": task.strip(),
        "delegation_id": delegation_id,
        "user_id": _optional_identifier(value.get("user_id"), field_name="user_id"),
    }


def _optional_identifier(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise LifecycleRequestError(
            f"{field_name} must be a non-empty string of at most 512 characters"
        )
    return value.strip()


_PATH_IDENTIFIER_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._~-"
)


def _required_identifier(value: Any, *, field_name: str) -> str:
    parsed = _optional_identifier(value, field_name=field_name)
    if parsed is None or any(character not in _PATH_IDENTIFIER_CHARS for character in parsed):
        raise LifecycleRequestError(f"{field_name} must be a safe URL path identifier")
    return parsed


def _parse_event_limit(value: Any) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise LifecycleRequestError("limit must be an integer between 1 and 100")
    parsed = int(value)
    if not 1 <= parsed <= MAX_LIFECYCLE_EVENT_PAGE_SIZE:
        raise LifecycleRequestError("limit must be between 1 and 100")
    return parsed


def _parse_output_limit(value: Any) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise LifecycleRequestError("limit must be an integer between 1 and 50")
    parsed = int(value)
    if not 1 <= parsed <= MAX_LIFECYCLE_OUTPUT_PAGE_SIZE:
        raise LifecycleRequestError("limit must be between 1 and 50")
    return parsed


def _parse_bounded_int(
    value: Any,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise LifecycleRequestError(
            f"{field_name} must be an integer between {minimum} and {maximum}"
        )
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise LifecycleRequestError(
            f"{field_name} must be between {minimum} and {maximum}"
        )
    return parsed


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _harness_error_status(error: HarnessError) -> int:
    if error.code in {"RUN_NOT_FOUND", "HARNESS_NOT_FOUND"}:
        return 404
    if error.code == "EVENT_CURSOR_EXPIRED":
        return 410
    if error.category in {"authorization", "identity"}:
        return 401 if error.code == "LIFECYCLE_AUTHENTICATION_REQUIRED" else 403
    if "CONFLICT" in error.code or error.code in {"RUN_TERMINAL_IMMUTABLE"}:
        return 409
    if error.category in {"validation", "protocol"} or error.code.startswith("EVENT_CURSOR_"):
        return 400
    return 503 if error.retryable else 422


__all__ = [
    "LifecycleAuthorizationError",
    "LifecycleRequestError",
    "build_lifecycle_router",
]
