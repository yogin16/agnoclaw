"""Tests for per-run dependencies/session_state and active RunContext exposure.

Covers issue #52:
  - Ask A: ``run``/``arun`` accept per-run ``dependencies`` / ``session_state``
    (and context flags), merged over construction defaults and scoped per-run.
  - Ask B: a stable way to read the active ``RunContext`` / ``dependencies`` from
    a custom tool-dispatch path.
"""

from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agnoclaw.config import HarnessConfig


def _build_harness(tmp_path, **kwargs):
    """Build an AgentHarness whose underlying Agno Agent is a MagicMock.

    The mock captures the kwargs forwarded to ``Agent.run`` / ``Agent.arun`` so
    tests can assert on the per-run context/state values.
    """
    from agnoclaw.agent import AgentHarness

    mock_agent = MagicMock()
    ctor_kwargs: dict = {}

    def _agent_ctor(*args, **kw):
        ctor_kwargs.update(kw)
        mock_agent.system_message = kw.get("system_message")
        mock_agent.session_id = kw.get("session_id")
        return mock_agent

    with patch("agnoclaw.agent.Agent", side_effect=_agent_ctor):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=tmp_path,
                config=HarnessConfig(),
                **kwargs,
            )
    return harness, mock_agent, ctor_kwargs


# ── _merge_run_mapping ───────────────────────────────────────────────────────


def test_merge_run_mapping_override_wins():
    from agnoclaw.agent import AgentHarness

    merged = AgentHarness._merge_run_mapping({"a": 1, "b": 2}, {"b": 9, "c": 3})
    assert merged == {"a": 1, "b": 9, "c": 3}


def test_merge_run_mapping_empty_returns_none():
    from agnoclaw.agent import AgentHarness

    assert AgentHarness._merge_run_mapping({}, None) is None
    assert AgentHarness._merge_run_mapping(None, None) is None
    assert AgentHarness._merge_run_mapping({}, {}) is None


def test_merge_run_mapping_base_only_passthrough():
    from agnoclaw.agent import AgentHarness

    assert AgentHarness._merge_run_mapping({"a": 1}, None) == {"a": 1}


def test_merge_run_mapping_does_not_mutate_base():
    from agnoclaw.agent import AgentHarness

    base = {"a": 1}
    AgentHarness._merge_run_mapping(base, {"b": 2})
    assert base == {"a": 1}


# ── Construction-time wiring ─────────────────────────────────────────────────


def test_constructor_accepts_session_state(tmp_path):
    _, _, ctor_kwargs = _build_harness(
        tmp_path,
        session_state={"counter": 0},
        add_session_state_to_context=True,
    )
    assert ctor_kwargs["session_state"] == {"counter": 0}
    assert ctor_kwargs["add_session_state_to_context"] is True


def test_constructor_dependencies_forwarded(tmp_path):
    _, _, ctor_kwargs = _build_harness(
        tmp_path,
        dependencies={"tenant_id": "acme"},
    )
    assert ctor_kwargs["dependencies"] == {"tenant_id": "acme"}


# ── Per-run dependencies on run() ────────────────────────────────────────────


def test_run_merges_per_run_dependencies(tmp_path):
    harness, mock_agent, _ = _build_harness(
        tmp_path, dependencies={"tenant_id": "acme", "env": "prod"}
    )
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    harness.run("hi", dependencies={"user_id": "u1", "env": "staging"})

    forwarded = mock_agent.run.call_args.kwargs["dependencies"]
    # per-run keys win; construction defaults preserved otherwise
    assert forwarded == {"tenant_id": "acme", "env": "staging", "user_id": "u1"}


def test_run_without_per_run_deps_does_not_forward(tmp_path):
    """Default path leaves Agno to use the agent-level dependency default."""
    harness, mock_agent, _ = _build_harness(
        tmp_path, dependencies={"tenant_id": "acme"}
    )
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    harness.run("hi")

    assert "dependencies" not in mock_agent.run.call_args.kwargs


def test_run_per_run_session_state_merged(tmp_path):
    harness, mock_agent, _ = _build_harness(
        tmp_path, session_state={"a": 1}
    )
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    harness.run("hi", session_state={"b": 2})

    assert mock_agent.run.call_args.kwargs["session_state"] == {"a": 1, "b": 2}


def test_run_forwards_context_flags(tmp_path):
    harness, mock_agent, _ = _build_harness(tmp_path)
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    harness.run(
        "hi",
        dependencies={"x": 1},
        add_dependencies_to_context=True,
        add_session_state_to_context=False,
        knowledge_filters={"k": "v"},
    )

    kw = mock_agent.run.call_args.kwargs
    assert kw["add_dependencies_to_context"] is True
    assert kw["add_session_state_to_context"] is False
    assert kw["knowledge_filters"] == {"k": "v"}


def test_run_does_not_mutate_construction_dependencies(tmp_path):
    """Sequential runs never leak per-run dependencies into the harness default."""
    harness, mock_agent, _ = _build_harness(
        tmp_path, dependencies={"tenant_id": "acme"}
    )
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    harness.run("first", dependencies={"req": "r1"})
    harness.run("second", dependencies={"req": "r2"})

    # The harness default is untouched...
    assert harness._dependencies == {"tenant_id": "acme"}
    # ...and each run forwarded only its own merged view.
    first = mock_agent.run.call_args_list[0].kwargs["dependencies"]
    second = mock_agent.run.call_args_list[1].kwargs["dependencies"]
    assert first == {"tenant_id": "acme", "req": "r1"}
    assert second == {"tenant_id": "acme", "req": "r2"}


def test_run_exception_leaves_dependencies_intact(tmp_path):
    from agnoclaw.runtime import HarnessError

    harness, mock_agent, _ = _build_harness(
        tmp_path, dependencies={"tenant_id": "acme"}
    )
    mock_agent.run.side_effect = RuntimeError("boom")

    with pytest.raises(HarnessError):
        harness.run("hi", dependencies={"req": "r1"})

    assert harness._dependencies == {"tenant_id": "acme"}


def test_run_streaming_forwards_dependencies(tmp_path):
    harness, mock_agent, _ = _build_harness(
        tmp_path, dependencies={"tenant_id": "acme"}
    )
    mock_agent.run.return_value = iter([])

    stream = harness.run("hi", stream=True, dependencies={"req": "r1"})
    list(stream)  # drain to execute the wrapped generator

    forwarded = mock_agent.run.call_args.kwargs["dependencies"]
    assert forwarded == {"tenant_id": "acme", "req": "r1"}


# ── Per-run dependencies on arun() ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_arun_merges_per_run_dependencies(tmp_path):
    harness, mock_agent, _ = _build_harness(
        tmp_path, dependencies={"tenant_id": "acme"}
    )
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="ok"))

    await harness.arun("hi", dependencies={"req": "r1"})

    forwarded = mock_agent.arun.call_args.kwargs["dependencies"]
    assert forwarded == {"tenant_id": "acme", "req": "r1"}
    assert harness._dependencies == {"tenant_id": "acme"}


# ── Ask B: active RunContext exposure ────────────────────────────────────────


def test_get_current_run_context_default_none():
    from agnoclaw.agent import get_current_run_context, get_current_dependencies

    assert get_current_run_context() is None
    assert get_current_dependencies() is None


def test_active_run_context_set_and_cleared():
    from agnoclaw.agent import (
        AgentHarness,
        get_current_run_context,
        get_current_dependencies,
    )

    fc = SimpleNamespace()
    run_context = SimpleNamespace(dependencies={"tenant_id": "acme"})

    AgentHarness._set_active_run_context(fc, run_context)
    try:
        assert get_current_run_context() is run_context
        assert get_current_dependencies() == {"tenant_id": "acme"}
    finally:
        AgentHarness._clear_active_run_context(fc)

    assert get_current_run_context() is None
    assert get_current_dependencies() is None


def test_custom_dispatch_reads_dependencies():
    """A custom dispatch adapter reads caller scope via the documented accessor."""
    from agnoclaw.agent import (
        AgentHarness,
        get_current_dependencies,
    )

    seen: dict = {}

    def custom_dispatch_adapter():
        # No run_context parameter — reads from the contextvar instead.
        seen.update(get_current_dependencies() or {})

    fc = SimpleNamespace()
    run_context = SimpleNamespace(dependencies={"tenant_id": "acme", "user_id": "u1"})
    AgentHarness._set_active_run_context(fc, run_context)
    try:
        custom_dispatch_adapter()
    finally:
        AgentHarness._clear_active_run_context(fc)

    assert seen == {"tenant_id": "acme", "user_id": "u1"}


def test_get_current_dependencies_handles_missing():
    from agnoclaw.agent import (
        AgentHarness,
        get_current_dependencies,
    )

    fc = SimpleNamespace()
    # run_context without a dependencies attribute → None, no crash
    AgentHarness._set_active_run_context(fc, SimpleNamespace())
    try:
        assert get_current_dependencies() is None
    finally:
        AgentHarness._clear_active_run_context(fc)


# ── Harness-level dependency / session_state proxies ─────────────────────────


def test_dependencies_property_returns_copy(tmp_path):
    harness, _, _ = _build_harness(tmp_path, dependencies={"tenant_id": "acme"})
    deps = harness.dependencies
    assert deps == {"tenant_id": "acme"}
    deps["mutated"] = True
    # mutating the returned copy must not affect the harness default
    assert harness.dependencies == {"tenant_id": "acme"}


def test_update_dependencies_merges_and_propagates(tmp_path):
    harness, mock_agent, _ = _build_harness(tmp_path, dependencies={"tenant_id": "acme"})

    result = harness.update_dependencies({"region": "us-east"})

    assert result == {"tenant_id": "acme", "region": "us-east"}
    assert harness._dependencies == {"tenant_id": "acme", "region": "us-east"}
    # propagated to the underlying agent for subsequent default-path runs
    assert mock_agent.dependencies == {"tenant_id": "acme", "region": "us-east"}


def test_get_session_state_proxies_agent(tmp_path):
    harness, mock_agent, _ = _build_harness(tmp_path, session_id="s1")
    mock_agent.get_session_state.return_value = {"counter": 3}

    assert harness.get_session_state() == {"counter": 3}
    assert mock_agent.get_session_state.call_args.kwargs["session_id"] == "s1"


def test_update_session_state_proxies_agent(tmp_path):
    harness, mock_agent, _ = _build_harness(tmp_path, session_id="s1")
    mock_agent.update_session_state.return_value = "s1"

    harness.update_session_state({"counter": 4}, session_id="other")

    args, kwargs = mock_agent.update_session_state.call_args
    assert args[0] == {"counter": 4}
    assert kwargs["session_id"] == "other"


@pytest.mark.asyncio
async def test_aget_session_state_proxies_agent(tmp_path):
    harness, mock_agent, _ = _build_harness(tmp_path, session_id="s1")
    mock_agent.aget_session_state = AsyncMock(return_value={"counter": 9})

    assert await harness.aget_session_state() == {"counter": 9}


def test_real_tool_hook_exposes_run_context(tmp_path):
    """Drive the actual attached tool pre/post hooks (not the static helpers).

    Mirrors what Agno does: it calls ``function.pre_hook(fc=..., run_context=...)``
    before the tool entrypoint and ``function.post_hook(...)`` after. The harness
    wires those to set/clear the active RunContext so a custom dispatch adapter
    can read caller scope mid-tool-call.
    """
    from agno.tools.function import Function
    from agno.tools.toolkit import Toolkit

    from agnoclaw.agent import (
        AgentHarness,
        get_current_dependencies,
        get_current_run_context,
        get_current_tool_runtime,
    )

    harness = AgentHarness(
        workspace_dir=tmp_path,
        config=HarnessConfig(),
        include_default_tools=True,
    )

    # Find a default-tool Function that has the harness runtime hooks attached.
    target = None
    for tool in harness._agent.tools:
        if isinstance(tool, Toolkit):
            for fn in tool.functions.values():
                if getattr(fn.pre_hook, "_agnoclaw_runtime_pre", False):
                    target = fn
                    break
        elif isinstance(tool, Function) and getattr(
            getattr(tool, "pre_hook", None), "_agnoclaw_runtime_pre", False
        ):
            target = tool
        if target is not None:
            break
    assert target is not None, "expected a tool with runtime hooks attached"

    fc = SimpleNamespace(
        function=SimpleNamespace(name=target.name),
        arguments={},
        result=None,
        error=None,
    )
    run_context = SimpleNamespace(
        run_id="run_x",
        session_id="s",
        user_id="u",
        metadata={},
        dependencies={"tenant_id": "acme", "user_id": "u1"},
    )

    target.pre_hook(fc=fc, run_context=run_context)
    try:
        assert get_current_run_context() is run_context
        assert get_current_dependencies() == {"tenant_id": "acme", "user_id": "u1"}
        # Also surfaced through the existing tool-runtime accessor.
        runtime = get_current_tool_runtime()
        assert runtime is not None and runtime.get("run_context") is run_context
    finally:
        target.post_hook(fc=fc, run_context=run_context)

    assert get_current_run_context() is None
    assert get_current_dependencies() is None
