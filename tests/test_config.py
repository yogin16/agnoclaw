"""Tests for the configuration system."""

from pathlib import Path

import pytest
from pydantic import ValidationError


def test_default_config():
    from agnoclaw.config import HarnessConfig, RuntimeProfile

    cfg = HarnessConfig()
    assert cfg.default_model == "claude-sonnet-4-6"
    assert cfg.default_provider == "anthropic"
    assert cfg.enable_bash is True
    assert cfg.storage.backend == "sqlite"
    assert cfg.profile is RuntimeProfile.LEGACY
    assert cfg.runtime_max_waiting == 1024
    assert cfg.runtime_max_waiting_per_tenant == 256
    assert cfg.runtime_max_waiting_per_session == 32
    assert cfg.runtime_admission_timeout_seconds == 30.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_max_waiting", 0),
        ("runtime_max_waiting", 100_001),
        ("runtime_max_waiting_per_tenant", 100_001),
        ("runtime_max_waiting_per_session", 100_001),
        ("runtime_admission_timeout_seconds", 0),
        ("runtime_admission_timeout_seconds", 3601),
    ],
)
def test_runtime_admission_config_is_bounded(field, value):
    from agnoclaw.config import HarnessConfig

    with pytest.raises(ValidationError):
        HarnessConfig(**{field: value})


@pytest.mark.parametrize(
    "values",
    [
        {"runtime_max_waiting": 4, "runtime_max_waiting_per_tenant": 5},
        {
            "runtime_max_waiting_per_tenant": 4,
            "runtime_max_waiting_per_session": 5,
        },
    ],
)
def test_runtime_admission_nested_queue_limits_are_monotonic(values):
    from agnoclaw.config import HarnessConfig

    with pytest.raises(ValidationError):
        HarnessConfig(**values)


def test_explicit_profile_presets_are_small_safe_and_orthogonal():
    from agnoclaw.config import HarnessConfig, RuntimeProfile

    quick = HarnessConfig.quick()
    durable = HarnessConfig.durable()
    service = HarnessConfig.service()

    assert quick.profile is RuntimeProfile.QUICK
    assert quick.storage.sqlite_path == ":memory:"
    assert quick.enable_learning is False
    assert quick.permission_mode == "default"
    assert quick.permission_require_approver is True
    assert quick.event_sink_mode == "fail_closed"
    assert quick.enable_plugins is False

    assert durable.profile is RuntimeProfile.DURABLE
    assert durable.storage.backend == "sqlite"
    assert service.profile is RuntimeProfile.SERVICE
    assert service.storage.backend == "postgres"
    assert durable.session_history_runs == service.session_history_runs == 10


def test_profile_presets_preserve_explicit_host_overrides():
    from agnoclaw.config import HarnessConfig, RuntimeProfile

    configured = HarnessConfig.quick(
        permission_mode="bypass",
        storage={"backend": "sqlite", "sqlite_path": "custom.db"},
    )
    migrated = HarnessConfig(
        default_model="custom-model",
        permission_mode="bypass",
    ).with_profile("durable")

    assert configured.profile is RuntimeProfile.QUICK
    assert configured.permission_mode == "bypass"
    assert configured.storage.sqlite_path == "custom.db"
    assert migrated.profile is RuntimeProfile.DURABLE
    assert migrated.default_model == "custom-model"
    assert migrated.permission_mode == "bypass"
    assert migrated.event_sink_mode == "fail_closed"


def test_profile_from_environment_receives_the_same_fail_closed_defaults(monkeypatch):
    from agnoclaw.config import HarnessConfig, RuntimeProfile

    monkeypatch.setenv("AGNOCLAW_PROFILE", "durable")
    configured = HarnessConfig()

    assert configured.profile is RuntimeProfile.DURABLE
    assert configured.permission_mode == "default"
    assert configured.permission_require_approver is True
    assert configured.event_sink_mode == "fail_closed"
    assert configured.enable_plugins is False


def test_local_safe_is_a_posture_not_a_fourth_runtime_profile():
    from agnoclaw.config import HarnessConfig, RuntimeProfile

    configured = HarnessConfig.local_safe(profile="durable")

    assert configured.profile is RuntimeProfile.DURABLE
    assert configured.sandbox_mode == "workspace_write"
    assert configured.network_block_private_hosts is True
    assert configured.permission_require_approver is True
    with pytest.raises(ValueError, match="quick and durable"):
        HarnessConfig.local_safe(profile="service")  # type: ignore[arg-type]


def test_named_profile_rejects_a_conflicting_profile_override():
    from agnoclaw.config import HarnessConfig

    with pytest.raises(ValueError, match="cannot be overridden"):
        HarnessConfig.durable(profile="quick")


def test_env_override(monkeypatch):
    from agnoclaw.config import HarnessConfig

    monkeypatch.setenv("AGNOCLAW_DEFAULT_MODEL", "gpt-4o")
    monkeypatch.setenv("AGNOCLAW_DEFAULT_PROVIDER", "openai")

    cfg = HarnessConfig()
    assert cfg.default_model == "gpt-4o"
    assert cfg.default_provider == "openai"


def test_explicit_constructor_values_override_environment(monkeypatch):
    from agnoclaw.config import HarnessConfig, HeartbeatConfig, StorageConfig

    monkeypatch.setenv("AGNOCLAW_DEFAULT_MODEL", "env-model")
    monkeypatch.setenv("AGNOCLAW_HB_ENABLED", "true")
    monkeypatch.setenv("AGNOCLAW_STORAGE_BACKEND", "postgres")

    assert HarnessConfig(default_model="code-model").default_model == "code-model"
    assert HeartbeatConfig(enabled=False).enabled is False
    assert StorageConfig(backend="sqlite").backend == "sqlite"


def test_get_config_precedence_is_environment_then_project_then_user(
    monkeypatch,
    tmp_path,
):
    from agnoclaw.config import get_config

    home = tmp_path / "home"
    user_config = home / ".agnoclaw" / "config.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text(
        'default_model = "user-model"\n'
        "[heartbeat]\ninterval_minutes = 30\nenabled = false\n"
        '[storage]\nbackend = "sqlite"\n',
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / ".agnoclaw.toml").write_text(
        'default_model = "project-model"\n'
        '[heartbeat]\ninterval_minutes = 15\nactive_hours_start = "06:00"\n'
        '[storage]\nsqlite_path = "project.db"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(project)
    monkeypatch.setenv("AGNOCLAW_DEFAULT_MODEL", "env-model")
    monkeypatch.setenv("AGNOCLAW_HEARTBEAT__INTERVAL_MINUTES", "5")
    monkeypatch.setenv("AGNOCLAW_HB_ENABLED", "true")
    monkeypatch.setenv("AGNOCLAW_STORAGE_BACKEND", "postgres")
    get_config.cache_clear()

    config = get_config()

    assert config.default_model == "env-model"
    assert config.heartbeat.interval_minutes == 5
    assert config.heartbeat.enabled is True
    assert config.heartbeat.active_hours_start == "06:00"
    assert config.storage.backend == "postgres"
    assert config.storage.sqlite_path == "project.db"
    get_config.cache_clear()


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


def test_session_context_default():
    from agnoclaw.config import HarnessConfig

    assert HarnessConfig().enable_session_context is False


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


def test_env_override_session_context(monkeypatch):
    from agnoclaw.config import HarnessConfig

    monkeypatch.setenv("AGNOCLAW_ENABLE_SESSION_CONTEXT", "true")
    assert HarnessConfig().enable_session_context is True


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


def test_automatic_context_compaction_is_explicitly_budgeted_and_opt_in():
    from agnoclaw.config import HarnessConfig

    default = HarnessConfig()
    configured = HarnessConfig(max_context_tokens=200_000, auto_compact_context=True)

    assert default.max_context_tokens is None
    assert default.auto_compact_context is False
    assert configured.max_context_tokens == 200_000
    assert configured.auto_compact_context is True


def test_context_budget_must_be_positive():
    from agnoclaw.config import HarnessConfig

    with pytest.raises(ValidationError):
        HarnessConfig(max_context_tokens=0)


def test_env_override_automatic_context_compaction(monkeypatch):
    from agnoclaw.config import HarnessConfig

    monkeypatch.setenv("AGNOCLAW_MAX_CONTEXT_TOKENS", "120000")
    monkeypatch.setenv("AGNOCLAW_AUTO_COMPACT_CONTEXT", "true")

    cfg = HarnessConfig()

    assert cfg.max_context_tokens == 120_000
    assert cfg.auto_compact_context is True


def test_output_spill_is_explicit_bounded_and_env_configurable(monkeypatch):
    from agnoclaw.config import HarnessConfig

    assert HarnessConfig().max_inline_output_chars is None
    assert HarnessConfig(max_inline_output_chars=1_024).max_inline_output_chars == 1_024
    with pytest.raises(ValidationError):
        HarnessConfig(max_inline_output_chars=1_023)

    monkeypatch.setenv("AGNOCLAW_MAX_INLINE_OUTPUT_CHARS", "4096")
    assert HarnessConfig().max_inline_output_chars == 4_096


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
    assert cfg.permission_durable_approvals is True
    assert cfg.permission_approval_ttl_seconds == 900
    assert cfg.permission_approval_poll_interval_seconds == 0.25


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
    monkeypatch.setenv("AGNOCLAW_PERMISSION_DURABLE_APPROVALS", "false")
    monkeypatch.setenv("AGNOCLAW_PERMISSION_APPROVAL_TTL_SECONDS", "120")
    monkeypatch.setenv(
        "AGNOCLAW_PERMISSION_APPROVAL_POLL_INTERVAL_SECONDS",
        "0.5",
    )
    cfg = HarnessConfig()
    assert cfg.permission_mode == "plan"
    assert cfg.permission_require_approver is True
    assert cfg.permission_durable_approvals is False
    assert cfg.permission_approval_ttl_seconds == 120
    assert cfg.permission_approval_poll_interval_seconds == 0.5


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("permission_approval_ttl_seconds", 0),
        ("permission_approval_ttl_seconds", 604_801),
        ("permission_approval_poll_interval_seconds", 0),
        ("permission_approval_poll_interval_seconds", 5.01),
    ],
)
def test_durable_approval_config_bounds(field, value):
    from agnoclaw.config import HarnessConfig

    with pytest.raises(ValidationError):
        HarnessConfig(**{field: value})


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
