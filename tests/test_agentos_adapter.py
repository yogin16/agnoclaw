"""Smoke tests for AgentOS compatibility adapter."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agnoclaw.agent import AgentHarness
from agnoclaw.config import HarnessConfig
from agnoclaw.runtime import AgentOSContextAdapter, InMemoryEventSink, create_agentos_app


def _make_harness(tmp_path):
    mock_agent = MagicMock()

    def _agent_ctor(*args, **kwargs):
        mock_agent.system_message = kwargs.get("system_message")
        mock_agent.session_id = kwargs.get("session_id")
        return mock_agent

    with patch("agnoclaw.agent.Agent", side_effect=_agent_ctor):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=tmp_path,
                config=HarnessConfig(),
            )
    return harness, mock_agent


def test_agentos_adapter_maps_claims_to_execution_context():
    adapter = AgentOSContextAdapter()
    claims = {
        "sub": "user-123",
        "sid": "session-abc",
        "tenant_id": "tenant-1",
        "org_id": "org-1",
        "team_id": "team-1",
        "roles": ["employee", "developer"],
        "scopes": "agents.read agents.run",
        "x_request_id": "req-55",
        "trace_id": "trace-77",
    }

    context = adapter.to_execution_context(claims, workspace_id="/tmp/ws")

    assert context.user_id == "user-123"
    assert context.session_id == "session-abc"
    assert context.tenant_id == "tenant-1"
    assert context.org_id == "org-1"
    assert context.team_id == "team-1"
    assert context.roles == ("employee", "developer")
    assert context.scopes == ("agents.read", "agents.run")
    assert context.request_id == "req-55"
    assert context.trace_id == "trace-77"


def test_agentos_adapter_smoke_with_harness_run(tmp_path):
    harness, mock_agent = _make_harness(tmp_path)
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    claims = {
        "sub": "user-claim",
        "sid": "sess-claim",
        "tenant": "tenant-claim",
        "org": "org-claim",
        "team": "team-claim",
        "roles": ["employee"],
        "permissions": ["run"],
    }
    adapter = AgentOSContextAdapter()
    context = adapter.to_execution_context(
        claims,
        workspace_id=str(harness.workspace.path),
    )

    harness.run("hello", context=context)

    call_kwargs = mock_agent.run.call_args.kwargs
    assert call_kwargs["user_id"] == "user-claim"
    assert call_kwargs["session_id"] == "sess-claim"
    assert call_kwargs["metadata"]["_agnoclaw_context"]["tenant_id"] == "tenant-claim"
    assert call_kwargs["metadata"]["_agnoclaw_context"]["roles"] == ["employee"]


@pytest.mark.asyncio
async def test_agentos_harness_agent_routes_through_harness_arun(tmp_path):
    harness, mock_agent = _make_harness(tmp_path)
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="ok"))
    agent = harness.as_agentos_agent(agent_id="deal-agent")

    result = await agent.arun(
        "hello",
        session_id="sess-1",
        user_id="user-1",
        metadata={"agentos_claims": {"tenant_id": "tenant-1"}},
    )

    assert result.content == "ok"
    call_kwargs = mock_agent.arun.call_args.kwargs
    assert call_kwargs["run_id"].startswith("run_")
    assert call_kwargs["session_id"] == "sess-1"
    assert call_kwargs["user_id"] == "user-1"
    assert call_kwargs["metadata"]["_agnoclaw_context"]["tenant_id"] == "tenant-1"


def test_create_agentos_app_registers_harness_admin_routes(tmp_path):
    pytest.importorskip("fastapi")
    harness, _ = _make_harness(tmp_path)

    app = create_agentos_app([harness], include_agnoclaw_admin=True, telemetry=False)

    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/agents/{agent_id}/runs" in paths
    assert "/agnoclaw/harnesses" in paths
    assert "/agnoclaw/harnesses/{harness_id}/events/{run_id}" in paths
    assert "/agnoclaw/harnesses/{harness_id}/sandboxes/{session_id}" in paths
    assert "/agnoclaw/harnesses/{harness_id}/sandboxes/{session_id}/files" in paths
    assert "/agnoclaw/harnesses/{harness_id}/sandboxes/{session_id}/snapshot" in paths
    assert "/agnoclaw/harnesses/{harness_id}/sandboxes/{session_id}/reset" in paths
    assert "/agnoclaw/harnesses/{harness_id}/policies" in paths
    assert "/agnoclaw/harnesses/{harness_id}/permissions" in paths
    assert "/agnoclaw/policies" in paths
    assert "/agnoclaw/permissions" in paths
    assert app.state.agnoclaw_harnesses["agnoclaw"] is harness


def test_harness_admin_helpers_report_runtime_and_sandbox(tmp_path):
    harness, _ = _make_harness(tmp_path)
    (harness.sandbox_dir / "artifact.txt").write_text("hello", encoding="utf-8")

    runtime = harness.admin_runtime_info()
    sandbox = harness.admin_snapshot_sandbox(session_id="sess-1")
    artifact = harness.admin_sandbox_artifact_path("artifact.txt")

    assert runtime["workspace_id"] == str(harness.workspace.path)
    assert sandbox["session_id"] == "sess-1"
    assert sandbox["file_count"] == 1
    assert sandbox["files"][0]["path"] == "artifact.txt"
    assert artifact == harness.sandbox_dir / "artifact.txt"


def test_harness_admin_events_filter_by_run_id(tmp_path):
    harness, _ = _make_harness(tmp_path)
    sink = InMemoryEventSink()
    harness.set_event_sink(sink)
    context = harness._build_execution_context(user_id=None, session_id=None)

    harness._emit_event_sync(event_type="one", run_id="run-1", context=context)
    harness._emit_event_sync(event_type="two", run_id="run-2", context=context)

    assert len(harness.admin_list_events()) == 2
    filtered = harness.admin_list_events(run_id="run-1")
    assert len(filtered) == 1
    assert filtered[0]["event_type"] == "one"
