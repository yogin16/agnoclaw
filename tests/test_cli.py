"""Tests for the agnoclaw CLI."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

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
    """init should always write advisory TOOLS.md guidance with the chosen model."""
    runner.invoke(
        cli,
        ["init", "--workspace", tmp_workspace],
        input="\n\n\nclaude-haiku-4-5-20251001\n\n",
    )
    tools_path = Path(tmp_workspace) / "TOOLS.md"
    assert tools_path.exists()
    assert "claude-haiku-4-5-20251001" in tools_path.read_text()
    assert "advisory workspace context" in tools_path.read_text()
    assert "default_model:" not in tools_path.read_text()


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


def test_skill_inspect_is_parse_only_and_shows_trust(runner, tmp_workspace, tmp_path):
    skill_dir = Path(tmp_workspace) / "skills" / "dangerous"
    skill_dir.mkdir(parents=True)
    marker = tmp_path / "skill-inspect-executed"
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: dangerous\ndescription: inspect only\n---\n\n"
        f"# Dangerous\n!`touch {marker}`\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        cli,
        ["skill", "inspect", "dangerous", "--workspace", tmp_workspace],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Trust: local" in result.output
    assert "touch" in result.output
    assert not marker.exists()


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
    output = result.output.lower()
    assert "onboarding" in output or "wizard" in output or "personalize" in output


def _runtime_inspection_db(tmp_path):
    from agnoclaw.runtime.lifecycle import RunSnapshot
    from agnoclaw.runtime.store import SQLiteRuntimeStore

    path = tmp_path / "runtime-inspection.db"
    store = SQLiteRuntimeStore(path)
    store.create_run(
        RunSnapshot(
            run_id="run-cli-private",
            tenant_id="tenant-cli",
            user_id="user-cli",
            metadata={"prompt": "CLI_RUNTIME_SECRET_SENTINEL"},
        )
    )
    store.close()
    return path


def test_runtime_inspect_emits_stable_content_free_json_from_read_only_store(runner, tmp_path):
    import json

    path = _runtime_inspection_db(tmp_path)
    before = path.read_bytes()

    result = runner.invoke(
        cli,
        [
            "inspect",
            "run",
            "run-cli-private",
            "--sqlite-db",
            str(path),
            "--tenant-id",
            "tenant-cli",
            "--user-id",
            "user-cli",
            "--json",
        ],
        env={"AGNOCLAW_TELEMETRY_IDENTIFIER_KEY": "cli-key-material-that-is-at-least-32-bytes"},
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "1.0"
    assert payload["state"] == "created"
    assert payload["recommendation"] == "start"
    assert payload["run_id_hash"].startswith("hmac-sha256:default:")
    assert "run-cli-private" not in result.stdout
    assert "CLI_RUNTIME_SECRET_SENTINEL" not in result.stdout
    assert path.read_bytes() == before


def test_runtime_inspect_wrong_owner_is_stable_permission_exit(runner, tmp_path):
    import json

    path = _runtime_inspection_db(tmp_path)
    secret_key = "cli-key-material-that-is-at-least-32-bytes"

    result = runner.invoke(
        cli,
        [
            "inspect",
            "run",
            "run-cli-private",
            "--sqlite-db",
            str(path),
            "--tenant-id",
            "tenant-cli",
            "--user-id",
            "wrong-user",
            "--json",
        ],
        env={"AGNOCLAW_TELEMETRY_IDENTIFIER_KEY": secret_key},
    )

    assert result.exit_code == 77
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "RUN_INSPECTION_NOT_AUTHORIZED"
    assert payload["exit_code"] == 77
    assert "run-cli-private" not in result.stderr
    assert secret_key not in result.stderr


def test_runtime_inspect_requires_exactly_one_backend_and_env_key(runner):
    import json

    result = runner.invoke(
        cli,
        ["inspect", "run", "run-1", "--user-id", "user-1", "--json"],
        env={"AGNOCLAW_TELEMETRY_IDENTIFIER_KEY": "cli-key-material-that-is-at-least-32-bytes"},
    )

    assert result.exit_code == 78
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "RUNTIME_INSPECTION_CONFIGURATION_INVALID"
    assert payload["exit_code"] == 78


def test_runtime_inspect_help_uses_credential_names_not_dsn_flags(runner):
    result = runner.invoke(cli, ["inspect", "run", "--help"])

    assert result.exit_code == 0
    assert "--postgres-credential-env" in result.output
    assert "--identifier-key-env" in result.output
    assert "--postgres-dsn" not in result.output


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


def test_handle_slash_elevated_uses_interactive_approver_when_missing():
    """The /elevated command should install a terminal approver and run elevated."""
    from types import SimpleNamespace

    agent = MagicMock()
    agent.admin_list_permissions.return_value = {"has_approver": False}
    agent.run_elevated_command.return_value = SimpleNamespace(
        stdout="ok\n",
        stderr="",
        exit_code=0,
    )

    handled, queued = _handle_slash_command("/elevated printf ok", agent, None)

    assert handled is True
    assert queued is None
    agent.set_permission_approver.assert_called_once()
    approver = agent.set_permission_approver.call_args.args[0]
    assert approver.__class__.__name__ == "InteractivePermissionApprover"
    agent.run_elevated_command.assert_called_once_with(
        "printf ok",
        reason="CLI /elevated directive",
        _skip_approval=False,
    )


def test_handle_slash_elevated_preserves_existing_approver():
    from types import SimpleNamespace

    agent = MagicMock()
    agent.admin_list_permissions.return_value = {"has_approver": True}
    agent.run_elevated_command.return_value = SimpleNamespace(
        stdout="",
        stderr="",
        exit_code=0,
    )

    handled, _ = _handle_slash_command("/elevated id", agent, None)

    assert handled is True
    agent.set_permission_approver.assert_not_called()
    agent.run_elevated_command.assert_called_once_with(
        "id",
        reason="CLI /elevated directive",
        _skip_approval=False,
    )


def test_handle_slash_elevated_sets_session_mode_and_approver():
    agent = MagicMock()
    agent.admin_list_permissions.return_value = {"has_approver": False}

    handled, queued = _handle_slash_command("/elevated on", agent, None)

    assert handled is True
    assert queued is None
    agent.set_elevated_mode.assert_called_once_with("on")
    agent.set_permission_approver.assert_called_once()
    approver = agent.set_permission_approver.call_args.args[0]
    assert approver.default is True
    agent.run_elevated_command.assert_not_called()


def test_handle_slash_elevated_full_command_skips_approver():
    from types import SimpleNamespace

    agent = MagicMock()
    agent.admin_list_permissions.return_value = {"has_approver": True}
    agent.run_elevated_command.return_value = SimpleNamespace(
        stdout="",
        stderr="",
        exit_code=0,
    )

    handled, _ = _handle_slash_command("/elevated full id", agent, None)

    assert handled is True
    agent.set_permission_approver.assert_not_called()
    agent.set_elevated_mode.assert_called_once_with("full")
    agent.run_elevated_command.assert_called_once_with(
        "id",
        reason="CLI /elevated directive",
        _skip_approval=True,
    )


# ── agnoclaw pack ─────────────────────────────────────────────────────────────


def _write_pack(tmp_path, name="cli-pack"):
    pack = tmp_path / name
    pack.mkdir()
    (pack / "agnoclaw-pack.toml").write_text(
        f"""
name = "{name}"
version = "0.1.0"
description = "CLI pack"

[provides]
skills = ["skills/"]
hooks = ["cli_pack.hooks:register"]

[trust]
requires_code_execution = true
""",
        encoding="utf-8",
    )
    return pack


def test_pack_inspect_does_not_execute_code(runner, tmp_path):
    pack = _write_pack(tmp_path)
    module_dir = pack / "cli_pack"
    module_dir.mkdir()
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    (module_dir / "hooks.py").write_text(
        "raise RuntimeError('should not execute')\n",
        encoding="utf-8",
    )

    result = runner.invoke(cli, ["pack", "inspect", str(pack)], catch_exceptions=False)

    assert result.exit_code == 0
    assert "cli-pack" in result.output
    assert "Requires code execution: True" in result.output


def test_pack_install_list_trust_and_remove(runner, tmp_path):
    pack = _write_pack(tmp_path)
    store = tmp_path / "store"

    install_result = runner.invoke(
        cli,
        ["pack", "install", str(pack), "--root", str(store)],
        catch_exceptions=False,
    )
    list_result = runner.invoke(
        cli,
        ["pack", "list", "--root", str(store)],
        catch_exceptions=False,
    )
    trust_result = runner.invoke(
        cli,
        ["pack", "trust", "cli-pack", "--root", str(store)],
        catch_exceptions=False,
    )
    remove_result = runner.invoke(
        cli,
        ["pack", "remove", "cli-pack", "--root", str(store)],
        catch_exceptions=False,
    )

    assert install_result.exit_code == 0
    assert "Installed pack 'cli-pack'" in install_result.output
    assert list_result.exit_code == 0
    assert "cli-pack" in list_result.output
    assert trust_result.exit_code == 0
    assert "Trusted pack 'cli-pack'" in trust_result.output
    assert remove_result.exit_code == 0
    assert "Removed pack 'cli-pack'" in remove_result.output


# ── agnoclaw schedule ─────────────────────────────────────────────────────────


def test_schedule_add_list_show_disable_enable_runs_remove(runner, tmp_path):
    store = tmp_path / "schedules.json"

    add_result = runner.invoke(
        cli,
        [
            "schedule",
            "add",
            "daily",
            "--schedule",
            "30m",
            "--prompt",
            "write brief",
            "--skill",
            "briefing",
            "--store",
            str(store),
        ],
        catch_exceptions=False,
    )
    list_result = runner.invoke(
        cli,
        ["schedule", "list", "--store", str(store)],
        catch_exceptions=False,
    )
    show_result = runner.invoke(
        cli,
        ["schedule", "show", "daily", "--store", str(store)],
        catch_exceptions=False,
    )
    disable_result = runner.invoke(
        cli,
        ["schedule", "disable", "daily", "--store", str(store)],
        catch_exceptions=False,
    )
    disabled_list_result = runner.invoke(
        cli,
        ["schedule", "list", "--disabled", "--store", str(store)],
        catch_exceptions=False,
    )
    enable_result = runner.invoke(
        cli,
        ["schedule", "enable", "daily", "--store", str(store)],
        catch_exceptions=False,
    )
    runs_result = runner.invoke(
        cli,
        ["schedule", "runs", "daily", "--store", str(store)],
        catch_exceptions=False,
    )
    remove_result = runner.invoke(
        cli,
        ["schedule", "remove", "daily", "--store", str(store)],
        catch_exceptions=False,
    )

    assert add_result.exit_code == 0
    assert "Saved schedule 'daily'" in add_result.output
    assert list_result.exit_code == 0
    assert "daily" in list_result.output
    assert show_result.exit_code == 0
    assert "write brief" in show_result.output
    assert disable_result.exit_code == 0
    assert "Disabled schedule 'daily'" in disable_result.output
    assert disabled_list_result.exit_code == 0
    assert "daily" in disabled_list_result.output
    assert enable_result.exit_code == 0
    assert "Enabled schedule 'daily'" in enable_result.output
    assert runs_result.exit_code == 0
    assert "No scheduler runs found" in runs_result.output
    assert remove_result.exit_code == 0
    assert "Removed schedule 'daily'" in remove_result.output


def test_schedule_rejects_invalid_schedule(runner, tmp_path):
    result = runner.invoke(
        cli,
        [
            "schedule",
            "add",
            "bad",
            "--schedule",
            "bad",
            "--prompt",
            "do it",
            "--store",
            str(tmp_path / "schedules.json"),
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Invalid schedule" in result.output


def test_durable_schedule_crud_uses_runtime_store(runner, tmp_path):
    runtime_db = tmp_path / "runtime.db"
    add = runner.invoke(
        cli,
        [
            "schedule",
            "add",
            "durable",
            "--schedule",
            "30m",
            "--prompt",
            "write durable brief",
            "--runtime-db",
            str(runtime_db),
            "--max-retries",
            "2",
            "--retry-delay",
            "5",
            "--retry-backoff",
            "3",
            "--retry-max-delay",
            "120",
            "--retry-jitter",
            "7",
            "--misfire-policy",
            "skip",
            "--concurrency-key",
            "briefs",
            "--learning-consent",
        ],
        catch_exceptions=False,
    )
    listed = runner.invoke(
        cli,
        ["schedule", "list", "--runtime-db", str(runtime_db)],
        catch_exceptions=False,
    )

    from agnoclaw.runtime import RuntimeSchedulerBackend, SQLiteRuntimeStore

    store = SQLiteRuntimeStore(runtime_db)
    job = RuntimeSchedulerBackend(store).get_job("durable")
    assert add.exit_code == 0
    assert listed.exit_code == 0
    assert "durable" in listed.output
    assert job is not None
    assert job.max_retries == 2
    assert job.retry_delay_seconds == 5
    assert job.retry_backoff_multiplier == 3
    assert job.retry_max_delay_seconds == 120
    assert job.retry_jitter_seconds == 7
    assert job.misfire_policy == "skip"
    assert job.concurrency_key == "briefs"
    assert job.metadata["learning_consent"] is True
    store.close()


def test_schedule_store_options_are_mutually_exclusive(runner, tmp_path):
    result = runner.invoke(
        cli,
        [
            "schedule",
            "list",
            "--store",
            str(tmp_path / "schedules.json"),
            "--runtime-db",
            str(tmp_path / "runtime.db"),
        ],
    )

    assert result.exit_code == 2
    assert "Use only one of --store or --runtime-db" in result.output


def test_schedule_worker_help_exposes_durable_controls(runner):
    result = runner.invoke(cli, ["schedule", "worker", "--help"])

    assert result.exit_code == 0
    assert "--runtime-db" in result.output
    assert "--artifacts" in result.output
    assert "--poll-interval" in result.output
    assert "--claim-limit" in result.output
    assert "--learning-profile" in result.output
    assert "--tenant-id" in result.output
    assert "--user-id" in result.output
    assert "--session" in result.output


def test_schedule_worker_learning_profile_requires_trusted_scope(runner):
    result = runner.invoke(
        cli,
        ["schedule", "worker", "--learning-profile", "personal-session"],
    )

    assert result.exit_code == 2
    assert "requires --tenant-id, --user-id, --session" in result.output


def test_schedule_trigger_records_run_history(runner, tmp_path):
    store = tmp_path / "schedules.json"
    runner.invoke(
        cli,
        [
            "schedule",
            "add",
            "daily",
            "--schedule",
            "30m",
            "--prompt",
            "write brief",
            "--store",
            str(store),
        ],
        catch_exceptions=False,
    )
    mock_agent = MagicMock()
    mock_agent.workspace = MagicMock()
    mock_agent.arun = AsyncMock(return_value=MagicMock(content="done"))
    mock_agent.aclose = AsyncMock()

    with patch("agnoclaw.cli.main._build_agent", return_value=mock_agent):
        trigger_result = runner.invoke(
            cli,
            ["schedule", "trigger", "daily", "--store", str(store)],
            catch_exceptions=False,
        )
    runs_result = runner.invoke(
        cli,
        ["schedule", "runs", "daily", "--store", str(store)],
        catch_exceptions=False,
    )

    assert trigger_result.exit_code == 0
    assert "done" in trigger_result.output
    assert runs_result.exit_code == 0
    assert "completed" in runs_result.output
    assert "daily" in runs_result.output
    mock_agent.aclose.assert_awaited_once_with()


def test_run_command_keeps_legacy_stream_and_closes_agent(runner):
    from agnoclaw.config import RuntimeProfile

    agent = MagicMock(profile=RuntimeProfile.LEGACY)

    with patch("agnoclaw.cli.main._build_agent", return_value=agent):
        result = runner.invoke(cli, ["run", "say hi"], catch_exceptions=False)

    assert result.exit_code == 0
    agent.print_response.assert_called_once_with("say hi", stream=True, skill=None)
    agent.start.assert_not_called()
    agent.close.assert_called_once_with()


def test_run_command_uses_durable_lifecycle_and_closes_agent(runner):
    from agnoclaw.config import RuntimeProfile

    response = MagicMock(content="durable result")
    lifecycle_run = MagicMock(run_id="run_cli")
    lifecycle_run.wait = AsyncMock(return_value=response)
    agent = MagicMock(profile=RuntimeProfile.DURABLE)
    agent.start = AsyncMock(return_value=lifecycle_run)
    agent.aclose = AsyncMock()

    with patch("agnoclaw.cli.main._build_agent", return_value=agent):
        result = runner.invoke(cli, ["run", "say hi"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "durable result" in result.output
    agent.start.assert_awaited_once_with("say hi", skill=None)
    lifecycle_run.wait.assert_awaited_once_with()
    agent.print_response.assert_not_called()
    agent.aclose.assert_awaited_once_with()
    agent.close.assert_not_called()


def test_run_command_closes_durable_agent_after_failure(runner):
    from agnoclaw.config import RuntimeProfile

    lifecycle_run = MagicMock(run_id="run_cli")
    lifecycle_run.wait = AsyncMock(side_effect=RuntimeError("provider failed"))
    agent = MagicMock(profile=RuntimeProfile.DURABLE)
    agent.start = AsyncMock(return_value=lifecycle_run)
    agent.aclose = AsyncMock()

    with patch("agnoclaw.cli.main._build_agent", return_value=agent):
        result = runner.invoke(cli, ["run", "say hi"])

    assert result.exit_code == 1
    agent.aclose.assert_awaited_once_with()
    agent.close.assert_not_called()


def test_tui_closes_agent_on_owning_async_loop(runner):
    agent = MagicMock()
    agent.aclose = AsyncMock()
    app = MagicMock()
    app.run_async = AsyncMock(return_value=None)

    with (
        patch("agnoclaw.cli.main._build_agent", return_value=agent),
        patch("agnoclaw.tui.AgnoClawApp", return_value=app),
    ):
        result = runner.invoke(cli, ["tui"], catch_exceptions=False)

    assert result.exit_code == 0
    app.run_async.assert_awaited_once_with()
    agent.aclose.assert_awaited_once_with(policy="cancel")
    agent.close.assert_not_called()


def test_async_chat_closes_agent_on_owning_async_loop(runner):
    import sys
    from types import ModuleType

    agent = MagicMock()
    agent.aclose = AsyncMock()
    repl = MagicMock()
    repl.run = AsyncMock(return_value=None)
    fake_repl_module = ModuleType("agnoclaw.cli.async_repl")
    fake_repl_module.AsyncREPL = MagicMock(return_value=repl)

    with (
        patch("agnoclaw.cli.main._build_agent", return_value=agent),
        patch.dict(sys.modules, {"agnoclaw.cli.async_repl": fake_repl_module}),
    ):
        result = runner.invoke(cli, ["chat"], catch_exceptions=False)

    assert result.exit_code == 0
    repl.run.assert_awaited_once_with()
    agent.aclose.assert_awaited_once_with(policy="cancel")
    agent.close.assert_not_called()


@pytest.mark.asyncio
async def test_async_repl_stream_uses_durable_lifecycle_and_waits_for_terminal():
    from types import SimpleNamespace

    from agnoclaw.config import RuntimeProfile

    AsyncREPL = _load_async_repl_without_optional_cli_dependency()

    raw = SimpleNamespace(event="RunContent", content="live")
    handle = MagicMock(run_id="run_repl")
    handle.wait = AsyncMock(return_value=SimpleNamespace(content="final"))

    async def start(_message, **kwargs):
        presentation = kwargs.pop("_presentation")
        presentation.publish(raw)
        presentation.finish()
        assert kwargs == {"skill": "review"}
        return handle

    agent = MagicMock(profile=RuntimeProfile.DURABLE)
    agent.start = AsyncMock(side_effect=start)
    agent.arun = AsyncMock()
    agent._extract_event_content.return_value = "live"
    agent._map_agno_event_type.return_value = None
    repl = object.__new__(AsyncREPL)
    repl._agent = agent
    repl._console = MagicMock()
    repl._debug = False

    await repl._stream_response("work", skill="review")

    handle.wait.assert_awaited_once_with()
    agent.arun.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_repl_reports_final_result_after_slow_display_detaches():
    from types import SimpleNamespace

    from agnoclaw.config import RuntimeProfile

    AsyncREPL = _load_async_repl_without_optional_cli_dependency()

    handle = MagicMock(run_id="run_repl_slow")
    handle.wait = AsyncMock(return_value=SimpleNamespace(content="authoritative final"))

    async def start(_message, **kwargs):
        presentation = kwargs.pop("_presentation")
        for index in range(257):
            presentation.publish(SimpleNamespace(event="RunContent", content=str(index)))
        presentation.finish()
        return handle

    agent = MagicMock(profile=RuntimeProfile.DURABLE)
    agent.start = AsyncMock(side_effect=start)
    repl = object.__new__(AsyncREPL)
    repl._agent = agent
    repl._console = MagicMock()
    repl._debug = False

    await repl._stream_response("work")

    rendered = "\n".join(str(call.args[0]) for call in repl._console.print.call_args_list)
    assert "Live display detached" in rendered
    assert "authoritative final" in rendered
    handle.wait.assert_awaited_once_with()


def _load_async_repl_without_optional_cli_dependency():
    """Import the REPL without weakening the provider-neutral core test lane."""
    import importlib
    import sys
    from contextlib import nullcontext
    from types import ModuleType

    prompt_toolkit = ModuleType("prompt_toolkit")
    prompt_toolkit.PromptSession = MagicMock
    patch_stdout_module = ModuleType("prompt_toolkit.patch_stdout")
    patch_stdout_module.patch_stdout = nullcontext
    with patch.dict(
        sys.modules,
        {
            "prompt_toolkit": prompt_toolkit,
            "prompt_toolkit.patch_stdout": patch_stdout_module,
        },
    ):
        return importlib.import_module("agnoclaw.cli.async_repl").AsyncREPL
