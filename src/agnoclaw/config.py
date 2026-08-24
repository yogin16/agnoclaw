"""
Configuration system for agnoclaw.

File-backed settings are loaded in priority order (highest → lowest):
  1. Environment variables (AGNOCLAW_* prefix)
  2. .agnoclaw.toml in cwd   (project-level)
  3. ~/.agnoclaw/config.toml  (user-level)
  4. Defaults defined here

Explicit values passed to HarnessConfig(...) remain above ambient environment values.

Usage:
    from agnoclaw.config import get_config
    cfg = get_config()
    cfg.default_model  # "claude-sonnet-4-6"
"""

from __future__ import annotations

import tomllib
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class HeartbeatConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGNOCLAW_HB_")

    enabled: bool = False
    interval_minutes: int = 30
    """Heartbeat interval in minutes."""

    active_hours_start: str = "08:00"
    active_hours_end: str = "22:00"
    """Active hours in HH:MM format (local time). Outside these hours, heartbeat is skipped."""

    model: str = "claude-haiku-4-5-20251001"
    """Cheaper model for heartbeat runs to control costs."""

    ok_threshold_chars: int = 300
    """HEARTBEAT_OK responses under this length are silently suppressed."""

    target: str = "last"
    """'last' = send to last-active session. 'none' = internal only (no notification)."""


class StorageConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGNOCLAW_STORAGE_")

    backend: str = "sqlite"
    """'sqlite' or 'postgres'"""

    sqlite_path: str = "~/.agnoclaw/sessions.db"
    postgres_url: str | None = None
    """PostgreSQL connection URL. Required when backend='postgres'."""

    session_table: str = "agnoclaw_sessions"
    memory_table: str = "agnoclaw_memories"


class RuntimeProfile(StrEnum):
    """Runtime semantics selected independently from session continuity."""

    QUICK = "quick"
    DURABLE = "durable"
    SERVICE = "service"
    LEGACY = "legacy"


class HarnessConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGNOCLAW_",
        env_nested_delimiter="__",
    )

    _PROFILE_DEFAULTS: ClassVar[dict[RuntimeProfile, dict[str, Any]]] = {
        RuntimeProfile.QUICK: {
            "storage": {"backend": "sqlite", "sqlite_path": ":memory:"},
            "permission_mode": "default",
            "permission_require_approver": True,
            "permission_durable_approvals": True,
            "policy_fail_open": False,
            "event_sink_mode": "fail_closed",
            "enable_plugins": False,
            "enable_learning": False,
            "enable_session_context": False,
            "enable_compression": False,
            "enable_session_summary": False,
        },
        RuntimeProfile.DURABLE: {
            "storage": {"backend": "sqlite"},
            "permission_mode": "default",
            "permission_require_approver": True,
            "permission_durable_approvals": True,
            "policy_fail_open": False,
            "event_sink_mode": "fail_closed",
            "enable_plugins": False,
        },
        RuntimeProfile.SERVICE: {
            "storage": {"backend": "postgres"},
            "permission_mode": "default",
            "permission_require_approver": True,
            "permission_durable_approvals": True,
            "policy_fail_open": False,
            "event_sink_mode": "fail_closed",
            "enable_plugins": False,
        },
        RuntimeProfile.LEGACY: {},
    }

    @model_validator(mode="before")
    @classmethod
    def _apply_selected_profile_defaults(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        try:
            profile = RuntimeProfile(value.get("profile", RuntimeProfile.LEGACY))
        except ValueError:
            return value
        return _deep_merge(cls._PROFILE_DEFAULTS[profile], value)

    profile: RuntimeProfile = RuntimeProfile.LEGACY
    """Runtime guarantee bundle. The compatibility default remains ``legacy``
    during the 0.12 preview; explicit profiles receive fail-closed defaults."""

    sandbox_mode: Literal["workspace_write", "read_only", "full"] | None = None
    """Optional default sandbox posture. ``None`` preserves the selected backend's mode."""

    @classmethod
    def _profile_config(cls, profile: RuntimeProfile, overrides: dict[str, Any]) -> Self:
        requested = overrides.pop("profile", profile)
        if RuntimeProfile(requested) is not profile:
            raise ValueError(
                f"profile preset {profile.value!r} cannot be overridden with {requested!r}"
            )
        values = _deep_merge(cls._PROFILE_DEFAULTS[profile], overrides)
        values["profile"] = profile
        return cls(**values)

    @classmethod
    def quick(cls, **overrides: Any) -> Self:
        """Ephemeral, low-overhead execution with safe explicit-profile defaults."""
        return cls._profile_config(RuntimeProfile.QUICK, dict(overrides))

    @classmethod
    def durable(cls, **overrides: Any) -> Self:
        """SQLite-oriented resumable execution; stores are supplied to AgentHarness."""
        return cls._profile_config(RuntimeProfile.DURABLE, dict(overrides))

    @classmethod
    def service(cls, **overrides: Any) -> Self:
        """Multi-worker PostgreSQL execution with fail-closed service defaults."""
        return cls._profile_config(RuntimeProfile.SERVICE, dict(overrides))

    @classmethod
    def legacy(cls, **overrides: Any) -> Self:
        """Isolated compatibility preset for the pre-0.12 execution posture."""
        return cls._profile_config(RuntimeProfile.LEGACY, dict(overrides))

    @classmethod
    def local_safe(
        cls,
        *,
        profile: RuntimeProfile | Literal["quick", "durable"] = RuntimeProfile.QUICK,
        **overrides: Any,
    ) -> Self:
        """Strengthen a local quick/durable profile without creating a fourth profile."""
        resolved = RuntimeProfile(profile)
        if resolved not in {RuntimeProfile.QUICK, RuntimeProfile.DURABLE}:
            raise ValueError("local_safe supports only the quick and durable profiles")
        safe = {
            "sandbox_mode": "workspace_write",
            "permission_mode": "default",
            "permission_require_approver": True,
            "permission_durable_approvals": True,
            "policy_fail_open": False,
            "guardrails_enabled": True,
            "path_guardrails_enabled": True,
            "network_block_private_hosts": True,
            "network_block_in_bash": True,
            "enable_plugins": False,
        }
        return cls._profile_config(resolved, _deep_merge(safe, dict(overrides)))

    def with_profile(self, profile: RuntimeProfile | str) -> Self:
        """Apply one profile while retaining fields explicitly supplied by the host."""
        resolved = RuntimeProfile(profile)
        explicit = _explicit_settings_values(self)
        explicit.pop("profile", None)
        return type(self)._profile_config(resolved, explicit)

    # Model
    default_model: str = "claude-sonnet-4-6"
    """Default model ID. Can be any Agno-supported model."""

    default_provider: str = "anthropic"
    """Model provider: 'anthropic', 'openai', 'google', 'groq', 'ollama', 'litellm'"""

    cache_prompts: bool = False
    """Enable provider-side prompt caching where the provider needs an
    explicit lever (routed through agnoclaw.models.materialize_model).
    Providers that cache automatically (e.g. OpenAI) ignore this."""

    model_effort: str | None = None
    """Reasoning-effort hint (e.g. 'low'|'medium'|'high'). Applied only
    on providers/models that support it; silently dropped elsewhere."""

    # Workspace
    workspace_dir: str = "~/.agnoclaw/workspace"
    """Root workspace directory. Expanded at runtime."""

    # Session
    session_history_runs: int = 10
    """How many prior runs to inject into context."""

    runtime_max_concurrency: int = Field(default=16, ge=1, le=10_000)
    """Maximum simultaneously executing lifecycle runs per harness process.
    Same-session runs are additionally serialized by an exact session lane."""

    runtime_max_waiting: int = Field(default=1024, ge=1, le=100_000)
    """Maximum process-local lifecycle admissions waiting for a session lane or
    execution slot. Excess work settles with a typed retryable overload error."""

    runtime_max_waiting_per_tenant: int = Field(default=256, ge=1, le=100_000)
    """Maximum queued lifecycle admissions attributable to one exact tenant."""

    runtime_max_waiting_per_session: int = Field(default=32, ge=1, le=100_000)
    """Maximum queued lifecycle admissions attributable to one exact session lane."""

    runtime_admission_timeout_seconds: float | None = Field(default=30.0, gt=0, le=3600)
    """Maximum process-local wait to begin lifecycle execution. ``None`` disables
    the time bound while retaining ``runtime_max_waiting`` as a hard queue bound."""

    @model_validator(mode="after")
    def _validate_runtime_waiting_hierarchy(self) -> HarnessConfig:
        if self.runtime_max_waiting_per_tenant > self.runtime_max_waiting:
            raise ValueError("runtime_max_waiting_per_tenant cannot exceed runtime_max_waiting")
        if self.runtime_max_waiting_per_session > self.runtime_max_waiting_per_tenant:
            raise ValueError(
                "runtime_max_waiting_per_session cannot exceed "
                "runtime_max_waiting_per_tenant"
            )
        return self

    runtime_close_policy: Literal["drain", "detach", "cancel"] = "drain"
    """Default ownership decision for active lifecycle runs during close/aclose."""

    runtime_close_timeout_seconds: float | None = Field(default=None, gt=0)
    """Optional wait bound for drain/cancel. A timed-out close leaves the
    shutdown supervisor active so resources close only after runs settle."""

    runtime_operation_result_cache_size: int = Field(default=128, ge=0, le=10_000)
    """Maximum in-process results retained for exact operation replay. Durable
    cross-process result loading requires an ArtifactStore/result loader."""

    runtime_lease_seconds: int = Field(default=30, ge=3, le=86_400)
    """Store-issued run/session ownership lease duration for lifecycle workers."""

    runtime_lease_renew_interval_seconds: float = Field(default=10.0, gt=0, le=28_800)
    """Heartbeat interval for execution ownership. Must be shorter than the lease."""

    permission_durable_approvals: bool = True
    """Persist registered-capability approval waits, decisions, and exact grants."""

    permission_approval_ttl_seconds: int = Field(default=900, ge=1, le=604_800)
    """Maximum lifetime of one exact capability approval request."""

    permission_approval_poll_interval_seconds: float = Field(
        default=0.25,
        gt=0,
        le=5,
    )
    """Authoritative-store polling interval while a live run awaits approval."""

    # Tools
    enable_bash: bool = True
    enable_web_search: bool = True
    enable_web_fetch: bool = True
    bash_timeout_seconds: int = 120
    """Timeout for bash tool executions."""
    enable_background_bash_tools: bool = False
    """Enable bash_start/bash_output/bash_kill in default tool suite."""

    # Skills
    skills_dirs: list[str] = Field(default_factory=list)
    """Additional skill directories to load from."""

    # Learning (Agno LearningMachine)
    enable_learning: bool = False
    """Enable cross-session institutional learning (Agno LearningMachine).
    When enabled, the agent accumulates patterns and insights that persist
    across all sessions and users — forming institutional memory.
    Disable if data isolation between users is required."""

    learning_mode: str = "agentic"
    """Learning mode: 'always' | 'agentic' | 'propose' | 'hitl'.
    - always:  extract learnings after every run (automatic)
    - agentic: agent decides when to record learnings (default)
    - propose: learnings are proposed to the human for review
    - hitl:    human must approve each learning before it is stored
    """

    enable_session_context: bool = False
    """Enable Agno Session Context for durable goals, plans, progress, and blockers.
    This is independent from institutional learning and is opt-in because it adds
    extraction cost and can retain noisy transient state."""

    # Compression (context window management)
    enable_compression: bool = False
    """Enable tool result compression to keep context window manageable.
    When enabled, Agno's CompressionManager compresses tool outputs before
    each LLM API call. Recommended for long-running sessions or agents that
    generate many tool results."""

    compress_token_limit: int | None = None
    """Token limit that triggers compression. When the accumulated tool results
    exceed this limit, compression runs. None uses Agno's default count-based
    trigger (compress_tool_results_limit=3)."""

    max_context_tokens: int | None = Field(default=None, gt=0)
    """Explicit model-context budget used by deterministic harness accounting."""

    auto_compact_context: bool = False
    """Artifact-first compaction at the 90% budget boundary; opt-in until drift certification."""

    max_inline_output_chars: int | None = Field(default=None, ge=1024, le=1_000_000)
    """Spill larger governed capability results to the ArtifactStore before model reuse."""

    # Session summaries
    enable_session_summary: bool = False
    """Enable automatic session summaries at the end of each run.
    SessionSummaryManager generates a summary of the run and injects it
    into subsequent runs for continuity across sessions."""

    # Heartbeat
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)

    # Storage
    storage: StorageConfig = Field(default_factory=StorageConfig)

    # Browser
    enable_browser: bool = False
    """Enable Playwright-based browser toolkit. Requires agnoclaw[browser]."""

    # MCP
    mcp_servers: list[dict] = Field(default_factory=list)
    """MCP v2 servers: {name, command|url, transport?, env?, headers?}."""

    # Media
    enable_media_tools: bool = False
    """Enable media toolkit (PDF, image reading). Requires agnoclaw[media]."""

    # Notebook
    enable_notebook_tools: bool = False
    """Enable Jupyter notebook toolkit."""

    # Plugins
    enable_plugins: bool = True
    """Enable automatic plugin discovery via entry points."""

    plugin_paths: list[str] = Field(default_factory=list)
    """Explicit plugin module paths to load (e.g., 'my_package.plugin')."""

    # ClawHub
    clawhub_url: str = "https://clawhub.ai"
    """ClawHub skill registry API base URL."""

    clawhub_cache_dir: str = "~/.agnoclaw/cache/hub"
    """Local cache directory for ClawHub metadata."""

    # Hierarchical workspace
    global_workspace_dir: str = "~/.agnoclaw/global"
    """Global workspace directory (lowest priority in hierarchy)."""

    project_workspace_dir: str = ".agnoclaw"
    """Project-level workspace directory (middle priority in hierarchy)."""

    # TUI
    theme: str = "textual-dark"
    """Textual theme name for TUI mode. Options: textual-dark, textual-light, etc."""

    # Debug
    debug: bool = False
    show_tool_calls: bool = False

    # v0.2 runtime contracts
    event_sink_mode: str = "best_effort"
    """Event sink behavior: 'best_effort' (default) or 'fail_closed'."""

    result_ref_keys: list[str] = Field(default_factory=list)
    """Extra dict keys the consumer's tool results use for identity, merged on top of
    the generic built-ins (id, name, title, type, version, filename) when building the
    structured `result_ref` on tool.call.completed events. Use this to surface
    deployment-specific identifiers (e.g. artifact_title, document_title) without the
    harness hardcoding any one app's schema."""

    policy_fail_open: bool = False
    """If True, policy engine evaluation errors default to ALLOW with warning."""

    guardrails_enabled: bool = True
    """Master toggle for runtime guardrails."""

    path_guardrails_enabled: bool = True
    """Enforce path boundary checks on tool path arguments."""

    path_allowed_roots: list[str] = Field(default_factory=list)
    """Allowlisted root paths for tool path arguments. Empty defaults to workspace root."""

    path_blocked_roots: list[str] = Field(default_factory=list)
    """Explicitly blocked root paths for tool path arguments."""

    network_enabled: bool = True
    """Allow networked tool access."""

    network_enforce_https: bool = True
    """Require https:// URLs for URL-based tools."""

    network_allowed_hosts: list[str] = Field(default_factory=list)
    """Optional host allowlist for networked tools. Empty allows any host."""

    network_blocked_hosts: list[str] = Field(default_factory=list)
    """Explicit host denylist for networked tools."""

    network_block_private_hosts: bool = True
    """Block localhost/private/link-local hosts in networked tools."""

    network_block_in_bash: bool = True
    """Apply heuristic network-command blocking to bash tool calls."""

    permission_mode: str = "bypass"
    """Runtime permission mode: bypass | default | accept_edits | plan | dont_ask."""

    permission_require_approver: bool = False
    """If True, permission-gated tool calls deny when no approver is configured."""

    permission_preapproved_tools: list[str] = Field(default_factory=list)
    """Tool names pre-approved for permission checks."""

    permission_preapproved_categories: list[str] = Field(default_factory=list)
    """Permission categories pre-approved for permission checks."""


def _load_toml_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge nested dictionaries; override wins on conflicts."""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _explicit_settings_values(settings: BaseSettings) -> dict[str, Any]:
    """Return only values supplied by settings sources, including nested sources."""
    values: dict[str, Any] = {}
    for field_name in type(settings).model_fields:
        value = getattr(settings, field_name)
        if isinstance(value, BaseSettings):
            nested = _explicit_settings_values(value)
            if nested:
                values[field_name] = nested
        elif field_name in settings.model_fields_set:
            values[field_name] = value
    return values


@lru_cache(maxsize=1)
def get_config() -> HarnessConfig:
    """Load and cache the merged configuration."""
    # TOML files (project-level overrides user-level).
    user_toml = _load_toml_config(Path.home() / ".agnoclaw" / "config.toml")
    project_toml = _load_toml_config(Path.cwd() / ".agnoclaw.toml")

    # A source-only instance tells us exactly which root or nested fields came from
    # environment variables. Overlay only those fields so unrelated defaults cannot
    # erase TOML values.
    merged = _deep_merge(user_toml, project_toml)
    merged = _deep_merge(merged, _explicit_settings_values(HarnessConfig()))

    return HarnessConfig(**merged)
