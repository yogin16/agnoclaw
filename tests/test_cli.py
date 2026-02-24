"""Tests for the agnoclaw CLI."""

import pytest
from click.testing import CliRunner
from pathlib import Path
from unittest.mock import MagicMock

from agnoclaw.cli.main import _handle_slash_command, cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def tmp_workspace(tmp_path):
    """Return a path for an isolated workspace."""
    return str(tmp_path / "workspace")


# ── agnoclaw init ─────────────────────────────────────────────────────────────


def test_init_creates_workspace(runner, tmp_workspace):
    """agnoclaw init should initialize the workspace directory."""
    result = runner.invoke(
        cli,
        ["init", "--workspace", tmp_workspace],
        input="\n\n\n\n\n",  # skip all questions with Enter
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert Path(tmp_workspace).exists()


def test_init_creates_default_files(runner, tmp_workspace):
    """init should create AGENTS.md, SOUL.md, HEARTBEAT.md at minimum."""
    runner.invoke(
        cli,
        ["init", "--workspace", tmp_workspace],
        input="\n\n\n\n\n",
    )
    ws_path = Path(tmp_workspace)
    assert (ws_path / "AGENTS.md").exists()
    assert (ws_path / "SOUL.md").exists()
    assert (ws_path / "HEARTBEAT.md").exists()


def test_init_writes_user_md(runner, tmp_workspace):
    """init should write USER.md when user identity is provided."""
    result = runner.invoke(
        cli,
        ["init", "--workspace", tmp_workspace],
        # soul, user, identity, model, bash
        input="\nAlice, UTC-8\n\n\n\n",
    )
    assert result.exit_code == 0
    user_path = Path(tmp_workspace) / "USER.md"
    assert user_path.exists()
    assert "Alice" in user_path.read_text()


def test_init_writes_identity_md(runner, tmp_workspace):
    """init should write IDENTITY.md when capabilities are provided."""
    runner.invoke(
        cli,
        ["init", "--workspace", tmp_workspace],
        # soul, user, identity, model, bash
        input="\n\nPython developer\n\n\n",
    )
    identity_path = Path(tmp_workspace) / "IDENTITY.md"
    assert identity_path.exists()
    assert "Python developer" in identity_path.read_text()


def test_init_writes_tools_md(runner, tmp_workspace):
    """init should always write TOOLS.md with the chosen model."""
    runner.invoke(
        cli,
        ["init", "--workspace", tmp_workspace],
        input="\n\n\nclaude-haiku-4-5-20251001\n\n",
    )
    tools_path = Path(tmp_workspace) / "TOOLS.md"
    assert tools_path.exists()
    assert "claude-haiku-4-5-20251001" in tools_path.read_text()


def test_init_soul_appended(runner, tmp_workspace):
    """Soul input should be appended to SOUL.md, not replace it."""
    runner.invoke(
        cli,
        ["init", "--workspace", tmp_workspace],
        input="Direct and concise\n\n\n\n\n",
    )
    soul_path = Path(tmp_workspace) / "SOUL.md"
    content = soul_path.read_text()
    # Default content preserved
    assert "Soul" in content
    # Custom persona appended
    assert "Direct and concise" in content


def test_init_skip_all_questions(runner, tmp_workspace):
    """Skipping all questions should still produce a valid workspace."""
    result = runner.invoke(
        cli,
        ["init", "--workspace", tmp_workspace],
        input="\n\n\n\n\n",
    )
    assert result.exit_code == 0
    assert "Workspace initialized" in result.output


def test_init_default_model_in_output(runner, tmp_workspace):
    """The chosen model should appear in the success output."""
    result = runner.invoke(
        cli,
        ["init", "--workspace", tmp_workspace],
        input="\n\n\nclaude-opus-4-6\n\n",
    )
    assert "claude-opus-4-6" in result.output


# ── agnoclaw workspace show ───────────────────────────────────────────────────


def test_workspace_show_includes_identity(runner, tmp_workspace):
    """workspace show should display IDENTITY.md in the file table."""
    # Init first so workspace exists
    runner.invoke(
        cli,
        ["init", "--workspace", tmp_workspace],
        input="\n\nPython dev\n\n\n",
    )
    result = runner.invoke(
        cli,
        ["workspace", "show", "--workspace", tmp_workspace],
    )
    assert result.exit_code == 0
    assert "IDENTITY.md" in result.output


def test_workspace_show_includes_tools(runner, tmp_workspace):
    """workspace show should display TOOLS.md in the file table."""
    runner.invoke(
        cli,
        ["init", "--workspace", tmp_workspace],
        input="\n\n\n\n\n",
    )
    result = runner.invoke(
        cli,
        ["workspace", "show", "--workspace", tmp_workspace],
    )
    assert result.exit_code == 0
    assert "TOOLS.md" in result.output


def test_workspace_show_uninitialized(runner, tmp_workspace):
    """workspace show on a missing workspace should prompt to init."""
    result = runner.invoke(
        cli,
        ["workspace", "show", "--workspace", tmp_workspace],
    )
    assert result.exit_code == 0
    assert "not initialized" in result.output.lower() or "init" in result.output.lower()


# ── agnoclaw workspace init ───────────────────────────────────────────────────


def test_workspace_init_command(runner, tmp_workspace):
    """agnoclaw workspace init should create default files."""
    result = runner.invoke(
        cli,
        ["workspace", "init", "--workspace", tmp_workspace],
    )
    assert result.exit_code == 0
    assert "initialized" in result.output.lower()
    assert Path(tmp_workspace).exists()


# ── agnoclaw heartbeat ────────────────────────────────────────────────────────


def test_heartbeat_start_empty_heartbeat_exits(runner, tmp_workspace):
    """heartbeat start should exit cleanly when HEARTBEAT.md has no actionable content."""
    from agnoclaw.workspace import Workspace
    ws = Workspace(tmp_workspace)
    ws.initialize()
    ws.write_file("heartbeat", "# Heartbeat\n\n## Section\n")  # headers only = empty

    result = runner.invoke(
        cli,
        ["heartbeat", "start", "--workspace", tmp_workspace],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "empty" in result.output.lower() or "nothing" in result.output.lower()


def test_heartbeat_group_exists(runner):
    """heartbeat command group should be registered."""
    result = runner.invoke(cli, ["heartbeat", "--help"])
    assert result.exit_code == 0
    assert "start" in result.output
    assert "trigger" in result.output


# ── agnoclaw --help ───────────────────────────────────────────────────────────


def test_root_help_shows_init(runner):
    """Root --help should mention the init command."""
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "init" in result.output


def test_init_help(runner):
    """agnoclaw init --help should describe the wizard."""
    result = runner.invoke(cli, ["init", "--help"])
    assert result.exit_code == 0
    assert "onboarding" in result.output.lower() or "wizard" in result.output.lower() or "personalize" in result.output.lower()


def test_handle_slash_skill_queues_skill():
    """The /skill command should queue a one-shot skill for the next message."""
    agent = MagicMock()
    agent.skills.list_skills.return_value = [{"name": "code-review"}]

    handled, queued = _handle_slash_command("/skill code-review", agent, None)
    assert handled is True
    assert queued == "code-review"


def test_handle_slash_clear_rotates_session():
    """The /clear command should call clear_session_context when available."""
    agent = MagicMock()
    agent.clear_session_context.return_value = "session-new"

    handled, queued = _handle_slash_command("/clear", agent, None)
    assert handled is True
    assert queued is None
    agent.clear_session_context.assert_called_once()
