"""Additional coverage tests for AgentHarness — easy utility methods and branches."""

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.exceptions import AgentRunException

from agnoclaw.agent import AgentHarness
from agnoclaw.config import HarnessConfig
from agnoclaw.runtime import (
    ElevatedCommandResult,
    ExecutionContext,
    InMemoryEventSink,
    LifecycleHookRequest,
    PlanExitSignal,
    PlanQuestionSignal,
    PolicyDecision,
)
from agnoclaw.runtime.errors import HarnessError


def _make_harness(tmp_path, **kwargs):
    """Create a harness with mocked internals."""
    mock_agent = MagicMock()
    config = kwargs.pop("config", HarnessConfig())

    def _agent_ctor(*args, **kw):
        mock_agent.system_message = kw.get("system_message")
        mock_agent.session_id = kw.get("session_id")
        mock_agent.tools = kw.get("tools", [])
        return mock_agent

    with patch("agnoclaw.agent.Agent", side_effect=_agent_ctor):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=tmp_path,
                config=config,
                **kwargs,
            )
    return harness, mock_agent


# ── set_event_sink ──────────────────────────────────────────────────────


def test_set_event_sink_basic(tmp_path):
    harness, _ = _make_harness(tmp_path)
    new_sink = MagicMock()
    harness.set_event_sink(new_sink)
    assert harness._event_sink is new_sink


def test_set_event_sink_with_mode(tmp_path):
    harness, _ = _make_harness(tmp_path)
    new_sink = MagicMock()
    harness.set_event_sink(new_sink, mode="fail_closed")
    assert harness._event_sink is new_sink


# ── set_policy_engine ───────────────────────────────────────────────────


def test_set_policy_engine(tmp_path):
    harness, _ = _make_harness(tmp_path)
    new_engine = MagicMock()
    harness.set_policy_engine(new_engine)
    assert harness._policy_engine is new_engine


# ── set_permission_mode ─────────────────────────────────────────────────


def test_set_permission_mode(tmp_path):
    harness, _ = _make_harness(tmp_path)
    harness.set_permission_mode("plan")
    assert harness.permission_mode == "plan"


@pytest.mark.asyncio
async def test_sdk_session_send_uses_execution_context(tmp_path):
    harness, mock_agent = _make_harness(tmp_path)
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="ok"))

    run = await harness.session(
        user_id="u1",
        workspace_id="repo-a",
        session_id="s1",
        metadata={"source": "sdk"},
    ).send("hello")

    assert run.result.content == "ok"
    call_kwargs = mock_agent.arun.call_args.kwargs
    context = call_kwargs["metadata"]["_agnoclaw_context"]
    assert context["user_id"] == "u1"
    assert context["session_id"] == "s1"
    assert context["workspace_id"] == "repo-a"
    assert context["metadata"]["source"] == "sdk"


# ── add_pre_run_hook / add_post_run_hook ────────────────────────────────


def test_add_pre_run_hook(tmp_path):
    harness, _ = _make_harness(tmp_path)
    hook = MagicMock()
    harness.add_pre_run_hook(hook)
    assert hook in harness._pre_run_hooks


def test_add_post_run_hook(tmp_path):
    harness, _ = _make_harness(tmp_path)
    hook = MagicMock()
    harness.add_post_run_hook(hook)
    assert hook in harness._post_run_hooks


def test_set_permission_approver(tmp_path):
    harness, _ = _make_harness(tmp_path)
    approver = MagicMock()

    harness.set_permission_approver(approver, require_approver=True)

    assert harness._permission_controller.approver is approver
    assert harness._permission_controller.require_approver is True


def test_set_elevated_mode(tmp_path):
    harness, _ = _make_harness(tmp_path)

    assert harness.elevated_mode == "off"
    assert harness.set_elevated_mode("on") == "on"
    assert harness.elevated_mode == "on"
    assert harness.admin_list_permissions()["elevated_mode"] == "on"


def test_session_elevated_full_routes_bash_through_host_path(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    bash = next(tool for tool in mock_agent.tools if getattr(tool, "name", None) == "bash")

    harness.set_elevated_mode("full")
    output = bash.entrypoint("printf elevated-tool")

    assert output == "elevated-tool"
    event_types = [event.event_type for event in sink.events]
    assert event_types == [
        "elevated.command.requested",
        "policy.decision",
        "elevated.command.approval_skipped",
        "elevated.command.started",
        "policy.decision",
        "elevated.command.completed",
    ]
    assert sink.events[0].payload["metadata"]["source"] == "elevated_session"
    assert sink.events[0].payload["metadata"]["elevated_mode"] == "full"


def test_session_elevated_ask_uses_permission_approver(tmp_path):
    sink = InMemoryEventSink()
    approver = MagicMock()
    approver.approve.return_value = True
    harness, mock_agent = _make_harness(
        tmp_path,
        event_sink=sink,
        permission_approver=approver,
    )
    bash = next(tool for tool in mock_agent.tools if getattr(tool, "name", None) == "bash")

    harness.set_elevated_mode("ask")
    output = bash.entrypoint("printf ask-mode")

    assert output == "ask-mode"
    permission_request = approver.approve.call_args.args[0]
    assert permission_request.tool_name == "bash.elevated"
    assert permission_request.category == "elevated_exec"
    assert permission_request.arguments["command"] == "printf ask-mode"


def test_add_lifecycle_hook_and_session_created_checkpoint(tmp_path):
    harness, _ = _make_harness(tmp_path)
    seen = []

    def hook(event, context):
        seen.append((event.event_type, context.session_id))
        event.metadata["seen"] = True
        return event

    harness.add_lifecycle_hook("session.created", hook)
    session = harness.session(session_id="session-1")

    assert session.session_id == "session-1"
    assert seen == [("session.created", "session-1")]


def test_workspace_lifecycle_hook_discovery_and_execution(tmp_path):
    sink = InMemoryEventSink()
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "session_hook.py").write_text(
        "import json, os\n"
        "print(json.dumps({'metadata': {'hook_seen': os.environ['AGNOCLAW_HOOK_SCOPE']}}))\n",
        encoding="utf-8",
    )
    (hooks_dir / "session_hook.json").write_text(
        json.dumps(
            {
                "name": "session-hook",
                "event": "session.created",
                "command": f'"{sys.executable}" session_hook.py',
            }
        ),
        encoding="utf-8",
    )
    harness, _ = _make_harness(tmp_path, event_sink=sink)
    context = ExecutionContext.create(
        user_id=None,
        session_id="session-hooks",
        workspace_id=str(tmp_path),
    )

    result = harness._run_lifecycle_hooks_sync(
        "session.created",
        context=context,
        metadata={"source": "test"},
    )

    assert result.metadata["hook_seen"] == "workspace"
    assert result.metadata["workspace_dir"] == str(tmp_path.resolve())
    assert "worktree_dir" in result.metadata
    hooks = harness.admin_list_hooks()
    assert hooks["lifecycle"]["session.created"] == 1
    assert hooks["workspace"][0]["scope"] == "workspace"
    event_types = [event.event_type for event in sink.events]
    assert event_types == ["workspace.hook.started", "workspace.hook.completed"]


def test_project_lifecycle_hook_discovery(tmp_path):
    project_dir = tmp_path / ".agnoclaw"
    hooks_dir = project_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "project_hook.py").write_text(
        "import json, os\n"
        "print(json.dumps({'metadata': {'project_dir': os.environ['AGNOCLAW_PROJECT_DIR']}}))\n",
        encoding="utf-8",
    )
    (hooks_dir / "project_hook.json").write_text(
        json.dumps(
            {
                "name": "project-hook",
                "event": "session.created",
                "command": f'"{sys.executable}" project_hook.py',
            }
        ),
        encoding="utf-8",
    )
    config = HarnessConfig(project_workspace_dir=str(project_dir))
    harness, _ = _make_harness(tmp_path / "workspace", config=config)
    context = ExecutionContext.create(
        user_id=None,
        session_id="session-project-hooks",
        workspace_id=str(harness.workspace.path),
    )

    result = harness._run_lifecycle_hooks_sync("session.created", context=context)

    assert result.metadata["project_dir"] == str(project_dir.resolve())
    assert harness.admin_list_hooks()["workspace"][0]["scope"] == "project"


@pytest.mark.asyncio
async def test_session_end_lifecycle_checkpoint(tmp_path):
    harness, _ = _make_harness(tmp_path)
    seen = []

    def hook(event, context):
        seen.append((event.event_type, context.session_id, dict(event.metadata)))
        return event

    harness.add_lifecycle_hook("session.end.completed", hook)

    result = await harness.end_session(generate_summary=False)

    assert result is None
    assert len(seen) == 1
    event_type, session_id, metadata = seen[0]
    assert event_type == "session.end.completed"
    assert session_id == harness.session_id
    assert metadata["session_id"] == harness.session_id
    assert metadata["summary_generated"] is False
    assert metadata["created_files"] == []
    assert metadata["workspace_dir"] == str(harness.workspace.path)
    assert "worktree_dir" in metadata


def test_lifecycle_hook_invalid_return_raises(tmp_path):
    harness, _ = _make_harness(tmp_path)
    harness.add_lifecycle_hook("session.created", lambda event, context: "bad")
    context = ExecutionContext.create(
        user_id=None,
        session_id=None,
        workspace_id=str(harness.workspace.path),
    )

    with pytest.raises(HarnessError, match="Lifecycle hook"):
        harness._run_lifecycle_hooks_sync("session.created", context=context)


@pytest.mark.asyncio
async def test_async_lifecycle_hook_updates_request(tmp_path):
    harness, _ = _make_harness(tmp_path)
    context = ExecutionContext.create(
        user_id=None,
        session_id=None,
        workspace_id=str(harness.workspace.path),
    )

    async def hook(event, context):
        event.metadata["async"] = True
        return event

    harness.add_lifecycle_hook("session.end.completed", hook)
    result = await harness._run_lifecycle_hooks_async(
        "session.end.completed",
        context=context,
        metadata={"done": True},
    )

    assert isinstance(result, LifecycleHookRequest)
    assert result.metadata["done"] is True
    assert result.metadata["async"] is True
    assert result.metadata["workspace_dir"] == str(harness.workspace.path)
    assert "worktree_dir" in result.metadata


def test_plan_signal_methods_emit_events_and_exit_plan_mode(tmp_path):
    sink = InMemoryEventSink()
    harness, _ = _make_harness(
        tmp_path,
        event_sink=sink,
        permission_mode="default",
    )
    harness.enter_plan_mode()

    question = harness.ask_user_question(
        "Which implementation path?",
        options=["Small", "Complete"],
        allow_freeform=False,
    )
    completed = harness.signal_plan_completion(
        "Implement the complete path.",
        plan_path="implementation.plan.md",
    )

    assert isinstance(question, PlanQuestionSignal)
    assert isinstance(completed, PlanExitSignal)
    assert harness.permission_mode == "default"
    assert harness.plan_signals() == [question, completed]
    assert [event.event_type for event in sink.events] == [
        "plan.question.requested",
        "plan.completed",
    ]
    assert sink.events[0].payload["options"] == ["Small", "Complete"]
    assert sink.events[1].payload["plan_path"] == "implementation.plan.md"

    harness.clear_plan_signals()
    assert harness.plan_signals() == []


def test_run_elevated_command_requires_approver(tmp_path):
    sink = InMemoryEventSink()
    harness, _ = _make_harness(tmp_path, event_sink=sink)

    with pytest.raises(HarnessError) as exc:
        harness.run_elevated_command("printf denied", reason="host diagnostic")

    assert exc.value.code == "ELEVATED_APPROVER_REQUIRED"
    assert [event.event_type for event in sink.events] == [
        "elevated.command.requested",
        "policy.decision",
        "elevated.command.rejected",
    ]
    assert sink.events[-1].payload["reason_code"] == "ELEVATED_APPROVER_REQUIRED"


def test_run_elevated_command_uses_approval_gate_and_audit_events(tmp_path):
    sink = InMemoryEventSink()
    approver = MagicMock()
    approver.approve.return_value = True
    harness, _ = _make_harness(
        tmp_path,
        event_sink=sink,
        permission_mode="default",
        permission_approver=approver,
    )

    result = harness.run_elevated_command(
        "printf elevated",
        reason="verify host execution path",
    )

    assert isinstance(result, ElevatedCommandResult)
    assert result.stdout == "elevated"
    assert result.stderr == ""
    assert result.exit_code == 0
    permission_request = approver.approve.call_args.args[0]
    assert permission_request.tool_name == "bash.elevated"
    assert permission_request.category == "elevated_exec"
    assert permission_request.arguments["reason"] == "verify host execution path"
    event_types = [event.event_type for event in sink.events]
    assert event_types == [
        "elevated.command.requested",
        "policy.decision",
        "elevated.command.approved",
        "elevated.command.started",
        "policy.decision",
        "elevated.command.completed",
    ]
    assert sink.events[-1].payload["exit_code"] == 0


def test_run_elevated_command_keeps_guardrails_before_approval(tmp_path):
    sink = InMemoryEventSink()
    approver = MagicMock()
    approver.approve.return_value = True
    harness, _ = _make_harness(
        tmp_path,
        config=HarnessConfig(network_enabled=False),
        event_sink=sink,
        permission_mode="default",
        permission_approver=approver,
    )

    with pytest.raises(HarnessError) as exc:
        harness.run_elevated_command(
            "curl https://example.com",
            reason="verify guardrail ordering",
        )

    assert exc.value.code == "GUARDRAIL_DENIED"
    approver.approve.assert_not_called()
    assert [event.event_type for event in sink.events] == [
        "elevated.command.requested",
        "guardrail.violation",
        "elevated.command.rejected",
    ]
    assert sink.events[-1].payload["reason_code"] == "ELEVATED_GUARDRAIL_DENIED"


def test_run_elevated_command_emits_rejected_event_for_policy_denial(tmp_path):
    sink = InMemoryEventSink()
    approver = MagicMock()
    approver.approve.return_value = True

    class DenyElevatedPolicy:
        def before_tool_call(self, request, context):
            del context
            if request.tool_name == "bash.elevated":
                return PolicyDecision.deny(
                    reason_code="NO_ELEVATED",
                    message="elevated execution disabled",
                )
            return PolicyDecision.allow()

    harness, _ = _make_harness(
        tmp_path,
        event_sink=sink,
        permission_mode="default",
        permission_approver=approver,
        policy_engine=DenyElevatedPolicy(),
    )

    with pytest.raises(HarnessError) as exc:
        harness.run_elevated_command(
            "printf blocked",
            reason="verify policy rejection",
        )

    assert exc.value.code == "POLICY_DENIED"
    approver.approve.assert_not_called()
    assert [event.event_type for event in sink.events] == [
        "elevated.command.requested",
        "elevated.command.rejected",
        "policy.decision",
    ]
    assert sink.events[1].payload["reason_code"] == "NO_ELEVATED"


@pytest.mark.asyncio
async def test_arun_elevated_command_accepts_async_approver(tmp_path):
    sink = InMemoryEventSink()

    class AsyncApprover:
        async def approve(self, request, context):
            assert request.category == "elevated_exec"
            assert context.metadata["source"] == "elevated_command"
            return True

    harness, _ = _make_harness(
        tmp_path,
        event_sink=sink,
        permission_mode="default",
        permission_approver=AsyncApprover(),
    )

    result = await harness.arun_elevated_command(
        "printf async-elevated",
        reason="verify async approval",
    )

    assert result.stdout == "async-elevated"
    assert [event.event_type for event in sink.events][-1] == "elevated.command.completed"


# ── _apply_redactions_to_object ─────────────────────────────────────────


def test_apply_redactions_no_redactions():
    result = AgentHarness._apply_redactions_to_object("hello", None)
    assert result == "hello"


def test_apply_redactions_empty_redactions():
    result = AgentHarness._apply_redactions_to_object("hello", ())
    assert result == "hello"


def test_apply_redactions_string():
    from agnoclaw.runtime.policy import RedactionRule

    rule = RedactionRule(target="secret-key123", replacement="[REDACTED]")
    result = AgentHarness._apply_redactions_to_object("my secret-key123 is here", (rule,))
    assert "[REDACTED]" in result
    assert "secret-key123" not in result


def test_apply_redactions_list():
    from agnoclaw.runtime.policy import RedactionRule

    rule = RedactionRule(target="password", replacement="***")
    result = AgentHarness._apply_redactions_to_object(["password", "ok"], (rule,))
    assert result == ["***", "ok"]


def test_apply_redactions_tuple():
    from agnoclaw.runtime.policy import RedactionRule

    rule = RedactionRule(target="password", replacement="***")
    result = AgentHarness._apply_redactions_to_object(("password", "ok"), (rule,))
    assert result == ("***", "ok")


def test_apply_redactions_dict():
    from agnoclaw.runtime.policy import RedactionRule

    rule = RedactionRule(target="password", replacement="***")
    result = AgentHarness._apply_redactions_to_object({"key": "password"}, (rule,))
    assert result == {"key": "***"}


def test_apply_redactions_other_type():
    from agnoclaw.runtime.policy import RedactionRule

    rule = RedactionRule(target="x", replacement="y")
    result = AgentHarness._apply_redactions_to_object(42, (rule,))
    assert result == 42


# ── _run_id_from_tool_hook ──────────────────────────────────────────────


def test_run_id_from_run_context(tmp_path):
    harness, _ = _make_harness(tmp_path)
    rc = MagicMock()
    rc.run_id = "my-run-123"
    result = harness._run_id_from_tool_hook(run_context=rc, fc=None)
    assert result == "my-run-123"


def test_run_id_from_fc_call_id(tmp_path):
    harness, _ = _make_harness(tmp_path)
    fc = MagicMock()
    fc.call_id = "call-456"
    result = harness._run_id_from_tool_hook(run_context=None, fc=fc)
    assert result == "run_from_call-456"


def test_run_id_generates_uuid(tmp_path):
    harness, _ = _make_harness(tmp_path)
    result = harness._run_id_from_tool_hook(run_context=None, fc=None)
    assert result.startswith("run_")
    # Verify the hex part is valid
    hex_part = result[4:]
    assert len(hex_part) == 32


# ── _tool_call_id ───────────────────────────────────────────────────────


def test_tool_call_id_present():
    fc = MagicMock()
    fc.call_id = "call-789"
    assert AgentHarness._tool_call_id(fc) == "call-789"


def test_tool_call_id_missing():
    fc = MagicMock(spec=[])  # no call_id attribute
    assert AgentHarness._tool_call_id(fc) is None


def test_tool_call_id_empty():
    fc = MagicMock()
    fc.call_id = ""
    assert AgentHarness._tool_call_id(fc) is None


# ── _extract_harness_error ──────────────────────────────────────────────


def test_extract_harness_error_direct():
    err = HarnessError(code="X", category="x", message="x", retryable=False)
    assert AgentHarness._extract_harness_error(err) is err


def test_extract_harness_error_wrapped_in_agent_run_exception():
    inner = HarnessError(code="Y", category="y", message="y", retryable=False)
    exc = AgentRunException(inner, user_message="y")
    result = AgentHarness._extract_harness_error(exc)
    assert result is inner


def test_extract_harness_error_unrelated():
    exc = ValueError("unrelated")
    assert AgentHarness._extract_harness_error(exc) is None


def test_extract_harness_error_agent_run_without_harness():
    exc = AgentRunException("plain string", user_message="msg")
    assert AgentHarness._extract_harness_error(exc) is None


# ── _dispatch_command_tool ──────────────────────────────────────────────


def test_dispatch_command_tool_not_found(tmp_path):
    harness, mock_agent = _make_harness(tmp_path)
    mock_agent.tools = []
    result = harness._dispatch_command_tool("nonexistent", {})
    assert "[error]" in result
    assert "nonexistent" in result


# ── session helpers ───────────────────────────────────────────────────────


def test_get_session_messages_with_explicit_session_id(tmp_path):
    harness, mock_agent = _make_harness(tmp_path)
    mock_agent.get_chat_history.return_value = [SimpleNamespace(role="user", content="hi")]

    messages = harness.get_session_messages("session-123")

    assert len(messages) == 1
    mock_agent.get_chat_history.assert_called_once_with("session-123")


def test_get_session_messages_without_session_returns_empty(tmp_path):
    harness, mock_agent = _make_harness(tmp_path)
    harness.session_id = None
    mock_agent.session_id = None

    assert harness.get_session_messages() == []
    mock_agent.get_chat_history.assert_not_called()


def test_list_sessions_uses_db_list_sessions_and_normalizes(tmp_path):
    harness, mock_agent = _make_harness(tmp_path, user_id="u-1")
    db = SimpleNamespace()
    db.list_sessions = MagicMock(
        return_value=[
            {"session_id": "s-1", "user_id": "u-1", "run_count": 2},
            SimpleNamespace(id="s-2", user_id="u-1", summary="hello"),
        ]
    )
    mock_agent.db = db

    sessions = harness.list_sessions(limit=10)

    assert [s["session_id"] for s in sessions] == ["s-1", "s-2"]
    db.list_sessions.assert_called_once_with(user_id="u-1", limit=10)


def test_list_sessions_returns_empty_when_no_backend_method(tmp_path):
    harness, mock_agent = _make_harness(tmp_path)
    mock_agent.db = SimpleNamespace()

    assert harness.list_sessions() == []


def test_resume_session_switches_active_session(tmp_path):
    harness, mock_agent = _make_harness(tmp_path, session_id="old-session")

    resumed = harness.resume_session("new-session")

    assert resumed == "new-session"
    assert harness.session_id == "new-session"
    assert mock_agent.session_id == "new-session"


def test_resume_session_verify_exists_raises_for_missing_session(tmp_path):
    harness, mock_agent = _make_harness(tmp_path)
    mock_agent.get_session.return_value = None
    mock_agent.get_chat_history.return_value = []

    try:
        harness.resume_session("missing-session", verify_exists=True)
        raised = False
    except HarnessError as exc:
        raised = True
        assert exc.code == "SESSION_NOT_FOUND"

    assert raised is True


def test_run_passes_max_turns_to_underlying_agent(tmp_path):
    harness, mock_agent = _make_harness(tmp_path)
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    harness.run("hello", max_turns=3)

    assert mock_agent.run.call_args.kwargs["max_turns"] == 3


def test_run_emits_scheduler_invocation_event(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    harness.run(
        "hello",
        metadata={
            "agentos": {
                "scheduler": {
                    "schedule_id": "sched-1",
                    "schedule_run_id": "sched-run-1",
                }
            }
        },
    )

    scheduler_events = [
        event for event in sink.events if event.event_type == "scheduler.invocation"
    ]
    assert len(scheduler_events) == 1
    assert scheduler_events[0].payload["schedule_id"] == "sched-1"
    assert scheduler_events[0].payload["schedule_run_id"] == "sched-run-1"
