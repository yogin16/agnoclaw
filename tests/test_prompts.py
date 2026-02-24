"""Tests for the system prompt assembler and sections."""

import pytest
from pathlib import Path

from agnoclaw.prompts.system import SystemPromptBuilder
from agnoclaw.prompts.sections import (
    IDENTITY,
    TONE_AND_STYLE,
    DOING_TASKS,
    TOOL_GUIDELINES,
    SECURITY,
    GIT_PROTOCOL,
    MEMORY_INSTRUCTIONS,
    SKILL_INSTRUCTIONS,
    LEARNING_INSTRUCTIONS,
    PLAN_MODE,
)


@pytest.fixture
def builder(tmp_path):
    return SystemPromptBuilder(tmp_path)


# ── Section content tests ────────────────────────────────────────────────


def test_identity_contains_workspace_placeholder():
    assert "{workspace_dir}" in IDENTITY


def test_identity_formats_correctly():
    result = IDENTITY.format(workspace_dir="/tmp/workspace")
    assert "/tmp/workspace" in result
    assert "{workspace_dir}" not in result


def test_tone_no_emojis_rule():
    assert "emoji" in TONE_AND_STYLE.lower()


def test_doing_tasks_has_all_items(tmp_path):
    # Check key behavioral rules are present
    assert "parallel" in DOING_TASKS.lower()
    assert "reversible" in DOING_TASKS.lower()
    assert "commit" in DOING_TASKS.lower()
    assert "context" in DOING_TASKS.lower()  # new context awareness rule


def test_security_mentions_injection():
    assert "injection" in SECURITY.lower()


def test_git_protocol_mentions_force_push():
    assert "force" in GIT_PROTOCOL.lower()
    assert "main" in GIT_PROTOCOL.lower()


def test_memory_instructions_all_files():
    """All workspace context files should be mentioned."""
    for fname in ("AGENTS.md", "SOUL.md", "USER.md", "MEMORY.md", "TOOLS.md", "BOOT.md"):
        assert fname in MEMORY_INSTRUCTIONS, f"{fname} missing from MEMORY_INSTRUCTIONS"


def test_memory_instructions_boot_protocol():
    assert "BOOT.md" in MEMORY_INSTRUCTIONS
    assert "startup" in MEMORY_INSTRUCTIONS.lower() or "boot" in MEMORY_INSTRUCTIONS.lower()


def test_plan_mode_no_implementation_rule():
    assert "no" in PLAN_MODE.lower() or "not" in PLAN_MODE.lower()
    assert "implement" in PLAN_MODE.lower()
    assert "plan" in PLAN_MODE.lower()


def test_learning_instructions_covers_agentic_mode():
    assert "agentic" in LEARNING_INSTRUCTIONS.lower()


# ── SystemPromptBuilder tests ────────────────────────────────────────────


def test_build_basic(builder):
    prompt = builder.build()
    assert "# Identity" in prompt
    assert "# Tone and Style" in prompt
    assert "# Doing Tasks" in prompt
    assert "# Tool Guidelines" in prompt
    assert "# Security" in prompt
    assert "# Git Safety Protocol" in prompt


def test_build_includes_runtime_section(builder):
    prompt = builder.build(include_datetime=True)
    assert "# Runtime" in prompt
    assert "Current date" in prompt
    assert "Workspace:" in prompt


def test_build_excludes_runtime_when_disabled(builder):
    prompt = builder.build(include_datetime=False)
    assert "# Runtime" not in prompt


def test_build_includes_session_id(builder):
    prompt = builder.build(session_id="sess-abc123")
    assert "sess-abc123" in prompt


def test_build_no_session_id_by_default(builder):
    prompt = builder.build()
    assert "Session ID:" not in prompt


def test_build_excludes_learning_by_default(builder):
    prompt = builder.build()
    assert "# Institutional Learning" not in prompt


def test_build_includes_learning_when_enabled(builder):
    prompt = builder.build(include_learning=True)
    assert "# Institutional Learning" in prompt


def test_build_excludes_plan_mode_by_default(builder):
    prompt = builder.build()
    assert "# Plan Mode" not in prompt


def test_build_includes_plan_mode_when_enabled(builder):
    prompt = builder.build(include_plan_mode=True)
    assert "# Plan Mode" in prompt


def test_build_includes_skill_content(builder):
    prompt = builder.build(skill_content="# My Skill\n\nDo this.")
    assert "# Active Skill" in prompt
    assert "# My Skill" in prompt
    assert "Do this." in prompt


def test_build_includes_extra_context(builder):
    prompt = builder.build(extra_context="## Project Rules\n\nAlways use type hints.")
    assert "# Project Context" in prompt
    assert "Always use type hints." in prompt


def test_build_with_workspace_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# My Guidelines\n\nBe helpful.", encoding="utf-8")
    b = SystemPromptBuilder(tmp_path)
    prompt = b.build()
    assert "# Workspace Context" in prompt
    assert "Be helpful." in prompt


def test_build_with_all_workspace_files(tmp_path):
    for fname, content in [
        ("AGENTS.md", "agents content"),
        ("SOUL.md", "soul content"),
        ("IDENTITY.md", "identity content"),
        ("USER.md", "user content"),
        ("MEMORY.md", "memory content"),
        ("TOOLS.md", "tools content"),
        ("BOOT.md", "boot content"),
    ]:
        (tmp_path / fname).write_text(content, encoding="utf-8")

    b = SystemPromptBuilder(tmp_path)
    prompt = b.build()

    for content in ["agents content", "soul content", "identity content",
                    "user content", "memory content", "tools content", "boot content"]:
        assert content in prompt, f"'{content}' missing from prompt"


def test_build_workspace_files_ordering(tmp_path):
    """BOOT.md should appear after MEMORY.md in the prompt."""
    (tmp_path / "MEMORY.md").write_text("memory here", encoding="utf-8")
    (tmp_path / "BOOT.md").write_text("boot here", encoding="utf-8")
    b = SystemPromptBuilder(tmp_path)
    prompt = b.build()
    mem_pos = prompt.index("memory here")
    boot_pos = prompt.index("boot here")
    assert boot_pos > mem_pos, "BOOT.md should appear after MEMORY.md"


def test_build_skips_empty_workspace_files(tmp_path):
    (tmp_path / "AGENTS.md").write_text("   \n\n  ", encoding="utf-8")
    b = SystemPromptBuilder(tmp_path)
    prompt = b.build()
    # Workspace context section should not appear for empty files
    assert "Agent Guidelines (AGENTS.md)" not in prompt


def test_build_caps_memory_startup_lines(tmp_path):
    from agnoclaw.workspace import MEMORY_STARTUP_LINES

    lines = [f"memory line {i}" for i in range(MEMORY_STARTUP_LINES + 25)]
    (tmp_path / "MEMORY.md").write_text("\n".join(lines), encoding="utf-8")
    b = SystemPromptBuilder(tmp_path)
    prompt = b.build(include_datetime=False)

    assert f"memory line {MEMORY_STARTUP_LINES - 1}" in prompt
    assert f"memory line {MEMORY_STARTUP_LINES}" not in prompt


def test_build_caps_workspace_file_chars(tmp_path):
    from agnoclaw.workspace import BOOTSTRAP_MAX_CHARS

    content = "B" * (BOOTSTRAP_MAX_CHARS + 500)
    (tmp_path / "AGENTS.md").write_text(content, encoding="utf-8")
    b = SystemPromptBuilder(tmp_path)
    prompt = b.build(include_datetime=False)

    assert "B" * BOOTSTRAP_MAX_CHARS in prompt
    assert "B" * (BOOTSTRAP_MAX_CHARS + 1) not in prompt


def test_add_section(builder):
    builder.add_section("# Enterprise Policy\n\nUse only approved tools.")
    prompt = builder.build()
    assert "# Enterprise Policy" in prompt
    assert "Use only approved tools." in prompt


def test_add_section_multiple(builder):
    builder.add_section("# Policy A\n\nRule A.")
    builder.add_section("# Policy B\n\nRule B.")
    prompt = builder.build()
    assert "Rule A." in prompt
    assert "Rule B." in prompt


def test_sections_joined_by_separator(builder):
    prompt = builder.build(include_datetime=False)
    assert "\n\n---\n\n" in prompt


def test_plan_mode_ordering(builder):
    """Plan mode section should appear before Learning section."""
    prompt = builder.build(include_plan_mode=True, include_learning=True)
    plan_pos = prompt.index("# Plan Mode")
    learning_pos = prompt.index("# Institutional Learning")
    assert plan_pos < learning_pos
