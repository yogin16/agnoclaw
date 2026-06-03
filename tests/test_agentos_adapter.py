"""Smoke tests for AgentOS compatibility adapter."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agnoclaw.agent import AgentHarness
from agnoclaw.config import HarnessConfig
from agnoclaw.runtime import (
    AgentOSContextAdapter,
    AgentOSHarnessAgent,
    AgentOSPermissionApprover,
    ExecutionContext,
    InMemoryEventSink,
    PermissionRequest,
    create_agentos_app,
)
from agnoclaw.runtime.agentos import _attach_agentos_approval_bridge


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


class FakeApprovalDb:
    def __init__(self):
        self.id = "fake-approval-db"
        self.approvals = []

    def create_approval(self, approval_data):
        self.approvals.append(dict(approval_data))
        return self.approvals[-1]

    def get_approvals(self, **filters):
        items = []
        for approval in self.approvals:
            matched = True
            for key, value in filters.items():
                if key in {"limit", "page"} or value is None:
                    continue
                if approval.get(key) != value:
                    matched = False
                    break
            if matched:
                items.append(approval)
        return items, len(items)


class AsyncApprovalDb:
    def get_approvals(self, **filters):
        async def _get():
            return []

        return _get()

    def create_approval(self, approval_data):
        async def _create():
            return approval_data

        return _create()


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


def test_agentos_harness_agent_properties_and_db_access(tmp_path):
    harness = SimpleNamespace(
        agent_id="agent-1",
        name="Agent One",
        workspace=SimpleNamespace(path=tmp_path),
        _agent=SimpleNamespace(db="db-1"),
    )
    agent = AgentOSHarnessAgent(harness)

    assert agent.id == "agent-1"
    assert agent.name == "Agent One"
    assert agent.description == "agnoclaw AgentHarness runtime adapter"
    assert agent.db == "db-1"

    agent.db = "db-2"

    assert harness._agent.db == "db-2"


@pytest.mark.asyncio
async def test_agentos_harness_agent_streams_from_harness(tmp_path):
    async def fake_arun(*args, **kwargs):
        async def _events():
            yield {"event": "one"}
            yield {"event": "two"}

        return _events()

    harness = SimpleNamespace(
        workspace=SimpleNamespace(path=tmp_path),
        arun=AsyncMock(side_effect=fake_arun),
    )
    agent = AgentOSHarnessAgent(harness, id="streamer")

    stream = agent.arun(
        "hello",
        stream=True,
        stream_events=False,
        session_id="sess-1",
        metadata={"claims": "ignore-me"},
        dependencies={"cache": "enabled"},
        background=True,
        schedule_run_id="sched-run-1",
        scheduled_at="2026-05-26T09:00:00Z",
    )
    events = [event async for event in stream]

    assert events == [{"event": "one"}, {"event": "two"}]
    call_kwargs = harness.arun.call_args.kwargs
    assert call_kwargs["stream"] is True
    assert call_kwargs["stream_events"] is False
    assert call_kwargs["context"].session_id == "sess-1"
    assert call_kwargs["context"].metadata["dependencies"] == {"cache": "enabled"}
    assert call_kwargs["metadata"]["agentos"]["scheduler"] == {
        "schedule_run_id": "sched-run-1",
        "scheduled_at": "2026-05-26T09:00:00Z",
    }
    assert "background" not in call_kwargs


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
        schedule_id="sched-1",
    )

    assert result.content == "ok"
    call_kwargs = mock_agent.arun.call_args.kwargs
    assert call_kwargs["run_id"].startswith("run_")
    assert call_kwargs["session_id"] == "sess-1"
    assert call_kwargs["user_id"] == "user-1"
    assert call_kwargs["metadata"]["_agnoclaw_context"]["tenant_id"] == "tenant-1"
    assert call_kwargs["metadata"]["_agnoclaw_context"]["metadata"]["agentos"]["scheduler"] == {
        "schedule_id": "sched-1"
    }


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
    agnoclaw_route = next(
        route for route in app.routes if getattr(route, "path", "") == "/agnoclaw/harnesses"
    )
    assert len(agnoclaw_route.dependencies) == 2


def test_harness_admin_helpers_report_runtime_and_sandbox(tmp_path):
    harness, _ = _make_harness(tmp_path)
    (harness.sandbox_dir / "artifact.txt").write_text("hello", encoding="utf-8")

    runtime = harness.admin_runtime_info()
    sandbox = harness.admin_snapshot_sandbox(session_id="sess-1")
    artifact = harness.admin_sandbox_artifact_path("artifact.txt")

    assert runtime["workspace_id"] == str(harness.workspace.path)
    assert runtime["agentos"]["approvals_enabled"] is False
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


def test_agentos_permission_approver_creates_pending_and_allows_approved():
    db = FakeApprovalDb()
    approver = AgentOSPermissionApprover(db, agent_id="deal-agent")
    request = PermissionRequest(
        run_id="run-1",
        tool_name="bash",
        category="exec",
        arguments={"command": "deploy"},
    )
    context = ExecutionContext.create(
        user_id="user-1",
        session_id="sess-1",
        workspace_id="/tmp/workspace",
    )

    first = approver.approve(request, context)
    db.approvals[0]["status"] = "approved"
    second = approver.approve(request, context)

    assert first is False
    assert second is True
    assert len(db.approvals) == 1
    assert db.approvals[0]["source_type"] == "agnoclaw"
    assert db.approvals[0]["pause_type"] == "permission"
    assert db.approvals[0]["tool_name"] == "bash"


def test_agentos_permission_approver_handles_unavailable_and_async_db():
    request = PermissionRequest(
        run_id="run-1",
        tool_name="bash",
        category="exec",
        arguments={"command": "deploy"},
    )
    context = ExecutionContext.create(
        user_id="user-1",
        session_id="sess-1",
        workspace_id="/tmp/workspace",
    )

    assert AgentOSPermissionApprover(None, agent_id="deal-agent").approve(request, context) is False
    assert (
        AgentOSPermissionApprover(SimpleNamespace(), agent_id="deal-agent").approve(
            request,
            context,
        )
        is False
    )
    async_approver = AgentOSPermissionApprover(AsyncApprovalDb(), agent_id="deal-agent")
    assert async_approver.approve(request, context) is False


def test_agentos_permission_approver_filters_non_matching_records():
    class FilteringDb:
        def __init__(self):
            self.created = []

        def get_approvals(self, **filters):
            return [
                "bad-record",
                {"tool_name": "python", "tool_args": {"command": "deploy"}},
                {"tool_name": "bash", "tool_args": {"command": "other"}},
            ]

        def create_approval(self, approval_data):
            self.created.append(dict(approval_data))

    request = PermissionRequest(
        run_id="run-1",
        tool_name="bash",
        category="exec",
        arguments={"command": "deploy"},
    )
    context = ExecutionContext.create(
        user_id=None,
        session_id=None,
        workspace_id="/tmp/workspace",
    )
    db = FilteringDb()

    assert AgentOSPermissionApprover(db, agent_id="deal-agent").approve(request, context) is False
    assert len(db.created) == 1


def test_attach_agentos_approval_bridge_preserves_existing_approver():
    existing_approver = object()
    controller = SimpleNamespace(approver=existing_approver)
    harness = SimpleNamespace(_permission_controller=controller)
    agent = SimpleNamespace(id="agent-1")

    _attach_agentos_approval_bridge([harness], [agent], db=None)
    assert controller.approver is existing_approver

    _attach_agentos_approval_bridge([harness], [agent], db=FakeApprovalDb())
    assert controller.approver is existing_approver


def test_create_agentos_app_requires_harness():
    with pytest.raises(ValueError, match="at least one harness"):
        create_agentos_app([])


def test_create_agentos_app_attaches_approval_bridge_and_runtime_flags(tmp_path):
    pytest.importorskip("fastapi")
    harness, _ = _make_harness(tmp_path)
    db = FakeApprovalDb()

    app = create_agentos_app(
        [harness],
        db=db,
        approvals=True,
        scheduler=True,
        telemetry=False,
    )

    assert app.state.agnoclaw_approvals_requested is True
    assert isinstance(harness._permission_controller.approver, AgentOSPermissionApprover)
    runtime = harness.admin_runtime_info()
    assert runtime["agentos"] == {
        "approvals_enabled": True,
        "scheduler_enabled": True,
        "mcp_enabled": False,
    }
