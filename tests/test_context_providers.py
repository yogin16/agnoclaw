"""Context provider bridge tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from agno.context.provider import Answer, ContextProvider, Status

from agnoclaw import AgentHarness, HarnessConfig, InMemoryEventSink


class DummyProvider(ContextProvider):
    def __init__(self, id: str = "docs", **kwargs):
        super().__init__(id, name="Docs", **kwargs)
        self.setup_calls = 0
        self.close_calls = 0

    def query(self, question: str, *, run_context=None) -> Answer:
        return Answer(text=f"sync:{question}")

    async def aquery(self, question: str, *, run_context=None) -> Answer:
        return Answer(text=f"async:{question}")

    def status(self) -> Status:
        return Status(ok=True)

    async def astatus(self) -> Status:
        return Status(ok=True)

    async def asetup(self) -> None:
        self.setup_calls += 1

    async def aclose(self) -> None:
        self.close_calls += 1

    def instructions(self) -> str:
        return "Use `query_docs(question)` for product docs."


def _make_harness(tmp_path, **kwargs):
    mock_agent = MagicMock()

    def _agent_ctor(*args, **kw):
        mock_agent.system_message = kw.get("system_message")
        mock_agent.session_id = kw.get("session_id")
        mock_agent.tools = kw.get("tools", [])
        mock_agent.arun.return_value = SimpleNamespace(content="ok")
        return mock_agent

    with patch("agnoclaw.agent.Agent", side_effect=_agent_ctor):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=tmp_path,
                config=HarnessConfig(),
                include_default_tools=False,
                **kwargs,
            )
    return harness, mock_agent


def test_context_provider_tools_and_instructions_are_added(tmp_path):
    provider = DummyProvider()
    harness, mock_agent = _make_harness(tmp_path, context_providers=[provider])

    tool_names = {getattr(tool, "name", None) for tool in mock_agent.tools}
    assert "query_docs" in tool_names
    assert "External Context Providers" in mock_agent.system_message
    assert "query_docs(question)" in mock_agent.system_message
    assert harness._context_provider_tool_map["query_docs"]["provider_id"] == "docs"


def test_duplicate_context_provider_tool_name_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Duplicate context provider tool name"):
        _make_harness(
            tmp_path,
            context_providers=[
                DummyProvider("docs"),
                DummyProvider("docs"),
            ],
        )


@pytest.mark.asyncio
async def test_create_runs_provider_setup_and_aclose_runs_provider_close(tmp_path):
    provider = DummyProvider()

    with patch("agnoclaw.agent.Agent") as agent_cls:
        mock_agent = MagicMock()
        mock_agent.system_message = ""
        agent_cls.return_value = mock_agent
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = await AgentHarness.create(
                workspace_dir=tmp_path,
                config=HarnessConfig(),
                include_default_tools=False,
                context_providers=[provider],
            )

    assert provider.setup_calls == 1
    await harness.aclose()
    assert provider.close_calls == 1


def test_context_provider_tool_events_are_emitted(tmp_path):
    sink = InMemoryEventSink()
    provider = DummyProvider()
    harness, _ = _make_harness(
        tmp_path,
        context_providers=[provider],
        event_sink=sink,
    )
    function = next(
        tool
        for tool in harness._agent.tools
        if getattr(tool, "name", None) == "query_docs"
    )
    fc = SimpleNamespace(
        function=SimpleNamespace(name="query_docs"),
        call_id="call-1",
        arguments={"question": "hello"},
        result='{"text":"world"}',
        error=None,
    )

    function.pre_hook(run_context=SimpleNamespace(run_id="run-1", metadata={}), fc=fc)
    function.post_hook(run_context=SimpleNamespace(run_id="run-1", metadata={}), fc=fc)

    event_types = [event.event_type for event in sink.events]
    assert "context.provider.query.started" in event_types
    assert "context.provider.query.completed" in event_types
