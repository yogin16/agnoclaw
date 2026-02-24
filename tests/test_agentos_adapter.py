"""Smoke tests for AgentOS compatibility adapter."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agnoclaw.agent import AgentHarness
from agnoclaw.config import HarnessConfig
from agnoclaw.runtime import AgentOSContextAdapter


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
