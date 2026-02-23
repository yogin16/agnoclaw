"""
Shared pytest fixtures for the agnoclaw test suite.

Fixtures:
  tmp_workspace    — an initialized Workspace in a temp directory
  mock_agent       — a HarnessAgent with all external I/O mocked out
  sample_skill_dir — a temp dir with one valid SKILL.md
  live_agent       — a real HarnessAgent for integration tests
                     (uses Ollama if available, else skips)

Integration tests:
  Tests that make real model calls are marked @pytest.mark.integration
  and skipped by default. Run them with:

    # With Ollama (local, no API key):
    uv run pytest tests/ -m integration

    # Override model/provider via env vars:
    AGNOCLAW_TEST_PROVIDER=ollama AGNOCLAW_TEST_MODEL=qwen3:0.6b pytest -m integration

    # With Anthropic:
    ANTHROPIC_API_KEY=... AGNOCLAW_TEST_PROVIDER=anthropic pytest -m integration
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agnoclaw.workspace import Workspace


# ── pytest marks ─────────────────────────────────────────────────────────────

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: tests that make real model calls (Ollama or cloud API)",
    )


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
        )

        # Attach mocks for test assertions
        agent._mock_run = mock_run
        agent._mock_print = mock_print

        yield agent


# ── Live integration agent ────────────────────────────────────────────────────
#
# Used by @pytest.mark.integration tests. Defaults to Ollama (qwen3:0.6b)
# so no API key is needed. Override via env vars:
#   AGNOCLAW_TEST_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-...
#   AGNOCLAW_TEST_PROVIDER=ollama AGNOCLAW_TEST_MODEL=qwen3:8b

_DEFAULT_LOCAL_PROVIDER = "ollama"
_DEFAULT_LOCAL_MODEL = "qwen3:0.6b"


def _get_live_provider() -> tuple[str, str]:
    """Return (provider, model) for integration tests."""
    provider = os.environ.get("AGNOCLAW_TEST_PROVIDER", _DEFAULT_LOCAL_PROVIDER)
    if provider == "ollama":
        model = os.environ.get("AGNOCLAW_TEST_MODEL", _DEFAULT_LOCAL_MODEL)
    elif provider == "anthropic":
        model = os.environ.get("AGNOCLAW_TEST_MODEL", "claude-haiku-4-5-20251001")
    else:
        model = os.environ.get("AGNOCLAW_TEST_MODEL", "")
    return provider, model


def _ollama_available() -> bool:
    """True if the Ollama daemon is reachable."""
    try:
        import httpx
        r = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


def _anthropic_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


@pytest.fixture
def live_agent(tmp_workspace_path: Path):
    """
    A real HarnessAgent that makes actual model calls.

    - Defaults to Ollama (qwen3:0.6b) — no API key, runs locally.
    - Set AGNOCLAW_TEST_PROVIDER=anthropic to use Claude instead.
    - Skips if neither provider is available.
    """
    provider, model = _get_live_provider()

    if provider == "ollama" and not _ollama_available():
        pytest.skip("Ollama daemon not running (start with: ollama serve)")
    elif provider == "anthropic" and not _anthropic_available():
        pytest.skip("ANTHROPIC_API_KEY not set")

    from agnoclaw import HarnessAgent

    return HarnessAgent(
        name="live-test-agent",
        provider=provider,
        model_id=model,
        workspace_dir=tmp_workspace_path,
    )


# ── ProgressToolkit fixture ───────────────────────────────────────────────────


@pytest.fixture
def progress_toolkit(tmp_path: Path):
    """A ProgressToolkit pointed at an isolated temp directory."""
    from agnoclaw.tools.tasks import ProgressToolkit

    return ProgressToolkit(project_dir=str(tmp_path))
