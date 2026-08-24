"""First-party lifecycle tools cross the same durable effect authority."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from agno.tools.function import Function, FunctionCall
from agno.tools.toolkit import Toolkit

from agnoclaw import AgentHarness, HarnessConfig
from agnoclaw.runtime import (
    HarnessError,
    LocalArtifactStore,
    PolicyAction,
    PolicyDecision,
    RedactionRule,
    RunReconciliationRequiredError,
    SQLiteRuntimeStore,
)
from agnoclaw.runtime.tool_ingress import (
    activate_builtin_ingress,
    builtin_effect,
    declare_builtin_effects,
    toolkit_functions,
)
from agnoclaw.tools.backends import LocalWorkspaceAdapter


class BuiltinCallingAgent:
    def __init__(self, **kwargs):
        self.system_message = kwargs["system_message"]
        self.session_id = kwargs.get("session_id")
        self.user_id = kwargs.get("user_id")
        self.tools = list(kwargs.get("tools") or [])

    def _function(self, name: str) -> Function:
        for tool in self.tools:
            if isinstance(tool, Function) and tool.name == name:
                return tool
            if isinstance(tool, Toolkit) and name in toolkit_functions(tool):
                return toolkit_functions(tool)[name]
        raise AssertionError(f"missing tool {name}")

    async def arun(self, message, **kwargs):
        function = self._function("read_file")
        result = None
        for _ in range(2):
            call = FunctionCall(
                function=function,
                arguments={"path": message},
                call_id="call-builtin-read-1",
            )
            run_context = SimpleNamespace(
                run_id=kwargs["run_id"],
                metadata=kwargs["metadata"],
                user_id=kwargs.get("user_id"),
                session_id=kwargs.get("session_id"),
            )
            function.pre_hook(agent=self, run_context=run_context, fc=call)
            try:
                result = await function.entrypoint(**dict(call.arguments or {}))
                call.result = result
            except Exception as exc:
                call.error = str(exc)
                raise
            finally:
                function.post_hook(agent=self, run_context=run_context, fc=call)
        return SimpleNamespace(content=result)


class BuiltinMutatingAgent(BuiltinCallingAgent):
    async def arun(self, message, **kwargs):
        function = self._function("write_file")
        call = FunctionCall(
            function=function,
            arguments={"path": message, "content": "committed"},
            call_id="call-builtin-write-1",
        )
        run_context = SimpleNamespace(
            run_id=kwargs["run_id"],
            metadata=kwargs["metadata"],
            user_id=kwargs.get("user_id"),
            session_id=kwargs.get("session_id"),
        )
        function.pre_hook(agent=self, run_context=run_context, fc=call)
        try:
            result = await function.entrypoint(**dict(call.arguments or {}))
            call.result = result
            return SimpleNamespace(content=result)
        finally:
            function.post_hook(agent=self, run_context=run_context, fc=call)


class MCPCallingAgent(BuiltinCallingAgent):
    async def _call(self, name, arguments, call_id, kwargs):
        function = self._function(name)
        call = FunctionCall(function=function, arguments=arguments, call_id=call_id)
        run_context = SimpleNamespace(
            run_id=kwargs["run_id"],
            metadata=kwargs["metadata"],
            user_id=kwargs.get("user_id"),
            session_id=kwargs.get("session_id"),
        )
        function.pre_hook(agent=self, run_context=run_context, fc=call)
        try:
            result = await function.entrypoint(**dict(call.arguments or {}))
            call.result = result
            return result
        finally:
            function.post_hook(agent=self, run_context=run_context, fc=call)

    async def arun(self, _message, **kwargs):
        catalog = await self._call(
            "search_mcp_tools",
            {"query": "lookup", "server": "catalog"},
            "call-mcp-search-1",
            kwargs,
        )
        selected = catalog["tools"][0]
        result = await self._call(
            "call_mcp_tool",
            {
                "server": "catalog",
                "tool_name": "lookup",
                "arguments": {"query": "dune"},
                "tool_digest": selected["tool_digest"],
            },
            "call-mcp-tool-1",
            kwargs,
        )
        return SimpleNamespace(content=result)


class _MCPClient:
    protocol_version = "2026-07-28"
    server_info = SimpleNamespace(name="catalog", version="1")

    def __init__(self):
        self.calls = []

    async def list_tools(self, **_kwargs):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="lookup",
                    title="Lookup",
                    description="Lookup",
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                    annotations=None,
                )
            ],
            next_cursor=None,
        )

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(
            is_error=False,
            content=[{"type": "text", "text": "found"}],
            structured_content={"result": "found"},
            meta=None,
        )


class _MCPContext:
    def __init__(self, client):
        self.client = client
        self.exits = 0

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *_args):
        self.exits += 1


class RedactingPolicy:
    policy_version = "builtin-redaction-v1"

    def __init__(self):
        self.after_calls = 0

    def before_run(self, _value, _context):
        return PolicyDecision.allow()

    def before_prompt_send(self, _value, _context):
        return PolicyDecision.allow()

    def before_skill_load(self, _value, _context):
        return PolicyDecision.allow()

    def before_tool_call(self, _value, _context):
        return PolicyDecision.allow()

    def after_tool_call(self, _value, _context):
        self.after_calls += 1
        return PolicyDecision(
            action=PolicyAction.ALLOW_WITH_REDACTION,
            reason_code="MASK_TOOL_SECRET",
            redactions=(RedactionRule(target="secret", replacement="[MASKED]"),),
        )


class ConstrainingPolicy(RedactingPolicy):
    policy_version = "builtin-constraint-v1"

    def before_tool_call(self, _value, _context):
        return PolicyDecision(
            action=PolicyAction.ALLOW_WITH_CONSTRAINTS,
            reason_code="REWRITE_PATH",
            constraints={"arguments": {"path": "/untrusted/replacement"}},
        )

    def after_tool_call(self, _value, _context):
        return PolicyDecision.allow()


def test_default_builtin_effects_are_declared_and_unknown_additions_fail_closed(tmp_path):
    from agnoclaw.tools import get_default_tools

    tools = get_default_tools(HarnessConfig.quick(), workspace_dir=tmp_path)
    declared = {
        function.name: builtin_effect(function).effect_class.value
        for tool in tools
        for function in (
            [tool] if isinstance(tool, Function) else list(toolkit_functions(tool).values())
        )
    }

    assert declared["read_file"] == "read_only"
    assert declared["write_file"] == "non_repeatable"
    assert declared["bash"] == "non_repeatable"
    assert declared["web_fetch"] == "read_only"
    with pytest.raises(HarnessError) as caught:
        declare_builtin_effects([Function(name="future_builtin", entrypoint=lambda: None)])
    assert caught.value.code == "BUILTIN_EFFECT_UNCLASSIFIED"


def test_lifecycle_builtin_rejects_agno_result_cache_before_dispatch() -> None:
    function = Function(
        name="read_file",
        entrypoint=lambda **_kwargs: "must-not-run",
        cache_results=True,
    )
    declare_builtin_effects([function])
    harness = SimpleNamespace(
        _active_runtime_run_id=SimpleNamespace(get=lambda: "run-1"),
    )

    with pytest.raises(HarnessError) as caught:
        activate_builtin_ingress(
            function,
            fc=SimpleNamespace(),
            harness=harness,
        )

    assert caught.value.code == "BUILTIN_AGNO_CACHE_UNSUPPORTED"
    assert caught.value.details == {"tool_name": "read_file"}


@pytest.mark.asyncio
async def test_deferred_mcp_call_crosses_first_party_effect_gateway(tmp_path):
    from agnoclaw.tools.mcp import MCPToolkit

    client = _MCPClient()
    contexts = []

    def factory(_server):
        context = _MCPContext(client)
        contexts.append(context)
        return context

    mcp_toolkit = MCPToolkit(
        name="catalog",
        command=["unused"],
        client_factory=factory,
    )
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    config = HarnessConfig.quick(
        enable_plugins=False,
        mcp_servers=[{"name": "catalog", "command": ["unused"]}],
    )
    with patch("agnoclaw.tools.mcp.MCPToolkit", return_value=mcp_toolkit):
        with patch("agnoclaw.agent.Agent", MCPCallingAgent):
            with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
                harness = AgentHarness(
                    model="model",
                    provider="custom",
                    workspace_dir=tmp_path / "workspace",
                    config=config,
                    runtime_store=store,
                    permission_mode="bypass",
                    tenant_id="tenant-1",
                    user_id="user-1",
                    session_id="session-1",
                )
                run = await harness.start("use MCP")
                result = await run.wait()

    assert result.content["structured_content"] == {"result": "found"}
    assert client.calls == [("lookup", {"query": "dune"})]
    planned = {
        event.payload["target"]: event.payload["operation_id"]
        for event in store.list_events(str(run.run_id))
        if event.event_type == "operation.planned"
    }
    search = store.get_operation(planned["agnoclaw.builtin/search_mcp_tools@1"])
    call = store.get_operation(planned["agnoclaw.builtin/call_mcp_tool@1"])
    assert search.intent.effect_class.value == "read_only"
    assert call.intent.effect_class.value == "non_repeatable"
    assert call.state.value == "succeeded"
    assert len(contexts) == 1

    await harness.aclose()
    assert contexts[0].exits == 1
    store.close()


@pytest.mark.asyncio
async def test_builtin_dispatch_is_fenced_replayed_redacted_and_spilled(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "large.txt"
    source.write_text("secret" * 1_000, encoding="utf-8")
    calls = 0
    original = LocalWorkspaceAdapter.read_file

    def counted_read(adapter, **kwargs):
        nonlocal calls
        calls += 1
        return original(adapter, **kwargs)

    monkeypatch.setattr(LocalWorkspaceAdapter, "read_file", counted_read)
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    policy = RedactingPolicy()
    with patch("agnoclaw.agent.Agent", BuiltinCallingAgent):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=workspace,
                config=HarnessConfig.quick(enable_plugins=False),
                runtime_store=store,
                artifact_store=artifacts,
                max_inline_output_chars=1_024,
                policy_engine=policy,
                permission_mode="bypass",
                tenant_id="tenant-1",
                user_id="user-1",
                session_id="session-1",
            )

    run = await harness.start(str(source))
    result = await run.wait()

    assert calls == 1
    assert policy.after_calls == 1
    assert result.content["type"] == "agnoclaw.spilled_output"
    planned = [
        event
        for event in store.list_events(str(run.run_id))
        if event.event_type == "operation.planned"
        and event.payload["target"] == "agnoclaw.builtin/read_file@1"
    ]
    assert len(planned) == 1
    operation = store.get_operation(planned[0].payload["operation_id"])
    assert operation.intent.effect_class.value == "read_only"
    assert operation.intent.metadata["ingress"] == "first_party_builtin"
    assert operation.intent.metadata["lease_fence_token"] == 1
    assert operation.state.value == "succeeded"
    reference = store.get_artifact(operation.settlement.result_reference)
    persisted = await artifacts.load_json(reference)
    assert persisted == "     1\t" + "[MASKED]" * 1_000
    assert "secret" not in persisted

    await harness.aclose()
    store.close()


@pytest.mark.asyncio
async def test_builtin_argument_constraints_fail_before_intent_or_dispatch(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "input.txt"
    source.write_text("safe", encoding="utf-8")
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    with patch("agnoclaw.agent.Agent", BuiltinCallingAgent):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=workspace,
                config=HarnessConfig.quick(enable_plugins=False),
                runtime_store=store,
                policy_engine=ConstrainingPolicy(),
                permission_mode="bypass",
                tenant_id="tenant-1",
                user_id="user-1",
                session_id="session-1",
            )

    run = await harness.start(str(source))
    with pytest.raises(RunReconciliationRequiredError):
        await run.wait()
    assert store.get_terminal(str(run.run_id)) is None
    operation = store.get_operation(f"{run.run_id}:model:1")
    assert operation.settlement.safe_error["code"] == (
        "BUILTIN_POLICY_CONSTRAINT_UNSUPPORTED"
    )
    assert not any(
        event.event_type == "operation.planned"
        and event.payload["target"] == "agnoclaw.builtin/read_file@1"
        for event in store.list_events(str(run.run_id))
    )

    await harness.aclose()
    store.close()


@pytest.mark.asyncio
async def test_cancelled_nonrepeatable_builtin_is_ambiguous_not_replayed(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    destination = workspace / "result.txt"
    entered = threading.Event()
    release = threading.Event()
    original = LocalWorkspaceAdapter.write_file

    def blocking_write(adapter, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(adapter, **kwargs)

    monkeypatch.setattr(LocalWorkspaceAdapter, "write_file", blocking_write)
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    with patch("agnoclaw.agent.Agent", BuiltinMutatingAgent):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=workspace,
                config=HarnessConfig.quick(enable_plugins=False),
                runtime_store=store,
                permission_mode="bypass",
                tenant_id="tenant-1",
                user_id="user-1",
                session_id="session-1",
            )

    run = await harness.start(str(destination))
    assert await asyncio.to_thread(entered.wait, 5)
    cancellation = asyncio.create_task(run.cancel())
    for _ in range(100):
        if store.get_run(str(run.run_id)).state.value == "cancelling":
            break
        await asyncio.sleep(0.01)
    release.set()
    cancelled = await cancellation

    assert cancelled.state.value == "waiting_for_reconciliation"
    assert destination.read_text(encoding="utf-8") == "committed"
    planned = [
        event
        for event in store.list_events(str(run.run_id))
        if event.event_type == "operation.planned"
        and event.payload["target"] == "agnoclaw.builtin/write_file@1"
    ]
    assert len(planned) == 1
    operation = store.get_operation(planned[0].payload["operation_id"])
    assert operation.state.value == "unknown"
    assert operation.intent.effect_class.value == "non_repeatable"

    await harness.aclose()
    store.close()
