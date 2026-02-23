"""
Configuration system for agnoclaw.

Settings are loaded in priority order (highest → lowest):
  1. Environment variables (AGNOCLAW_* prefix)
  2. ~/.agnoclaw/config.toml  (user-level)
  3. .agnoclaw.toml in cwd   (project-level)
  4. Defaults defined here

Usage:
    from agnoclaw.config import get_config
    cfg = get_config()
    cfg.default_model  # "claude-sonnet-4-5-20250929"
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Optional

from pydantic import Field
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
    postgres_url: Optional[str] = None
    """PostgreSQL connection URL. Required when backend='postgres'."""

    session_table: str = "agnoclaw_sessions"
    memory_table: str = "agnoclaw_memories"


class HarnessConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGNOCLAW_",
        env_nested_delimiter="__",
    )

    # Model
    default_model: str = "claude-sonnet-4-6"
    """Default model ID. Can be any Agno-supported model."""

    default_provider: str = "anthropic"
    """Model provider: 'anthropic', 'openai', 'google', 'groq', 'ollama', 'litellm'"""

    # Workspace
    workspace_dir: str = "~/.agnoclaw/workspace"
    """Root workspace directory. Expanded at runtime."""

    # Session
    session_history_runs: int = 10
    """How many prior runs to inject into context."""

    # Tools
    enable_bash: bool = True
    enable_web_search: bool = True
    enable_web_fetch: bool = True
    bash_timeout_seconds: int = 120
    """Timeout for bash tool executions."""

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

    # Compression (context window management)
    enable_compression: bool = False
    """Enable tool result compression to keep context window manageable.
    When enabled, Agno's CompressionManager compresses tool outputs before
    each LLM API call. Recommended for long-running sessions or agents that
    generate many tool results."""

    compress_token_limit: Optional[int] = None
    """Token limit that triggers compression. When the accumulated tool results
    exceed this limit, compression runs. None uses Agno's default count-based
    trigger (compress_tool_results_limit=3)."""

    # Session summaries
    enable_session_summary: bool = False
    """Enable automatic session summaries at the end of each run.
    SessionSummaryManager generates a summary of the run and injects it
    into subsequent runs for continuity across sessions."""

    # Heartbeat
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)

    # Storage
    storage: StorageConfig = Field(default_factory=StorageConfig)

    # Debug
    debug: bool = False
    show_tool_calls: bool = False


def _load_toml_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


@lru_cache(maxsize=1)
def get_config() -> HarnessConfig:
    """Load and cache the merged configuration."""
    # TOML files (project-level overrides user-level)
    user_toml = _load_toml_config(Path.home() / ".agnoclaw" / "config.toml")
    project_toml = _load_toml_config(Path.cwd() / ".agnoclaw.toml")

    # Merge: user → project → env vars (env wins)
    merged = {**user_toml, **project_toml}

    return HarnessConfig(**merged)
