"""agnoclaw pack manifest tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agnoclaw import AgentHarness, HarnessConfig, HarnessError, InMemoryEventSink
from agnoclaw.packs import (
    PackError,
    PackTrustError,
    inspect_pack,
    install_pack,
    is_pack_trusted,
    list_installed_packs,
    load_pack,
    remove_pack,
    trust_pack,
)
from agnoclaw.runtime import ExecutionContext, PolicyDecision, RunInput


def _write_pack(tmp_path, manifest: str):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "agnoclaw-pack.toml").write_text(manifest, encoding="utf-8")
    return pack


def test_inspect_pack_parses_manifest_without_executing_code(tmp_path):
    pack = _write_pack(
        tmp_path,
        """
name = "deal-pack"
version = "0.1.0"
description = "Deal workflow"

[provides]
skills = ["skills/"]
tools = ["deal_pack.tools:register"]

[trust]
default = "local"
requires_code_execution = true
""",
    )

    manifest = inspect_pack(pack)

    assert manifest.name == "deal-pack"
    assert manifest.provides.skills == ["skills/"]
    assert manifest.provides.tools == ["deal_pack.tools:register"]
    assert manifest.trust.requires_code_execution is True


def test_load_pack_rejects_untrusted_code_execution(tmp_path):
    pack = _write_pack(
        tmp_path,
        """
name = "code-pack"

[provides]
tools = ["code_pack.tools:register"]

[trust]
requires_code_execution = true
""",
    )

    with pytest.raises(PackTrustError):
        load_pack(pack)


def test_load_pack_allows_skills_only_without_trust(tmp_path):
    pack = _write_pack(
        tmp_path,
        """
name = "skills-pack"

[provides]
skills = ["skills/"]
""",
    )
    (pack / "skills").mkdir()

    loaded = load_pack(pack)

    assert loaded.manifest.name == "skills-pack"
    assert loaded.skills_dirs == [(pack / "skills").resolve()]


def test_harness_loads_pack_skill_directory(tmp_path):
    pack = _write_pack(
        tmp_path,
        """
name = "skills-pack"

[provides]
skills = ["skills/"]
""",
    )
    skill_dir = pack / "skills" / "pack-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: pack-skill\ndescription: From pack\n---\n\nPack body.\n",
        encoding="utf-8",
    )

    with patch("agnoclaw.agent.Agent") as agent_cls:
        mock_agent = MagicMock()
        mock_agent.system_message = ""
        agent_cls.return_value = mock_agent
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=tmp_path / "workspace",
                config=HarnessConfig(),
                include_default_tools=False,
                packs=[pack],
            )

    assert "Pack body." in harness.skills.load_skill("pack-skill")
    assert harness._loaded_packs[0].manifest.name == "skills-pack"


def test_pack_install_trust_list_and_remove(tmp_path):
    pack = _write_pack(
        tmp_path,
        """
name = "Code Pack"
version = "0.2.0"
description = "Installed pack"

[provides]
tools = ["code_pack.tools:register"]

[trust]
requires_code_execution = true
""",
    )
    store = tmp_path / "store"

    manifest = install_pack(pack, root=store)
    installed = list_installed_packs(root=store)

    assert manifest.name == "Code Pack"
    assert [item.name for item in installed] == ["Code Pack"]
    assert is_pack_trusted("Code Pack", root=store) is False

    trusted = trust_pack("Code Pack", root=store)

    assert trusted.name == "Code Pack"
    assert is_pack_trusted("Code Pack", root=store) is True
    assert remove_pack("Code Pack", root=store) is True
    assert list_installed_packs(root=store) == []


def test_load_pack_honors_local_trust_marker(tmp_path):
    pack = _write_pack(
        tmp_path,
        """
name = "hook-pack"

[provides]
hooks = ["hook_pack.hooks:register"]

[trust]
requires_code_execution = true
""",
    )
    module_dir = pack / "hook_pack"
    module_dir.mkdir()
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    (module_dir / "hooks.py").write_text(
        "def register():\n"
        "    def before(run_input, context):\n"
        "        run_input.metadata['hooked'] = True\n"
        "        return run_input\n"
        "    return {'pre_run_hooks': [before]}\n",
        encoding="utf-8",
    )
    store = tmp_path / "store"
    manifest = install_pack(pack, root=store)
    trust_pack(manifest.name, root=store)

    loaded = load_pack(manifest.root)

    assert loaded.manifest.name == "hook-pack"
    assert len(loaded.pre_run_hooks) == 1


def test_load_pack_collects_lifecycle_hooks(tmp_path):
    pack = _write_pack(
        tmp_path,
        """
name = "lifecycle-pack"

[provides]
hooks = ["lifecycle_pack_events.hooks:register"]

[trust]
requires_code_execution = true
""",
    )
    module_dir = pack / "lifecycle_pack_events"
    module_dir.mkdir()
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    (module_dir / "hooks.py").write_text(
        "def register():\n"
        "    def on_end(event, context):\n"
        "        event.metadata['ended'] = True\n"
        "        return event\n"
        "    return {'lifecycle_hooks': {'session.end.completed': [on_end]}}\n",
        encoding="utf-8",
    )

    loaded = load_pack(pack, trusted=True)

    assert list(loaded.lifecycle_hooks) == ["session.end.completed"]
    assert len(loaded.lifecycle_hooks["session.end.completed"]) == 1


def test_load_pack_rejects_non_callable_lifecycle_hook(tmp_path):
    pack = _write_pack(
        tmp_path,
        """
name = "bad-lifecycle-pack"

[provides]
hooks = ["bad_lifecycle_pack.hooks:register"]

[trust]
requires_code_execution = true
""",
    )
    module_dir = pack / "bad_lifecycle_pack"
    module_dir.mkdir()
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    (module_dir / "hooks.py").write_text(
        "def register():\n"
        "    return {'lifecycle_hooks': {'session.end.completed': ['bad']}}\n",
        encoding="utf-8",
    )

    with pytest.raises(PackError, match="list of callables"):
        load_pack(pack, trusted=True)


def test_harness_emits_pack_hook_events(tmp_path):
    pack = _write_pack(
        tmp_path,
        """
name = "hook-pack"

[provides]
hooks = ["hook_pack.hooks:register"]

[trust]
requires_code_execution = true
""",
    )
    module_dir = pack / "hook_pack"
    module_dir.mkdir()
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    (module_dir / "hooks.py").write_text(
        "def register():\n"
        "    def before(run_input, context):\n"
        "        run_input.metadata['hooked'] = True\n"
        "        return run_input\n"
        "    return {'pre_run_hooks': [before]}\n",
        encoding="utf-8",
    )
    sink = InMemoryEventSink()

    with patch("agnoclaw.agent.Agent") as agent_cls:
        mock_agent = MagicMock()
        mock_agent.system_message = ""
        agent_cls.return_value = mock_agent
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=tmp_path / "workspace",
                config=HarnessConfig(),
                include_default_tools=False,
                packs=[pack],
                trusted_packs=True,
                event_sink=sink,
            )

    run_input = RunInput(
        run_id="run-pack",
        message="hello",
        skill=None,
        stream=False,
        stream_events=False,
    )
    context = ExecutionContext.create(
        user_id=None,
        session_id=None,
        workspace_id=str(harness.workspace.path),
    )

    updated = harness._run_pre_hooks_sync(run_input=run_input, context=context)

    assert updated.metadata["hooked"] is True
    assert [event.event_type for event in sink.events] == [
        "pack.hook.started",
        "pack.hook.completed",
    ]


@pytest.mark.asyncio
async def test_harness_emits_pack_lifecycle_hook_events(tmp_path):
    pack = _write_pack(
        tmp_path,
        """
name = "lifecycle-pack"

[provides]
hooks = ["lifecycle_pack.hooks:register"]

[trust]
requires_code_execution = true
""",
    )
    module_dir = pack / "lifecycle_pack"
    module_dir.mkdir()
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    (module_dir / "hooks.py").write_text(
        "def register():\n"
        "    def on_end(event, context):\n"
        "        event.metadata['ended'] = True\n"
        "        return event\n"
        "    return {'session_end_hooks': [on_end]}\n",
        encoding="utf-8",
    )
    sink = InMemoryEventSink()

    with patch("agnoclaw.agent.Agent") as agent_cls:
        mock_agent = MagicMock()
        mock_agent.system_message = ""
        agent_cls.return_value = mock_agent
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=tmp_path / "workspace",
                config=HarnessConfig(),
                include_default_tools=False,
                packs=[pack],
                trusted_packs=True,
                event_sink=sink,
            )
    result = await harness.end_session(generate_summary=False)

    assert result is None
    assert [event.event_type for event in sink.events] == [
        "pack.hook.started",
        "pack.hook.completed",
    ]
    assert sink.events[0].payload["kind"] == "lifecycle"
    assert sink.events[0].payload["lifecycle_event"] == "session.end.completed"


def test_harness_runs_pack_policies_and_emits_events(tmp_path):
    pack = _write_pack(
        tmp_path,
        """
name = "policy-pack"

[provides]
policies = ["policy_pack.policies:register"]

[trust]
requires_code_execution = true
""",
    )
    module_dir = pack / "policy_pack"
    module_dir.mkdir()
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    (module_dir / "policies.py").write_text(
        "from agnoclaw.runtime import PolicyDecision\n\n"
        "class DenyPolicy:\n"
        "    def before_run(self, run_input, context):\n"
        "        return PolicyDecision.deny(\n"
        "            reason_code='PACK_DENIED',\n"
        "            message='Denied by pack policy.',\n"
        "        )\n\n"
        "def register():\n"
        "    return DenyPolicy()\n",
        encoding="utf-8",
    )
    sink = InMemoryEventSink()

    with patch("agnoclaw.agent.Agent") as agent_cls:
        mock_agent = MagicMock()
        mock_agent.system_message = ""
        agent_cls.return_value = mock_agent
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=tmp_path / "workspace",
                config=HarnessConfig(),
                include_default_tools=False,
                packs=[pack],
                trusted_packs=True,
                event_sink=sink,
            )

    with pytest.raises(HarnessError) as exc_info:
        harness.run("hello")

    assert exc_info.value.details["reason_code"] == "PACK_DENIED"
    assert mock_agent.run.called is False
    pack_events = [
        event.event_type for event in sink.events if event.event_type.startswith("pack.policy.")
    ]
    assert pack_events == ["pack.policy.started", "pack.policy.completed"]


def test_set_policy_engine_preserves_pack_policies(tmp_path):
    pack = _write_pack(
        tmp_path,
        """
name = "policy-pack"

[provides]
policies = ["policy_pack.policies:register"]

[trust]
requires_code_execution = true
""",
    )
    module_dir = pack / "policy_pack"
    module_dir.mkdir()
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    (module_dir / "policies.py").write_text(
        "from agnoclaw.runtime import PolicyDecision\n\n"
        "class AllowPolicy:\n"
        "    def before_run(self, run_input, context):\n"
        "        return PolicyDecision.allow()\n\n"
        "def register():\n"
        "    return AllowPolicy()\n",
        encoding="utf-8",
    )

    with patch("agnoclaw.agent.Agent") as agent_cls:
        mock_agent = MagicMock()
        mock_agent.system_message = ""
        agent_cls.return_value = mock_agent
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=tmp_path / "workspace",
                config=HarnessConfig(),
                include_default_tools=False,
                packs=[pack],
                trusted_packs=True,
            )

    class ReplacementPolicy:
        def before_run(self, run_input, context):
            return PolicyDecision.allow()

    harness.set_policy_engine(ReplacementPolicy())

    assert [name for name, _ in harness._policy_engines] == ["harness", "pack:policy-pack"]
