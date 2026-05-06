"""Tests for AgentHarness and related utilities."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agnoclaw.backends import RuntimeBackend


# ── _resolve_model tests ─────────────────────────────────────────────────────


def test_resolve_model_combined_string():
    """'provider:model_id' string returned as-is (normalized)."""
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert _resolve_model("anthropic:claude-sonnet-4-6", None, cfg) == "anthropic:claude-sonnet-4-6"


def test_resolve_model_separate_provider():
    """model_id + provider combined into 'provider:model_id'."""
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert _resolve_model("claude-sonnet-4-6", "anthropic", cfg) == "anthropic:claude-sonnet-4-6"


def test_resolve_model_openai():
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert _resolve_model("gpt-4o", "openai", cfg) == "openai:gpt-4o"


def test_resolve_model_google():
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert _resolve_model("gemini-2.0-flash", "google", cfg) == "google:gemini-2.0-flash"


def test_resolve_model_groq():
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert _resolve_model("llama3-70b", "groq", cfg) == "groq:llama3-70b"


def test_resolve_model_ollama():
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert _resolve_model("llama3.2", "ollama", cfg) == "ollama:llama3.2"


def test_resolve_model_aws_alias():
    """'aws' provider alias → 'aws-bedrock'."""
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert _resolve_model("anthropic.claude-3-haiku", "aws", cfg) == "aws-bedrock:anthropic.claude-3-haiku"


def test_resolve_model_bedrock_alias():
    """'bedrock' provider alias → 'aws-bedrock'."""
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert _resolve_model("anthropic.claude-3-haiku", "bedrock", cfg) == "aws-bedrock:anthropic.claude-3-haiku"


def test_resolve_model_grok_alias():
    """'grok' provider alias → 'xai'."""
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert _resolve_model("grok-2", "grok", cfg) == "xai:grok-2"


def test_resolve_model_xai():
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert _resolve_model("grok-2", "xai", cfg) == "xai:grok-2"


def test_resolve_model_mistral():
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert _resolve_model("mistral-large", "mistral", cfg) == "mistral:mistral-large"


def test_resolve_model_deepseek():
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert _resolve_model("deepseek-chat", "deepseek", cfg) == "deepseek:deepseek-chat"


def test_resolve_model_case_insensitive():
    """Provider name should be normalized to lowercase."""
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert _resolve_model("claude-sonnet-4-6", "Anthropic", cfg) == "anthropic:claude-sonnet-4-6"
    assert _resolve_model("claude-sonnet-4-6", "ANTHROPIC", cfg) == "anthropic:claude-sonnet-4-6"


def test_resolve_model_falls_back_to_config():
    """None model/provider falls back to config defaults."""
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig(default_model="claude-haiku-4-5-20251001", default_provider="anthropic")
    result = _resolve_model(None, None, cfg)
    assert result == "anthropic:claude-haiku-4-5-20251001"


def test_resolve_model_combined_string_with_alias():
    """Combined string with alias provider is normalized."""
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    result = _resolve_model("aws-bedrock:my-model", None, cfg)
    assert result == "aws-bedrock:my-model"


def test_resolve_model_ollama_model_id_contains_colon_with_provider():
    """Model IDs with ':' should remain model IDs when provider is explicit."""
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig

    cfg = HarnessConfig(default_provider="anthropic")
    result = _resolve_model("qwen3:0.6b", "ollama", cfg)
    assert result == "ollama:qwen3:0.6b"


def test_resolve_model_ollama_model_id_contains_colon_with_default_provider():
    """Unknown prefix in model string should use configured default provider."""
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig

    cfg = HarnessConfig(default_provider="ollama")
    result = _resolve_model("qwen3:0.6b", None, cfg)
    assert result == "ollama:qwen3:0.6b"


# ── _make_db tests ───────────────────────────────────────────────────────────


def test_make_db_sqlite(tmp_path):
    from agnoclaw.agent import _make_db
    from agnoclaw.config import HarnessConfig, StorageConfig

    cfg = HarnessConfig(storage=StorageConfig(
        backend="sqlite",
        sqlite_path=str(tmp_path / "test.db"),
    ))

    with patch("agno.db.sqlite.SqliteDb") as mock_db:
        mock_db.return_value = MagicMock()
        _make_db(cfg)
        mock_db.assert_called_once()
        call_kwargs = mock_db.call_args[1]
        assert "db_file" in call_kwargs


def test_make_db_postgres():
    from agnoclaw.agent import _make_db
    from agnoclaw.config import HarnessConfig, StorageConfig

    cfg = HarnessConfig(storage=StorageConfig(
        backend="postgres",
        postgres_url="postgresql://user:pass@localhost/mydb",
    ))

    with patch("agno.db.postgres.PostgresDb") as mock_db:
        mock_db.return_value = MagicMock()
        _make_db(cfg)
        mock_db.assert_called_once()
        call_kwargs = mock_db.call_args[1]
        assert call_kwargs["db_url"] == "postgresql://user:pass@localhost/mydb"


def test_make_db_postgres_missing_url_raises():
    from agnoclaw.agent import _make_db
    from agnoclaw.config import HarnessConfig, StorageConfig

    cfg = HarnessConfig(storage=StorageConfig(
        backend="postgres",
        postgres_url=None,
    ))

    with pytest.raises(ValueError, match="AGNOCLAW_STORAGE__POSTGRES_URL"):
        _make_db(cfg)


# ── AgentHarness construction tests ──────────────────────────────────────────


def _make_mock_agent_deps():
    """Return a patcher context for AgentHarness dependencies."""
    return patch.multiple(
        "agnoclaw.agent",
        _resolve_model=MagicMock(return_value="anthropic:claude-sonnet-4-6"),
        _make_db=MagicMock(return_value=MagicMock()),
    )


def test_agent_harness_is_primary_name():
    """AgentHarness is the primary class name."""
    from agnoclaw.agent import AgentHarness
    assert AgentHarness is not None


def test_harness_agent_is_alias():
    """HarnessAgent is a backward-compat alias for AgentHarness."""
    from agnoclaw.agent import AgentHarness, HarnessAgent
    assert HarnessAgent is AgentHarness


def test_agent_harness_init_no_culture_param(tmp_path):
    """AgentHarness should not accept enable_culture parameter."""
    from agnoclaw.agent import AgentHarness
    import inspect
    sig = inspect.signature(AgentHarness.__init__)
    assert "enable_culture" not in sig.parameters


def test_agent_harness_has_plan_mode_methods():
    """AgentHarness must have enter_plan_mode and exit_plan_mode."""
    from agnoclaw.agent import AgentHarness
    assert hasattr(AgentHarness, "enter_plan_mode")
    assert hasattr(AgentHarness, "exit_plan_mode")


def test_plan_mode_toggles_permission_mode(tmp_path):
    from agnoclaw.agent import AgentHarness
    from agnoclaw.config import HarnessConfig

    with patch("agnoclaw.agent.Agent", return_value=MagicMock()):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=tmp_path,
                config=HarnessConfig(permission_mode="default"),
            )

    assert harness.permission_mode == "default"
    harness.enter_plan_mode()
    assert harness.permission_mode == "plan"
    harness.exit_plan_mode()
    assert harness.permission_mode == "default"


def test_agent_harness_has_save_session_summary():
    from agnoclaw.agent import AgentHarness
    assert hasattr(AgentHarness, "save_session_summary")


def test_agent_harness_underlying_agent_property():
    """underlying_agent property should exist."""
    from agnoclaw.agent import AgentHarness
    assert hasattr(AgentHarness, "underlying_agent")


def test_agent_harness_model_as_positional_string():
    """AgentHarness accepts model as first positional arg."""
    from agnoclaw.agent import AgentHarness
    import inspect
    sig = inspect.signature(AgentHarness.__init__)
    params = list(sig.parameters.keys())
    # 'model' should be first positional param after self
    assert params[1] == "model"


def test_agent_harness_tools_param():
    """AgentHarness should accept 'tools' as primary name."""
    from agnoclaw.agent import AgentHarness
    import inspect
    sig = inspect.signature(AgentHarness.__init__)
    assert "tools" in sig.parameters


def test_agent_harness_default_tools_use_constructor_workspace(tmp_path):
    from agnoclaw.agent import AgentHarness
    from agnoclaw.config import HarnessConfig
    from agnoclaw.tools.files import FilesToolkit
    from agnoclaw.tools.tasks import ProgressToolkit

    captured_tools = []
    mock_agent = MagicMock()

    def _agent_ctor(*args, **kwargs):
        captured_tools[:] = kwargs.get("tools", [])
        mock_agent.system_message = kwargs.get("system_message")
        mock_agent.session_id = kwargs.get("session_id")
        return mock_agent

    config_workspace = tmp_path / "config-workspace"
    harness_workspace = tmp_path / "constructor-workspace"
    harness_sandbox = tmp_path / "constructor-sandbox"
    cfg = HarnessConfig(workspace_dir=str(config_workspace))

    with patch("agnoclaw.agent.Agent", side_effect=_agent_ctor):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            AgentHarness(
                workspace_dir=harness_workspace,
                sandbox_dir=harness_sandbox,
                config=cfg,
            )

    files = next(t for t in captured_tools if isinstance(t, FilesToolkit))
    progress = next(t for t in captured_tools if isinstance(t, ProgressToolkit))

    assert files.workspace_dir == Path(harness_sandbox).resolve()
    assert Path(progress._project_dir) == Path(harness_workspace).resolve()


@pytest.mark.asyncio
async def test_agent_harness_session_sandbox_end_to_end(tmp_path):
    from agnoclaw.agent import AgentHarness
    from agnoclaw.config import HarnessConfig
    from agnoclaw.tools.files import FilesToolkit

    captured_tools = []
    callback_payload = {}
    mock_agent = MagicMock()

    def _agent_ctor(*args, **kwargs):
        captured_tools[:] = kwargs.get("tools", [])
        mock_agent.system_message = kwargs.get("system_message")
        mock_agent.session_id = kwargs.get("session_id")
        return mock_agent

    workspace_dir = tmp_path / "workspace"
    sandbox_dir = tmp_path / "sandbox"
    workspace_dir.mkdir()
    workspace_input = workspace_dir / "input.txt"
    workspace_input.write_text("alpha", encoding="utf-8")

    async def on_session_end(summary: str, created_files: list[str] | None = None) -> None:
        callback_payload["summary"] = summary
        callback_payload["created_files"] = created_files

    with patch("agnoclaw.agent.Agent", side_effect=_agent_ctor):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=workspace_dir,
                sandbox_dir=sandbox_dir,
                config=HarnessConfig(),
                on_session_end=on_session_end,
            )

    files = next(t for t in captured_tools if isinstance(t, FilesToolkit))
    bash = next(t for t in captured_tools if getattr(t, "name", None) == "bash")

    assert str(workspace_dir.resolve()) in mock_agent.system_message
    assert str(sandbox_dir.resolve()) in mock_agent.system_message

    files.write_file("notes/session.txt", "sandbox file")
    files.write_file(str(workspace_dir / "workspace.txt"), "workspace file")

    bash.entrypoint(
        f'"{sys.executable}" -c "from pathlib import Path; '
        f'text = Path(r\'{workspace_input}\').read_text(); '
        f'Path(\'session-script.txt\').write_text(text.upper()); '
        f'Path(r\'{workspace_dir / "output.txt"}\').write_text(text + \'!\')"'
    )

    assert (sandbox_dir / "notes" / "session.txt").read_text(encoding="utf-8") == "sandbox file"
    assert (sandbox_dir / "session-script.txt").read_text(encoding="utf-8") == "ALPHA"
    assert (workspace_dir / "workspace.txt").read_text(encoding="utf-8") == "workspace file"
    assert (workspace_dir / "output.txt").read_text(encoding="utf-8") == "alpha!"

    mock_agent.get_chat_history.return_value = [
        SimpleNamespace(role="user", content="hello"),
        SimpleNamespace(role="assistant", content="world"),
    ]
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="summary"))

    result = await harness.end_session()

    assert result == "summary"
    assert callback_payload["summary"] == "summary"
    assert callback_payload["created_files"] == [
        str(sandbox_dir / "notes" / "session.txt"),
        str(sandbox_dir / "session-script.txt"),
    ]
    assert not sandbox_dir.exists()


def test_agent_harness_close_closes_owned_storage_once(tmp_path):
    from agnoclaw.agent import AgentHarness
    from agnoclaw.config import HarnessConfig

    storage = MagicMock()
    storage.Session.remove = MagicMock()
    storage.close = MagicMock()

    with patch("agnoclaw.agent.Agent", return_value=MagicMock()):
        with patch("agnoclaw.agent._make_db", return_value=storage):
            harness = AgentHarness(
                workspace_dir=tmp_path,
                config=HarnessConfig(),
            )

    harness.close()
    harness.close()

    storage.Session.remove.assert_called_once_with()
    storage.close.assert_called_once_with()


def test_agent_harness_close_does_not_close_injected_storage(tmp_path):
    from agnoclaw.agent import AgentHarness
    from agnoclaw.config import HarnessConfig

    storage = MagicMock()
    storage.Session.remove = MagicMock()
    storage.close = MagicMock()

    with patch("agnoclaw.agent.Agent", return_value=MagicMock()):
        harness = AgentHarness(
            workspace_dir=tmp_path,
            config=HarnessConfig(),
            db=storage,
        )

    harness.close()

    storage.Session.remove.assert_not_called()
    storage.close.assert_not_called()


def test_agent_harness_context_manager_closes_owned_storage(tmp_path):
    from agnoclaw.agent import AgentHarness
    from agnoclaw.config import HarnessConfig

    storage = MagicMock()
    storage.Session.remove = MagicMock()
    storage.close = MagicMock()

    with patch("agnoclaw.agent.Agent", return_value=MagicMock()):
        with patch("agnoclaw.agent._make_db", return_value=storage):
            with AgentHarness(
                workspace_dir=tmp_path,
                config=HarnessConfig(),
            ) as harness:
                assert harness is not None

    storage.Session.remove.assert_called_once_with()
    storage.close.assert_called_once_with()


def test_agent_harness_passes_backend_to_default_tools(tmp_path):
    from agnoclaw.agent import AgentHarness
    from agnoclaw.config import HarnessConfig

    backend = RuntimeBackend(command_executor=object(), workspace_adapter=object())

    with patch("agnoclaw.agent.Agent", return_value=MagicMock()):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            with patch("agnoclaw.agent.get_default_tools", return_value=[]) as mock_tools:
                AgentHarness(
                    workspace_dir=tmp_path,
                    config=HarnessConfig(),
                    backend=backend,
                )

    assert mock_tools.call_args[1]["backend"] is backend


def test_agent_harness_forwards_structured_output_options(tmp_path):
    from agnoclaw.agent import AgentHarness
    from agnoclaw.config import HarnessConfig

    captured_kwargs = {}

    def _agent_ctor(*args, **kwargs):
        captured_kwargs.update(kwargs)
        mock_agent = MagicMock()
        mock_agent.system_message = kwargs.get("system_message")
        mock_agent.session_id = kwargs.get("session_id")
        return mock_agent

    with patch("agnoclaw.agent.Agent", side_effect=_agent_ctor):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            AgentHarness(
                workspace_dir=tmp_path,
                config=HarnessConfig(),
                output_schema={"type": "object"},
                parser_model="anthropic:claude-haiku-4-5",
                parser_model_prompt="parse this",
                output_model="anthropic:claude-haiku-4-5",
                output_model_prompt="rewrite this",
                parse_response=False,
                structured_outputs=True,
                use_json_mode=True,
            )

    assert captured_kwargs["output_schema"] == {"type": "object"}
    assert captured_kwargs["parser_model"] == "anthropic:claude-haiku-4-5"
    assert captured_kwargs["parser_model_prompt"] == "parse this"
    assert captured_kwargs["output_model"] == "anthropic:claude-haiku-4-5"
    assert captured_kwargs["output_model_prompt"] == "rewrite this"
    assert captured_kwargs["parse_response"] is False
    assert captured_kwargs["structured_outputs"] is True
    assert captured_kwargs["use_json_mode"] is True


def test_runtime_backend_requires_command_and_workspace_together():
    with pytest.raises(ValueError, match="both command_executor and workspace_adapter"):
        RuntimeBackend(command_executor=object())


def test_agent_harness_instructions_param():
    """AgentHarness should accept 'instructions' as primary name."""
    from agnoclaw.agent import AgentHarness
    import inspect
    sig = inspect.signature(AgentHarness.__init__)
    assert "instructions" in sig.parameters


def test_agent_harness_accepts_structured_output_params():
    """AgentHarness should expose Agno structured-output constructor options."""
    from agnoclaw.agent import AgentHarness
    import inspect

    sig = inspect.signature(AgentHarness.__init__)
    assert "output_schema" in sig.parameters
    assert "parser_model" in sig.parameters
    assert "parse_response" in sig.parameters
    assert "output_model" in sig.parameters


def test_agent_harness_legacy_model_id_param():
    """AgentHarness should still accept legacy model_id param."""
    from agnoclaw.agent import AgentHarness
    import inspect
    sig = inspect.signature(AgentHarness.__init__)
    assert "model_id" in sig.parameters


# ── Config integration tests ──────────────────────────────────────────────────


def test_config_no_enable_culture_field():
    """HarnessConfig should NOT have enable_culture field after removal."""
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert not hasattr(cfg, "enable_culture"), "enable_culture should be removed"


def test_config_enable_learning_default_false():
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.enable_learning is False


def test_config_learning_mode_default_agentic():
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.learning_mode == "agentic"


# ── Compression / session summary parameter tests ─────────────────────────────


def test_agent_harness_accepts_compression_params():
    """AgentHarness should accept enable_compression and compress_token_limit."""
    import inspect
    from agnoclaw.agent import AgentHarness
    sig = inspect.signature(AgentHarness.__init__)
    assert "enable_compression" in sig.parameters
    assert "compress_token_limit" in sig.parameters


def test_agent_harness_accepts_session_summary_param():
    """AgentHarness should accept enable_session_summary."""
    import inspect
    from agnoclaw.agent import AgentHarness
    sig = inspect.signature(AgentHarness.__init__)
    assert "enable_session_summary" in sig.parameters


def test_agent_harness_compression_default_none():
    """enable_compression default should be None (falls back to config)."""
    import inspect
    from agnoclaw.agent import AgentHarness
    sig = inspect.signature(AgentHarness.__init__)
    assert sig.parameters["enable_compression"].default is None


def test_agent_harness_session_summary_default_none():
    """enable_session_summary default should be None (falls back to config)."""
    import inspect
    from agnoclaw.agent import AgentHarness
    sig = inspect.signature(AgentHarness.__init__)
    assert sig.parameters["enable_session_summary"].default is None


# ── Context budget guard tests ─────────────────────────────────────────────


def test_agent_harness_accepts_max_context_tokens():
    """AgentHarness should accept max_context_tokens parameter."""
    import inspect
    from agnoclaw.agent import AgentHarness
    sig = inspect.signature(AgentHarness.__init__)
    assert "max_context_tokens" in sig.parameters


def test_agent_harness_max_context_tokens_default_none():
    """max_context_tokens should default to None (disabled)."""
    import inspect
    from agnoclaw.agent import AgentHarness
    sig = inspect.signature(AgentHarness.__init__)
    assert sig.parameters["max_context_tokens"].default is None


def test_check_context_budget_noop_without_max():
    """_check_context_budget should silently skip when max_context_tokens is None."""
    from agnoclaw.agent import AgentHarness
    harness = MagicMock(spec=AgentHarness)
    harness._max_context_tokens = None
    AgentHarness._check_context_budget(harness)
    # Should not raise or access model


def test_check_context_budget_skips_without_count_tokens():
    """_check_context_budget should skip if model lacks count_tokens()."""
    from agnoclaw.agent import AgentHarness
    harness = MagicMock(spec=AgentHarness)
    harness._max_context_tokens = 100000
    mock_agent = MagicMock()
    mock_agent.model = MagicMock(spec=[])  # no count_tokens
    harness._agent = mock_agent
    AgentHarness._check_context_budget(harness)
    # Should not raise


def test_check_context_budget_warns_at_85_pct():
    """_check_context_budget should log warning at 85% usage."""
    from agnoclaw.agent import AgentHarness
    harness = MagicMock(spec=AgentHarness)
    harness._max_context_tokens = 100000
    harness.session_id = "test-session"
    mock_agent = MagicMock()
    mock_agent.model.count_tokens.return_value = 87000  # 87%
    mock_agent.get_chat_history.return_value = [{"role": "user", "content": "hi"}]
    harness._agent = mock_agent

    with patch("agnoclaw.agent.logger") as mock_logger:
        AgentHarness._check_context_budget(harness)
        mock_logger.warning.assert_called_once()
        assert "87%" in mock_logger.warning.call_args[0][0] % mock_logger.warning.call_args[0][1:]


def test_check_context_budget_critical_at_95_pct():
    """_check_context_budget should log critical at 95% usage."""
    from agnoclaw.agent import AgentHarness
    harness = MagicMock(spec=AgentHarness)
    harness._max_context_tokens = 100000
    harness.session_id = "test-session"
    mock_agent = MagicMock()
    mock_agent.model.count_tokens.return_value = 96000  # 96%
    mock_agent.get_chat_history.return_value = [{"role": "user", "content": "hi"}]
    harness._agent = mock_agent

    with patch("agnoclaw.agent.logger") as mock_logger:
        AgentHarness._check_context_budget(harness)
        mock_logger.critical.assert_called_once()


def test_check_context_budget_no_warning_under_85_pct():
    """_check_context_budget should not log below 85% usage."""
    from agnoclaw.agent import AgentHarness
    harness = MagicMock(spec=AgentHarness)
    harness._max_context_tokens = 100000
    harness.session_id = "test-session"
    mock_agent = MagicMock()
    mock_agent.model.count_tokens.return_value = 50000  # 50%
    mock_agent.get_chat_history.return_value = [{"role": "user", "content": "hi"}]
    harness._agent = mock_agent

    with patch("agnoclaw.agent.logger") as mock_logger:
        AgentHarness._check_context_budget(harness)
        mock_logger.warning.assert_not_called()
        mock_logger.critical.assert_not_called()


def test_check_context_budget_empty_history():
    """_check_context_budget should skip when history is empty."""
    from agnoclaw.agent import AgentHarness
    harness = MagicMock(spec=AgentHarness)
    harness._max_context_tokens = 100000
    harness.session_id = "test-session"
    mock_agent = MagicMock()
    mock_agent.get_chat_history.return_value = []
    harness._agent = mock_agent

    with patch("agnoclaw.agent.logger") as mock_logger:
        AgentHarness._check_context_budget(harness)
        mock_logger.warning.assert_not_called()
        mock_logger.critical.assert_not_called()


# ── Subagent parameter tests ──────────────────────────────────────────────


def test_agent_harness_accepts_subagents_param():
    """AgentHarness should accept subagents parameter."""
    import inspect
    from agnoclaw.agent import AgentHarness
    sig = inspect.signature(AgentHarness.__init__)
    assert "subagents" in sig.parameters


def test_agent_harness_subagents_default_none():
    """subagents default should be None."""
    import inspect
    from agnoclaw.agent import AgentHarness
    sig = inspect.signature(AgentHarness.__init__)
    assert sig.parameters["subagents"].default is None


# ── Memory optimization auto-trigger tests ────────────────────────────────


def test_maybe_optimize_memories_skips_without_learning():
    """_maybe_optimize_memories should skip when learning is None."""
    from agnoclaw.agent import AgentHarness
    harness = MagicMock(spec=AgentHarness)
    harness._run_count = 9
    harness._optimize_every_n_runs = 10
    mock_agent = MagicMock()
    mock_agent.learning = None
    harness._agent = mock_agent

    AgentHarness._maybe_optimize_memories(harness)
    # Should increment count but not call optimize
    assert harness._run_count == 10


def test_maybe_optimize_memories_triggers_at_interval():
    """_maybe_optimize_memories should call optimize_memories every N runs."""
    from agnoclaw.agent import AgentHarness
    harness = MagicMock(spec=AgentHarness)
    harness._run_count = 9  # next increment makes it 10
    harness._optimize_every_n_runs = 10
    mock_learning = MagicMock()
    mock_learning.optimize_memories = MagicMock()
    mock_agent = MagicMock()
    mock_agent.learning = mock_learning
    harness._agent = mock_agent

    AgentHarness._maybe_optimize_memories(harness)
    mock_learning.optimize_memories.assert_called_once()


def test_maybe_optimize_memories_does_not_trigger_off_interval():
    """_maybe_optimize_memories should skip when not at interval."""
    from agnoclaw.agent import AgentHarness
    harness = MagicMock(spec=AgentHarness)
    harness._run_count = 7  # next increment makes it 8 (not a multiple of 10)
    harness._optimize_every_n_runs = 10
    mock_learning = MagicMock()
    mock_agent = MagicMock()
    mock_agent.learning = mock_learning
    harness._agent = mock_agent

    AgentHarness._maybe_optimize_memories(harness)
    mock_learning.optimize_memories.assert_not_called()


def test_maybe_optimize_memories_handles_exception():
    """_maybe_optimize_memories should not raise on optimize failure."""
    from agnoclaw.agent import AgentHarness
    harness = MagicMock(spec=AgentHarness)
    harness._run_count = 9
    harness._optimize_every_n_runs = 10
    mock_learning = MagicMock()
    mock_learning.optimize_memories.side_effect = RuntimeError("db error")
    mock_agent = MagicMock()
    mock_agent.learning = mock_learning
    harness._agent = mock_agent

    # Should not raise
    AgentHarness._maybe_optimize_memories(harness)


def test_run_skill_injection_restores_base_prompt(tmp_path):
    """Skill prompts should be one-shot and preserve base prompt sections."""
    from agnoclaw.agent import AgentHarness
    from agnoclaw.config import HarnessConfig

    captured_prompts = []
    mock_agent = MagicMock()

    def _agent_ctor(*args, **kwargs):
        mock_agent.system_message = kwargs.get("system_message")
        mock_agent.session_id = kwargs.get("session_id")
        return mock_agent

    def _run(*args, **kwargs):
        captured_prompts.append(mock_agent.system_message)
        return MagicMock(content="ok")

    mock_agent.run.side_effect = _run

    with patch("agnoclaw.agent.Agent", side_effect=_agent_ctor):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=tmp_path,
                config=HarnessConfig(),
                instructions="Project rule: always run tests",
                enable_learning=True,
            )

    harness.skills.load_skill = MagicMock(return_value="# Skill\n\nFollow this skill.")
    base_prompt = harness.underlying_agent.system_message

    harness.run("first", skill="code-review")
    assert len(captured_prompts) == 1
    assert "# Active Skill" in captured_prompts[0]
    assert "Project rule: always run tests" in captured_prompts[0]
    assert "# Institutional Learning" in captured_prompts[0]
    assert harness.underlying_agent.system_message == base_prompt

    captured_prompts.clear()
    harness.run("second")
    assert len(captured_prompts) == 1
    assert "# Active Skill" not in captured_prompts[0]


def test_clear_session_context_rotates_session_id(tmp_path):
    """clear_session_context should switch to a fresh session ID."""
    from agnoclaw.agent import AgentHarness
    from agnoclaw.config import HarnessConfig

    mock_agent = MagicMock()

    def _agent_ctor(*args, **kwargs):
        mock_agent.system_message = kwargs.get("system_message")
        mock_agent.session_id = kwargs.get("session_id")
        return mock_agent

    with patch("agnoclaw.agent.Agent", side_effect=_agent_ctor):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=tmp_path,
                config=HarnessConfig(),
                session_id="session-old",
            )

    new_session = harness.clear_session_context("session-new")
    assert new_session == "session-new"
    assert harness.session_id == "session-new"
    assert harness.underlying_agent.session_id == "session-new"
    assert "Session ID: session-new" in harness.underlying_agent.system_message
