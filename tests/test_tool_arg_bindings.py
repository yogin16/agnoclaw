"""Tests for per-run tool argument binding (partial application) — issue #54.

A binding removes the named args from the schema the model sees AND supplies
them at dispatch (via a per-run ``functools.partial``), restoring both on exit.
"""

import logging
import tempfile
from types import SimpleNamespace

import pytest
from unittest.mock import MagicMock, patch

from agno.tools import tool
from agno.tools.function import Function

from agnoclaw.config import HarnessConfig


@tool
def save_thing(name: str, kind: str, schema: str, note: str = "") -> str:
    """Save a thing.

    Args:
      name: the name
      kind: the kind
      schema: the schema
      note: optional note
    """
    return f"{name}/{kind}/{schema}/{note}"


def _harness(tools=None, **kwargs):
    from agnoclaw.agent import AgentHarness

    return AgentHarness(
        workspace_dir=tempfile.mkdtemp(),
        config=HarnessConfig(),
        include_default_tools=False,
        tools=tools if tools is not None else [save_thing],
        **kwargs,
    )


def _live_function(harness, name):
    for tool_obj in harness._agent.tools:
        if isinstance(tool_obj, Function) and tool_obj.name == name:
            return tool_obj
    return None


def _model_facing(fn):
    """Resolve the schema + entrypoint the model/dispatch would see for ``fn``.

    Mirrors Agno's per-run tool resolution: deep-copy the function and run
    ``process_entrypoint`` (which keeps a stripped schema verbatim only because
    the binding sets ``skip_entrypoint_processing``).
    """
    logging.disable(logging.WARNING)
    try:
        copied = fn.model_copy(deep=True)
        copied.process_entrypoint()
        return copied
    finally:
        logging.disable(logging.NOTSET)


# ── Schema hiding + dispatch injection ───────────────────────────────────────


def test_bound_args_absent_from_schema_and_present_at_dispatch():
    harness = _harness()
    scope = harness._apply_tool_scope(
        arg_bindings={"save_thing": {"kind": "alpha", "schema": "beta"}}
    )
    try:
        fn = _live_function(harness, "save_thing")
        model_fn = _model_facing(fn)

        # bound args removed from BOTH properties and required (no dangling entry)
        assert sorted(model_fn.parameters["properties"]) == ["name", "note"]
        assert model_fn.parameters.get("required") == ["name"]

        # bound values supplied at dispatch; model only provides visible args
        assert model_fn.entrypoint(name="N", note="Z") == "N/alpha/beta/Z"
    finally:
        scope.restore()


def test_binding_restored_after_run_sees_full_signature():
    harness = _harness()
    fn = _live_function(harness, "save_thing")
    original_entrypoint = fn.entrypoint

    scope = harness._apply_tool_scope(
        arg_bindings={"save_thing": {"kind": "alpha", "schema": "beta"}}
    )
    scope.restore()

    # entrypoint + skip flag restored, and a fresh resolution shows all args
    assert fn.entrypoint is original_entrypoint
    assert fn.skip_entrypoint_processing is False
    model_fn = _model_facing(fn)
    assert sorted(model_fn.parameters["properties"]) == ["kind", "name", "note", "schema"]


def test_apply_tool_scope_returns_none_when_nothing_to_scope():
    harness = _harness()
    assert harness._apply_tool_scope() is None
    assert harness._apply_tool_scope(arg_bindings={}) is None
    assert harness._apply_tool_scope(arg_bindings={"save_thing": {}}) is not None  # tool present but empty values → scope created, no-op binding


def test_binding_unknown_tool_is_ignored():
    harness = _harness()
    scope = harness._apply_tool_scope(arg_bindings={"nope": {"a": 1}})
    try:
        # nothing to bind, but a scope object is still returned; no crash
        assert scope is not None
    finally:
        scope.restore()


# ── Composition ──────────────────────────────────────────────────────────────


def test_binding_composes_with_allowed_tools():
    @tool
    def other(x: str) -> str:
        "Other.\n\nArgs:\n  x: x"
        return x

    harness = _harness(tools=[save_thing, other])
    scope = harness._apply_tool_scope(
        allowed=["save_thing"],
        arg_bindings={"save_thing": {"kind": "k", "schema": "s"}},
    )
    try:
        names = {getattr(t, "name", None) for t in harness._agent.tools}
        assert names == {"save_thing"}  # allow-scoped
        fn = _live_function(harness, "save_thing")
        model_fn = _model_facing(fn)
        assert sorted(model_fn.parameters["properties"]) == ["name", "note"]
        assert model_fn.entrypoint(name="N") == "N/k/s/"
    finally:
        scope.restore()


def test_binding_composes_with_schema_overrides():
    # Override reshapes the VISIBLE args; binding strips the bound ones from it.
    override = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "renamed"},
            "kind": {"type": "string"},
            "schema": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["name", "kind", "schema"],
    }
    harness = _harness()
    scope = harness._apply_tool_scope(
        schema_overrides={"save_thing": override},
        arg_bindings={"save_thing": {"kind": "k", "schema": "s"}},
    )
    try:
        fn = _live_function(harness, "save_thing")
        # binding stripped kind/schema from the override-provided schema
        assert sorted(fn.parameters["properties"]) == ["name", "note"]
        assert fn.parameters.get("required") == ["name"]
        assert fn.parameters["properties"]["name"]["description"] == "renamed"
        assert fn.entrypoint(name="N", note="Z") == "N/k/s/Z"
    finally:
        scope.restore()
    # fully restored
    assert sorted(_model_facing(fn).parameters["properties"]) == ["kind", "name", "note", "schema"]


# ── run()/arun() threading ───────────────────────────────────────────────────


def _mock_agent_harness():
    from agnoclaw.agent import AgentHarness

    mock_agent = MagicMock()

    def _ctor(*a, **kw):
        mock_agent.system_message = kw.get("system_message")
        mock_agent.session_id = kw.get("session_id")
        mock_agent.tools = kw.get("tools")
        return mock_agent

    with patch("agnoclaw.agent.Agent", side_effect=_ctor):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=tempfile.mkdtemp(),
                config=HarnessConfig(),
                include_default_tools=False,
                tools=[save_thing],
            )
    return harness, mock_agent


def test_run_forwards_bindings_to_apply_tool_scope():
    harness, mock_agent = _mock_agent_harness()
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    with patch.object(harness, "_apply_tool_scope", wraps=harness._apply_tool_scope) as spy:
        harness.run("hi", tool_arg_bindings={"save_thing": {"kind": "k"}})

    # the non-streaming path applies the scope once with our bindings
    assert any(
        call.kwargs.get("arg_bindings") == {"save_thing": {"kind": "k"}}
        for call in spy.call_args_list
    )


@pytest.mark.asyncio
async def test_arun_forwards_bindings_to_apply_tool_scope():
    from unittest.mock import AsyncMock

    harness, mock_agent = _mock_agent_harness()
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="ok"))

    with patch.object(harness, "_apply_tool_scope", wraps=harness._apply_tool_scope) as spy:
        await harness.arun("hi", tool_arg_bindings={"save_thing": {"schema": "s"}})

    assert any(
        call.kwargs.get("arg_bindings") == {"save_thing": {"schema": "s"}}
        for call in spy.call_args_list
    )


# ── _skill_tool_scope_args resolution ────────────────────────────────────────


def test_skill_tool_scope_args_returns_bindings_triple():
    from agnoclaw.agent import AgentHarness

    allowed, overrides, bindings = AgentHarness._skill_tool_scope_args(
        None, None, {"save_thing": {"kind": "k"}}
    )
    assert allowed is None and overrides is None
    assert bindings == {"save_thing": {"kind": "k"}}


def test_skill_declared_bindings_merge_under_per_run():
    from agnoclaw.agent import AgentHarness

    skill = SimpleNamespace(
        meta=SimpleNamespace(
            allowed_tools=None,
            tool_schemas=None,
            tool_arg_bindings={"save_thing": {"kind": "from_skill"}},
        )
    )
    # per-run binding for the same tool wins
    _, _, bindings = AgentHarness._skill_tool_scope_args(
        skill, None, {"save_thing": {"kind": "from_run"}}
    )
    assert bindings == {"save_thing": {"kind": "from_run"}}


def test_skill_meta_parses_tool_arg_bindings(tmp_path):
    from agnoclaw.skills.loader import load_skill_from_path

    skill_dir = tmp_path / "binder"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: binder\n"
        "description: test\n"
        'tool-arg-bindings: {"save_thing": {"kind": "x"}}\n'
        "---\n\n"
        "Body.\n"
    )
    skill = load_skill_from_path(skill_dir / "SKILL.md")
    assert skill is not None
    assert skill.meta.tool_arg_bindings == {"save_thing": {"kind": "x"}}
