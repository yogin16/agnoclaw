"""Additional coverage tests for AgentHarness — easy utility methods and branches."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agno.exceptions import AgentRunException

from agnoclaw.agent import AgentHarness
from agnoclaw.config import HarnessConfig
from agnoclaw.runtime.errors import HarnessError


def _make_harness(tmp_path, **kwargs):
    """Create a harness with mocked internals."""
    mock_agent = MagicMock()

    def _agent_ctor(*args, **kw):
        mock_agent.system_message = kw.get("system_message")
        mock_agent.session_id = kw.get("session_id")
        mock_agent.tools = kw.get("tools", [])
        return mock_agent

    with patch("agnoclaw.agent.Agent", side_effect=_agent_ctor):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=tmp_path,
                config=HarnessConfig(),
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
