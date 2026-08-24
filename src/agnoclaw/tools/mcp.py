"""Deferred, async-first MCP 2026-07-28 client tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from agno.tools.toolkit import Toolkit

from ..runtime.errors import HarnessError
from ..runtime.guardrails import RuntimeGuardrails
from ..runtime.hooks import ToolCallRequest

_SERVER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_MAX_ARGUMENT_BYTES = 65_536
_MAX_SCHEMA_BYTES = 65_536
_MAX_TOOLS = 1_000
_MAX_PAGES = 100
_MAX_SERVERS = 32
_MAX_CATALOG_BYTES = 1_048_576
_MAX_SEARCH_RESULT_BYTES = 1_048_576
_MAX_RESULT_BYTES = 4_194_304
_MAX_JSON_DEPTH = 64


def _check_mcp() -> bool:
    """Return whether the supported stable MCP SDK is installed."""
    try:
        major = int(version("mcp").split(".", 1)[0])
    except (PackageNotFoundError, ValueError):
        return False
    return major == 2


def _mcp_dependency_error() -> HarnessError:
    return HarnessError(
        code="MCP_SDK_REQUIRED",
        category="dependency",
        message="MCP tools require the stable v2 SDK. Install agnoclaw[mcp].",
        retryable=False,
        details={"extra": "mcp", "supported_sdk": ">=2.0,<3"},
    )


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise HarnessError(
            code="MCP_JSON_VALUE_REQUIRED",
            category="validation",
            message="MCP arguments and results must be finite JSON-like values.",
            retryable=False,
        ) from exc


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical(value).encode()).hexdigest()}"


def _json_value(value: Any, *, _depth: int = 0) -> Any:
    if _depth > _MAX_JSON_DEPTH:
        raise HarnessError(
            code="MCP_JSON_BUDGET_EXCEEDED",
            category="mcp",
            message="MCP JSON exceeded the nesting budget.",
            retryable=False,
            details={"max_depth": _MAX_JSON_DEPTH},
        )
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item, _depth=_depth + 1)
            for key, item in value.items()
            if str(key) != "_meta"
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item, _depth=_depth + 1) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _field(value: Any, snake: str, camel: str, default: Any = None) -> Any:
    if hasattr(value, snake):
        return getattr(value, snake)
    if hasattr(value, camel):
        return getattr(value, camel)
    if isinstance(value, Mapping):
        return value.get(snake, value.get(camel, default))
    return default


@dataclass(frozen=True)
class MCPServerDefinition:
    """Content-minimized connection configuration for one named MCP server."""

    name: str
    command: tuple[str, ...] = ()
    url: str | None = None
    transport: str = "streamable_http"
    env: Mapping[str, str] | None = None
    headers: Mapping[str, str] | None = None

    @classmethod
    def parse(cls, value: Mapping[str, Any]) -> MCPServerDefinition:
        name = str(value.get("name", "mcp")).strip()
        if not _SERVER_NAME.fullmatch(name):
            raise HarnessError(
                code="MCP_SERVER_NAME_INVALID",
                category="configuration",
                message="MCP server names must be provider-safe and at most 64 characters.",
                retryable=False,
            )
        raw_command = value.get("command") or ()
        if isinstance(raw_command, str):
            raw_command = (raw_command,)
        if not isinstance(raw_command, (list, tuple)) or any(
            not isinstance(item, str) or not item for item in raw_command
        ):
            raise HarnessError(
                code="MCP_SERVER_COMMAND_INVALID",
                category="configuration",
                message="MCP command must be a non-empty string list.",
                retryable=False,
                details={"server": name},
            )
        command = tuple(raw_command)
        url = value.get("url")
        url = str(url).strip() if url is not None else None
        if bool(command) == bool(url):
            raise HarnessError(
                code="MCP_SERVER_TRANSPORT_INVALID",
                category="configuration",
                message="Each MCP server requires exactly one command or URL.",
                retryable=False,
                details={"server": name},
            )
        transport = str(value.get("transport", "streamable_http")).strip().lower()
        if command:
            transport = "stdio"
        if transport not in {"stdio", "streamable_http", "sse"}:
            raise HarnessError(
                code="MCP_SERVER_TRANSPORT_INVALID",
                category="configuration",
                message="MCP transport must be stdio, streamable_http, or legacy sse.",
                retryable=False,
                details={"server": name, "transport": transport},
            )
        env = cls._strings(value.get("env"), field="env", server=name)
        headers = cls._strings(value.get("headers"), field="headers", server=name)
        return cls(name, command, url, transport, env, headers)

    @staticmethod
    def _strings(value: Any, *, field: str, server: str) -> Mapping[str, str] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping) or any(
            not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
        ):
            raise HarnessError(
                code="MCP_SERVER_CONFIGURATION_INVALID",
                category="configuration",
                message=f"MCP {field} must be a string mapping.",
                retryable=False,
                details={"server": server, "field": field},
            )
        return dict(value)


def validate_mcp_server_urls(config: Any, *, workspace_dir: str) -> None:
    """Apply the harness network posture to configured remote MCP endpoints."""
    guardrails = RuntimeGuardrails(
        workspace_dir=workspace_dir,
        enabled=config.guardrails_enabled,
        path_enabled=False,
        network_enabled=config.network_enabled,
        network_enforce_https=config.network_enforce_https,
        network_allowed_hosts=config.network_allowed_hosts,
        network_blocked_hosts=config.network_blocked_hosts,
        network_block_private_hosts=config.network_block_private_hosts,
        network_block_in_bash=False,
    )
    for raw in config.mcp_servers:
        server = MCPServerDefinition.parse(raw)
        if server.url is None:
            continue
        violations = guardrails.check(
            ToolCallRequest(
                run_id="mcp-configuration",
                tool_name="mcp_connect",
                arguments={"url": server.url},
            )
        )
        if violations:
            raise HarnessError(
                code="MCP_SERVER_URL_DENIED",
                category="guardrail",
                message="Configured MCP endpoint violates the harness network posture.",
                retryable=False,
                details={
                    "server": server.name,
                    "violation_codes": tuple(item.code for item in violations),
                },
            )


class MCPToolkit(Toolkit):
    """Two-tool deferred MCP surface over the stable async v2 SDK.

    Remote schemas stay out of the base prompt. ``search_mcp_tools`` returns a bounded
    catalog and immutable schema digest; ``call_mcp_tool`` requires that digest and
    refreshes discovery before dispatch. Remote effect annotations are returned only as
    untrusted hints. Lifecycle calls remain conservatively non-repeatable.
    """

    def __init__(
        self,
        name: str = "mcp",
        command: list[str] | None = None,
        url: str | None = None,
        env: dict[str, str] | None = None,
        *,
        headers: dict[str, str] | None = None,
        transport: str = "streamable_http",
        servers: Sequence[Mapping[str, Any]] | None = None,
        client_factory: Callable[[MCPServerDefinition], Any] | None = None,
    ) -> None:
        super().__init__(name=name)
        raw_servers = list(servers or ())
        if not raw_servers:
            raw_servers = [
                {
                    "name": name,
                    "command": command,
                    "url": url,
                    "env": env,
                    "headers": headers,
                    "transport": transport,
                }
            ]
        definitions = tuple(MCPServerDefinition.parse(item) for item in raw_servers)
        if len(definitions) > _MAX_SERVERS:
            raise HarnessError(
                code="MCP_SERVER_BUDGET_EXCEEDED",
                category="configuration",
                message="Configured MCP servers exceed the harness budget.",
                retryable=False,
                details={"limit": _MAX_SERVERS},
            )
        if len({item.name for item in definitions}) != len(definitions):
            raise HarnessError(
                code="MCP_SERVER_NAME_CONFLICT",
                category="configuration",
                message="MCP server names must be unique within one harness.",
                retryable=False,
            )
        self._servers = {item.name: item for item in definitions}
        self._client_factory = client_factory
        self._contexts: dict[str, Any] = {}
        self._clients: dict[str, Any] = {}
        self._catalogs: dict[str, tuple[dict[str, Any], ...]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self.register(self.search_mcp_tools)
        self.register(self.call_mcp_tool)

    def _server(self, name: str) -> MCPServerDefinition:
        server = self._servers.get(str(name))
        if server is None:
            raise HarnessError(
                code="MCP_SERVER_NOT_FOUND",
                category="mcp",
                message="The requested MCP server is not configured.",
                retryable=False,
                details={"server": str(name), "available": tuple(sorted(self._servers))},
            )
        return server

    def _client_context(self, server: MCPServerDefinition):
        if self._client_factory is not None:
            return self._client_factory(server)
        if not _check_mcp():
            raise _mcp_dependency_error()
        from mcp import Client, StdioServerParameters

        if server.command:
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=server.command[0],
                args=list(server.command[1:]),
                env=dict(server.env or {}) or None,
            )
            return Client(stdio_client(params))
        if server.transport == "sse":
            from mcp.client.sse import sse_client

            if server.url is None:
                raise HarnessError(
                    code="MCP_SERVER_TRANSPORT_INVALID",
                    category="configuration",
                    message="A remote MCP transport requires a URL.",
                    retryable=False,
                    details={"server": server.name},
                )
            return Client(sse_client(server.url, headers=dict(server.headers or {}) or None))
        return self._http_client_context(server)

    @asynccontextmanager
    async def _http_client_context(self, server: MCPServerDefinition) -> AsyncIterator[Any]:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        if server.url is None:
            raise HarnessError(
                code="MCP_SERVER_TRANSPORT_INVALID",
                category="configuration",
                message="A remote MCP transport requires a URL.",
                retryable=False,
                details={"server": server.name},
            )
        async with httpx2.AsyncClient(
            headers=dict(server.headers or {}),
            follow_redirects=False,
            timeout=httpx2.Timeout(30.0, read=300.0),
        ) as http_client:
            transport = streamable_http_client(server.url, http_client=http_client)
            async with Client(transport) as client:
                yield client

    def _lifecycle_call(self) -> bool:
        try:
            from ..agent import get_current_run_context

            return get_current_run_context() is not None
        except ImportError:  # pragma: no cover - defensive import-cycle boundary
            return False

    async def _persistent_client(self, server: MCPServerDefinition) -> Any:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop and self._clients:
            raise HarnessError(
                code="MCP_LOOP_OWNERSHIP_VIOLATION",
                category="lifecycle",
                message="A connected MCP toolkit must stay on its owning event loop.",
                retryable=False,
                details={"server": server.name},
            )
        if self._loop is None:
            self._loop = loop
            self._lock = asyncio.Lock()
        if server.name in self._clients:
            return self._clients[server.name]
        assert self._lock is not None
        async with self._lock:
            if server.name not in self._clients:
                context = self._client_context(server)
                try:
                    client = await context.__aenter__()
                except Exception as exc:
                    raise self._connection_error(server, exc) from exc
                self._contexts[server.name] = context
                self._clients[server.name] = client
        return self._clients[server.name]

    @asynccontextmanager
    async def _client(
        self,
        server: MCPServerDefinition,
        *,
        persistent: bool | None = None,
    ) -> AsyncIterator[Any]:
        if self._lifecycle_call() if persistent is None else persistent:
            yield await self._persistent_client(server)
            return
        try:
            context = self._client_context(server)
            client = await context.__aenter__()
        except HarnessError:
            raise
        except Exception as exc:
            raise self._connection_error(server, exc) from exc
        try:
            yield client
        except BaseException as exc:
            try:
                suppressed = await context.__aexit__(type(exc), exc, exc.__traceback__)
            except Exception as close_exc:
                raise self._close_error((type(close_exc).__name__,)) from close_exc
            if not suppressed:
                raise
        else:
            try:
                await context.__aexit__(None, None, None)
            except Exception as exc:
                raise self._close_error((type(exc).__name__,)) from exc

    @staticmethod
    def _connection_error(server: MCPServerDefinition, exc: BaseException) -> HarnessError:
        return HarnessError(
            code="MCP_CONNECTION_FAILED",
            category="mcp",
            message="The MCP server connection failed.",
            retryable=True,
            details={"server": server.name, "exception_type": type(exc).__name__},
        )

    @staticmethod
    def _close_error(exception_types: tuple[str, ...]) -> HarnessError:
        return HarnessError(
            code="MCP_CLOSE_FAILED",
            category="lifecycle",
            message="One or more MCP clients failed to close.",
            retryable=False,
            details={
                "failure_count": len(exception_types),
                "exception_types": exception_types,
            },
        )

    async def _catalog(
        self,
        server: MCPServerDefinition,
        *,
        refresh: bool,
        persistent: bool | None = None,
        client: Any | None = None,
    ) -> tuple[dict[str, Any], ...]:
        if client is None:
            async with self._client(server, persistent=persistent) as active:
                return await self._catalog(server, refresh=refresh, client=active)
        tools: list[dict[str, Any]] = []
        names: set[str] = set()
        catalog_bytes = 0
        cursor: str | None = None
        for _page in range(_MAX_PAGES):
            kwargs = {"cursor": cursor}
            if refresh:
                kwargs["cache_mode"] = "refresh"
            try:
                result = await client.list_tools(**kwargs)
            except HarnessError:
                raise
            except Exception as exc:
                raise HarnessError(
                    code="MCP_DISCOVERY_FAILED",
                    category="mcp",
                    message="MCP tool discovery failed before a typed result was returned.",
                    retryable=True,
                    details={"server": server.name, "exception_type": type(exc).__name__},
                ) from exc
            for tool in result.tools:
                name = str(_field(tool, "name", "name", "")).strip()
                if not name or len(name) > 256 or any(ord(char) < 32 for char in name):
                    raise HarnessError(
                        code="MCP_TOOL_NAME_INVALID",
                        category="mcp",
                        message="An MCP server advertised an invalid tool name.",
                        retryable=False,
                        details={"server": server.name},
                    )
                if name in names:
                    raise HarnessError(
                        code="MCP_TOOL_NAME_CONFLICT",
                        category="mcp",
                        message="An MCP server advertised a duplicate tool name.",
                        retryable=False,
                        details={"server": server.name, "tool_name": name},
                    )
                names.add(name)
                input_schema = _json_value(_field(tool, "input_schema", "inputSchema", {}))
                output_schema = _json_value(_field(tool, "output_schema", "outputSchema", None))
                if any(
                    len(_canonical(schema).encode()) > _MAX_SCHEMA_BYTES
                    for schema in (input_schema, output_schema)
                ):
                    raise HarnessError(
                        code="MCP_SCHEMA_BUDGET_EXCEEDED",
                        category="mcp",
                        message="An MCP tool schema exceeds the per-tool budget.",
                        retryable=False,
                        details={"server": server.name, "tool_name": name},
                    )
                title = _field(tool, "title", "title")
                entry = {
                    "server": server.name,
                    "name": name,
                    "title": str(title)[:256] if title is not None else None,
                    "description": str(_field(tool, "description", "description", ""))[:1024],
                    "input_schema": input_schema,
                    "output_schema": output_schema,
                    "annotations": _json_value(_field(tool, "annotations", "annotations")),
                    "annotation_trust": "untrusted_hint",
                }
                entry["tool_digest"] = _digest([server.name, name, input_schema, output_schema])
                catalog_bytes += len(_canonical(entry).encode())
                if catalog_bytes > _MAX_CATALOG_BYTES:
                    raise HarnessError(
                        code="MCP_CATALOG_BUDGET_EXCEEDED",
                        category="mcp",
                        message="MCP discovery exceeded the catalog byte budget.",
                        retryable=False,
                        details={"server": server.name, "limit": _MAX_CATALOG_BYTES},
                    )
                tools.append(entry)
                if len(tools) > _MAX_TOOLS:
                    raise HarnessError(
                        code="MCP_TOOL_BUDGET_EXCEEDED",
                        category="mcp",
                        message="MCP discovery exceeded the server tool budget.",
                        retryable=False,
                        details={"server": server.name, "limit": _MAX_TOOLS},
                    )
            cursor = _field(result, "next_cursor", "nextCursor")
            if not cursor:
                break
        else:
            raise HarnessError(
                code="MCP_PAGINATION_BUDGET_EXCEEDED",
                category="mcp",
                message="MCP discovery exceeded the page budget.",
                retryable=False,
                details={"server": server.name, "limit": _MAX_PAGES},
            )
        protocol = str(getattr(client, "protocol_version", "unknown"))[:64]
        server_info = _json_value(getattr(client, "server_info", None))
        if len(_canonical(server_info).encode()) > 8_192:
            raise HarnessError(
                code="MCP_SERVER_INFO_BUDGET_EXCEEDED",
                category="mcp",
                message="MCP server metadata exceeds the byte budget.",
                retryable=False,
                details={"server": server.name, "limit": 8_192},
            )
        catalog = tuple(
            {**item, "protocol_version": protocol, "server_info": server_info}
            for item in sorted(tools, key=lambda value: value["name"])
        )
        self._catalogs[server.name] = catalog
        return catalog

    async def search_mcp_tools(
        self,
        query: str = "",
        server: str | None = None,
        limit: int = 20,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Search bounded remote tool metadata; returned annotations are untrusted hints."""
        if not isinstance(query, str) or len(query) > 1_024:
            raise HarnessError(
                code="MCP_SEARCH_QUERY_INVALID",
                category="validation",
                message="MCP search query must be a string of at most 1,024 characters.",
                retryable=False,
            )
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise HarnessError(
                code="MCP_SEARCH_LIMIT_INVALID",
                category="validation",
                message="MCP search limit must be between 1 and 100.",
                retryable=False,
            )
        if not isinstance(refresh, bool):
            raise HarnessError(
                code="MCP_SEARCH_REFRESH_INVALID",
                category="validation",
                message="MCP search refresh must be a boolean.",
                retryable=False,
            )
        selected = (self._server(server),) if server else tuple(self._servers.values())
        outcomes = await asyncio.gather(
            *(self._catalog(item, refresh=refresh) for item in selected),
            return_exceptions=server is None,
        )
        catalogs: list[tuple[dict[str, Any], ...]] = []
        server_errors: list[dict[str, Any]] = []
        for definition, outcome in zip(selected, outcomes, strict=True):
            if not isinstance(outcome, BaseException):
                catalogs.append(outcome)
                continue
            if server is not None:
                raise outcome
            if isinstance(outcome, HarnessError):
                server_errors.append(
                    {
                        "server": definition.name,
                        "code": outcome.code,
                        "retryable": outcome.retryable,
                    }
                )
            else:  # pragma: no cover - _catalog owns its third-party failure boundary
                server_errors.append(
                    {
                        "server": definition.name,
                        "code": "MCP_DISCOVERY_FAILED",
                        "retryable": True,
                    }
                )
        tokens = tuple(token.lower() for token in re.findall(r"[A-Za-z0-9]+", query))

        def score(item: dict[str, Any]) -> int:
            haystack = " ".join(
                (item["server"], item["name"], item["description"], str(item["title"] or ""))
            ).lower()
            return sum(1 for token in tokens if token in haystack) if tokens else 1

        matches = [item for catalog in catalogs for item in catalog if score(item)]
        matches.sort(key=lambda item: (-score(item), item["server"], item["name"]))
        returned: list[dict[str, Any]] = []
        returned_bytes = 0
        for item in matches[:limit]:
            item_bytes = len(_canonical(item).encode())
            if returned_bytes + item_bytes > _MAX_SEARCH_RESULT_BYTES:
                break
            returned.append(item)
            returned_bytes += item_bytes
        return {
            "query": query,
            "tools": returned,
            "returned": len(returned),
            "matched": len(matches),
            "budget_truncated": len(returned) < min(len(matches), limit),
            "server_errors": server_errors,
            "instruction": (
                "Pass the exact server, name, arguments, and tool_digest to call_mcp_tool."
            ),
        }

    async def call_mcp_tool(
        self,
        server: str,
        tool_name: str,
        arguments: dict[str, Any],
        tool_digest: str,
    ) -> dict[str, Any]:
        """Call one freshly verified remote tool through conservative effect governance."""
        definition = self._server(server)
        if not isinstance(tool_name, str) or not isinstance(tool_digest, str):
            raise HarnessError(
                code="MCP_TOOL_SELECTION_INVALID",
                category="validation",
                message="MCP tool name and digest must be strings returned by search.",
                retryable=False,
            )
        if not isinstance(arguments, dict):
            raise HarnessError(
                code="MCP_ARGUMENTS_INVALID",
                category="validation",
                message="MCP tool arguments must be an object.",
                retryable=False,
            )
        argument_bytes = len(_canonical(arguments).encode())
        if argument_bytes > _MAX_ARGUMENT_BYTES:
            raise HarnessError(
                code="MCP_ARGUMENT_BUDGET_EXCEEDED",
                category="validation",
                message="MCP tool arguments exceed the byte budget.",
                retryable=False,
                details={"argument_bytes": argument_bytes, "limit": _MAX_ARGUMENT_BYTES},
            )
        async with self._client(definition) as client:
            catalog = await self._catalog(definition, refresh=True, client=client)
            match = next((item for item in catalog if item["name"] == tool_name), None)
            if match is None:
                raise HarnessError(
                    code="MCP_TOOL_NOT_FOUND",
                    category="mcp",
                    message="The requested MCP tool is no longer advertised.",
                    retryable=False,
                    details={"server": server, "tool_name": tool_name},
                )
            if tool_digest != match["tool_digest"]:
                raise HarnessError(
                    code="MCP_TOOL_DRIFT",
                    category="mcp",
                    message="The MCP tool schema changed after selection; search again.",
                    retryable=False,
                    details={"server": server, "tool_name": tool_name},
                )
            try:
                result = await client.call_tool(tool_name, arguments)
            except HarnessError:
                raise
            except Exception as exc:
                raise HarnessError(
                    code="MCP_CALL_FAILED",
                    category="mcp",
                    message="The MCP tool call failed before a typed result was returned.",
                    retryable=True,
                    details={
                        "server": server,
                        "tool_name": tool_name,
                        "exception_type": type(exc).__name__,
                    },
                ) from exc
        payload = {
            "server": server,
            "tool_name": tool_name,
            "tool_digest": tool_digest,
            "is_error": bool(_field(result, "is_error", "isError", False)),
            "content": _json_value(_field(result, "content", "content", [])),
            "structured_content": _json_value(
                _field(result, "structured_content", "structuredContent")
            ),
            "private_metadata_withheld": bool(
                _field(result, "meta", "_meta") or _field(result, "_meta", "_meta")
            ),
        }
        result_bytes = len(_canonical(payload).encode())
        if result_bytes > _MAX_RESULT_BYTES:
            raise HarnessError(
                code="MCP_RESULT_BUDGET_EXCEEDED",
                category="mcp",
                message="MCP tool result exceeds the byte budget.",
                retryable=False,
                details={"server": server, "tool_name": tool_name, "limit": _MAX_RESULT_BYTES},
            )
        return payload

    async def aconnect(self) -> list[str]:
        """Explicitly connect all servers and return their namespaced tool names."""
        names: list[str] = []
        for server in self._servers.values():
            await self._persistent_client(server)
            names.extend(
                f"{server.name}:{item['name']}"
                for item in await self._catalog(server, refresh=True, persistent=True)
            )
        return names

    def connect(self) -> list[str]:  # type: ignore[override]
        """Connect from synchronous code; async hosts must await ``aconnect``."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aconnect())
        raise HarnessError(
            code="MCP_ASYNC_CONNECT_REQUIRED",
            category="lifecycle",
            message="Await MCPToolkit.aconnect() from a running event loop.",
            retryable=False,
        )

    @property
    def tool_schemas(self) -> list[dict[str, Any]]:
        """Return the last discovered, content-safe catalog snapshot."""
        return [dict(item) for name in sorted(self._catalogs) for item in self._catalogs[name]]

    @property
    def connected(self) -> bool:
        return bool(self._clients)

    async def aclose(self) -> None:
        """Close every persistent client on its owning event loop, in reverse order."""
        if self._clients and self._loop is not asyncio.get_running_loop():
            raise HarnessError(
                code="MCP_LOOP_OWNERSHIP_VIOLATION",
                category="lifecycle",
                message="MCP clients must close on their owning event loop.",
                retryable=False,
            )
        failures: list[str] = []
        for name in reversed(tuple(self._contexts)):
            try:
                await self._contexts[name].__aexit__(None, None, None)
            except Exception as exc:  # pragma: no cover - SDK/transport-specific cleanup
                failures.append(type(exc).__name__)
        self._contexts.clear()
        self._clients.clear()
        self._loop = None
        self._lock = None
        if failures:
            raise self._close_error(tuple(failures))

    def close(self) -> None:
        """Close from synchronous code when no event loop owns live clients."""
        if self._clients:
            raise HarnessError(
                code="MCP_ASYNC_CLOSE_REQUIRED",
                category="lifecycle",
                message="Connected MCP clients require await toolkit.aclose().",
                retryable=False,
            )


__all__ = ["MCPServerDefinition", "MCPToolkit", "validate_mcp_server_urls"]
