"""Tests for HarnessAgent and related utilities."""

import pytest
from unittest.mock import MagicMock, patch


# ── _make_model tests ────────────────────────────────────────────────────


def test_make_model_anthropic():
    from agnoclaw.agent import _make_model
    with patch("agno.models.anthropic.Claude") as mock_cls:
        mock_cls.return_value = MagicMock()
        model = _make_model("claude-sonnet-4-6", "anthropic")
        mock_cls.assert_called_once_with(id="claude-sonnet-4-6")


def test_make_model_openai():
    from agnoclaw.agent import _make_model
    pytest.importorskip("agno.models.openai")
    with patch("agno.models.openai.OpenAIChat") as mock_cls:
        mock_cls.return_value = MagicMock()
        _make_model("gpt-4o", "openai")
        mock_cls.assert_called_once_with(id="gpt-4o")


def test_make_model_google():
    from agnoclaw.agent import _make_model
    pytest.importorskip("agno.models.google")
    with patch("agno.models.google.Gemini") as mock_cls:
        mock_cls.return_value = MagicMock()
        _make_model("gemini-2.0-flash", "google")
        mock_cls.assert_called_once_with(id="gemini-2.0-flash")


def test_make_model_groq():
    from agnoclaw.agent import _make_model
    pytest.importorskip("agno.models.groq")
    with patch("agno.models.groq.Groq") as mock_cls:
        mock_cls.return_value = MagicMock()
        _make_model("llama3-70b", "groq")
        mock_cls.assert_called_once_with(id="llama3-70b")


def test_make_model_ollama():
    from agnoclaw.agent import _make_model
    pytest.importorskip("agno.models.ollama")
    with patch("agno.models.ollama.Ollama") as mock_cls:
        mock_cls.return_value = MagicMock()
        _make_model("llama3.2", "ollama")
        mock_cls.assert_called_once_with(id="llama3.2")


def test_make_model_bedrock():
    from agnoclaw.agent import _make_model
    pytest.importorskip("agno.models.aws.bedrock")
    with patch("agno.models.aws.bedrock.AwsBedrock") as mock_cls:
        mock_cls.return_value = MagicMock()
        _make_model("anthropic.claude-3-haiku", "aws")
        mock_cls.assert_called_once_with(id="anthropic.claude-3-haiku")


def test_make_model_bedrock_alias():
    from agnoclaw.agent import _make_model
    pytest.importorskip("agno.models.aws.bedrock")
    with patch("agno.models.aws.bedrock.AwsBedrock") as mock_cls:
        mock_cls.return_value = MagicMock()
        _make_model("anthropic.claude-3-haiku", "bedrock")


def test_make_model_mistral():
    from agnoclaw.agent import _make_model
    pytest.importorskip("agno.models.mistral")
    with patch("agno.models.mistral.MistralChat") as mock_cls:
        mock_cls.return_value = MagicMock()
        _make_model("mistral-large", "mistral")


def test_make_model_xai():
    from agnoclaw.agent import _make_model
    pytest.importorskip("agno.models.xai")
    with patch("agno.models.xai.xAI") as mock_cls:
        mock_cls.return_value = MagicMock()
        _make_model("grok-2", "xai")


def test_make_model_grok_alias():
    from agnoclaw.agent import _make_model
    pytest.importorskip("agno.models.xai")
    with patch("agno.models.xai.xAI") as mock_cls:
        mock_cls.return_value = MagicMock()
        _make_model("grok-2", "grok")


def test_make_model_deepseek():
    from agnoclaw.agent import _make_model
    pytest.importorskip("agno.models.deepseek")
    with patch("agno.models.deepseek.DeepSeek") as mock_cls:
        mock_cls.return_value = MagicMock()
        _make_model("deepseek-chat", "deepseek")


def test_make_model_litellm():
    from agnoclaw.agent import _make_model
    pytest.importorskip("agno.models.litellm")
    with patch("agno.models.litellm.LiteLLM") as mock_cls:
        mock_cls.return_value = MagicMock()
        _make_model("gpt-4o", "litellm")


def test_make_model_unknown_provider_raises():
    from agnoclaw.agent import _make_model
    with pytest.raises(ValueError, match="Unknown provider"):
        _make_model("some-model", "unknown-provider")


def test_make_model_case_insensitive():
    """Provider name should be case-insensitive."""
    from agnoclaw.agent import _make_model
    with patch("agno.models.anthropic.Claude") as mock_cls:
        mock_cls.return_value = MagicMock()
        _make_model("claude-sonnet-4-6", "Anthropic")
        _make_model("claude-sonnet-4-6", "ANTHROPIC")


# ── _make_db tests ───────────────────────────────────────────────────────


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


# ── HarnessAgent construction tests ─────────────────────────────────────


def _make_mock_agent_deps():
    """Return a patcher context for HarnessAgent dependencies."""
    return patch.multiple(
        "agnoclaw.agent",
        _make_model=MagicMock(return_value=MagicMock()),
        _make_db=MagicMock(return_value=MagicMock()),
    )


def test_harness_agent_init_no_culture_param(tmp_path):
    """HarnessAgent should not accept enable_culture parameter."""
    from agnoclaw.agent import HarnessAgent
    import inspect
    sig = inspect.signature(HarnessAgent.__init__)
    assert "enable_culture" not in sig.parameters


def test_harness_agent_has_plan_mode_methods(tmp_path):
    """HarnessAgent must have enter_plan_mode and exit_plan_mode."""
    from agnoclaw.agent import HarnessAgent
    assert hasattr(HarnessAgent, "enter_plan_mode")
    assert hasattr(HarnessAgent, "exit_plan_mode")


def test_harness_agent_has_save_session_summary(tmp_path):
    from agnoclaw.agent import HarnessAgent
    assert hasattr(HarnessAgent, "save_session_summary")


def test_harness_agent_underlying_agent_property():
    """underlying_agent property should return Agno Agent."""
    from agnoclaw.agent import HarnessAgent
    prop = HarnessAgent.__dict__.get("underlying_agent")
    assert prop is not None or hasattr(HarnessAgent, "underlying_agent")


# ── Config integration tests ─────────────────────────────────────────────


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


# ── Compression / session summary parameter tests ────────────────────────


def test_harness_agent_accepts_compression_params():
    """HarnessAgent should accept enable_compression and compress_token_limit."""
    import inspect
    from agnoclaw.agent import HarnessAgent
    sig = inspect.signature(HarnessAgent.__init__)
    assert "enable_compression" in sig.parameters
    assert "compress_token_limit" in sig.parameters


def test_harness_agent_accepts_session_summary_param():
    """HarnessAgent should accept enable_session_summary."""
    import inspect
    from agnoclaw.agent import HarnessAgent
    sig = inspect.signature(HarnessAgent.__init__)
    assert "enable_session_summary" in sig.parameters


def test_harness_agent_compression_default_none():
    """enable_compression default should be None (falls back to config)."""
    import inspect
    from agnoclaw.agent import HarnessAgent
    sig = inspect.signature(HarnessAgent.__init__)
    assert sig.parameters["enable_compression"].default is None


def test_harness_agent_session_summary_default_none():
    """enable_session_summary default should be None (falls back to config)."""
    import inspect
    from agnoclaw.agent import HarnessAgent
    sig = inspect.signature(HarnessAgent.__init__)
    assert sig.parameters["enable_session_summary"].default is None
