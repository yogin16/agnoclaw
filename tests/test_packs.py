"""agnoclaw pack manifest tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agnoclaw import AgentHarness, HarnessConfig
from agnoclaw.packs import PackTrustError, inspect_pack, load_pack


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
