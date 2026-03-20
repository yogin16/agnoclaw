"""Tests for pre-built team factories."""

from pathlib import Path

import pytest

from agnoclaw.config import HarnessConfig
from agnoclaw.tools.backends import CommandResult


class FakeWorkspaceAdapter:
    def __init__(self, workspace_dir: str | Path | None = None):
        self.workspace_dir = (
            Path(workspace_dir).expanduser().resolve()
            if workspace_dir is not None
            else Path.cwd().resolve()
        )

    def read_file(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        return f"team-adapter-read:{path}:{offset}:{limit}"

    def write_file(self, path: str, content: str) -> str:
        return f"team-adapter-write:{path}:{content}"

    def edit_file(self, path: str, old_string: str, new_string: str) -> str:
        return f"team-adapter-edit:{path}:{old_string}->{new_string}"

    def multi_edit_file(self, path: str, edits: list[dict[str, str]]) -> str:
        return f"team-adapter-multi:{path}:{len(edits)}"

    def glob_files(self, pattern: str, base_dir: str | None = None, path: str | None = None) -> str:
        return f"team-adapter-glob:{pattern}:{base_dir}:{path}"

    def grep_files(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        case_insensitive: bool = False,
        context_lines: int = 0,
        max_results: int = 50,
    ) -> str:
        return f"team-adapter-grep:{pattern}:{path}:{glob}:{case_insensitive}:{context_lines}:{max_results}"

    def list_dir(self, path: str | None = None) -> str:
        return f"team-adapter-list:{path}"


class FakeCommandExecutor:
    def __init__(self, workspace_dir: str | Path | None = None):
        self.workspace_dir = (
            str(Path(workspace_dir).expanduser().resolve())
            if workspace_dir is not None
            else None
        )

    def run(self, *, command: str, workdir: str | None, timeout_seconds: int | None) -> CommandResult:
        return CommandResult(stdout=f"team-executor-run:{command}:{workdir}:{timeout_seconds}")

    def start(self, *, command: str, workdir: str | None, description: str | None = None):
        raise NotImplementedError

    def output(self, *, task_id: str, max_chars: int = 8000, tail: bool = True):
        raise NotImplementedError

    def kill(self, *, task_id: str, force: bool = False) -> str:
        raise NotImplementedError


@pytest.fixture
def cfg():
    return HarnessConfig()


def test_research_team_returns_team(cfg):
    """research_team() returns an Agno Team with 3 members."""
    from agnoclaw.teams import research_team

    team = research_team(model_id="anthropic:claude-sonnet-4-6", config=cfg)
    assert team.name == "Research Team"
    assert len(team.members) == 3
    member_names = [m.name for m in team.members]
    assert "Researcher" in member_names
    assert "Analyst" in member_names
    assert "Writer" in member_names


def test_research_team_uses_coordinate_mode(cfg):
    """research_team uses coordinate mode."""
    from agno.team import TeamMode
    from agnoclaw.teams import research_team

    team = research_team(model_id="anthropic:claude-sonnet-4-6", config=cfg)
    assert team.mode == TeamMode.coordinate


def test_research_team_passes_session_id(cfg):
    """Session ID is forwarded to the Team."""
    from agnoclaw.teams import research_team

    team = research_team(
        model_id="anthropic:claude-sonnet-4-6",
        config=cfg,
        session_id="test-session",
    )
    assert team.session_id == "test-session"


def test_research_team_learning_disabled_by_default(cfg):
    """Learning is off by default."""
    from agnoclaw.teams import research_team

    team = research_team(model_id="anthropic:claude-sonnet-4-6", config=cfg)
    assert team.learning is None


def test_research_team_learning_enabled(cfg):
    """enable_learning=True wires up a LearningMachine."""
    from agnoclaw.teams import research_team

    team = research_team(
        model_id="anthropic:claude-sonnet-4-6",
        config=cfg,
        enable_learning=True,
    )
    assert team.learning is not None


def test_code_team_returns_team(cfg):
    """code_team() returns an Agno Team with 3 members."""
    from agnoclaw.teams import code_team

    team = code_team(model_id="anthropic:claude-sonnet-4-6", config=cfg)
    assert team.name == "Code Team"
    assert len(team.members) == 3
    member_names = [m.name for m in team.members]
    assert "Architect" in member_names
    assert "Implementer" in member_names
    assert "Reviewer" in member_names


def test_code_team_passes_session_id(cfg):
    """Session ID is forwarded to the code Team."""
    from agnoclaw.teams import code_team

    team = code_team(
        model_id="anthropic:claude-sonnet-4-6",
        config=cfg,
        session_id="code-sess",
    )
    assert team.session_id == "code-sess"


def test_code_team_learning_enabled(cfg):
    """enable_learning=True wires up a LearningMachine for code team."""
    from agnoclaw.teams import code_team

    team = code_team(
        model_id="anthropic:claude-sonnet-4-6",
        config=cfg,
        enable_learning=True,
    )
    assert team.learning is not None


def test_data_team_returns_team(cfg):
    """data_team() returns an Agno Team with 2 members."""
    from agnoclaw.teams import data_team

    team = data_team(model_id="anthropic:claude-sonnet-4-6", config=cfg)
    assert team.name == "Data Team"
    assert len(team.members) == 2
    member_names = [m.name for m in team.members]
    assert "DataFetcher" in member_names
    assert "DataAnalyst" in member_names


def test_data_team_passes_session_id(cfg):
    """Session ID is forwarded to the data Team."""
    from agnoclaw.teams import data_team

    team = data_team(
        model_id="anthropic:claude-sonnet-4-6",
        config=cfg,
        session_id="data-sess",
    )
    assert team.session_id == "data-sess"


def test_team_model_resolution_combined_string(cfg):
    """Teams accept 'provider:model' combined string as model_id."""
    from agnoclaw.teams import research_team

    team = research_team(model_id="openai:gpt-4o", config=cfg)
    # Agno resolves the string into a model object — check the id
    for member in team.members:
        assert member.model.id == "gpt-4o"


def test_team_model_resolution_separate_provider(cfg):
    """Teams accept separate model_id + provider."""
    from agnoclaw.teams import data_team

    team = data_team(model_id="gpt-4o", provider="openai", config=cfg)
    for member in team.members:
        assert member.model.id == "gpt-4o"


def test_team_defaults_to_config_model(cfg):
    """Without model_id, teams use config defaults."""
    from agnoclaw.teams import research_team

    team = research_team(config=cfg)
    assert team.model.id == cfg.default_model


def test_code_team_uses_config_workspace_for_file_and_bash_tools(tmp_path):
    from agnoclaw.teams import code_team
    from agnoclaw.tools.files import FilesToolkit

    workspace_dir = tmp_path / "team-workspace"
    workspace_dir.mkdir()
    team = code_team(config=HarnessConfig(workspace_dir=str(workspace_dir)))

    architect_files = next(t for t in team.members[0].tools if isinstance(t, FilesToolkit))
    implementer_files = next(t for t in team.members[1].tools if isinstance(t, FilesToolkit))
    implementer_bash = team.members[1].tools[1]

    assert architect_files.workspace_dir == workspace_dir.resolve()
    assert implementer_files.workspace_dir == workspace_dir.resolve()
    assert implementer_bash.entrypoint("pwd").strip() == str(workspace_dir.resolve())
    assert implementer_bash.pre_hook is not None
    assert architect_files.functions["read_file"].pre_hook is not None


def test_data_team_uses_config_workspace_for_file_and_bash_tools(tmp_path):
    from agnoclaw.teams import data_team
    from agnoclaw.tools.files import FilesToolkit

    workspace_dir = tmp_path / "data-workspace"
    workspace_dir.mkdir()
    team = data_team(config=HarnessConfig(workspace_dir=str(workspace_dir)))

    fetcher_files = next(t for t in team.members[0].tools if isinstance(t, FilesToolkit))
    analyst_files = next(t for t in team.members[1].tools if isinstance(t, FilesToolkit))
    fetcher_bash = team.members[0].tools[2]
    analyst_bash = team.members[1].tools[1]

    assert fetcher_files.workspace_dir == workspace_dir.resolve()
    assert analyst_files.workspace_dir == workspace_dir.resolve()
    assert fetcher_bash.entrypoint("pwd").strip() == str(workspace_dir.resolve())
    assert analyst_bash.entrypoint("pwd").strip() == str(workspace_dir.resolve())
    assert fetcher_bash.pre_hook is not None
    assert fetcher_files.functions["read_file"].pre_hook is not None


def test_code_team_uses_custom_backends(tmp_path):
    from agnoclaw.teams import code_team
    from agnoclaw.tools.files import FilesToolkit

    adapter = FakeWorkspaceAdapter(workspace_dir=tmp_path)
    executor = FakeCommandExecutor(workspace_dir=tmp_path)
    team = code_team(
        config=HarnessConfig(workspace_dir=str(tmp_path)),
        workspace_adapter=adapter,
        command_executor=executor,
    )

    implementer_files = next(t for t in team.members[1].tools if isinstance(t, FilesToolkit))
    implementer_bash = team.members[1].tools[1]

    assert implementer_files.adapter is adapter
    assert implementer_files.read_file("/tmp/demo.txt") == "team-adapter-read:/tmp/demo.txt:0:2000"
    assert implementer_bash.entrypoint("echo hi") == "team-executor-run:echo hi:None:120"


def test_data_team_uses_custom_backends(tmp_path):
    from agnoclaw.teams import data_team
    from agnoclaw.tools.files import FilesToolkit

    adapter = FakeWorkspaceAdapter(workspace_dir=tmp_path)
    executor = FakeCommandExecutor(workspace_dir=tmp_path)
    team = data_team(
        config=HarnessConfig(workspace_dir=str(tmp_path)),
        workspace_adapter=adapter,
        command_executor=executor,
    )

    fetcher_files = next(t for t in team.members[0].tools if isinstance(t, FilesToolkit))
    fetcher_bash = team.members[0].tools[2]

    assert fetcher_files.adapter is adapter
    assert fetcher_bash.entrypoint("echo hi") == "team-executor-run:echo hi:None:120"
