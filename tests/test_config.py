"""Tests for the configuration system."""



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
    from agnoclaw.config import HarnessConfig
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
    assert cfg.enable_background_bash_tools is False


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


def test_compression_disabled_by_default():
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.enable_compression is False


def test_compress_token_limit_none_by_default():
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.compress_token_limit is None


def test_session_summary_disabled_by_default():
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.enable_session_summary is False


def test_event_sink_mode_default_best_effort():
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.event_sink_mode == "best_effort"


def test_policy_fail_open_default_false():
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.policy_fail_open is False


def test_permission_mode_default_bypass():
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.permission_mode == "bypass"
    assert cfg.permission_require_approver is False
    assert cfg.permission_preapproved_tools == []
    assert cfg.permission_preapproved_categories == []


def test_env_override_enable_compression(monkeypatch):
    from agnoclaw.config import HarnessConfig
    monkeypatch.setenv("AGNOCLAW_ENABLE_COMPRESSION", "true")
    cfg = HarnessConfig()
    assert cfg.enable_compression is True


def test_env_override_compress_token_limit(monkeypatch):
    from agnoclaw.config import HarnessConfig
    monkeypatch.setenv("AGNOCLAW_COMPRESS_TOKEN_LIMIT", "4000")
    cfg = HarnessConfig()
    assert cfg.compress_token_limit == 4000


def test_env_override_enable_session_summary(monkeypatch):
    from agnoclaw.config import HarnessConfig
    monkeypatch.setenv("AGNOCLAW_ENABLE_SESSION_SUMMARY", "true")
    cfg = HarnessConfig()
    assert cfg.enable_session_summary is True


def test_env_override_enable_background_bash_tools(monkeypatch):
    from agnoclaw.config import HarnessConfig
    monkeypatch.setenv("AGNOCLAW_ENABLE_BACKGROUND_BASH_TOOLS", "true")
    cfg = HarnessConfig()
    assert cfg.enable_background_bash_tools is True


def test_env_override_event_sink_mode(monkeypatch):
    from agnoclaw.config import HarnessConfig
    monkeypatch.setenv("AGNOCLAW_EVENT_SINK_MODE", "fail_closed")
    cfg = HarnessConfig()
    assert cfg.event_sink_mode == "fail_closed"


def test_env_override_policy_fail_open(monkeypatch):
    from agnoclaw.config import HarnessConfig
    monkeypatch.setenv("AGNOCLAW_POLICY_FAIL_OPEN", "true")
    cfg = HarnessConfig()
    assert cfg.policy_fail_open is True


def test_env_override_permission_mode(monkeypatch):
    from agnoclaw.config import HarnessConfig
    monkeypatch.setenv("AGNOCLAW_PERMISSION_MODE", "plan")
    monkeypatch.setenv("AGNOCLAW_PERMISSION_REQUIRE_APPROVER", "true")
    cfg = HarnessConfig()
    assert cfg.permission_mode == "plan"
    assert cfg.permission_require_approver is True


def test_guardrails_defaults():
    from agnoclaw.config import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.guardrails_enabled is True
    assert cfg.path_guardrails_enabled is True
    assert cfg.network_enabled is True
    assert cfg.network_enforce_https is True
    assert cfg.network_block_private_hosts is True
    assert cfg.path_allowed_roots == []
    assert cfg.network_allowed_hosts == []


def test_env_override_guardrails(monkeypatch):
    from agnoclaw.config import HarnessConfig
    monkeypatch.setenv("AGNOCLAW_GUARDRAILS_ENABLED", "false")
    monkeypatch.setenv("AGNOCLAW_NETWORK_ENABLED", "false")
    cfg = HarnessConfig()
    assert cfg.guardrails_enabled is False
    assert cfg.network_enabled is False


def test_deep_merge_preserves_user_nested_values():
    from agnoclaw.config import _deep_merge

    user = {
        "heartbeat": {
            "enabled": True,
            "interval_minutes": 30,
            "active_hours_start": "08:00",
        },
        "storage": {
            "backend": "sqlite",
            "sqlite_path": "~/user.db",
        },
    }
    project = {
        "heartbeat": {
            "interval_minutes": 10,
        },
        "storage": {
            "backend": "postgres",
        },
    }

    merged = _deep_merge(user, project)
    assert merged["heartbeat"]["enabled"] is True
    assert merged["heartbeat"]["interval_minutes"] == 10
    assert merged["heartbeat"]["active_hours_start"] == "08:00"
    assert merged["storage"]["backend"] == "postgres"
    assert merged["storage"]["sqlite_path"] == "~/user.db"
