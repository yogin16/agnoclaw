"""Tests for the skills system."""

from pathlib import Path
import tempfile
import pytest

from agnoclaw.skills.loader import load_skill_from_path
from agnoclaw.skills.registry import SkillRegistry


SAMPLE_SKILL_MD = """---
name: test-skill
description: A test skill for unit testing
user-invocable: true
disable-model-invocation: false
allowed-tools: bash, web_search
---

# Test Skill

This is a test skill. Arguments: $ARGUMENTS

First arg: $ARGUMENTS[0]
"""


@pytest.fixture
def skill_dir(tmp_path):
    """Create a temporary skill directory."""
    skill_path = tmp_path / "test-skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text(SAMPLE_SKILL_MD)
    return tmp_path


def test_load_skill_from_path(skill_dir):
    skill_md = skill_dir / "test-skill" / "SKILL.md"
    skill = load_skill_from_path(skill_md)

    assert skill is not None
    assert skill.name == "test-skill"
    assert skill.meta.description == "A test skill for unit testing"
    assert skill.meta.user_invocable is True
    assert skill.meta.disable_model_invocation is False
    assert "bash" in skill.meta.allowed_tools
    assert "web_search" in skill.meta.allowed_tools


def test_skill_render_arguments(skill_dir):
    skill_md = skill_dir / "test-skill" / "SKILL.md"
    skill = load_skill_from_path(skill_md)

    rendered = skill.render("hello world")
    assert "hello world" in rendered
    assert "hello" in rendered  # $ARGUMENTS[0]


def test_skill_render_no_arguments(skill_dir):
    skill_md = skill_dir / "test-skill" / "SKILL.md"
    skill = load_skill_from_path(skill_md)

    rendered = skill.render()
    assert "$ARGUMENTS" not in rendered  # substituted to empty string


def test_skill_registry_discovery(skill_dir):
    registry = SkillRegistry(workspace_skills_dir=skill_dir)
    skills = registry.discover_all()

    assert len(skills) >= 1
    names = [s.name for s in skills]
    assert "test-skill" in names


def test_skill_registry_load(skill_dir):
    registry = SkillRegistry(workspace_skills_dir=skill_dir)
    content = registry.load_skill("test-skill", arguments="foo bar")

    assert content is not None
    assert "Test Skill" in content
    assert "foo bar" in content


def test_skill_registry_missing(skill_dir):
    registry = SkillRegistry(workspace_skills_dir=skill_dir)
    content = registry.load_skill("nonexistent-skill")
    assert content is None


def test_bundled_skills_discoverable():
    """Bundled skills should be discoverable from the package."""
    registry = SkillRegistry()
    skills = registry.discover_all()
    names = [s.name for s in skills]

    # At minimum the bundled skills should be found
    expected = ["deep-research", "code-review", "git-workflow", "daily-standup", "memory-manage"]
    for expected_name in expected:
        assert expected_name in names, f"Bundled skill '{expected_name}' not found. Found: {names}"
