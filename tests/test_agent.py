"""Tests for AgentHarness and related utilities."""

import pytest
from unittest.mock import MagicMock, patch


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


def test_agent_harness_instructions_param():
    """AgentHarness should accept 'instructions' as primary name."""
    from agnoclaw.agent import AgentHarness
    import inspect
    sig = inspect.signature(AgentHarness.__init__)
    assert "instructions" in sig.parameters


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
