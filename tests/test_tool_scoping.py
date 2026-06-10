"""
Per-run tool scoping + input-schema specialization (issue #49).

Covers:
  * honoring ``allowed_tools`` for inline skills (filtering the visible toolset),
  * per-run tool input-schema specialization (advertising a typed schema),
  * restoration of both after the run — including on exceptions mid-run,
  * no persisted mutation of ``Agent.tools`` across runs,
  * SkillMeta parsing of ``tool-schemas`` frontmatter.

These exercise the harness boundary directly; no model API is required.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from agno.tools import Toolkit
from agno.tools.function import Function

from agnoclaw import AgentHarness, HarnessError
from agnoclaw.skills.loader import Skill, SkillMeta, load_skill_from_path

# ── Fixtures / helpers ────────────────────────────────────────────────────────


class SaveKit(Toolkit):
    """A toolkit with one wanted tool and two distractor tools."""

    def __init__(self) -> None:
        super().__init__(name="savekit")
        self.register(self.save_artifact)
        self.register(self.grep_files)
        self.register(self.spawn_subagent)

    def save_artifact(self, content: dict) -> str:
        """Save an artifact."""
        return "saved"

    def grep_files(self, pattern: str) -> str:
        """Grep files."""
        return "grepped"

    def spawn_subagent(self, task: str) -> str:
        """Spawn a subagent."""
        return "spawned"


def standalone_tool(query: str) -> str:
    """A standalone (non-toolkit) tool."""
    return "ran"


CONTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {
            "type": "object",
            "properties": {
                "company_id": {"type": "string"},
                "new_money": {"type": "number"},
                "pre_money": {"type": "number"},
            },
            "required": ["company_id", "new_money", "pre_money"],
        }
    },
    "required": ["content"],
}


@pytest.fixture
def harness() -> AgentHarness:
    """A harness with a known, network-free toolset (no default tools)."""
    return AgentHarness(
        include_default_tools=False,
        tools=[SaveKit(), standalone_tool],
    )


def _advertised_parameters(function: Function) -> dict:
    """Reproduce what Agno advertises to the model for a Function.

    ``parse_tools`` deep-copies each Function and runs ``process_entrypoint``
    before sending the schema to the provider, so we do the same here to verify
    the *payload* (not just the stored attribute).
    """
    copied = function.model_copy(deep=True)
    copied.process_entrypoint(strict=False)
    return copied.parameters


# ── _apply_tool_scope / _restore_tool_scope ──────────────────────────────────


def test_allowed_tools_filters_toolkit_inline(harness: AgentHarness) -> None:
    original = harness._agent.tools
    scope = harness._apply_tool_scope(allowed=["save_artifact"])

    assert harness._tool_names(harness._agent.tools) == {"save_artifact"}
    # The single surfaced tool is the Function pulled out of the toolkit.
    assert all(isinstance(t, Function) for t in harness._agent.tools)

    scope.restore()
    assert harness._agent.tools is original
    assert harness._tool_names(harness._agent.tools) == {
        "save_artifact",
        "grep_files",
        "spawn_subagent",
        "standalone_tool",
    }


def test_allowed_tools_can_surface_a_standalone_tool(harness: AgentHarness) -> None:
    scope = harness._apply_tool_scope(allowed=["standalone_tool"])
    assert harness._tool_names(harness._agent.tools) == {"standalone_tool"}
    scope.restore()


def test_allowed_tools_empty_list_hides_everything(harness: AgentHarness) -> None:
    scope = harness._apply_tool_scope(allowed=[])
    assert harness._agent.tools == []
    scope.restore()
    assert harness._tool_names(harness._agent.tools)  # back to full set


def test_scope_noop_returns_none(harness: AgentHarness) -> None:
    assert harness._apply_tool_scope(allowed=None, schema_overrides=None) is None
    assert harness._apply_tool_scope() is None
    # An empty override mapping is also a no-op.
    assert harness._apply_tool_scope(schema_overrides={}) is None


def test_schema_override_specializes_payload_and_restores(harness: AgentHarness) -> None:
    save_fn = harness._agent.tools[0].functions["save_artifact"]
    original_params = save_fn.parameters
    # Baseline: content is an untyped dict — advertised with no nested properties.
    baseline = _advertised_parameters(save_fn)
    assert baseline["properties"]["content"].get("properties", {}) == {}

    scope = harness._apply_tool_scope(
        allowed=["save_artifact"],
        schema_overrides={"save_artifact": CONTENT_SCHEMA},
    )

    scoped_fn = harness._agent.tools[0]
    advertised = _advertised_parameters(scoped_fn)
    assert list(advertised["properties"]["content"]["properties"]) == [
        "company_id",
        "new_money",
        "pre_money",
    ]
    assert advertised["required"] == ["content"]

    scope.restore()
    # Original schema object restored by identity — no leaked mutation.
    assert harness._agent.tools[0].functions["save_artifact"].parameters is original_params


def test_schema_override_without_allowed_targets_toolkit_function(harness: AgentHarness) -> None:
    save_fn = harness._agent.tools[0].functions["save_artifact"]
    original_params = save_fn.parameters

    scope = harness._apply_tool_scope(schema_overrides={"save_artifact": CONTENT_SCHEMA})
    # Toolset is unchanged (no allowed filter) but the schema is specialized.
    assert harness._tool_names(harness._agent.tools) == {
        "save_artifact",
        "grep_files",
        "spawn_subagent",
        "standalone_tool",
    }
    assert "properties" in _advertised_parameters(save_fn)["properties"]["content"]

    scope.restore()
    assert save_fn.parameters is original_params


def test_schema_override_for_unknown_tool_is_ignored(harness: AgentHarness) -> None:
    scope = harness._apply_tool_scope(schema_overrides={"does_not_exist": CONTENT_SCHEMA})
    # Nothing to override, but the call still produces a (restorable) scope.
    assert scope is not None
    scope.restore()


def test_advertised_schema_is_isolated_from_override_dict(harness: AgentHarness) -> None:
    """Agno mutates the schema in place per run; the caller's dict must not leak."""
    overrides = {"save_artifact": CONTENT_SCHEMA}
    scope = harness._apply_tool_scope(allowed=["save_artifact"], schema_overrides=overrides)
    _advertised_parameters(harness._agent.tools[0])  # triggers in-place processing on the copy
    # Our source dict is untouched (deep-copied on apply).
    assert "additionalProperties" not in CONTENT_SCHEMA
    scope.restore()


def test_restore_is_idempotent(harness: AgentHarness) -> None:
    original = harness._agent.tools
    scope = harness._apply_tool_scope(allowed=["save_artifact"])
    scope.restore()
    scope.restore()  # second call is a no-op
    assert harness._agent.tools is original


def test_duplicate_tool_name_surfaces_once_and_override_matches() -> None:
    """A name present in two toolkits is surfaced once, and the override hits it."""

    class KitA(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="kit_a")
            self.register(self.save_artifact)

        def save_artifact(self, content: dict) -> str:
            """Save (A)."""
            return "a"

    class KitB(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="kit_b")
            self.register(self.save_artifact)

        def save_artifact(self, content: dict) -> str:
            """Save (B)."""
            return "b"

    h = AgentHarness(include_default_tools=False, tools=[KitA(), KitB()])
    scope = h._apply_tool_scope(
        allowed=["save_artifact"], schema_overrides={"save_artifact": CONTENT_SCHEMA}
    )

    # Exactly one tool is advertised (no double-listing under the same name)...
    assert len(h._agent.tools) == 1
    # ...and it is the one whose schema was specialized.
    advertised = _advertised_parameters(h._agent.tools[0])
    assert "properties" in advertised["properties"]["content"]

    scope.restore()
    assert len(h._agent.tools) == 2


# ── _skill_tool_scope_args ────────────────────────────────────────────────────


def test_scope_args_reads_skill_meta(harness: AgentHarness) -> None:
    meta = SkillMeta(
        name="saver",
        allowed_tools=["save_artifact"],
        tool_schemas={"save_artifact": CONTENT_SCHEMA},
    )
    skill = Skill(meta=meta, content="x", path=Path("x"))
    allowed, overrides = harness._skill_tool_scope_args(skill, None)
    assert allowed == ["save_artifact"]
    assert overrides == {"save_artifact": CONTENT_SCHEMA}


def test_scope_args_explicit_overrides_take_precedence(harness: AgentHarness) -> None:
    meta = SkillMeta(name="saver", tool_schemas={"save_artifact": {"type": "object"}})
    skill = Skill(meta=meta, content="x", path=Path("x"))
    explicit = {"save_artifact": CONTENT_SCHEMA}
    _allowed, overrides = harness._skill_tool_scope_args(skill, explicit)
    assert overrides["save_artifact"] is CONTENT_SCHEMA


def test_scope_args_no_skill(harness: AgentHarness) -> None:
    allowed, overrides = harness._skill_tool_scope_args(None, None)
    assert allowed is None
    assert overrides is None
    allowed, overrides = harness._skill_tool_scope_args(None, {"t": CONTENT_SCHEMA})
    assert allowed is None
    assert overrides == {"t": CONTENT_SCHEMA}


# ── End-to-end run()/arun() integration ───────────────────────────────────────


def _install_skill(harness: AgentHarness, meta: SkillMeta, monkeypatch) -> None:
    skill = Skill(meta=meta, content="Read the numbers and save them.", path=Path("x"))
    monkeypatch.setattr(harness.skills, "load_skill", lambda name: skill.content)
    monkeypatch.setattr(harness.skills, "_get_skill", lambda name: skill)


def test_run_honors_allowed_tools_inline_and_restores(harness: AgentHarness, monkeypatch) -> None:
    _install_skill(harness, SkillMeta(name="saver", allowed_tools=["save_artifact"]), monkeypatch)

    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured["names"] = harness._tool_names(harness._agent.tools)
        return SimpleNamespace(content="ok")

    monkeypatch.setattr(harness._agent, "run", fake_run)

    harness.run("acme 5 20", skill="saver")

    # During the run, only the allowed tool was visible to the model.
    assert captured["names"] == {"save_artifact"}
    # After the run, the full toolset is restored (no persisted mutation).
    assert harness._tool_names(harness._agent.tools) == {
        "save_artifact",
        "grep_files",
        "spawn_subagent",
        "standalone_tool",
    }
    assert any(isinstance(t, Toolkit) for t in harness._agent.tools)


def test_run_applies_schema_override_in_payload(harness: AgentHarness, monkeypatch) -> None:
    _install_skill(
        harness,
        SkillMeta(
            name="saver",
            allowed_tools=["save_artifact"],
            tool_schemas={"save_artifact": CONTENT_SCHEMA},
        ),
        monkeypatch,
    )

    captured: dict = {}

    def fake_run(*args, **kwargs):
        (fn,) = harness._agent.tools
        captured["params"] = _advertised_parameters(fn)
        return SimpleNamespace(content="ok")

    monkeypatch.setattr(harness._agent, "run", fake_run)

    harness.run("acme 5 20", skill="saver")

    assert list(captured["params"]["properties"]["content"]["properties"]) == [
        "company_id",
        "new_money",
        "pre_money",
    ]
    # Restored to the untyped baseline after the run.
    restored = harness._agent.tools[0].functions["save_artifact"]
    assert _advertised_parameters(restored)["properties"]["content"].get("properties", {}) == {}


def test_run_restores_scope_on_exception(harness: AgentHarness, monkeypatch) -> None:
    _install_skill(harness, SkillMeta(name="saver", allowed_tools=["save_artifact"]), monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("model exploded mid-run")

    monkeypatch.setattr(harness._agent, "run", boom)

    with pytest.raises(HarnessError):
        harness.run("acme 5 20", skill="saver")

    # Toolset restored even though the run raised.
    assert harness._tool_names(harness._agent.tools) == {
        "save_artifact",
        "grep_files",
        "spawn_subagent",
        "standalone_tool",
    }


def test_run_without_skill_leaves_toolset_untouched(harness: AgentHarness, monkeypatch) -> None:
    before = harness._agent.tools

    def fake_run(*args, **kwargs):
        assert harness._agent.tools is before  # no scoping applied
        return SimpleNamespace(content="ok")

    monkeypatch.setattr(harness._agent, "run", fake_run)
    harness.run("hello")
    assert harness._agent.tools is before


def test_run_programmatic_override_without_skill(harness: AgentHarness, monkeypatch) -> None:
    captured: dict = {}

    def fake_run(*args, **kwargs):
        fn = harness._agent.tools[0].functions["save_artifact"]
        captured["params"] = _advertised_parameters(fn)
        return SimpleNamespace(content="ok")

    monkeypatch.setattr(harness._agent, "run", fake_run)
    harness.run("hi", tool_schema_overrides={"save_artifact": CONTENT_SCHEMA})

    assert "properties" in captured["params"]["properties"]["content"]
    # Full toolset preserved (override only, no allowed filter).
    assert any(isinstance(t, Toolkit) for t in harness._agent.tools)


@pytest.mark.asyncio
async def test_arun_honors_allowed_tools_inline(harness: AgentHarness, monkeypatch) -> None:
    _install_skill(harness, SkillMeta(name="saver", allowed_tools=["save_artifact"]), monkeypatch)

    captured: dict = {}

    async def fake_arun(*args, **kwargs):
        captured["names"] = harness._tool_names(harness._agent.tools)
        return SimpleNamespace(content="ok")

    monkeypatch.setattr(harness._agent, "arun", fake_arun)

    await harness.arun("acme 5 20", skill="saver")

    assert captured["names"] == {"save_artifact"}
    assert harness._tool_names(harness._agent.tools) == {
        "save_artifact",
        "grep_files",
        "spawn_subagent",
        "standalone_tool",
    }


@pytest.mark.asyncio
async def test_arun_streaming_unconsumed_does_not_leak_scope(
    harness: AgentHarness, monkeypatch
) -> None:
    """Async counterpart: an abandoned async stream must not leak the scope."""
    import gc

    _install_skill(harness, SkillMeta(name="saver", allowed_tools=["save_artifact"]), monkeypatch)

    async def empty_stream(*args, **kwargs):
        return
        yield  # async generator that yields nothing

    monkeypatch.setattr(harness._agent, "arun", lambda *a, **k: empty_stream())

    full = {"save_artifact", "grep_files", "spawn_subagent", "standalone_tool"}
    stream = await harness.arun("acme 5 20", skill="saver", stream=True)
    assert harness._tool_names(harness._agent.tools) == full  # not yet applied
    await stream.aclose()
    assert harness._tool_names(harness._agent.tools) == full
    del stream
    gc.collect()
    assert harness._tool_names(harness._agent.tools) == full


@pytest.mark.asyncio
async def test_arun_restores_scope_on_exception(harness: AgentHarness, monkeypatch) -> None:
    _install_skill(harness, SkillMeta(name="saver", allowed_tools=["save_artifact"]), monkeypatch)

    async def boom(*args, **kwargs):
        raise RuntimeError("async model exploded")

    monkeypatch.setattr(harness._agent, "arun", boom)

    with pytest.raises(HarnessError):
        await harness.arun("acme 5 20", skill="saver")

    assert any(isinstance(t, Toolkit) for t in harness._agent.tools)


def test_run_streaming_scopes_during_iteration_and_restores(
    harness: AgentHarness, monkeypatch
) -> None:
    """A streamed run scopes the toolset *while the generator runs*, then restores.

    Agno resolves tools lazily during iteration, so the scope is applied inside the
    stream wrapper (not at call time) and torn down in its finally.
    """
    _install_skill(harness, SkillMeta(name="saver", allowed_tools=["save_artifact"]), monkeypatch)

    captured: dict = {}

    def fake_run(*args, **kwargs):
        def events():
            captured["during"] = harness._tool_names(harness._agent.tools)
            return
            yield  # make this a generator

        return events()

    monkeypatch.setattr(harness._agent, "run", fake_run)

    stream = harness.run("acme 5 20", skill="saver", stream=True)
    list(stream)  # drain the wrapped stream → applies scope, then restores it

    assert captured["during"] == {"save_artifact"}
    assert harness._tool_names(harness._agent.tools) == {
        "save_artifact",
        "grep_files",
        "spawn_subagent",
        "standalone_tool",
    }


def test_run_streaming_unconsumed_does_not_leak_scope(
    harness: AgentHarness, monkeypatch
) -> None:
    """An abandoned stream must not permanently corrupt the harness toolset.

    Regression for the leak where the scope was applied before the generator was
    returned: an unstarted generator's finally never runs, so close()/GC could not
    restore. Binding the scope to generator execution fixes it.
    """
    import gc

    _install_skill(harness, SkillMeta(name="saver", allowed_tools=["save_artifact"]), monkeypatch)
    monkeypatch.setattr(harness._agent, "run", lambda *a, **k: iter(()))

    full = {"save_artifact", "grep_files", "spawn_subagent", "standalone_tool"}

    stream = harness.run("acme 5 20", skill="saver", stream=True)
    assert harness._tool_names(harness._agent.tools) == full  # not yet applied
    stream.close()
    assert harness._tool_names(harness._agent.tools) == full
    del stream
    gc.collect()
    assert harness._tool_names(harness._agent.tools) == full


def test_sequential_runs_dont_leak_scope(harness: AgentHarness, monkeypatch) -> None:
    """A scoped run must not affect a later run with a different (or no) skill."""
    seen: list = []

    def fake_run(*args, **kwargs):
        seen.append(harness._tool_names(harness._agent.tools))
        return SimpleNamespace(content="ok")

    monkeypatch.setattr(harness._agent, "run", fake_run)

    _install_skill(harness, SkillMeta(name="saver", allowed_tools=["save_artifact"]), monkeypatch)
    harness.run("first", skill="saver")

    # Second run: no skill → full toolset, proving the first run's scope was released.
    monkeypatch.setattr(harness.skills, "load_skill", lambda name: None)
    harness.run("second")

    assert seen[0] == {"save_artifact"}
    assert seen[1] == {"save_artifact", "grep_files", "spawn_subagent", "standalone_tool"}


# ── Loader parsing ────────────────────────────────────────────────────────────


def test_loader_parses_tool_schemas(tmp_path: Path) -> None:
    skill_dir = tmp_path / "saver"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: saver\n"
        "description: Save a dilution seed\n"
        "allowed-tools: save_artifact\n"
        "tool-schemas:\n"
        "  save_artifact:\n"
        "    type: object\n"
        "    properties:\n"
        "      content:\n"
        "        type: object\n"
        "        properties:\n"
        "          company_id:\n"
        "            type: string\n"
        "    required:\n"
        "      - content\n"
        "---\n\n"
        "# Saver\n",
        encoding="utf-8",
    )

    skill = load_skill_from_path(skill_dir / "SKILL.md")
    assert skill is not None
    assert skill.meta.allowed_tools == ["save_artifact"]
    assert skill.meta.tool_schemas["save_artifact"]["properties"]["content"]["type"] == "object"
    assert skill.meta.tool_schemas["save_artifact"]["required"] == ["content"]


def test_loader_ignores_non_object_tool_schema(tmp_path: Path) -> None:
    skill_dir = tmp_path / "bad"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: bad\n"
        "tool-schemas:\n"
        "  save_artifact: not-a-schema\n"
        "---\n\n# Bad\n",
        encoding="utf-8",
    )
    skill = load_skill_from_path(skill_dir / "SKILL.md")
    assert skill is not None
    assert skill.meta.tool_schemas == {}


def test_skillmeta_tool_schemas_defaults_empty() -> None:
    assert SkillMeta(name="x").tool_schemas == {}
