"""Tests for the configuration system."""

import os
import pytest


def test_default_config():
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.default_model == "claude-sonnet-4-6"
    assert cfg.default_provider == "anthropic"
    assert cfg.enable_bash is True
    assert cfg.storage.backend == "sqlite"


def test_env_override(monkeypatch):
    from agnoclaw.config import HarnessConfig
    monkeypatch.setenv("AGNOCLAW_DEFAULT_MODEL", "gpt-4o")
    monkeypatch.setenv("AGNOCLAW_DEFAULT_PROVIDER", "openai")

    cfg = HarnessConfig()
    assert cfg.default_model == "gpt-4o"
    assert cfg.default_provider == "openai"


def test_heartbeat_defaults():
    from agnoclaw.config import HeartbeatConfig
    hb = HeartbeatConfig()
    assert hb.interval_minutes == 30
    assert hb.model == "claude-haiku-4-5-20251001"
    assert hb.ok_threshold_chars == 300


def test_no_enable_culture():
    """enable_culture must be removed from HarnessConfig."""
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert not hasattr(cfg, "enable_culture")


def test_enable_learning_default():
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.enable_learning is False


def test_learning_mode_default():
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.learning_mode == "agentic"


def test_storage_sqlite_default():
    from agnoclaw.config import HarnessConfig, StorageConfig
    cfg = HarnessConfig()
    assert cfg.storage.backend == "sqlite"
    assert "sessions.db" in cfg.storage.sqlite_path


def test_storage_table_names():
    from agnoclaw.config import StorageConfig
    sc = StorageConfig()
    assert "agnoclaw" in sc.session_table
    assert "agnoclaw" in sc.memory_table


def test_env_override_learning_mode(monkeypatch):
    from agnoclaw.config import HarnessConfig
    monkeypatch.setenv("AGNOCLAW_LEARNING_MODE", "always")
    cfg = HarnessConfig()
    assert cfg.learning_mode == "always"


def test_env_override_enable_learning(monkeypatch):
    from agnoclaw.config import HarnessConfig
    monkeypatch.setenv("AGNOCLAW_ENABLE_LEARNING", "true")
    cfg = HarnessConfig()
    assert cfg.enable_learning is True


def test_skills_dirs_default_empty():
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.skills_dirs == []


def test_bash_timeout_default():
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.bash_timeout_seconds == 120


def test_heartbeat_active_hours():
    from agnoclaw.config import HeartbeatConfig
    hb = HeartbeatConfig()
    assert hb.active_hours_start == "08:00"
    assert hb.active_hours_end == "22:00"


def test_heartbeat_disabled_by_default():
    from agnoclaw.config import HeartbeatConfig
    hb = HeartbeatConfig()
    assert hb.enabled is False


def test_session_history_runs_default():
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.session_history_runs == 10
