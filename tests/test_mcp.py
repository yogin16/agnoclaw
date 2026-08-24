"""MCP 2026-07-28 deferred-disclosure and lifecycle contracts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agnoclaw import AgentHarness, HarnessConfig, HarnessError
from agnoclaw.runtime.guardrails import RuntimeGuardrails
from agnoclaw.runtime.hooks import ToolCallRequest
from agnoclaw.runtime.tool_ingress import (
    builtin_effect,
    declare_builtin_effects,
    toolkit_functions,
)
from agnoclaw.tools.mcp import MCPToolkit, _check_mcp


class FakeClient:
    def __init__(self) -> None:
        self.schema = {"type": "object", "properties": {"query": {"type": "string"}}}
        self.output_schema = {"type": "object"}
        self.protocol_version = "2026-07-28"
        self.server_info = SimpleNamespace(name="catalog", version="1.0")
        self.calls: list[tuple[str, dict]] = []
        self.list_error: Exception | None = None
        self.call_error: Exception | None = None

    async def list_tools(self, **_kwargs):
        if self.list_error is not None:
            raise self.list_error
        tool = SimpleNamespace(
            name="lookup",
            title="Lookup",
            description="Search the catalog",
            input_schema=self.schema,
            output_schema=self.output_schema,
            annotations={"readOnlyHint": True},
        )
        return SimpleNamespace(tools=[tool], next_cursor=None)

    async def call_tool(self, name, arguments):
        if self.call_error is not None:
            raise self.call_error
        self.calls.append((name, arguments))
        return SimpleNamespace(
            is_error=False,
            content=[{"type": "text", "text": "found", "_meta": {"secret": "no"}}],
            structured_content={"result": "found"},
            meta={"private": "withheld"},
        )


class FakeContext:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.enters = 0
        self.exits = 0

    async def __aenter__(self):
        self.enters += 1
        return self.client

    async def __aexit__(self, *_args):
        self.exits += 1


def _toolkit(client: FakeClient | None = None):
    active = client or FakeClient()
    contexts: list[FakeContext] = []

    def factory(_server):
        context = FakeContext(active)
        contexts.append(context)
        return context

    toolkit = MCPToolkit(name="catalog", command=["unused"], client_factory=factory)
    declare_builtin_effects([toolkit])
    return toolkit, active, contexts


def test_mcp_toolkit_has_two_stable_deferred_tools() -> None:
    toolkit, _, _ = _toolkit()

    functions = toolkit_functions(toolkit)
    assert set(functions) == {"search_mcp_tools", "call_mcp_tool"}
    assert builtin_effect(functions["search_mcp_tools"]).effect_class.value == "read_only"
    assert builtin_effect(functions["call_mcp_tool"]).effect_class.value == ("non_repeatable")


def test_mcp_check_reports_supported_optional_sdk_state() -> None:
    assert isinstance(_check_mcp(), bool)


def test_mcp_server_requires_exactly_one_transport() -> None:
    with pytest.raises(HarnessError) as missing:
        MCPToolkit(name="missing")
    assert missing.value.code == "MCP_SERVER_TRANSPORT_INVALID"

    with pytest.raises(HarnessError) as duplicate:
        MCPToolkit(name="duplicate", command=["cmd"], url="https://example.com/mcp")
    assert duplicate.value.code == "MCP_SERVER_TRANSPORT_INVALID"

    with pytest.raises(HarnessError) as server_budget:
        MCPToolkit(
            servers=[{"name": f"server-{index}", "command": ["unused"]} for index in range(33)]
        )
    assert server_budget.value.code == "MCP_SERVER_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_search_then_digest_bound_call_preserves_structured_content() -> None:
    toolkit, client, contexts = _toolkit()

    search = await toolkit.search_mcp_tools("catalog")
    selected = search["tools"][0]
    result = await toolkit.call_mcp_tool(
        "catalog",
        "lookup",
        {"query": "dune"},
        selected["tool_digest"],
    )

    assert selected["protocol_version"] == "2026-07-28"
    assert selected["annotation_trust"] == "untrusted_hint"
    assert result["structured_content"] == {"result": "found"}
    assert result["content"] == [{"type": "text", "text": "found"}]
    assert result["private_metadata_withheld"] is True
    assert client.calls == [("lookup", {"query": "dune"})]
    # Initial search opens one client; refresh and call share a second client so a
    # direct stdio server cannot change process between selection check and dispatch.
    assert sum(context.enters for context in contexts) == 2
    assert sum(context.exits for context in contexts) == 2


@pytest.mark.asyncio
async def test_nested_tool_name_argument_cannot_override_digest_bound_selection() -> None:
    toolkit, client, _ = _toolkit()
    selected = (await toolkit.search_mcp_tools("catalog"))["tools"][0]

    await toolkit.call_mcp_tool(
        "catalog",
        "lookup",
        {"query": "dune", "tool_name": "delete_repo"},
        selected["tool_digest"],
    )

    assert client.calls == [
        ("lookup", {"query": "dune", "tool_name": "delete_repo"}),
    ]


@pytest.mark.asyncio
async def test_transport_discovery_and_call_failures_keep_distinct_error_codes() -> None:
    toolkit, client, _ = _toolkit()
    client.list_error = TimeoutError("private discovery detail")
    with pytest.raises(HarnessError) as discovery:
        await toolkit.search_mcp_tools(server="catalog")
    assert discovery.value.code == "MCP_DISCOVERY_FAILED"
    assert discovery.value.details == {
        "server": "catalog",
        "exception_type": "TimeoutError",
    }

    client.list_error = None
    selected = (await toolkit.search_mcp_tools())["tools"][0]
    client.call_error = ConnectionError("private call detail")
    with pytest.raises(HarnessError) as call:
        await toolkit.call_mcp_tool(
            "catalog",
            "lookup",
            {"query": "dune"},
            selected["tool_digest"],
        )
    assert call.value.code == "MCP_CALL_FAILED"
    assert call.value.details == {
        "server": "catalog",
        "tool_name": "lookup",
        "exception_type": "ConnectionError",
    }


@pytest.mark.asyncio
async def test_transport_entry_failure_is_content_minimized_connection_error() -> None:
    class BrokenContext:
        async def __aenter__(self):
            raise OSError("private endpoint detail")

        async def __aexit__(self, *_args):
            return None

    toolkit = MCPToolkit(
        name="broken",
        command=["unused"],
        client_factory=lambda _server: BrokenContext(),
    )

    with pytest.raises(HarnessError) as caught:
        await toolkit.search_mcp_tools(server="broken")

    assert caught.value.code == "MCP_CONNECTION_FAILED"
    assert caught.value.details == {"server": "broken", "exception_type": "OSError"}


@pytest.mark.asyncio
async def test_multi_server_search_isolates_one_server_failure() -> None:
    class BrokenContext:
        async def __aenter__(self):
            raise OSError("private endpoint detail")

        async def __aexit__(self, *_args):
            return None

    client = FakeClient()

    def factory(server):
        return BrokenContext() if server.name == "broken" else FakeContext(client)

    toolkit = MCPToolkit(
        servers=[
            {"name": "healthy", "command": ["unused"]},
            {"name": "broken", "command": ["unused"]},
        ],
        client_factory=factory,
    )
    search = await toolkit.search_mcp_tools()

    assert [item["server"] for item in search["tools"]] == ["healthy"]
    assert search["server_errors"] == [
        {"server": "broken", "code": "MCP_CONNECTION_FAILED", "retryable": True}
    ]

    with pytest.raises(HarnessError) as selected:
        await toolkit.search_mcp_tools(server="broken")
    assert selected.value.code == "MCP_CONNECTION_FAILED"


@pytest.mark.asyncio
async def test_untrusted_catalog_and_result_budgets_fail_closed(monkeypatch) -> None:
    import agnoclaw.tools.mcp as mcp_module

    client = FakeClient()

    class DuplicateClient(FakeClient):
        async def list_tools(self, **kwargs):
            result = await super().list_tools(**kwargs)
            result.tools.append(result.tools[0])
            return result

    duplicate_toolkit, _, _ = _toolkit(DuplicateClient())
    with pytest.raises(HarnessError) as duplicate:
        await duplicate_toolkit.search_mcp_tools(server="catalog")
    assert duplicate.value.code == "MCP_TOOL_NAME_CONFLICT"

    toolkit, _, _ = _toolkit(client)
    client.output_schema = {"const": "x" * 65_536}
    with pytest.raises(HarnessError) as schema:
        await toolkit.search_mcp_tools(server="catalog")
    assert schema.value.code == "MCP_SCHEMA_BUDGET_EXCEEDED"

    client.output_schema = {"type": "object"}
    monkeypatch.setattr(mcp_module, "_MAX_CATALOG_BYTES", 128)
    with pytest.raises(HarnessError) as catalog:
        await toolkit.search_mcp_tools(server="catalog")
    assert catalog.value.code == "MCP_CATALOG_BUDGET_EXCEEDED"

    monkeypatch.setattr(mcp_module, "_MAX_CATALOG_BYTES", 1_048_576)
    selected = (await toolkit.search_mcp_tools())["tools"][0]
    monkeypatch.setattr(mcp_module, "_MAX_RESULT_BYTES", 128)
    with pytest.raises(HarnessError) as result:
        await toolkit.call_mcp_tool(
            "catalog",
            "lookup",
            {"query": "dune"},
            selected["tool_digest"],
        )
    assert result.value.code == "MCP_RESULT_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_search_control_arguments_are_strict() -> None:
    toolkit, _, _ = _toolkit()

    with pytest.raises(HarnessError) as query:
        await toolkit.search_mcp_tools(1)  # type: ignore[arg-type]
    assert query.value.code == "MCP_SEARCH_QUERY_INVALID"

    with pytest.raises(HarnessError) as refresh:
        await toolkit.search_mcp_tools(refresh="false")  # type: ignore[arg-type]
    assert refresh.value.code == "MCP_SEARCH_REFRESH_INVALID"


@pytest.mark.asyncio
async def test_schema_drift_fails_before_remote_dispatch() -> None:
    toolkit, client, _ = _toolkit()
    selected = (await toolkit.search_mcp_tools())["tools"][0]
    client.schema = {"type": "object", "properties": {"sku": {"type": "string"}}}

    with pytest.raises(HarnessError) as caught:
        await toolkit.call_mcp_tool(
            "catalog",
            "lookup",
            {"query": "dune"},
            selected["tool_digest"],
        )

    assert caught.value.code == "MCP_TOOL_DRIFT"
    assert client.calls == []


@pytest.mark.asyncio
async def test_lifecycle_calls_reuse_one_loop_owned_client_and_close_once() -> None:
    toolkit, client, contexts = _toolkit()
    with patch.object(toolkit, "_lifecycle_call", return_value=True):
        selected = (await toolkit.search_mcp_tools())["tools"][0]
        await toolkit.call_mcp_tool(
            "catalog",
            "lookup",
            {"query": "dune"},
            selected["tool_digest"],
        )

    assert toolkit.connected is True
    assert len(contexts) == 1
    assert contexts[0].enters == 1
    await toolkit.aclose()
    assert contexts[0].exits == 1
    assert toolkit.connected is False
    assert client.calls


@pytest.mark.asyncio
async def test_sync_connect_fails_cleanly_inside_running_loop() -> None:
    toolkit, _, _ = _toolkit()

    with pytest.raises(HarnessError) as caught:
        toolkit.connect()

    assert caught.value.code == "MCP_ASYNC_CONNECT_REQUIRED"


def test_remote_mcp_url_uses_harness_network_posture(tmp_path) -> None:
    denied = HarnessConfig(
        enable_plugins=False,
        mcp_servers=[{"name": "local", "url": "http://127.0.0.1:8000/mcp"}],
    )
    with patch("agnoclaw.agent.Agent", MagicMock()):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            with pytest.raises(HarnessError) as caught:
                AgentHarness(
                    model="model",
                    provider="custom",
                    workspace_dir=tmp_path / "denied",
                    config=denied,
                )
    assert caught.value.code == "MCP_SERVER_URL_DENIED"
    assert set(caught.value.details["violation_codes"]) == {
        "NETWORK_HTTPS_REQUIRED",
        "NETWORK_PRIVATE_HOST_BLOCKED",
    }


def test_mcp_search_and_call_fail_closed_when_network_is_disabled(tmp_path) -> None:
    guardrails = RuntimeGuardrails(workspace_dir=tmp_path, network_enabled=False)

    for tool_name in ("search_mcp_tools", "call_mcp_tool"):
        violations = guardrails.check(
            ToolCallRequest(run_id="run-1", tool_name=tool_name, arguments={})
        )
        assert [item.code for item in violations] == ["NETWORK_DISABLED"]


def test_configured_mcp_is_governed_not_an_empty_dynamic_bypass(tmp_path) -> None:
    config = HarnessConfig(
        enable_plugins=False,
        mcp_servers=[{"name": "remote", "command": ["unused"]}],
    )
    with patch("agnoclaw.agent.Agent", MagicMock()):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path,
                config=config,
            )

    registry = harness.admin_harness_capabilities()["registry"]
    assert registry["extension_compatibility_tools"] == []
    tool_names = harness._tool_names(list(harness._agent_blueprint["tools"]))
    assert {"search_mcp_tools", "call_mcp_tool"}.issubset(tool_names)


@pytest.mark.asyncio
@pytest.mark.skipif(not _check_mcp(), reason="agnoclaw[mcp] is not installed")
async def test_real_v2_sdk_in_memory_negotiates_latest_protocol() -> None:
    from mcp import Client
    from mcp.server import MCPServer

    server = MCPServer("in-memory")

    @server.tool()
    def echo(value: str) -> dict[str, str]:
        return {"value": value}

    toolkit = MCPToolkit(
        name="memory",
        command=["unused"],
        client_factory=lambda _definition: Client(server),
    )
    selected = (await toolkit.search_mcp_tools("echo"))["tools"][0]
    result = await toolkit.call_mcp_tool(
        "memory",
        "echo",
        {"value": "ok"},
        selected["tool_digest"],
    )

    assert selected["protocol_version"] == "2026-07-28"
    assert result["structured_content"] == {"value": "ok"}
