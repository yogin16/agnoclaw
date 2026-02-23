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
