"""Agno-native skill disclosure and final-boundary restriction contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agno.exceptions import AgentRunException
from agno.tools.function import FunctionCall

from agnoclaw import AgentHarness
from agnoclaw.runtime.tool_ingress import builtin_effect, toolkit_functions
from agnoclaw.skills.registry import ModelSkillActivationError, SkillRegistry


def _write_skill(
    root: Path,
    *,
    name: str = "reviewer",
    extra_frontmatter: str = "",
    body: str = "Follow the review protocol for $ARGUMENTS.",
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Review changes safely\n"
        "disable-model-invocation: false\n"
        "allowed-tools: read_file\n"
        f"{extra_frontmatter}"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def test_model_activation_is_bounded_and_does_not_execute_inline_commands(
    tmp_path: Path,
) -> None:
    skills = tmp_path / "skills"
    marker = tmp_path / "must-not-exist"
    _write_skill(
        skills,
        body=f"Inspect $ARGUMENTS, then record !`touch {marker}`.",
    )
    registry = SkillRegistry(workspace_skills_dir=skills)

    activation = registry.activate_for_model("reviewer", "src/widget.py")

    assert activation.allowed_tools == ("read_file",)
    assert "src/widget.py" in activation.content
    assert f"!`touch {marker}`" in activation.content
    assert not marker.exists()
    assert activation.content_digest.startswith("sha256:")


def test_model_activation_denies_community_and_mid_run_semantic_changes(
    tmp_path: Path,
) -> None:
    local = tmp_path / "local"
    local.mkdir()
    community = tmp_path / "community"
    _write_skill(community, name="remote")
    _write_skill(local, name="forked", extra_frontmatter="context: fork\n")
    registry = SkillRegistry(workspace_skills_dir=local)
    registry.add_directory(community, trust="community")

    with pytest.raises(ModelSkillActivationError) as remote:
        registry.activate_for_model("remote")
    assert remote.value.code == "SKILL_MODEL_ACTIVATION_DENIED"

    with pytest.raises(ModelSkillActivationError) as forked:
        registry.activate_for_model("forked")
    assert forked.value.code == "SKILL_EXPLICIT_ACTIVATION_REQUIRED"


def test_agno_native_skill_tool_is_governed_and_enforces_allowed_tools(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _write_skill(workspace / "skills")
    harness = AgentHarness(
        model="openai:gpt-4o-mini",
        workspace_dir=workspace,
    )
    try:
        assert harness._agent.skills is harness._agno_skills
        skill_function = harness._agent.skills.get_tools()[0]
        assert skill_function.name == "get_skill_instructions"
        assert builtin_effect(skill_function) is not None
        assert skill_function.pre_hook is not None
        assert skill_function.post_hook is not None

        run_context = SimpleNamespace(
            run_id="run-skill-1",
            user_id="user-1",
            session_id="session-1",
            metadata={},
        )
        call = FunctionCall(
            function=skill_function,
            arguments={"skill_name": "reviewer", "arguments": "src/widget.py"},
            call_id="call-skill-1",
        )
        skill_function.pre_hook(agent=harness._agent, run_context=run_context, fc=call)
        try:
            result = skill_function.entrypoint(**dict(call.arguments or {}))
            call.result = result
        finally:
            skill_function.post_hook(agent=harness._agent, run_context=run_context, fc=call)

        envelope = json.loads(result)
        assert envelope["status"] == "activated"
        assert envelope["skill_name"] == "reviewer"
        assert envelope["allowed_tools"] == ["read_file"]

        write_file = next(
            function
            for tool in harness._agent.tools
            for function in (
                toolkit_functions(tool).values()
                if hasattr(tool, "functions")
                else ()
            )
            if function.name == "write_file"
        )
        denied_call = FunctionCall(
            function=write_file,
            arguments={"path": "blocked.txt", "content": "no"},
            call_id="call-write-1",
        )
        with pytest.raises(AgentRunException) as denied:
            write_file.pre_hook(
                agent=harness._agent,
                run_context=run_context,
                fc=denied_call,
            )
        assert denied.value.args[0].code == "SKILL_TOOL_NOT_ALLOWED"
    finally:
        harness._cleanup_tool_step_state("run-skill-1")
        harness.close()


def test_no_default_tools_means_no_model_skill_activation_surface(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_skill(workspace / "skills")
    harness = AgentHarness(
        model="openai:gpt-4o-mini",
        workspace_dir=workspace,
        include_default_tools=False,
    )
    try:
        assert harness._agno_skills is None
        assert harness._agent.skills is None
        assert "# Available Skills" not in harness.system_prompt
    finally:
        harness.close()


def test_internal_synthesis_cannot_gain_dynamic_skill_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_skill(workspace / "skills")
    harness = AgentHarness(model="openai:gpt-4o-mini", workspace_dir=workspace)
    try:
        assert harness._agent.skills.get_tools()
        token = harness._internal_run_kind.set("summary")
        try:
            assert harness._agent.skills.get_tools() == []
            assert harness._agent.skills.get_system_prompt_snippet() == ""
        finally:
            harness._internal_run_kind.reset(token)
    finally:
        harness.close()
