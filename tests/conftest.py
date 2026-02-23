"""
Shared pytest fixtures for the agnoclaw test suite.

Fixtures:
  tmp_workspace    — an initialized Workspace in a temp directory
  mock_agent       — a HarnessAgent with all external I/O mocked out
  sample_skill_dir — a temp dir with one valid SKILL.md
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agnoclaw.workspace import Workspace


# ── Workspace fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Workspace:
    """An initialized Workspace in an isolated temp directory."""
    ws = Workspace(tmp_path / "workspace")
    ws.initialize()
    return ws


@pytest.fixture
def tmp_workspace_path(tmp_path: Path) -> Path:
    """Raw Path to an initialized workspace directory."""
    path = tmp_path / "workspace"
    ws = Workspace(path)
    ws.initialize()
    return path


# ── Skill fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def sample_skill_dir(tmp_path: Path) -> Path:
    """
    A temp directory containing one valid skill (my-skill/SKILL.md).

    Returns the parent skills directory (not the skill subdirectory).
    """
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: my-skill\n"
        "description: A test skill for unit tests\n"
        "user-invocable: true\n"
        "---\n\n"
        "# My Skill\n\n"
        "Do the thing with $ARGUMENTS.\n",
        encoding="utf-8",
    )
    return skills_root


@pytest.fixture
def multi_skill_dir(tmp_path: Path) -> Path:
    """A temp directory containing two valid skills."""
    skills_root = tmp_path / "skills"
    for name, desc in [
        ("skill-a", "First test skill"),
        ("skill-b", "Second test skill"),
    ]:
        skill_dir = skills_root / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n",
            encoding="utf-8",
        )
    return skills_root


# ── Agent mock fixture ────────────────────────────────────────────────────────


@pytest.fixture
def mock_agent(tmp_workspace_path: Path):
    """
    A HarnessAgent with external I/O (Agno Agent, DB) mocked out.

    The agent is initialized with real workspace/skills/config logic,
    but the underlying Agno Agent.run/print_response are replaced with
    MagicMocks so tests don't require API keys.
    """
    with (
        patch("agno.agent.Agent.run") as mock_run,
        patch("agno.agent.Agent.print_response") as mock_print,
    ):
        from agnoclaw import HarnessAgent

        agent = HarnessAgent(
            name="test-agent",
            workspace_dir=tmp_workspace_path,
            db=None,
        )

        # Attach mocks for test assertions
        agent._mock_run = mock_run
        agent._mock_print = mock_print

        yield agent


# ── ProgressToolkit fixture ───────────────────────────────────────────────────


@pytest.fixture
def progress_toolkit(tmp_path: Path):
    """A ProgressToolkit pointed at an isolated temp directory."""
    from agnoclaw.tools.tasks import ProgressToolkit

    return ProgressToolkit(project_dir=str(tmp_path))
