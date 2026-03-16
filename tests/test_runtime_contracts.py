"""Contract-style tests for v0.2 harness runtime behavior."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.exceptions import AgentRunException

from agnoclaw.agent import AgentHarness
from agnoclaw.config import HarnessConfig
from agnoclaw.runtime import (
    AgnoAuthError,
    ExecutionContext,
    HarnessError,
    InMemoryEventSink,
    PolicyDecision,
    PolicyAction,
    RedactionRule,
    RunResultEnvelope,
)


def _make_harness(
    tmp_path,
    *,
    event_sink=None,
    policy_engine=None,
    pre_run_hooks=None,
    post_run_hooks=None,
    **harness_kwargs,
):
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
                event_sink=event_sink,
                policy_engine=policy_engine,
                pre_run_hooks=pre_run_hooks,
                post_run_hooks=post_run_hooks,
                **harness_kwargs,
            )
    return harness, mock_agent


def test_execution_context_is_immutable():
    ctx = ExecutionContext.create(
        user_id="u-1",
        session_id="s-1",
        workspace_id="ws-1",
        roles=["developer"],
    )
    with pytest.raises(FrozenInstanceError):
        ctx.user_id = "u-2"


def test_run_emits_lifecycle_events(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    harness.run("hello")

    event_types = [e.event_type for e in sink.events]
    assert event_types[0] == "run.started"
    assert "prompt.built" in event_types
    assert "model.request.started" in event_types
    assert "model.request.completed" in event_types
    assert "run.completed" in event_types
    assert event_types.count("policy.decision") >= 2
    first_event = sink.events[0]
    payload = first_event.to_dict()
    assert first_event.event_version == "0.2"
    assert payload["context"]["workspace_id"] == str(harness.workspace.path)


def test_run_raises_auth_error_for_auth_failures(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    mock_agent.run.return_value = SimpleNamespace(
        content='"Could not resolve authentication method. Expected either api_key or auth_token to be set."',
        status=SimpleNamespace(value="error"),
        events=[],
    )

    with pytest.raises(AgnoAuthError):
        harness.run("hello")

    event_types = [e.event_type for e in sink.events]
    assert "run.failed" in event_types
    assert "run.completed" not in event_types


def test_run_keeps_recoverable_error_as_output_and_marks_failed(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    model_output = SimpleNamespace(
        content="Rate limit exceeded, please retry later",
        status=SimpleNamespace(value="error"),
        events=[],
    )
    mock_agent.run.return_value = model_output

    result = harness.run("hello")

    assert result is model_output
    event_types = [e.event_type for e in sink.events]
    assert "model.request.failed" in event_types
    assert "run.failed" in event_types
    assert "run.completed" not in event_types


def test_policy_deny_blocks_run(tmp_path):
    class DenyPolicy:
        def before_run(self, run_input, context):
            del run_input, context
            return PolicyDecision.deny(reason_code="BLOCKED", message="run denied")

        def before_prompt_send(self, prompt, context):
            del prompt, context
            return PolicyDecision.allow()

        def before_skill_load(self, request, context):
            del request, context
            return PolicyDecision.allow()

    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink, policy_engine=DenyPolicy())
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    with pytest.raises(HarnessError, match="run denied") as exc:
        harness.run("hello")

    assert exc.value.code == "POLICY_DENIED"
    mock_agent.run.assert_not_called()
    assert "run.failed" in [e.event_type for e in sink.events]


def test_pre_and_post_hooks_are_ordered(tmp_path):
    order: list[str] = []

    def pre_hook(run_input, context):
        del context
        order.append("pre")
        run_input.message = f"{run_input.message} transformed"
        return run_input

    def post_hook(run_input, result, context):
        del run_input, context
        order.append("post")
        result.metadata["seen"] = True
        return result

    harness, mock_agent = _make_harness(
        tmp_path,
        pre_run_hooks=[pre_hook],
        post_run_hooks=[post_hook],
    )
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    harness.run("hello")

    call_args = mock_agent.run.call_args
    assert call_args.args[0] == "hello transformed"
    assert order == ["pre", "post"]


def test_stream_run_emits_content_events(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    mock_agent.run.return_value = iter(
        [
            SimpleNamespace(event="ToolCallStarted", content=""),
            SimpleNamespace(event="RunContent", content="A"),
            SimpleNamespace(event="ToolCallCompleted", content=""),
            SimpleNamespace(event="RunContent", content="B"),
        ]
    )

    list(harness.run("hello", stream=True, stream_events=True))

    event_types = [e.event_type for e in sink.events]
    assert event_types.count("run.content") == 2
    assert "tool.call.started" in event_types
    assert "tool.call.completed" in event_types
    assert "run.completed" in event_types


def test_stream_emits_response_and_thinking_events_and_marks_failed(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    mock_agent.run.return_value = iter(
        [
            SimpleNamespace(event="ReasoningContentDelta", content="", reasoning_content="plan step"),
            SimpleNamespace(event="RunContent", content="A"),
            SimpleNamespace(
                event="RunError",
                content="Rate limit exceeded",
                error_id="model_rate_limit_error",
                error_type="model_provider_error",
                additional_data={},
            ),
        ]
    )

    with pytest.raises(HarnessError) as exc:
        list(harness.run("hello", stream=True, stream_events=True))

    event_types = [e.event_type for e in sink.events]
    assert exc.value.code == "MODEL_STREAM_FAILED"
    assert "thinking" in event_types
    assert "response_chunk" in event_types
    assert "run.failed" in event_types
    assert "run.completed" not in event_types

    chunks = [e.payload for e in sink.events if e.event_type == "response_chunk"]
    assert chunks[0]["content"] == "A"
    assert chunks[-1]["is_final"] is True


@pytest.mark.asyncio
async def test_arun_stream_raises_on_stream_error_and_marks_failed(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)

    async def _stream():
        yield SimpleNamespace(event="RunContent", content="A")
        yield SimpleNamespace(
            event="RunError",
            content="Rate limit exceeded",
            error_id="model_rate_limit_error",
            error_type="model_provider_error",
            additional_data={},
        )

    mock_agent.arun = AsyncMock(return_value=_stream())

    stream = await harness.arun("hello", stream=True, stream_events=True)
    with pytest.raises(HarnessError) as exc:
        async for _ in stream:
            pass

    assert exc.value.code == "MODEL_STREAM_FAILED"
    event_types = [e.event_type for e in sink.events]
    assert "model.request.failed" in event_types
    assert "run.failed" in event_types
    assert "run.completed" not in event_types


@pytest.mark.asyncio
async def test_arun_stream_emits_single_response_chunk_per_text_delta(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)

    async def _stream():
        yield SimpleNamespace(event="RunContent", content="Hello!")

    mock_agent.arun = AsyncMock(return_value=_stream())

    stream = await harness.arun("hello", stream=True, stream_events=True)
    async for _ in stream:
        pass

    chunks = [e.payload for e in sink.events if e.event_type == "response_chunk"]
    assert chunks == [
        {"content": "Hello!", "cumulative": "Hello!", "is_final": False},
        {"content": "", "cumulative": "Hello!", "is_final": True},
    ]

    run_content_events = [e for e in sink.events if e.event_type == "run.content"]
    assert len(run_content_events) == 1
    assert run_content_events[0].payload["chars"] == len("Hello!")


def test_skill_fork_resolves_model_using_active_provider(tmp_path):
    harness, mock_agent = _make_harness(tmp_path, model="openai:gpt-4o")
    harness.skills.load_skill = MagicMock(return_value="skill instructions")
    harness.skills._get_skill = MagicMock(
        return_value=SimpleNamespace(
            meta=SimpleNamespace(
                context="fork",
                model="gpt-4",
                allowed_tools=None,
                command_dispatch=None,
                command_tool=None,
            )
        )
    )

    with patch("agnoclaw.tools.tasks._run_subagent", return_value="fork ok") as mock_run:
        result = harness.run("hello", skill="forked-skill")

    assert result == "fork ok"
    assert mock_run.call_args[1]["model_id"] == "openai:gpt-4"
    mock_agent.run.assert_not_called()


def test_context_override_propagates_to_model_call(tmp_path):
    harness, mock_agent = _make_harness(tmp_path)
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    context = ExecutionContext.create(
        user_id="ctx-user",
        session_id="ctx-session",
        workspace_id="ws-id",
    )
    harness.run("hello", context=context)

    call_kwargs = mock_agent.run.call_args.kwargs
    assert call_kwargs["user_id"] == "ctx-user"
    assert call_kwargs["session_id"] == "ctx-session"
    assert isinstance(call_kwargs["run_id"], str)
    assert call_kwargs["run_id"].startswith("run_")
    assert "_agnoclaw_context" in call_kwargs["metadata"]


def test_fail_closed_event_sink_raises(tmp_path):
    class FailingSink:
        def emit(self, event):
            del event
            raise RuntimeError("sink down")

    harness, mock_agent = _make_harness(
        tmp_path,
        event_sink=FailingSink(),
        event_sink_mode="fail_closed",
    )
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    with pytest.raises(HarnessError) as exc:
        harness.run("hello")
    assert exc.value.code == "EVENT_SINK_FAILED"


@pytest.mark.asyncio
async def test_async_hooks_and_events(tmp_path):
    sink = InMemoryEventSink()
    order: list[str] = []

    async def pre_hook(run_input, context):
        del context
        order.append("pre")
        run_input.message = f"{run_input.message} async"
        return run_input

    async def post_hook(run_input, result, context):
        del run_input, context
        order.append("post")
        return RunResultEnvelope(
            run_id=result.run_id,
            content=result.content,
            raw_output=result.raw_output,
            metadata=result.metadata,
        )

    harness, mock_agent = _make_harness(
        tmp_path,
        event_sink=sink,
        pre_run_hooks=[pre_hook],
        post_run_hooks=[post_hook],
    )
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="ok"))

    await harness.arun("hello")

    call_args = mock_agent.arun.call_args
    assert call_args.args[0] == "hello async"
    assert order == ["pre", "post"]
    assert "run.completed" in [e.event_type for e in sink.events]


@pytest.mark.asyncio
async def test_arun_raises_auth_error_for_auth_failures(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    mock_agent.arun = AsyncMock(
        return_value=SimpleNamespace(
            content="Could not resolve authentication method. Expected either api_key or auth_token to be set.",
            status=SimpleNamespace(value="error"),
            events=[],
        )
    )

    with pytest.raises(AgnoAuthError):
        await harness.arun("hello")

    event_types = [e.event_type for e in sink.events]
    assert "run.failed" in event_types
    assert "run.completed" not in event_types


@pytest.mark.asyncio
async def test_arun_keeps_recoverable_error_as_output_and_marks_failed(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    model_output = SimpleNamespace(
        content="Rate limit exceeded, please retry later",
        status=SimpleNamespace(value="error"),
        events=[],
    )
    mock_agent.arun = AsyncMock(return_value=model_output)

    result = await harness.arun("hello")

    assert result is model_output
    event_types = [e.event_type for e in sink.events]
    assert "model.request.failed" in event_types
    assert "run.failed" in event_types
    assert "run.completed" not in event_types


@pytest.mark.asyncio
async def test_arun_passes_max_turns_to_underlying_agent(tmp_path):
    harness, mock_agent = _make_harness(tmp_path)
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="ok"))

    await harness.arun("hello", max_turns=4)

    assert mock_agent.arun.call_args.kwargs["max_turns"] == 4


def _tool_run_context(harness: AgentHarness):
    ctx = ExecutionContext.create(
        user_id="tool-user",
        session_id="tool-session",
        workspace_id=str(harness.workspace.path),
    )
    return SimpleNamespace(
        run_id="run_tool_123",
        session_id="tool-session",
        user_id="tool-user",
        metadata={"_agnoclaw_context": harness._context_to_metadata(ctx)},
    )


def test_before_tool_policy_denies_call(tmp_path):
    class ToolDenyPolicy:
        def before_run(self, run_input, context):
            del run_input, context
            return PolicyDecision.allow()

        def before_prompt_send(self, prompt, context):
            del prompt, context
            return PolicyDecision.allow()

        def before_skill_load(self, request, context):
            del request, context
            return PolicyDecision.allow()

        def before_tool_call(self, request, context):
            del request, context
            return PolicyDecision.deny(reason_code="TOOL_BLOCK", message="tool denied")

        def after_tool_call(self, result, context):
            del result, context
            return PolicyDecision.allow()

    harness, _ = _make_harness(tmp_path, policy_engine=ToolDenyPolicy())
    fc = SimpleNamespace(
        function=SimpleNamespace(name="web_fetch"),
        arguments={"url": "https://example.com"},
        result=None,
        error=None,
        call_id="tc-1",
    )

    with pytest.raises(AgentRunException) as exc:
        harness._handle_tool_pre_hook(fc=fc, run_context=_tool_run_context(harness))

    inner = exc.value.args[0]
    assert isinstance(inner, HarnessError)
    assert inner.code == "POLICY_DENIED"


def test_tool_policy_redacts_input_and_output(tmp_path):
    class RedactPolicy:
        def before_run(self, run_input, context):
            del run_input, context
            return PolicyDecision.allow()

        def before_prompt_send(self, prompt, context):
            del prompt, context
            return PolicyDecision.allow()

        def before_skill_load(self, request, context):
            del request, context
            return PolicyDecision.allow()

        def before_tool_call(self, request, context):
            del request, context
            return PolicyDecision(
                action=PolicyAction.ALLOW_WITH_REDACTION,
                reason_code="REDACT_PRE",
                redactions=(RedactionRule(target="secret"),),
            )

        def after_tool_call(self, result, context):
            del result, context
            return PolicyDecision(
                action=PolicyAction.ALLOW_WITH_REDACTION,
                reason_code="REDACT_POST",
                redactions=(RedactionRule(target="secret"),),
            )

    harness, _ = _make_harness(tmp_path, policy_engine=RedactPolicy())
    fc = SimpleNamespace(
        function=SimpleNamespace(name="web_fetch"),
        arguments={"url": "https://example.com?q=secret"},
        result="secret output",
        error=None,
        call_id="tc-2",
    )
    run_context = _tool_run_context(harness)

    harness._handle_tool_pre_hook(fc=fc, run_context=run_context)
    assert "[REDACTED]" in fc.arguments["url"]

    harness._handle_tool_post_hook(fc=fc, run_context=run_context)
    assert fc.result == "[REDACTED] output"


def test_tool_events_include_step_progress_and_result_preview(tmp_path):
    sink = InMemoryEventSink()
    harness, _ = _make_harness(tmp_path, event_sink=sink)
    run_context = _tool_run_context(harness)
    fc = SimpleNamespace(
        function=SimpleNamespace(name="web_fetch"),
        arguments={"url": "https://example.com"},
        result="line one\nline two",
        error=None,
        call_id="tc-step-1",
    )

    harness._handle_tool_pre_hook(fc=fc, run_context=run_context)
    harness._handle_tool_post_hook(fc=fc, run_context=run_context)

    event_types = [e.event_type for e in sink.events]
    assert "step_started" in event_types
    assert "step_completed" in event_types
    assert "tool.call.completed" in event_types

    completed = [e for e in sink.events if e.event_type == "tool.call.completed"][-1]
    assert completed.payload["step_id"] == "tc-step-1"
    assert completed.payload["duration_ms"] >= 0
    assert completed.payload["result_preview"] == "line one line two"


def test_guardrails_block_path_outside_workspace(tmp_path):
    harness, _ = _make_harness(tmp_path)
    fc = SimpleNamespace(
        function=SimpleNamespace(name="read_file"),
        arguments={"path": "/etc/passwd"},
        result=None,
        error=None,
        call_id="tc-3",
    )

    with pytest.raises(AgentRunException) as exc:
        harness._handle_tool_pre_hook(fc=fc, run_context=_tool_run_context(harness))

    inner = exc.value.args[0]
    assert isinstance(inner, HarnessError)
    assert inner.code == "GUARDRAIL_DENIED"


def test_guardrails_block_private_network_host(tmp_path):
    harness, _ = _make_harness(tmp_path)
    fc = SimpleNamespace(
        function=SimpleNamespace(name="web_fetch"),
        arguments={"url": "https://localhost/internal"},
        result=None,
        error=None,
        call_id="tc-4",
    )

    with pytest.raises(AgentRunException) as exc:
        harness._handle_tool_pre_hook(fc=fc, run_context=_tool_run_context(harness))

    inner = exc.value.args[0]
    assert isinstance(inner, HarnessError)
    assert inner.code == "GUARDRAIL_DENIED"


def test_permission_plan_mode_blocks_mutating_tools(tmp_path):
    harness, _ = _make_harness(tmp_path, permission_mode="plan")
    fc = SimpleNamespace(
        function=SimpleNamespace(name="write_file"),
        arguments={"path": "note.txt", "content": "x"},
        result=None,
        error=None,
        call_id="tc-plan-1",
    )

    with pytest.raises(AgentRunException) as exc:
        harness._handle_tool_pre_hook(fc=fc, run_context=_tool_run_context(harness))

    inner = exc.value.args[0]
    assert isinstance(inner, HarnessError)
    assert inner.code == "POLICY_DENIED"
    assert "Plan mode is read-only" in inner.message


def test_permission_dont_ask_denies_without_preapproval(tmp_path):
    harness, _ = _make_harness(tmp_path, permission_mode="dont_ask")
    fc = SimpleNamespace(
        function=SimpleNamespace(name="bash"),
        arguments={"command": "echo hi"},
        result=None,
        error=None,
        call_id="tc-pa-1",
    )

    with pytest.raises(AgentRunException) as exc:
        harness._handle_tool_pre_hook(fc=fc, run_context=_tool_run_context(harness))

    inner = exc.value.args[0]
    assert isinstance(inner, HarnessError)
    assert inner.code == "POLICY_DENIED"
    assert "dont_ask" in inner.message


def test_permission_preapproved_tool_allows_in_dont_ask_mode(tmp_path):
    harness, _ = _make_harness(tmp_path, permission_mode="dont_ask")
    run_context = _tool_run_context(harness)
    run_context.metadata["_agnoclaw_context"]["metadata"]["permission_preapproved_tools"] = ["bash"]
    fc = SimpleNamespace(
        function=SimpleNamespace(name="bash"),
        arguments={"command": "echo hi"},
        result=None,
        error=None,
        call_id="tc-pa-2",
    )

    harness._handle_tool_pre_hook(fc=fc, run_context=run_context)


def test_guardrails_apply_to_bash_start_network_calls(tmp_path):
    harness, _ = _make_harness(tmp_path)
    fc = SimpleNamespace(
        function=SimpleNamespace(name="bash_start"),
        arguments={"command": "curl https://localhost/internal"},
        result=None,
        error=None,
        call_id="tc-net-1",
    )

    with pytest.raises(AgentRunException) as exc:
        harness._handle_tool_pre_hook(fc=fc, run_context=_tool_run_context(harness))

    inner = exc.value.args[0]
    assert isinstance(inner, HarnessError)
    assert inner.code == "GUARDRAIL_DENIED"


# ── session_metadata on events ────────────────────────────────────────


def test_session_metadata_merged_into_events(tmp_path):
    """session_metadata dict is merged into every emitted event's payload."""
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(
        tmp_path,
        event_sink=sink,
        session_metadata={"deal_id": "acme-123", "fund_id": "fund-iii"},
    )
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    harness.run("hello")

    for event in sink.events:
        assert event.payload.get("deal_id") == "acme-123", (
            f"Event {event.event_type} missing deal_id"
        )
        assert event.payload.get("fund_id") == "fund-iii", (
            f"Event {event.event_type} missing fund_id"
        )


def test_session_metadata_does_not_clobber_event_payload(tmp_path):
    """Event-specific payload keys take precedence over session_metadata."""
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(
        tmp_path,
        event_sink=sink,
        session_metadata={"stream": "should-be-overridden"},
    )
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    harness.run("hello")

    started = [e for e in sink.events if e.event_type == "run.started"][0]
    # The run.started payload sets stream=False, which should win
    assert started.payload["stream"] is False


@pytest.mark.asyncio
async def test_session_metadata_on_async_events(tmp_path):
    """session_metadata works in the async path too."""
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(
        tmp_path,
        event_sink=sink,
        session_metadata={"tenant": "t-1"},
    )
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="ok"))

    await harness.arun("hello")

    for event in sink.events:
        assert event.payload.get("tenant") == "t-1", (
            f"Async event {event.event_type} missing tenant"
        )


# ── on_compaction callback ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_compaction_callback_fires(tmp_path):
    """on_compaction is called with the summary after compact_session()."""
    received: list[str] = []

    async def on_compaction(summary: str) -> None:
        received.append(summary)

    harness, mock_agent = _make_harness(tmp_path, on_compaction=on_compaction)
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="flushed"))

    # Mock session_summary_manager to return a summary
    mock_ssm = MagicMock()
    mock_ssm.create_session_summary.return_value = "Session summary text"
    mock_agent.session_summary_manager = mock_ssm
    mock_agent.get_session.return_value = SimpleNamespace(session_id="s-1")

    await harness.compact_session()

    assert received == ["Session summary text"]


@pytest.mark.asyncio
async def test_on_compaction_not_called_when_no_summary(tmp_path):
    """on_compaction is NOT called when there's no summary to report."""
    received: list[str] = []

    async def on_compaction(summary: str) -> None:
        received.append(summary)

    harness, mock_agent = _make_harness(tmp_path, on_compaction=on_compaction)
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="flushed"))
    mock_agent.session_summary_manager = None

    await harness.compact_session()

    assert received == []


@pytest.mark.asyncio
async def test_on_compaction_callback_exception_is_swallowed(tmp_path):
    """compact_session should not raise when on_compaction callback fails."""

    async def on_compaction(summary: str) -> None:
        del summary
        raise RuntimeError("callback failed")

    harness, mock_agent = _make_harness(tmp_path, on_compaction=on_compaction)
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="flushed"))
    mock_ssm = MagicMock()
    mock_ssm.create_session_summary.return_value = "Session summary text"
    mock_agent.session_summary_manager = mock_ssm
    mock_agent.get_session.return_value = SimpleNamespace(session_id="s-1")

    await harness.compact_session()


# ── end_session + on_session_end callback ─────────────────────────────


@pytest.mark.asyncio
async def test_end_session_generates_summary_and_fires_callback(tmp_path):
    """end_session() generates a summary and fires on_session_end."""
    received: list[str] = []

    async def on_session_end(summary: str) -> None:
        received.append(summary)

    harness, mock_agent = _make_harness(tmp_path, on_session_end=on_session_end)

    # Mock chat history so _generate_session_summary has messages
    mock_agent.get_chat_history.return_value = [
        SimpleNamespace(role="user", content="hello"),
        SimpleNamespace(role="assistant", content="world"),
    ]
    # Mock arun to return the summary response
    mock_agent.arun = AsyncMock(
        return_value=SimpleNamespace(content="- Decision 1\n- Decision 2")
    )

    result = await harness.end_session()

    assert result == "- Decision 1\n- Decision 2"
    assert received == ["- Decision 1\n- Decision 2"]


@pytest.mark.asyncio
async def test_end_session_skips_summary_when_disabled(tmp_path):
    """end_session(generate_summary=False) skips LLM call and callback."""
    received: list[str] = []

    async def on_session_end(summary: str) -> None:
        received.append(summary)

    harness, mock_agent = _make_harness(tmp_path, on_session_end=on_session_end)
    mock_agent.arun = AsyncMock()

    result = await harness.end_session(generate_summary=False)

    assert result is None
    assert received == []
    mock_agent.arun.assert_not_called()


@pytest.mark.asyncio
async def test_end_session_no_callback_still_returns_summary(tmp_path):
    """end_session() works without on_session_end callback."""
    harness, mock_agent = _make_harness(tmp_path)

    mock_agent.get_chat_history.return_value = [
        SimpleNamespace(role="user", content="hi"),
    ]
    mock_agent.arun = AsyncMock(
        return_value=SimpleNamespace(content="summary")
    )

    result = await harness.end_session()

    assert result == "summary"


@pytest.mark.asyncio
async def test_end_session_callback_exception_is_swallowed(tmp_path):
    """end_session should still return summary when callback raises."""

    async def on_session_end(summary: str) -> None:
        del summary
        raise RuntimeError("callback failed")

    harness, mock_agent = _make_harness(tmp_path, on_session_end=on_session_end)
    mock_agent.get_chat_history.return_value = [
        SimpleNamespace(role="user", content="hi"),
    ]
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="summary"))

    result = await harness.end_session()

    assert result == "summary"
