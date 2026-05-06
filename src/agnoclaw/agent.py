"""
AgentHarness — the central class of agnoclaw.

Wraps Agno's Agent with:
  - Claude Code-inspired system prompt (layered, assembled at runtime)
  - Workspace awareness (AGENTS.md, SOUL.md, USER.md, MEMORY.md)
  - Skill injection (selective — one SKILL.md at a time)
  - Default tool suite (bash, files, web, tasks, subagent)
  - Persistent session storage (SQLite or Postgres)
  - Multi-provider model support (any Agno-supported model)
  - Unified memory: workspace files + LearningMachine (per-user + institutional)
  - Context management: compression, session summaries, history limiting

Memory layers:
  1. Workspace files (Markdown) — human-readable, per-workspace context
  2. LearningMachine — unified memory system handling both per-user facts
     (user_profile, user_memory) and institutional cross-user knowledge
     (learned_knowledge, entity_memory, decision_log)

Usage:
    from agnoclaw import AgentHarness

    # Basic — works out of the box with defaults
    agent = AgentHarness()
    agent.print_response("Find and fix the bug in src/auth.py")

    # Specify model as a single string (provider:model_id)
    agent = AgentHarness("anthropic:claude-sonnet-4-6")
    agent = AgentHarness("openai:gpt-4o")
    agent = AgentHarness("ollama:qwen3:8b")     # local, no API key
    agent = AgentHarness("groq:llama-3.3-70b-versatile")

    # With institutional learning
    agent = AgentHarness(enable_learning=True, learning_mode="agentic")

    # With per-user memory (via LearningMachine's user_profile + user_memory)
    agent = AgentHarness(
        user_id="alice",
        enable_user_memory=True,
        enable_learning=True,
        learning_namespace="code-review",
    )

    # Full context management for long-running sessions
    agent = AgentHarness(
        enable_compression=True,
        compress_token_limit=100_000,
        enable_session_summary=True,
        num_history_runs=5,
        max_tool_calls_from_history=10,
    )
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import shlex
import shutil
import tempfile
import warnings
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextvars import ContextVar
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from agno.agent import Agent
from agno.exceptions import AgentRunException
from agno.run.agent import RunOutput, RunOutputEvent
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from .backends import RuntimeBackend
from .config import HarnessConfig, get_config
from .prompts.system import SystemPromptBuilder
from .runtime import (
    AgnoAuthError,
    AgnoConfigError,
    AllowAllPolicyEngine,
    EventSink,
    EventSinkMode,
    ExecutionContext,
    HarnessError,
    NullEventSink,
    PermissionApprover,
    PermissionController,
    PermissionMode,
    PolicyAction,
    PolicyDecision,
    PolicyEngine,
    PostRunHook,
    PreRunHook,
    PromptEnvelope,
    RunInput,
    RunResultEnvelope,
    RuntimeGuardrails,
    SkillLoadRequest,
    ToolCallRequest,
    ToolCallResult,
    apply_redactions,
    build_event,
    normalize_permission_mode,
)
from .skills.backends import SkillInstallApprover
from .skills.registry import SkillRegistry
from .tools import get_default_tools
from .workspace import Workspace

logger = logging.getLogger("agnoclaw.agent")

_ERROR_MESSAGE_LIMIT = 500
_RESULT_PREVIEW_LIMIT = 240
_TRACE_METADATA_KEY = "_agnoclaw_trace"
_ASSISTANT_STREAM_EVENTS = frozenset({"RunContent"})
_TOOL_LIFECYCLE_EVENT_TYPES = frozenset(
    {"tool.call.started", "tool.call.completed", "tool.call.failed"}
)
_CURRENT_TOOL_RUNTIME: ContextVar[dict[str, Any] | None] = ContextVar(
    "agnoclaw_current_tool_runtime",
    default=None,
)

# Provider name aliases → Agno's canonical provider names
_PROVIDER_ALIASES: dict[str, str] = {
    "bedrock": "aws-bedrock",
    "aws": "aws-bedrock",
    "grok": "xai",
}

# Canonical provider names supported by this harness.
_KNOWN_PROVIDERS: set[str] = {
    "aimlapi",
    "anthropic",
    "aws-bedrock",
    "azure-ai-foundry",
    "azure-openai",
    "cerebras",
    "cohere",
    "cometapi",
    "dashscope",
    "deepinfra",
    "deepseek",
    "fireworks",
    "google",
    "groq",
    "huggingface",
    "ibm",
    "internlm",
    "langdb",
    "litellm",
    "llama-cpp",
    "lmstudio",
    "meta",
    "mistral",
    "moonshot",
    "nebius",
    "neosantara",
    "nexus",
    "nvidia",
    "ollama",
    "openai",
    "openrouter",
    "perplexity",
    "portkey",
    "requesty",
    "sambanova",
    "siliconflow",
    "together",
    "vercel",
    "vertexai-claude",
    "vllm",
    "xai",
}


def get_current_tool_runtime() -> dict[str, Any] | None:
    """Return the currently executing tool runtime context, if any."""
    runtime = _CURRENT_TOOL_RUNTIME.get()
    if runtime is None:
        return None
    return dict(runtime)


def _run_output_status_value(value: Any) -> str | None:
    status = getattr(value, "status", None)
    if status is None:
        return None
    raw = getattr(status, "value", status)
    if raw is None:
        return None
    text = str(raw).strip().lower()
    return text or None


def _run_output_is_error(value: Any) -> bool:
    return _run_output_status_value(value) == "error"


def _resolve_model(model: str | None, provider: str | None, config: HarnessConfig) -> str:
    """
    Return an Agno-compatible 'provider:model_id' string.

    Accepts:
      - "anthropic:claude-sonnet-4-6"  (combined, no provider arg needed)
      - "claude-sonnet-4-6" + provider="anthropic"
      - None → falls back to config defaults

    Provider aliases: "aws"/"bedrock" → "aws-bedrock", "grok" → "xai"
    """
    model_str = model or config.default_model
    prov = provider or config.default_provider

    # If the model string looks like "x:y", it may be either:
    #   1) provider:model_id (e.g. openai:gpt-4o), or
    #   2) a model_id that itself contains ":" (e.g. qwen3:0.6b for Ollama).
    if ":" in model_str:
        left, right = model_str.split(":", 1)
        normalized_left = _PROVIDER_ALIASES.get(left.lower(), left.lower())

        # Explicit provider prefix wins when recognized.
        if normalized_left in _KNOWN_PROVIDERS:
            return f"{normalized_left}:{right}"

        # Unknown prefix: treat entire model_str as model_id and use provider arg/default.
        p = _PROVIDER_ALIASES.get(prov.lower(), prov.lower())
        return f"{p}:{model_str}"

    # Separate model_id + provider
    p = prov.lower()
    p = _PROVIDER_ALIASES.get(p, p)
    return f"{p}:{model_str}"


def _make_db(config: HarnessConfig):
    """Instantiate storage backend from config."""
    if config.storage.backend == "postgres":
        if not config.storage.postgres_url:
            raise ValueError(
                "AGNOCLAW_STORAGE__POSTGRES_URL is required when storage backend is 'postgres'"
            )
        from agno.db.postgres import PostgresDb
        return PostgresDb(
            db_url=config.storage.postgres_url,
            session_table=config.storage.session_table,
            memory_table=config.storage.memory_table,
        )
    else:
        from agno.db.sqlite import SqliteDb
        db_path = Path(config.storage.sqlite_path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return SqliteDb(
            db_file=str(db_path),
            session_table=config.storage.session_table,
            memory_table=config.storage.memory_table,
        )


class AgentHarness:
    """
    A hackable, model-agnostic agent harness built on Agno.

    AgentHarness is the primary public interface. It wires together:
    - System prompt assembly (Claude Code-inspired, layered)
    - Workspace (persistent Markdown context files)
    - Skills (SKILL.md selective injection)
    - Default tools (bash, files, web, tasks, subagent)
    - Agno Agent (model invocation, tool calling, storage, streaming)

    Args:
        model: Model string. Accepts "provider:model_id" format
               (e.g. "anthropic:claude-sonnet-4-6", "ollama:qwen3:8b")
               or just "model_id" when provider is also given.
               Falls back to config default if not provided.
        session_id: Session ID for persistence. Auto-generated if not provided.
        user_id: User identifier for per-user memory.
        workspace_dir: Workspace path override. Defaults to ~/.agnoclaw/workspace.
        sandbox_dir: Session scratch path. Defaults to a temp dir per harness instance.
        include_default_tools: Whether to include the built-in default tool suite.
        tools: Additional tools to add alongside the defaults.
        instructions: Additional instructions appended to the system prompt.
        config: HarnessConfig override. Loaded from env/TOML if not provided.
        db: Optional prebuilt Agno DB backend to share across harnesses/teams.
        name: Agent name (cosmetic).
        agent_id: Stable agent ID (cosmetic, used in logs).
        debug: Enable debug mode (show tool calls, verbose output).
        subagents: Named subagent definitions for the spawn_subagent tool.
                   Dict mapping name → SubagentDefinition. Pre-registered agents
                   appear in the tool description for the model to invoke by name.
        enable_compression: Enable tool result compression for long sessions.
        compress_token_limit: Token threshold that triggers compression.
        enable_session_summary: Enable automatic session summaries for continuity.
        num_history_runs: Number of prior runs to include in context (default: from config).
        num_history_messages: Max history messages (alternative to num_history_runs).
        max_tool_calls_from_history: Keep only N most recent tool calls from history.
        max_context_tokens: Max context window tokens. When set, enables automatic
                           context budget monitoring with warnings at 85% and
                           auto-compaction at 95%.
        output_schema: Optional default structured-output schema for all runs.
        parser_model: Optional secondary model used to parse structured output.
        parse_response: Whether structured outputs should be parsed into objects.
        provider: Provider name — only needed when model is not in "provider:model_id"
                  format. Accepts "anthropic", "openai", "ollama", "groq", "google",
                  "aws"/"bedrock", "mistral", "xai"/"grok", "deepseek", "litellm".
        permission_mode: Runtime permission mode for tool calls:
                         "bypass", "default", "accept_edits", "plan", "dont_ask".
        permission_approver: Optional approval callback used in non-bypass modes.
        permission_require_approver: If True, deny approval-required calls when no approver exists.
        backend: Optional coherent runtime backend for tools/skills/browser.
        skill_install_approver: Optional approval backend for skill dependency installs.
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        provider: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        workspace_dir: str | Path | None = None,
        sandbox_dir: str | Path | None = None,
        include_default_tools: bool = True,
        tools: list | None = None,
        instructions: str | None = None,
        config: HarnessConfig | None = None,
        db=None,
        name: str = "agnoclaw",
        agent_id: str | None = None,
        debug: bool = False,
        # Subagents
        subagents: dict | None = None,
        # Memory options
        enable_user_memory: bool = False,
        enable_learning: bool | None = None,
        learning_mode: str | None = None,
        learning_namespace: str | None = None,
        # Context management
        enable_compression: bool | None = None,
        compress_token_limit: int | None = None,
        enable_session_summary: bool | None = None,
        num_history_runs: int | None = None,
        num_history_messages: int | None = None,
        max_tool_calls_from_history: int | None = None,
        max_context_tokens: int | None = None,
        # Structured output / response parsing
        output_schema: type | dict[str, Any] | None = None,
        parser_model: Any | None = None,
        parser_model_prompt: str | None = None,
        output_model: Any | None = None,
        output_model_prompt: str | None = None,
        parse_response: bool = True,
        structured_outputs: bool | None = None,
        use_json_mode: bool = False,
        # v0.2 runtime contracts
        event_sink: EventSink | None = None,
        event_sink_mode: str | None = None,
        policy_engine: PolicyEngine | None = None,
        policy_fail_open: bool | None = None,
        permission_mode: str | None = None,
        permission_approver: PermissionApprover | None = None,
        permission_require_approver: bool | None = None,
        permission_preapproved_tools: list[str] | tuple[str, ...] | None = None,
        permission_preapproved_categories: list[str] | tuple[str, ...] | None = None,
        backend: RuntimeBackend | None = None,
        skill_install_approver: SkillInstallApprover | None = None,
        pre_run_hooks: list[PreRunHook] | None = None,
        post_run_hooks: list[PostRunHook] | None = None,
        tenant_id: str | None = None,
        org_id: str | None = None,
        team_id: str | None = None,
        roles: list[str] | tuple[str, ...] | None = None,
        scopes: list[str] | tuple[str, ...] | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        context_metadata: dict[str, Any] | None = None,
        # Skills directories — list of (path, trust_level) tuples
        skills_dirs: list[tuple[str | Path, str]] | None = None,
        # Session lifecycle callbacks
        on_compaction: Callable[[str], Awaitable[None]] | None = None,
        on_session_end: Callable[..., Awaitable[None] | None] | None = None,
        # Event enrichment — merged into every HarnessEvent's metadata
        session_metadata: dict[str, Any] | None = None,
        # Legacy compat — use model + provider instead
        model_id: str | None = None,
        extra_tools: list | None = None,
        extra_instructions: str | None = None,
    ):
        self.config = config or get_config()
        self.name = name
        self.user_id = user_id
        self.session_id = session_id
        self._roles = tuple(roles or ())
        self._scopes = tuple(scopes or ())
        self._tenant_id = tenant_id
        self._org_id = org_id
        self._team_id = team_id
        self._request_id = request_id
        self._trace_id = trace_id
        self._context_metadata = dict(context_metadata or {})
        self._on_compaction = on_compaction
        self._on_session_end = on_session_end
        self._session_metadata = dict(session_metadata or {})
        self._closed = False

        # Runtime extension contracts
        self._event_sink: EventSink = event_sink or NullEventSink()
        mode_value = event_sink_mode or self.config.event_sink_mode
        try:
            self._event_sink_mode = EventSinkMode(mode_value)
        except ValueError:
            raise ValueError(
                f"Invalid event_sink_mode={mode_value!r}. "
                f"Use '{EventSinkMode.BEST_EFFORT.value}' or '{EventSinkMode.FAIL_CLOSED.value}'."
            ) from None
        self._policy_engine: PolicyEngine = policy_engine or AllowAllPolicyEngine()
        self._policy_fail_open = (
            policy_fail_open if policy_fail_open is not None else self.config.policy_fail_open
        )
        self._pre_run_hooks: list[PreRunHook] = list(pre_run_hooks or [])
        self._post_run_hooks: list[PostRunHook] = list(post_run_hooks or [])
        permission_mode_value = permission_mode or self.config.permission_mode
        require_approver = (
            permission_require_approver
            if permission_require_approver is not None
            else self.config.permission_require_approver
        )
        self._permission_controller = PermissionController(
            mode=permission_mode_value,
            approver=permission_approver,
            require_approver=require_approver,
            preapproved_tools=tuple(
                permission_preapproved_tools
                or self.config.permission_preapproved_tools
            ),
            preapproved_categories=tuple(
                permission_preapproved_categories
                or self.config.permission_preapproved_categories
            ),
        )
        self._plan_mode_restore_permission_mode: PermissionMode | None = None

        # Legacy compat: model_id / extra_tools / extra_instructions
        if model_id is not None:
            warnings.warn(
                "model_id is deprecated, use model instead",
                DeprecationWarning,
                stacklevel=2,
            )
        if extra_tools is not None:
            warnings.warn(
                "extra_tools is deprecated, use tools instead",
                DeprecationWarning,
                stacklevel=2,
            )
        if extra_instructions is not None:
            warnings.warn(
                "extra_instructions is deprecated, use instructions instead",
                DeprecationWarning,
                stacklevel=2,
            )
        _model = model or model_id
        _tools = tools or extra_tools
        _instructions = instructions or extra_instructions

        # Resolve model → Agno-native "provider:model_id" string
        self._model = _resolve_model(_model, provider, self.config)

        # Workspace (with hierarchical parent chain)
        _ws_dir = workspace_dir or self.config.workspace_dir
        self.workspace = Workspace(
            _ws_dir,
            global_dir=self.config.global_workspace_dir,
            project_dir=self.config.project_workspace_dir,
        )
        self.workspace.initialize()
        self.sandbox_dir = self._resolve_sandbox_dir(sandbox_dir, session_id=session_id)
        effective_backend = backend or RuntimeBackend()
        resolved_backend = effective_backend.resolve(workspace_dir=self.workspace.path)
        self._session_command_executor = resolved_backend.command_executor
        self._ensure_sandbox_dir()
        resolved_skill_runtime_backend = resolved_backend.skill_runtime
        self._guardrails = RuntimeGuardrails(
            workspace_dir=self.workspace.path,
            enabled=self.config.guardrails_enabled,
            path_enabled=self.config.path_guardrails_enabled,
            path_allowed_roots=self.config.path_allowed_roots,
            path_blocked_roots=self.config.path_blocked_roots,
            network_enabled=self.config.network_enabled,
            network_enforce_https=self.config.network_enforce_https,
            network_allowed_hosts=self.config.network_allowed_hosts,
            network_blocked_hosts=self.config.network_blocked_hosts,
            network_block_private_hosts=self.config.network_block_private_hosts,
            network_block_in_bash=self.config.network_block_in_bash,
        )

        # Skills registry
        self.skills = SkillRegistry(
            self.workspace.skills_dir(),
            runtime_backend=resolved_skill_runtime_backend,
            install_approver=skill_install_approver,
            working_dir=self.workspace.path,
        )
        if skills_dirs:
            for path, trust in skills_dirs:
                self.skills.add_directory(path, trust=trust)

        # System prompt builder
        self._prompt_builder = SystemPromptBuilder(
            self.workspace.path,
            sandbox_dir=self.sandbox_dir,
        )

        # Context budget monitoring
        self._max_context_tokens = max_context_tokens

        # Memory optimization: run Curator periodically to deduplicate/prune
        self._run_count = 0
        self._optimize_every_n_runs = 10  # trigger Curator every N runs

        # Build tool list (pass through named subagent definitions)
        _all_tools = []
        if include_default_tools:
            _all_tools = get_default_tools(
                self.config,
                subagents=subagents,
                workspace_dir=self.workspace.path,
                sandbox_dir=self.sandbox_dir,
                backend=effective_backend,
            )
        if _tools:
            _all_tools.extend(_tools)

        # ── Plugin system ────────────────────────────────────────────────
        self._plugin_loader = None
        if self.config.enable_plugins:
            from .plugins import PluginLoader
            self._plugin_loader = PluginLoader()
            manifests = self._plugin_loader.discover()
            # Also load explicitly configured plugin paths
            for path in self.config.plugin_paths:
                self._plugin_loader.load_from_path(path)

            # Merge plugin contributions
            _all_tools.extend(self._plugin_loader.get_all_tools())
            for skills_dir in self._plugin_loader.get_all_skills_dirs():
                self.skills.add_directory(skills_dir, trust="community")
            self._pre_run_hooks.extend(self._plugin_loader.get_all_pre_run_hooks())
            self._post_run_hooks.extend(self._plugin_loader.get_all_post_run_hooks())

            if manifests:
                logger.info(
                    "Loaded %d plugin(s): %s",
                    len(manifests),
                    ", ".join(m.name for m in manifests),
                )

        self._attach_tool_runtime_hooks(_all_tools)

        # Resolve learning flags before building system prompt
        _enable_learning = (
            enable_learning
            if enable_learning is not None
            else self.config.enable_learning
        )
        _learning_mode = learning_mode or self.config.learning_mode

        # Persist prompt options so per-run skill injection can be one-shot
        self._extra_instructions = _instructions
        self._include_learning = _enable_learning
        self._plan_mode = False

        # Assemble system prompt (skills are injected per-run, then reset)
        system_prompt = self._build_system_prompt(session_id=session_id)

        # Storage backend
        provided_db = db
        db = provided_db if provided_db is not None else _make_db(self.config)
        self._owns_storage = provided_db is None
        self._finalizer = weakref.finalize(
            self,
            AgentHarness._finalize_resources,
            db if self._owns_storage else None,
            str(self.sandbox_dir) if sandbox_dir is None else None,
        )

        # ── Memory: LearningMachine (unified per-user + institutional) ──────
        # LearningMachine handles both per-user memory (user_profile,
        # user_memory) and institutional knowledge (learned_knowledge,
        # entity_memory, decision_log) in a single system.
        learning = None
        if _enable_learning or enable_user_memory:
            from .memory import build_learning_machine
            learning = build_learning_machine(
                db=db,
                namespace=learning_namespace or name,
                mode=_learning_mode,
                enable_user_memory=enable_user_memory,
            )

        # ── Context management: compression ───────────────────────────────
        _enable_compression = (
            enable_compression if enable_compression is not None
            else self.config.enable_compression
        )
        _compress_token_limit = compress_token_limit or self.config.compress_token_limit
        compression_manager = None
        if _enable_compression:
            from agno.compression.manager import CompressionManager
            if _compress_token_limit:
                compression_manager = CompressionManager(
                    compress_token_limit=_compress_token_limit
                )
            else:
                compression_manager = CompressionManager()

        # ── Context management: session summaries ─────────────────────────
        _enable_session_summary = (
            enable_session_summary if enable_session_summary is not None
            else self.config.enable_session_summary
        )
        session_summary_manager = None
        if _enable_session_summary:
            from agno.session import SessionSummaryManager
            session_summary_manager = SessionSummaryManager()

        # Core Agno Agent — model accepted as "provider:model_id" string
        self._agent = Agent(
            model=self._model,
            name=name,
            id=agent_id,
            system_message=system_prompt,
            tools=_all_tools,
            db=db,
            session_id=session_id,
            user_id=user_id,
            add_history_to_context=True,
            num_history_runs=num_history_runs or self.config.session_history_runs,
            num_history_messages=num_history_messages,
            max_tool_calls_from_history=max_tool_calls_from_history,
            output_schema=output_schema,
            parser_model=parser_model,
            parser_model_prompt=parser_model_prompt,
            output_model=output_model,
            output_model_prompt=output_model_prompt,
            parse_response=parse_response,
            structured_outputs=structured_outputs,
            use_json_mode=use_json_mode,
            markdown=True,
            debug_mode=debug or self.config.debug,
            # Unified memory via LearningMachine (per-user + institutional)
            learning=learning,
            add_learnings_to_context=bool(learning),
            # Context window management
            compress_tool_results=_enable_compression,
            compression_manager=compression_manager,
            # Session continuity
            enable_session_summaries=_enable_session_summary,
            session_summary_manager=session_summary_manager,
            add_session_summary_to_context=_enable_session_summary,
        )
        # Per-run tool step tracking for progress events and duration metrics.
        self._tool_step_state: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _close_storage_resource(storage: Any) -> None:
        if storage is None:
            return

        session_factory = getattr(storage, "Session", None)
        remover = getattr(session_factory, "remove", None)
        if callable(remover):
            try:
                remover()
            except Exception:
                logger.debug("Failed to remove scoped storage sessions", exc_info=True)

        closer = getattr(storage, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                logger.debug("Failed to close storage backend", exc_info=True)

    @staticmethod
    def _finalize_resources(storage: Any, sandbox_dir: str | None) -> None:
        AgentHarness._close_storage_resource(storage)
        if sandbox_dir:
            shutil.rmtree(sandbox_dir, ignore_errors=True)

    def _resolve_sandbox_dir(
        self,
        sandbox_dir: str | Path | None,
        *,
        session_id: str | None,
    ) -> Path:
        if sandbox_dir is not None:
            return Path(sandbox_dir).expanduser().resolve(strict=False)
        prefix = f"agnoclaw-{session_id or uuid4().hex[:8]}-"
        return Path(tempfile.mkdtemp(prefix=prefix)).resolve(strict=False)

    def _ensure_sandbox_dir(self) -> None:
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._session_command_executor.run(
                command=f"mkdir -p {shlex.quote(str(self.sandbox_dir))}",
                workdir=None,
                timeout_seconds=30,
            )
        except Exception:
            logger.debug("Could not pre-create sandbox dir %s via runtime backend", self.sandbox_dir)

    def _list_created_sandbox_files(self) -> list[str]:
        command = (
            f"if [ -d {shlex.quote(str(self.sandbox_dir))} ]; then "
            f"find {shlex.quote(str(self.sandbox_dir))} -type f -print | sort; "
            f"fi"
        )
        try:
            result = self._session_command_executor.run(
                command=command,
                workdir=None,
                timeout_seconds=30,
            )
            if result.exit_code == 0 and result.stdout.strip():
                return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except Exception:
            logger.debug("Could not enumerate sandbox files via runtime backend", exc_info=True)

        if not self.sandbox_dir.exists():
            return []
        return sorted(
            str(path)
            for path in self.sandbox_dir.rglob("*")
            if path.is_file()
        )

    def _cleanup_sandbox_dir(self) -> None:
        command = f"rm -rf {shlex.quote(str(self.sandbox_dir))}"
        try:
            self._session_command_executor.run(
                command=command,
                workdir=None,
                timeout_seconds=30,
            )
        except Exception:
            logger.debug("Could not remove sandbox dir %s via runtime backend", self.sandbox_dir)
        shutil.rmtree(self.sandbox_dir, ignore_errors=True)

    async def _maybe_await(self, result: Any) -> None:
        if inspect.isawaitable(result):
            await result

    async def _emit_session_end_callback(
        self,
        summary: str,
        *,
        created_files: list[str] | None,
    ) -> None:
        callback = self._on_session_end
        if callback is None:
            return

        signature = inspect.signature(callback)
        params = list(signature.parameters.values())
        accepts_kwargs = any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params)
        accepts_varargs = any(param.kind is inspect.Parameter.VAR_POSITIONAL for param in params)
        positional_params = [
            param
            for param in params
            if param.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        has_created_files_param = "created_files" in signature.parameters

        if has_created_files_param or accepts_kwargs:
            await self._maybe_await(callback(summary, created_files=created_files))
            return
        if accepts_varargs or len(positional_params) >= 2:
            await self._maybe_await(callback(summary, created_files))
            return
        await self._maybe_await(callback(summary))

    def _build_system_prompt(
        self,
        *,
        skill_content: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Build the canonical system prompt for this harness instance."""
        include_learning = self._include_learning and not self._plan_mode

        # Compose extra context: user instructions + skill catalog for auto-selection
        extra_parts = []
        if self._extra_instructions:
            extra_parts.append(self._extra_instructions)

        # Inject available skill descriptions so the model can auto-select skills.
        # This mirrors Claude Code's skill awareness: the model sees all available
        # skills and can request activation of the most relevant one.
        if not skill_content:
            skill_descriptions = self.skills.get_skill_descriptions()
            if skill_descriptions:
                extra_parts.append(skill_descriptions)

        extra_context = "\n\n".join(extra_parts) if extra_parts else None

        return self._prompt_builder.build(
            skill_content=skill_content,
            extra_context=extra_context,
            include_learning=include_learning,
            include_plan_mode=self._plan_mode,
            session_id=session_id if session_id is not None else self.session_id,
        )

    def _set_system_prompt(
        self,
        *,
        skill_content: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Update the underlying agent's system prompt."""
        self._agent.system_message = self._build_system_prompt(
            skill_content=skill_content,
            session_id=session_id,
        )

    def _dispatch_command_tool(self, tool_name: str, arguments: str) -> str:
        """
        Invoke a registered tool directly, bypassing the LLM.

        Used for skills with command-dispatch: tool.
        """
        # Search registered tools for the target
        for tool in self._agent.tools or []:
            if isinstance(tool, Toolkit):
                for fname, func in tool.functions.items():
                    if fname == tool_name:
                        try:
                            return str(func.entrypoint(arguments))
                        except TypeError:
                            return str(func.entrypoint())
            elif isinstance(tool, Function):
                if tool.name == tool_name:
                    try:
                        return str(tool.entrypoint(arguments))
                    except TypeError:
                        return str(tool.entrypoint())
            elif callable(tool) and getattr(tool, "__name__", "") == tool_name:
                try:
                    return str(tool(arguments))
                except TypeError:
                    return str(tool())

        return f"[error] Command-dispatch tool '{tool_name}' not found in registered tools."

    def set_event_sink(self, sink: EventSink, mode: str | None = None) -> None:
        """Swap event sink at runtime."""
        self._event_sink = sink
        if mode is not None:
            self._event_sink_mode = EventSinkMode(mode)

    def set_policy_engine(self, engine: PolicyEngine) -> None:
        """Swap policy engine at runtime."""
        self._policy_engine = engine

    def set_permission_mode(self, mode: str) -> None:
        """Set runtime permission mode for tool calls."""
        self._permission_controller.set_mode(mode)

    @property
    def permission_mode(self) -> str:
        """Current runtime permission mode."""
        return self._permission_controller.current_mode().value

    def add_pre_run_hook(self, hook: PreRunHook) -> None:
        """Register a pre-run hook."""
        self._pre_run_hooks.append(hook)

    def add_post_run_hook(self, hook: PostRunHook) -> None:
        """Register a post-run hook."""
        self._post_run_hooks.append(hook)

    def _attach_tool_runtime_hooks(self, tools: list[Any]) -> None:
        for tool in tools:
            if isinstance(tool, Function):
                self._attach_function_runtime_hooks(tool)
            elif isinstance(tool, Toolkit):
                for function in tool.functions.values():
                    self._attach_function_runtime_hooks(function)

    def _attach_function_runtime_hooks(self, function: Function) -> None:
        pre_wrapped = getattr(function.pre_hook, "_agnoclaw_runtime_pre", False)
        post_wrapped = getattr(function.post_hook, "_agnoclaw_runtime_post", False)
        if pre_wrapped and post_wrapped:
            return

        original_pre_hook = function.pre_hook
        original_post_hook = function.post_hook

        def runtime_pre_hook(agent=None, team=None, run_context=None, fc=None):
            if original_pre_hook is not None:
                try:
                    self._invoke_original_tool_hook(
                        original_pre_hook,
                        agent=agent,
                        team=team,
                        run_context=run_context,
                        fc=fc,
                    )
                except HarnessError as exc:
                    self._raise_agent_run_exception(exc)
            self._handle_tool_pre_hook(fc=fc, run_context=run_context)
            runtime = getattr(fc, "_agnoclaw_tool_runtime", None)
            if isinstance(runtime, dict):
                self._set_active_tool_runtime(fc, runtime)

        def runtime_post_hook(agent=None, team=None, run_context=None, fc=None):
            try:
                if original_post_hook is not None:
                    try:
                        self._invoke_original_tool_hook(
                            original_post_hook,
                            agent=agent,
                            team=team,
                            run_context=run_context,
                            fc=fc,
                        )
                    except HarnessError as exc:
                        self._raise_agent_run_exception(exc)
                self._handle_tool_post_hook(fc=fc, run_context=run_context)
            finally:
                self._clear_active_tool_runtime(fc)

        runtime_pre_hook._agnoclaw_runtime_pre = True  # type: ignore[attr-defined]
        runtime_post_hook._agnoclaw_runtime_post = True  # type: ignore[attr-defined]
        function.pre_hook = runtime_pre_hook
        function.post_hook = runtime_post_hook

    def _invoke_original_tool_hook(
        self,
        hook,
        *,
        agent=None,
        team=None,
        run_context=None,
        fc=None,
    ) -> None:
        signature = inspect.signature(hook).parameters
        kwargs: dict[str, Any] = {}
        if "agent" in signature:
            kwargs["agent"] = agent
        if "team" in signature:
            kwargs["team"] = team
        if "run_context" in signature:
            kwargs["run_context"] = run_context
        if "fc" in signature:
            kwargs["fc"] = fc
        result = hook(**kwargs)
        self._resolve_sync_value(
            result,
            operation=f"tool_hook:{getattr(hook, '__name__', hook.__class__.__name__)}",
        )

    @staticmethod
    def _apply_redactions_to_object(value: Any, redactions) -> Any:
        if not redactions:
            return value
        if isinstance(value, str):
            return apply_redactions(value, redactions)
        if isinstance(value, list):
            return [AgentHarness._apply_redactions_to_object(item, redactions) for item in value]
        if isinstance(value, tuple):
            return tuple(AgentHarness._apply_redactions_to_object(item, redactions) for item in value)
        if isinstance(value, dict):
            return {
                key: AgentHarness._apply_redactions_to_object(item, redactions)
                for key, item in value.items()
            }
        return value

    @staticmethod
    def _set_active_tool_runtime(fc, runtime: dict[str, Any]) -> None:
        token = _CURRENT_TOOL_RUNTIME.set(dict(runtime))
        if fc is not None:
            fc._agnoclaw_tool_runtime_token = token

    @staticmethod
    def _clear_active_tool_runtime(fc) -> None:
        if fc is None:
            return
        token = getattr(fc, "_agnoclaw_tool_runtime_token", None)
        if token is None:
            return
        _CURRENT_TOOL_RUNTIME.reset(token)
        delattr(fc, "_agnoclaw_tool_runtime_token")

    @staticmethod
    def _context_to_metadata(context: ExecutionContext) -> dict[str, Any]:
        return {
            "tenant_id": context.tenant_id,
            "org_id": context.org_id,
            "team_id": context.team_id,
            "workspace_id": context.workspace_id,
            "session_id": context.session_id,
            "user_id": context.user_id,
            "request_id": context.request_id,
            "trace_id": context.trace_id,
            "roles": list(context.roles),
            "scopes": list(context.scopes),
            "metadata": dict(context.metadata),
        }

    def _build_agent_run_metadata(
        self,
        *,
        context: ExecutionContext,
        run_input: RunInput,
    ) -> dict[str, Any]:
        payload = dict(run_input.metadata)
        payload["_agnoclaw_context"] = self._context_to_metadata(context)
        return payload

    @staticmethod
    def _trace_payload_from_context(context: ExecutionContext) -> dict[str, Any]:
        metadata = getattr(context, "metadata", None) or {}
        trace = metadata.get(_TRACE_METADATA_KEY)
        if not isinstance(trace, dict):
            return {}
        payload: dict[str, Any] = {}
        for key in (
            "parent_run_id",
            "parent_tool_call_id",
            "parent_step_id",
            "parent_tool_name",
            "subagent_depth",
            "subagent_root_run_id",
        ):
            value = trace.get(key)
            if value is not None:
                payload[key] = value
        return payload

    @staticmethod
    def _build_subagent_execution_context(
        runtime: dict[str, Any] | None,
        *,
        workspace_id: str | None,
    ) -> ExecutionContext | None:
        if not runtime:
            return None

        parent_context = runtime.get("context")
        if not isinstance(parent_context, ExecutionContext):
            return None

        metadata = dict(parent_context.metadata)
        existing_trace = metadata.get(_TRACE_METADATA_KEY)
        if not isinstance(existing_trace, dict):
            existing_trace = {}

        root_run_id = (
            existing_trace.get("subagent_root_run_id")
            or existing_trace.get("parent_run_id")
            or runtime.get("parent_run_id")
        )
        trace = {
            "parent_run_id": runtime.get("parent_run_id"),
            "parent_tool_call_id": runtime.get("parent_tool_call_id"),
            "parent_step_id": runtime.get("parent_step_id"),
            "parent_tool_name": runtime.get("parent_tool_name"),
            "subagent_depth": int(existing_trace.get("subagent_depth") or 0) + 1,
            "subagent_root_run_id": root_run_id,
        }
        metadata[_TRACE_METADATA_KEY] = {
            key: value for key, value in trace.items() if value is not None
        }

        return ExecutionContext.create(
            user_id=parent_context.user_id,
            session_id=None,
            workspace_id=workspace_id or parent_context.workspace_id,
            tenant_id=parent_context.tenant_id,
            org_id=parent_context.org_id,
            team_id=parent_context.team_id,
            roles=parent_context.roles,
            scopes=parent_context.scopes,
            request_id=parent_context.request_id,
            trace_id=parent_context.trace_id,
            metadata=metadata,
        )

    def _context_from_run_context(self, run_context) -> ExecutionContext:
        payload = {}
        if run_context is not None and isinstance(getattr(run_context, "metadata", None), dict):
            payload = dict(run_context.metadata or {})

        raw_context = payload.get("_agnoclaw_context")
        if isinstance(raw_context, dict):
            return ExecutionContext.create(
                user_id=raw_context.get("user_id"),
                session_id=raw_context.get("session_id"),
                workspace_id=raw_context.get("workspace_id") or str(self.workspace.path),
                tenant_id=raw_context.get("tenant_id"),
                org_id=raw_context.get("org_id"),
                team_id=raw_context.get("team_id"),
                roles=raw_context.get("roles") or (),
                scopes=raw_context.get("scopes") or (),
                request_id=raw_context.get("request_id"),
                trace_id=raw_context.get("trace_id"),
                metadata=raw_context.get("metadata") or {},
            )

        return ExecutionContext.create(
            user_id=getattr(run_context, "user_id", None),
            session_id=getattr(run_context, "session_id", None),
            workspace_id=str(self.workspace.path),
            metadata=payload,
        )

    def _run_id_from_tool_hook(self, *, run_context, fc) -> str:
        if run_context is not None:
            run_id = getattr(run_context, "run_id", None)
            if isinstance(run_id, str) and run_id:
                return run_id
        if fc is not None:
            call_id = getattr(fc, "call_id", None)
            if isinstance(call_id, str) and call_id:
                return f"run_from_{call_id}"
        return f"run_{uuid4().hex}"

    @staticmethod
    def _tool_call_id(fc) -> str | None:
        call_id = getattr(fc, "call_id", None)
        if isinstance(call_id, str) and call_id:
            return call_id
        return None

    @staticmethod
    def _truncate_text(value: str, *, limit: int) -> str:
        if len(value) <= limit:
            return value
        return f"{value[: limit - 1].rstrip()}…"

    @staticmethod
    def _normalize_error_message(value: Any) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            text = text[1:-1].strip()
        return text

    @staticmethod
    def _status_is_error(value: Any) -> bool:
        return _run_output_is_error(value)

    @staticmethod
    def _extract_error_signal_from_run_output(run_output: Any) -> dict[str, Any]:
        status = _run_output_status_value(run_output)
        raw_message = getattr(run_output, "content", None)
        message = AgentHarness._normalize_error_message(raw_message)
        signal: dict[str, Any] = {
            "status": status,
            "message": message,
            "error_type": None,
            "error_id": None,
            "additional_data": {},
            "source": "run_output",
        }

        events = getattr(run_output, "events", None)
        if isinstance(events, list):
            for event in reversed(events):
                event_name = str(getattr(event, "event", "")).lower()
                if event_name not in {"runerror", "run_error"}:
                    continue
                signal["error_type"] = getattr(event, "error_type", None)
                signal["error_id"] = getattr(event, "error_id", None)
                additional = getattr(event, "additional_data", None)
                if isinstance(additional, dict):
                    signal["additional_data"] = dict(additional)
                event_content = getattr(event, "content", None)
                if event_content:
                    signal["message"] = AgentHarness._normalize_error_message(event_content)
                break

        return signal

    @staticmethod
    def _extract_error_signal_from_stream_event(event: Any) -> dict[str, Any] | None:
        event_name = str(getattr(event, "event", "")).lower()
        if event_name not in {"runerror", "run_error"}:
            return None
        content = AgentHarness._normalize_error_message(getattr(event, "content", ""))
        additional = getattr(event, "additional_data", None)
        return {
            "status": "error",
            "message": content,
            "error_type": getattr(event, "error_type", None),
            "error_id": getattr(event, "error_id", None),
            "additional_data": dict(additional) if isinstance(additional, dict) else {},
            "source": "stream_event",
        }

    @staticmethod
    def _classify_error_signal(signal: dict[str, Any]) -> str:
        error_type = str(signal.get("error_type") or "").lower()
        error_id = str(signal.get("error_id") or "").lower()
        message = str(signal.get("message") or "").lower()

        auth_markers = (
            "model_authentication_error",
            "authentication",
            "auth token",
            "auth_token",
            "api key",
            "api_key",
            "unauthorized",
            "invalid_api_key",
            "could not resolve authentication method",
            "anthropic_api_key",
            "openai_api_key",
            "access token",
        )
        if any(marker in error_type for marker in auth_markers) or any(
            marker in error_id for marker in auth_markers
        ):
            return "auth"
        if any(marker in message for marker in auth_markers):
            return "auth"

        config_markers = (
            "invalid model",
            "model not found",
            "does not support",
            "unknown model",
            "unknown provider",
            "not configured",
            "must be set",
            "missing required",
            "unsupported",
            "configuration",
        )
        if any(marker in message for marker in config_markers):
            return "config"

        recoverable_markers = (
            "rate limit",
            "429",
            "timeout",
            "timed out",
            "temporarily unavailable",
            "connection error",
            "network error",
            "try again",
            "overloaded",
            "retry",
        )
        if any(marker in message for marker in recoverable_markers):
            return "recoverable"

        return "unknown"

    def _raise_if_fatal_error_signal(self, signal: dict[str, Any]) -> None:
        if str(signal.get("status") or "").lower() != "error":
            return
        category = self._classify_error_signal(signal)
        message = self._normalize_error_message(signal.get("message"))
        if not message:
            message = "Model invocation failed."
        message = self._truncate_text(message, limit=_ERROR_MESSAGE_LIMIT)
        details = {
            "error_type": signal.get("error_type"),
            "error_id": signal.get("error_id"),
            "source": signal.get("source"),
            "additional_data": signal.get("additional_data") or {},
        }
        if category == "auth":
            raise AgnoAuthError(message, details=details)
        if category == "config":
            raise AgnoConfigError(message, details=details)

    def _raise_stream_error_signal(self, signal: dict[str, Any]) -> None:
        """Raise a typed error for stream failures after run.failed emission."""
        self._raise_if_fatal_error_signal(signal)
        category = self._classify_error_signal(signal)
        message = self._normalize_error_message(signal.get("message"))
        if not message:
            message = "Model invocation failed."
        message = self._truncate_text(message, limit=_ERROR_MESSAGE_LIMIT)
        details = {
            "error_type": signal.get("error_type"),
            "error_id": signal.get("error_id"),
            "source": signal.get("source"),
            "additional_data": signal.get("additional_data") or {},
        }
        raise HarnessError(
            code="MODEL_STREAM_FAILED",
            category="model",
            message=message,
            retryable=(category == "recoverable"),
            details=details,
        )

    @staticmethod
    def _format_result_preview(value: Any) -> str:
        if value is None:
            return ""
        text = " ".join(str(value).split())
        return AgentHarness._truncate_text(text, limit=_RESULT_PREVIEW_LIMIT)

    def _start_tool_step(
        self,
        *,
        run_id: str,
        tool_name: str,
        tool_call_id: str | None,
        context: ExecutionContext,
    ) -> dict[str, Any]:
        state = self._tool_step_state.setdefault(run_id, {"next_index": 1, "active": {}})
        step_index = int(state.get("next_index", 1))
        state["next_index"] = step_index + 1

        if tool_call_id:
            step_id = tool_call_id
        else:
            step_id = f"{run_id}:step:{step_index}"
        step_name = f"Running {tool_name}"

        step_data = {
            "step_id": step_id,
            "step_name": step_name,
            "step_index": step_index,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "started_at": monotonic(),
        }
        state["active"][step_id] = step_data
        self._emit_event_sync(
            event_type="step_started",
            run_id=run_id,
            context=context,
            payload={
                "step_id": step_id,
                "step_name": step_name,
                "step_index": step_index,
                "total_steps": None,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
            },
        )
        return step_data

    def _finish_tool_step(
        self,
        *,
        run_id: str,
        tool_name: str,
        tool_call_id: str | None,
        context: ExecutionContext,
    ) -> tuple[dict[str, Any], int]:
        state = self._tool_step_state.get(run_id, {})
        active = state.get("active", {})
        step_data: dict[str, Any] | None = None

        if isinstance(active, dict):
            if tool_call_id and tool_call_id in active:
                step_data = active.pop(tool_call_id, None)
            if step_data is None and active:
                # Fallback for missing call IDs: finish earliest active step.
                first_key = next(iter(active.keys()))
                step_data = active.pop(first_key)

        if step_data is None:
            state = self._tool_step_state.setdefault(run_id, {"next_index": 1, "active": {}})
            step_index = int(state.get("next_index", 1))
            state["next_index"] = step_index + 1
            step_data = {
                "step_id": tool_call_id or f"{run_id}:step:{step_index}",
                "step_name": f"Running {tool_name}",
                "step_index": step_index,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "started_at": monotonic(),
            }

        started_at = float(step_data.get("started_at") or monotonic())
        duration_ms = max(0, int((monotonic() - started_at) * 1000))
        self._emit_event_sync(
            event_type="step_completed",
            run_id=run_id,
            context=context,
            payload={
                "step_id": step_data.get("step_id"),
                "step_name": step_data.get("step_name"),
                "step_index": step_data.get("step_index"),
                "total_steps": None,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "duration_ms": duration_ms,
            },
        )
        return step_data, duration_ms

    def _cleanup_tool_step_state(self, run_id: str) -> None:
        self._tool_step_state.pop(run_id, None)

    def _raise_agent_run_exception(self, error: HarnessError) -> None:
        raise AgentRunException(error, user_message=error.message)

    @staticmethod
    def _extract_harness_error(exc: Exception) -> HarnessError | None:
        if isinstance(exc, HarnessError):
            return exc
        if isinstance(exc, AgentRunException) and exc.args:
            inner = exc.args[0]
            if isinstance(inner, HarnessError):
                return inner
        return None

    def _handle_tool_pre_hook(self, *, fc, run_context) -> None:
        if fc is None or getattr(fc, "function", None) is None:
            return

        try:
            run_id = self._run_id_from_tool_hook(run_context=run_context, fc=fc)
            context = self._context_from_run_context(run_context)
            tool_name = getattr(fc.function, "name", "unknown_tool")
            tool_call_id = self._tool_call_id(fc)
            arguments = dict(getattr(fc, "arguments", None) or {})
            request = ToolCallRequest(
                run_id=run_id,
                tool_name=tool_name,
                arguments=arguments,
                metadata={"tool_call_id": tool_call_id},
            )

            violations = self._guardrails.check(request)
            if violations:
                for violation in violations:
                    self._emit_event_sync(
                        event_type="guardrail.violation",
                        run_id=run_id,
                        context=context,
                        payload={
                            "tool_name": tool_name,
                            "code": violation.code,
                            "message": violation.message,
                            "details": violation.details,
                        },
                    )
                raise HarnessError(
                    code="GUARDRAIL_DENIED",
                    category="guardrail",
                    message=f"Guardrail denied tool call: {tool_name}",
                    retryable=False,
                    details={
                        "tool_name": tool_name,
                        "violations": [
                            {
                                "code": violation.code,
                                "message": violation.message,
                                "details": violation.details,
                            }
                            for violation in violations
                        ],
                    },
                )

            permission_decision = self._permission_controller.check_tool_call(
                request,
                context,
                resolve_sync_value=self._resolve_sync_value,
            )
            self._enforce_policy_decision(
                decision=permission_decision,
                checkpoint="permission.before_tool_call",
                run_id=run_id,
                context=context,
            )

            decision = self._run_policy_sync(
                method_name="before_tool_call",
                payload=request,
                run_input=None,
                context=context,
            )
            self._enforce_policy_decision(
                decision=decision,
                checkpoint="before_tool_call",
                run_id=run_id,
                context=context,
            )

            if (
                decision.action == PolicyAction.ALLOW_WITH_REDACTION
                and getattr(fc, "arguments", None)
            ):
                fc.arguments = self._apply_redactions_to_object(
                    dict(fc.arguments),
                    decision.redactions,
                )

            emitted_arguments = self._normalize_tool_arguments(getattr(fc, "arguments", None))
            step = self._start_tool_step(
                run_id=run_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                context=context,
            )
            self._emit_event_sync(
                event_type="tool.call.started",
                run_id=run_id,
                context=context,
                payload={
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "step_id": step["step_id"],
                    "step_name": step["step_name"],
                    "step_index": step["step_index"],
                    "argument_keys": sorted(emitted_arguments.keys()),
                    "arguments": emitted_arguments,
                },
            )

            fc._agnoclaw_tool_runtime = {
                "context": context,
                "event_sink": self._event_sink,
                "event_sink_mode": self._event_sink_mode.value,
                "session_metadata": dict(self._session_metadata),
                "parent_run_id": run_id,
                "parent_tool_name": tool_name,
                "parent_tool_call_id": tool_call_id,
                "parent_step_id": step["step_id"],
            }
        except HarnessError as exc:
            self._raise_agent_run_exception(exc)

    def _handle_tool_post_hook(self, *, fc, run_context) -> None:
        if fc is None or getattr(fc, "function", None) is None:
            return

        try:
            run_id = self._run_id_from_tool_hook(run_context=run_context, fc=fc)
            context = self._context_from_run_context(run_context)
            tool_name = getattr(fc.function, "name", "unknown_tool")
            tool_call_id = self._tool_call_id(fc)
            result = ToolCallResult(
                run_id=run_id,
                tool_name=tool_name,
                arguments=dict(getattr(fc, "arguments", None) or {}),
                output=getattr(fc, "result", None),
                error=getattr(fc, "error", None),
                metadata={"tool_call_id": tool_call_id},
            )

            decision = self._run_policy_sync(
                method_name="after_tool_call",
                payload=result,
                run_input=None,
                context=context,
            )
            self._enforce_policy_decision(
                decision=decision,
                checkpoint="after_tool_call",
                run_id=run_id,
                context=context,
            )

            if decision.action == PolicyAction.ALLOW_WITH_REDACTION and hasattr(fc, "result"):
                fc.result = self._apply_redactions_to_object(fc.result, decision.redactions)

            step, duration_ms = self._finish_tool_step(
                run_id=run_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                context=context,
            )
            result_preview = self._format_result_preview(getattr(fc, "result", None))
            event_type = "tool.call.failed" if getattr(fc, "error", None) else "tool.call.completed"
            payload = {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "step_id": step.get("step_id"),
                "step_name": step.get("step_name"),
                "step_index": step.get("step_index"),
                "argument_keys": sorted(result.arguments.keys()),
                "arguments": self._normalize_tool_arguments(result.arguments),
                "error": getattr(fc, "error", None),
                "duration_ms": duration_ms,
                "result_preview": result_preview,
                "result_chars": (
                    len(str(getattr(fc, "result", "")))
                    if getattr(fc, "result", None) is not None
                    else 0
                ),
            }
            self._emit_event_sync(
                event_type=event_type,
                run_id=run_id,
                context=context,
                payload=payload,
            )
        except HarnessError as exc:
            self._raise_agent_run_exception(exc)

    def _active_session_id(self, override: str | None) -> str | None:
        if override is not None:
            return override
        return self.session_id or getattr(self._agent, "session_id", None)

    def _build_execution_context(
        self,
        *,
        user_id: str | None,
        session_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> ExecutionContext:
        merged_metadata = dict(self._context_metadata)
        merged_metadata.setdefault("permission_mode", self.permission_mode)
        if metadata:
            merged_metadata.update(metadata)
        return ExecutionContext.create(
            user_id=user_id,
            session_id=session_id,
            workspace_id=str(self.workspace.path),
            tenant_id=self._tenant_id,
            org_id=self._org_id,
            team_id=self._team_id,
            roles=self._roles,
            scopes=self._scopes,
            request_id=self._request_id,
            trace_id=self._trace_id,
            metadata=merged_metadata,
        )

    def _emit_event_sync(
        self,
        *,
        event_type: str,
        run_id: str,
        context: ExecutionContext,
        payload: dict[str, Any] | None = None,
    ) -> None:
        merged_payload = {
            **self._session_metadata,
            **self._trace_payload_from_context(context),
            **(payload or {}),
        }
        event = build_event(
            event_type=event_type,
            run_id=run_id,
            context=context,
            payload=merged_payload,
        )
        try:
            maybe_awaitable = self._event_sink.emit(event)
            if inspect.isawaitable(maybe_awaitable):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(maybe_awaitable)
                else:
                    if self._event_sink_mode == EventSinkMode.FAIL_CLOSED:
                        raise HarnessError(
                            code="EVENT_SINK_ASYNC_IN_SYNC",
                            category="event",
                            message=(
                                "Event sink returned an awaitable during sync run while fail-closed mode is active."
                            ),
                            retryable=False,
                            details={"event_type": event_type},
                        )
                    task = loop.create_task(maybe_awaitable)
                    task.add_done_callback(self._build_event_task_callback(event_type))
        except Exception as exc:
            if self._event_sink_mode == EventSinkMode.FAIL_CLOSED:
                raise HarnessError(
                    code="EVENT_SINK_FAILED",
                    category="event",
                    message=f"Failed to emit event '{event_type}': {exc}",
                    retryable=True,
                    details={"event_type": event_type},
                ) from exc
            logger.warning("Event sink failure for %s: %s", event_type, exc)

    def _build_event_task_callback(self, event_type: str):
        def _callback(task: asyncio.Task) -> None:
            try:
                task.result()
            except Exception as exc:  # pragma: no cover - loop callback path
                logger.warning("Async event sink failure for %s: %s", event_type, exc)

        return _callback

    async def _emit_event_async(
        self,
        *,
        event_type: str,
        run_id: str,
        context: ExecutionContext,
        payload: dict[str, Any] | None = None,
    ) -> None:
        merged_payload = {
            **self._session_metadata,
            **self._trace_payload_from_context(context),
            **(payload or {}),
        }
        event = build_event(
            event_type=event_type,
            run_id=run_id,
            context=context,
            payload=merged_payload,
        )
        try:
            maybe_awaitable = self._event_sink.emit(event)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        except Exception as exc:
            if self._event_sink_mode == EventSinkMode.FAIL_CLOSED:
                raise HarnessError(
                    code="EVENT_SINK_FAILED",
                    category="event",
                    message=f"Failed to emit event '{event_type}': {exc}",
                    retryable=True,
                    details={"event_type": event_type},
                ) from exc
            logger.warning("Event sink failure for %s: %s", event_type, exc)

    def _resolve_sync_value(self, value: Any, *, operation: str) -> Any:
        if not inspect.isawaitable(value):
            return value
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(value)
        raise HarnessError(
            code="ASYNC_VALUE_IN_SYNC_RUN",
            category="validation",
            message=f"{operation} returned awaitable in sync run. Use arun() or sync implementations.",
            retryable=False,
        )

    async def _resolve_async_value(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    def _enforce_policy_decision(
        self,
        *,
        decision: PolicyDecision,
        checkpoint: str,
        run_id: str,
        context: ExecutionContext,
    ) -> None:
        self._emit_event_sync(
            event_type="policy.decision",
            run_id=run_id,
            context=context,
            payload={
                "checkpoint": checkpoint,
                "action": decision.action.value,
                "reason_code": decision.reason_code,
                "message": decision.message,
            },
        )
        if decision.action == PolicyAction.DENY:
            raise HarnessError(
                code="POLICY_DENIED",
                category="policy",
                message=decision.message or f"Policy denied at {checkpoint}",
                retryable=False,
                details={
                    "checkpoint": checkpoint,
                    "reason_code": decision.reason_code,
                },
            )

    async def _enforce_policy_decision_async(
        self,
        *,
        decision: PolicyDecision,
        checkpoint: str,
        run_id: str,
        context: ExecutionContext,
    ) -> None:
        await self._emit_event_async(
            event_type="policy.decision",
            run_id=run_id,
            context=context,
            payload={
                "checkpoint": checkpoint,
                "action": decision.action.value,
                "reason_code": decision.reason_code,
                "message": decision.message,
            },
        )
        if decision.action == PolicyAction.DENY:
            raise HarnessError(
                code="POLICY_DENIED",
                category="policy",
                message=decision.message or f"Policy denied at {checkpoint}",
                retryable=False,
                details={
                    "checkpoint": checkpoint,
                    "reason_code": decision.reason_code,
                },
            )

    def _run_policy_sync(
        self,
        *,
        method_name: str,
        payload: Any,
        run_input: RunInput | None,
        context: ExecutionContext,
    ) -> PolicyDecision:
        method = getattr(self._policy_engine, method_name, None)
        if method is None:
            return PolicyDecision.allow()
        try:
            decision = self._resolve_sync_value(
                method(payload, context),
                operation=f"policy.{method_name}",
            )
        except Exception as exc:
            if self._policy_fail_open:
                logger.warning("Policy engine failed at %s; fail-open enabled: %s", method_name, exc)
                return PolicyDecision.allow()
            raise HarnessError(
                code="POLICY_EVALUATION_FAILED",
                category="policy",
                message=f"Policy evaluation failed at {method_name}: {exc}",
                retryable=False,
            ) from exc
        if not isinstance(decision, PolicyDecision):
            raise HarnessError(
                code="POLICY_INVALID_DECISION",
                category="policy",
                message=f"Policy method {method_name} returned invalid type: {type(decision).__name__}",
                retryable=False,
            )
        if (
            run_input is not None
            and decision.action == PolicyAction.ALLOW_WITH_CONSTRAINTS
            and decision.constraints
        ):
            run_input.metadata.setdefault("policy_constraints", {}).update(decision.constraints)
        return decision

    async def _run_policy_async(
        self,
        *,
        method_name: str,
        payload: Any,
        run_input: RunInput | None,
        context: ExecutionContext,
    ) -> PolicyDecision:
        method = getattr(self._policy_engine, method_name, None)
        if method is None:
            return PolicyDecision.allow()
        try:
            decision = await self._resolve_async_value(method(payload, context))
        except Exception as exc:
            if self._policy_fail_open:
                logger.warning("Policy engine failed at %s; fail-open enabled: %s", method_name, exc)
                return PolicyDecision.allow()
            raise HarnessError(
                code="POLICY_EVALUATION_FAILED",
                category="policy",
                message=f"Policy evaluation failed at {method_name}: {exc}",
                retryable=False,
            ) from exc
        if not isinstance(decision, PolicyDecision):
            raise HarnessError(
                code="POLICY_INVALID_DECISION",
                category="policy",
                message=f"Policy method {method_name} returned invalid type: {type(decision).__name__}",
                retryable=False,
            )
        if (
            run_input is not None
            and decision.action == PolicyAction.ALLOW_WITH_CONSTRAINTS
            and decision.constraints
        ):
            run_input.metadata.setdefault("policy_constraints", {}).update(decision.constraints)
        return decision

    def _run_pre_hooks_sync(
        self,
        *,
        run_input: RunInput,
        context: ExecutionContext,
    ) -> RunInput:
        current = run_input
        for hook in self._pre_run_hooks:
            hook_name = getattr(hook, "__name__", hook.__class__.__name__)
            try:
                maybe_result = self._resolve_sync_value(
                    hook(current, context),
                    operation=f"pre_hook:{hook_name}",
                )
            except Exception as exc:
                raise HarnessError(
                    code="HOOK_PRE_FAILED",
                    category="hook",
                    message=f"Pre-run hook failed: {hook_name}: {exc}",
                    retryable=False,
                ) from exc
            if maybe_result is None:
                continue
            if not isinstance(maybe_result, RunInput):
                raise HarnessError(
                    code="HOOK_PRE_INVALID_RETURN",
                    category="hook",
                    message=f"Pre-run hook {hook_name} must return RunInput or None",
                    retryable=False,
                )
            current = maybe_result
        return current

    async def _run_pre_hooks_async(
        self,
        *,
        run_input: RunInput,
        context: ExecutionContext,
    ) -> RunInput:
        current = run_input
        for hook in self._pre_run_hooks:
            hook_name = getattr(hook, "__name__", hook.__class__.__name__)
            try:
                maybe_result = await self._resolve_async_value(hook(current, context))
            except Exception as exc:
                raise HarnessError(
                    code="HOOK_PRE_FAILED",
                    category="hook",
                    message=f"Pre-run hook failed: {hook_name}: {exc}",
                    retryable=False,
                ) from exc
            if maybe_result is None:
                continue
            if not isinstance(maybe_result, RunInput):
                raise HarnessError(
                    code="HOOK_PRE_INVALID_RETURN",
                    category="hook",
                    message=f"Pre-run hook {hook_name} must return RunInput or None",
                    retryable=False,
                )
            current = maybe_result
        return current

    def _run_post_hooks_sync(
        self,
        *,
        run_input: RunInput,
        result: RunResultEnvelope,
        context: ExecutionContext,
    ) -> RunResultEnvelope:
        current = result
        for hook in self._post_run_hooks:
            hook_name = getattr(hook, "__name__", hook.__class__.__name__)
            try:
                maybe_result = self._resolve_sync_value(
                    hook(run_input, current, context),
                    operation=f"post_hook:{hook_name}",
                )
            except Exception as exc:
                raise HarnessError(
                    code="HOOK_POST_FAILED",
                    category="hook",
                    message=f"Post-run hook failed: {hook_name}: {exc}",
                    retryable=False,
                ) from exc
            if maybe_result is None:
                continue
            if not isinstance(maybe_result, RunResultEnvelope):
                raise HarnessError(
                    code="HOOK_POST_INVALID_RETURN",
                    category="hook",
                    message=f"Post-run hook {hook_name} must return RunResultEnvelope or None",
                    retryable=False,
                )
            current = maybe_result
        return current

    async def _run_post_hooks_async(
        self,
        *,
        run_input: RunInput,
        result: RunResultEnvelope,
        context: ExecutionContext,
    ) -> RunResultEnvelope:
        current = result
        for hook in self._post_run_hooks:
            hook_name = getattr(hook, "__name__", hook.__class__.__name__)
            try:
                maybe_result = await self._resolve_async_value(hook(run_input, current, context))
            except Exception as exc:
                raise HarnessError(
                    code="HOOK_POST_FAILED",
                    category="hook",
                    message=f"Post-run hook failed: {hook_name}: {exc}",
                    retryable=False,
                ) from exc
            if maybe_result is None:
                continue
            if not isinstance(maybe_result, RunResultEnvelope):
                raise HarnessError(
                    code="HOOK_POST_INVALID_RETURN",
                    category="hook",
                    message=f"Post-run hook {hook_name} must return RunResultEnvelope or None",
                    retryable=False,
                )
            current = maybe_result
        return current

    @staticmethod
    def _extract_event_content(event: Any) -> str:
        if event is None:
            return ""
        if isinstance(event, str):
            return event
        event_name = AgentHarness._event_name(event)
        if event_name and event_name not in _ASSISTANT_STREAM_EVENTS:
            return ""
        content = getattr(event, "content", None)
        if content is not None:
            return str(content)
        if isinstance(event, dict) and "content" in event:
            return str(event["content"])
        return ""

    @staticmethod
    def _event_name(event: Any) -> str:
        if event is None:
            return ""
        raw_event = getattr(event, "event", None)
        if raw_event is None and isinstance(event, dict):
            raw_event = event.get("event")
        return str(raw_event or "")

    @staticmethod
    def _event_attr(event: Any, key: str, default: Any = None) -> Any:
        if isinstance(event, dict):
            return event.get(key, default)
        return getattr(event, key, default)

    @staticmethod
    def _normalize_tool_arguments(arguments: Any) -> dict[str, Any]:
        if arguments is None:
            return {}
        if isinstance(arguments, dict):
            raw = arguments
        else:
            items = getattr(arguments, "items", None)
            if not callable(items):
                return {}
            try:
                raw = dict(items())
            except Exception:
                return {}
        serialized = AgentHarness._serialize_event_value(raw)
        return serialized if isinstance(serialized, dict) else {}

    @staticmethod
    def _serialize_event_value(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {
                str(key): AgentHarness._serialize_event_value(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [AgentHarness._serialize_event_value(item) for item in value]

        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            try:
                return AgentHarness._serialize_event_value(to_dict())
            except Exception:
                pass

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return AgentHarness._serialize_event_value(model_dump())
            except Exception:
                pass

        if hasattr(value, "__dict__"):
            return {
                str(key): AgentHarness._serialize_event_value(item)
                for key, item in vars(value).items()
                if not key.startswith("_")
            }

        return str(value)

    @staticmethod
    def _stream_event_details(event: Any) -> dict[str, Any]:
        if event is None:
            return {}
        if isinstance(event, dict):
            details = AgentHarness._serialize_event_value(event)
        else:
            to_dict = getattr(event, "to_dict", None)
            if callable(to_dict):
                try:
                    details = AgentHarness._serialize_event_value(to_dict())
                except Exception:
                    details = AgentHarness._serialize_event_value(vars(event))
            elif hasattr(event, "__dict__"):
                details = AgentHarness._serialize_event_value(vars(event))
            else:
                details = {"value": AgentHarness._serialize_event_value(event)}

        if not isinstance(details, dict):
            details = {"value": details}
        event_name = AgentHarness._event_name(event)
        if event_name and "event" not in details:
            details["event"] = event_name
        return details

    @staticmethod
    def _tool_stream_payload(event: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        tool_obj = AgentHarness._event_attr(event, "tool", None)

        arguments = AgentHarness._normalize_tool_arguments(
            AgentHarness._event_attr(event, "arguments", None)
        )
        if not arguments and tool_obj is not None:
            arguments = AgentHarness._normalize_tool_arguments(getattr(tool_obj, "arguments", None))
        if arguments:
            payload["argument_keys"] = sorted(arguments.keys())
            payload["arguments"] = arguments

        duration_ms = AgentHarness._event_attr(event, "duration_ms", None)
        if duration_ms is None and tool_obj is not None:
            duration_ms = getattr(tool_obj, "duration_ms", None)
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms

        error = AgentHarness._event_attr(event, "error", None)
        if error is None and tool_obj is not None:
            error = getattr(tool_obj, "error", None)
        if error is not None:
            payload["error"] = AgentHarness._serialize_event_value(error)

        result = AgentHarness._event_attr(event, "result", None)
        if result is None and tool_obj is not None:
            result = getattr(tool_obj, "result", None)
        if result is None and AgentHarness._event_name(event) in {
            "ToolCallCompleted",
            "ToolCallError",
        }:
            result = AgentHarness._event_attr(event, "content", None)
        if result is not None:
            payload["result_preview"] = AgentHarness._format_result_preview(result)
            payload["result_chars"] = len(str(result))

        return payload

    @staticmethod
    def _stream_event_summary(event: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key in (
            "parent_run_id",
            "step_id",
            "step_name",
            "step_index",
            "tool_name",
            "tool_call_id",
        ):
            value = AgentHarness._event_attr(event, key, None)
            if value is not None:
                payload[key] = value

        tool_obj = AgentHarness._event_attr(event, "tool", None)
        if tool_obj is not None:
            tool_name = getattr(tool_obj, "tool_name", None) or getattr(tool_obj, "name", None)
            tool_call_id = getattr(tool_obj, "tool_call_id", None)
            if tool_name and "tool_name" not in payload:
                payload["tool_name"] = tool_name
            if tool_call_id and "tool_call_id" not in payload:
                payload["tool_call_id"] = tool_call_id
        payload.update(AgentHarness._tool_stream_payload(event))
        return payload

    @staticmethod
    def _format_tool_invocation_label(
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        limit: int = 140,
    ) -> str:
        if not arguments:
            return tool_name

        rendered_parts: list[str] = []
        for key, value in arguments.items():
            if isinstance(value, str):
                rendered = json.dumps(" ".join(value.split()), ensure_ascii=True)
            else:
                try:
                    rendered = json.dumps(value, ensure_ascii=True, sort_keys=True)
                except TypeError:
                    rendered = json.dumps(str(value), ensure_ascii=True)
            rendered = AgentHarness._truncate_text(rendered, limit=48)
            rendered_parts.append(f"{key}={rendered}")

        label = f"{tool_name}({', '.join(rendered_parts)})"
        return AgentHarness._truncate_text(label, limit=limit)

    @staticmethod
    def _extract_thinking_content(event: Any) -> str:
        reasoning_content = AgentHarness._event_attr(event, "reasoning_content", None)
        if reasoning_content:
            return str(reasoning_content)

        if AgentHarness._event_name(event) == "ReasoningStep":
            content = AgentHarness._event_attr(event, "content", None)
            if content is None:
                return ""
            if isinstance(content, str):
                return content
            summary = getattr(content, "summary", None)
            title = getattr(content, "title", None)
            reasoning = getattr(content, "reasoning", None)
            for value in (summary, title, reasoning):
                if value:
                    return str(value)
            return str(content)

        return ""

    @staticmethod
    def _thinking_phase(event: Any) -> str:
        event_name = AgentHarness._event_name(event)
        if event_name == "ReasoningStarted":
            return "planning"
        if event_name in {"ReasoningStep", "ReasoningContentDelta"}:
            return "analyzing"
        if event_name == "ReasoningCompleted":
            return "evaluating"
        return "analyzing"

    @staticmethod
    def _map_agno_event_type(event: Any) -> str | None:
        raw_event = AgentHarness._event_name(event)
        if not raw_event:
            return None
        mapping = {
            "ToolCallStarted": "tool.call.started",
            "ToolCallCompleted": "tool.call.completed",
            "ToolCallError": "tool.call.failed",
            "ReasoningStarted": "reasoning.started",
            "ReasoningStep": "reasoning.step",
            "ReasoningContentDelta": "reasoning.delta",
            "ReasoningCompleted": "reasoning.completed",
            "MemoryUpdateStarted": "memory.write.started",
            "MemoryUpdateCompleted": "memory.write.completed",
            "SessionSummaryStarted": "session.summary.started",
            "SessionSummaryCompleted": "session.summary.completed",
        }
        return mapping.get(str(raw_event))

    def run(
        self,
        message: str,
        *,
        stream: bool = False,
        stream_events: bool = False,
        max_turns: int | None = None,
        skill: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        context: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
        output_schema: type | None = None,
        **kwargs,
    ) -> RunOutput | Iterator[RunOutputEvent]:
        """Run the agent on a message."""
        self._check_context_budget()

        run_id = f"run_{uuid4().hex}"
        effective_session = session_id or (context.session_id if context else None) or self._active_session_id(None)
        effective_user = user_id or (context.user_id if context else None) or self.user_id
        ctx = context.with_metadata(metadata) if context else self._build_execution_context(
            user_id=effective_user,
            session_id=effective_session,
            metadata=metadata,
        )
        run_input = RunInput(
            run_id=run_id,
            message=message,
            skill=skill,
            stream=stream,
            stream_events=stream_events,
            metadata=dict(metadata or {}),
        )
        if max_turns is not None:
            run_input.metadata.setdefault("max_turns", int(max_turns))
        base_prompt = self._agent.system_message
        stream_cleanup_deferred = False

        self._emit_event_sync(
            event_type="run.started",
            run_id=run_id,
            context=ctx,
            payload={
                "stream": stream,
                "stream_events": stream_events,
                "skill": skill,
                "max_turns": max_turns,
            },
        )

        try:
            run_input = self._run_pre_hooks_sync(run_input=run_input, context=ctx)

            before_run_decision = self._run_policy_sync(
                method_name="before_run",
                payload=run_input,
                run_input=run_input,
                context=ctx,
            )
            self._enforce_policy_decision(
                decision=before_run_decision,
                checkpoint="before_run",
                run_id=run_id,
                context=ctx,
            )
            if before_run_decision.action == PolicyAction.ALLOW_WITH_REDACTION:
                run_input.message = apply_redactions(run_input.message, before_run_decision.redactions)

            skill_content: str | None = None
            if run_input.skill:
                self._emit_event_sync(
                    event_type="skill.load.started",
                    run_id=run_id,
                    context=ctx,
                    payload={"name": run_input.skill},
                )
                skill_decision = self._run_policy_sync(
                    method_name="before_skill_load",
                    payload=SkillLoadRequest(name=run_input.skill),
                    run_input=run_input,
                    context=ctx,
                )
                self._enforce_policy_decision(
                    decision=skill_decision,
                    checkpoint="before_skill_load",
                    run_id=run_id,
                    context=ctx,
                )
                skill_content = self.skills.load_skill(run_input.skill)
                self._emit_event_sync(
                    event_type="skill.load.completed",
                    run_id=run_id,
                    context=ctx,
                    payload={"name": run_input.skill, "loaded": bool(skill_content)},
                )

                if skill_content:
                    # ── Skill enforcement: context:fork ──────────────────
                    # If the skill declares context: fork, run it in an isolated
                    # subagent instead of the main agent loop.
                    skill_obj = self.skills._get_skill(run_input.skill)
                    if skill_obj and skill_obj.meta.context == "fork":
                        from .tools.tasks import _run_subagent
                        active_provider = self._model.split(":", 1)[0] if ":" in self._model else None
                        subagent_model = _resolve_model(
                            skill_obj.meta.model or self._model,
                            active_provider,
                            self.config,
                        )
                        fork_result = _run_subagent(
                            task=run_input.message,
                            instructions=skill_content,
                            model_id=subagent_model,
                            tool_names=skill_obj.meta.allowed_tools or None,
                        )
                        self._emit_event_sync(
                            event_type="skill.fork.completed",
                            run_id=run_id,
                            context=ctx,
                            payload={"name": run_input.skill, "result_chars": len(fork_result)},
                        )
                        if self._agent.system_message != base_prompt:
                            self._agent.system_message = base_prompt
                        return fork_result

                    # ── Skill enforcement: command-dispatch ──────────────
                    # If the skill declares command-dispatch: tool, invoke the
                    # specified tool directly — bypassing the LLM entirely.
                    if skill_obj and skill_obj.meta.command_dispatch == "tool" and skill_obj.meta.command_tool:
                        tool_result = self._dispatch_command_tool(
                            tool_name=skill_obj.meta.command_tool,
                            arguments=run_input.message,
                        )
                        self._emit_event_sync(
                            event_type="skill.command_dispatch.completed",
                            run_id=run_id,
                            context=ctx,
                            payload={"name": run_input.skill, "tool": skill_obj.meta.command_tool},
                        )
                        if self._agent.system_message != base_prompt:
                            self._agent.system_message = base_prompt
                        return tool_result

                    self._set_system_prompt(skill_content=skill_content, session_id=effective_session)

            prompt = PromptEnvelope(
                system_prompt=self._agent.system_message,
                user_message=run_input.message,
                skill=run_input.skill,
            )
            prompt_decision = self._run_policy_sync(
                method_name="before_prompt_send",
                payload=prompt,
                run_input=run_input,
                context=ctx,
            )
            self._enforce_policy_decision(
                decision=prompt_decision,
                checkpoint="before_prompt_send",
                run_id=run_id,
                context=ctx,
            )
            if prompt_decision.action == PolicyAction.ALLOW_WITH_REDACTION:
                prompt.user_message = apply_redactions(prompt.user_message, prompt_decision.redactions)
                prompt.system_prompt = apply_redactions(prompt.system_prompt, prompt_decision.redactions)
                self._agent.system_message = prompt.system_prompt

            self._emit_event_sync(
                event_type="prompt.built",
                run_id=run_id,
                context=ctx,
                payload={
                    "system_chars": len(prompt.system_prompt),
                    "user_chars": len(prompt.user_message),
                    "skill": run_input.skill,
                },
            )
            self._emit_event_sync(
                event_type="model.request.started",
                run_id=run_id,
                context=ctx,
                payload={"stream": stream, "stream_events": stream_events},
            )

            call_kwargs = dict(kwargs)
            extra_metadata = call_kwargs.pop("metadata", None)
            call_kwargs.pop("run_id", None)
            if output_schema is not None:
                call_kwargs["output_schema"] = output_schema
            if max_turns is not None:
                call_kwargs["max_turns"] = int(max_turns)
            agent_metadata = self._build_agent_run_metadata(
                context=ctx,
                run_input=run_input,
            )
            if isinstance(extra_metadata, dict):
                agent_metadata.update(extra_metadata)

            result = self._agent.run(
                prompt.user_message,
                stream=stream,
                stream_events=stream_events,
                session_id=effective_session,
                user_id=effective_user,
                run_id=run_id,
                metadata=agent_metadata,
                **call_kwargs,
            )

            if self._agent.system_message != base_prompt:
                self._agent.system_message = base_prompt

            if stream:

                def _wrapped_stream() -> Iterator[RunOutputEvent]:
                    collected: list[str] = []
                    cumulative = ""
                    stream_error_signal: dict[str, Any] | None = None
                    run_failed_emitted = False
                    try:
                        for event in result:
                            source_event = self._event_name(event) or None
                            stream_summary = self._stream_event_summary(event)
                            stream_details = self._stream_event_details(event)
                            if source_event:
                                self._emit_event_sync(
                                    event_type="agno.event",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={
                                        "source_event": source_event,
                                        **stream_summary,
                                        "details": stream_details,
                                    },
                                )

                            mapped_event = self._map_agno_event_type(event)
                            if mapped_event and mapped_event not in _TOOL_LIFECYCLE_EVENT_TYPES:
                                self._emit_event_sync(
                                    event_type=mapped_event,
                                    run_id=run_id,
                                    context=ctx,
                                    payload={
                                        "source_event": source_event,
                                        **stream_summary,
                                        "details": stream_details,
                                    },
                                )

                            thinking = self._extract_thinking_content(event)
                            if thinking:
                                self._emit_event_sync(
                                    event_type="thinking",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={
                                        "content": thinking,
                                        "phase": self._thinking_phase(event),
                                        "source_event": self._event_name(event),
                                    },
                                )

                            error_signal = self._extract_error_signal_from_stream_event(event)
                            if error_signal is not None and stream_error_signal is None:
                                stream_error_signal = error_signal

                            text = self._extract_event_content(event)
                            if text:
                                collected.append(text)
                                cumulative += text
                                self._emit_event_sync(
                                    event_type="run.content",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={"chars": len(text)},
                                )
                                self._emit_event_sync(
                                    event_type="response_chunk",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={
                                        "content": text,
                                        "cumulative": cumulative,
                                        "is_final": False,
                                    },
                                )
                            yield event
                        self._emit_event_sync(
                            event_type="response_chunk",
                            run_id=run_id,
                            context=ctx,
                            payload={
                                "content": "",
                                "cumulative": cumulative,
                                "is_final": True,
                            },
                        )
                        post_result = RunResultEnvelope(
                            run_id=run_id,
                            content="".join(collected),
                            raw_output=None,
                            metadata=dict(run_input.metadata),
                        )
                        post_result = self._run_post_hooks_sync(
                            run_input=run_input,
                            result=post_result,
                            context=ctx,
                        )
                        output_text = str(post_result.content) if post_result.content is not None else ""
                        if stream_error_signal is not None:
                            error_message = (
                                self._normalize_error_message(stream_error_signal.get("message"))
                                or "Model invocation failed."
                            )
                            error_message = self._truncate_text(error_message, limit=_ERROR_MESSAGE_LIMIT)
                            code = stream_error_signal.get("error_id") or stream_error_signal.get("error_type")
                            self._emit_event_sync(
                                event_type="model.request.failed",
                                run_id=run_id,
                                context=ctx,
                                payload={"error": error_message, "code": code, "output_chars": len(output_text)},
                            )
                            self._emit_event_sync(
                                event_type="run.failed",
                                run_id=run_id,
                                context=ctx,
                                payload={"error": error_message, "code": code},
                            )
                            run_failed_emitted = True
                            self._raise_stream_error_signal(stream_error_signal)
                        self._emit_event_sync(
                            event_type="model.request.completed",
                            run_id=run_id,
                            context=ctx,
                            payload={"output_chars": len(output_text)},
                        )
                        self._emit_event_sync(
                            event_type="run.completed",
                            run_id=run_id,
                            context=ctx,
                            payload={"output_chars": len(output_text)},
                        )
                        self._maybe_optimize_memories()
                    except Exception as exc:
                        harness_error = self._extract_harness_error(exc)
                        error_code = harness_error.code if harness_error is not None else None
                        if not run_failed_emitted:
                            self._emit_event_sync(
                                event_type="run.failed",
                                run_id=run_id,
                                context=ctx,
                                payload={"error": str(exc), "code": error_code},
                            )
                        if harness_error is not None:
                            raise harness_error from exc
                        raise
                    finally:
                        self._cleanup_tool_step_state(run_id)

                stream_cleanup_deferred = True
                return _wrapped_stream()

            post_result = RunResultEnvelope(
                run_id=run_id,
                content=getattr(result, "content", result),
                raw_output=result,
                metadata=dict(run_input.metadata),
            )
            post_result = self._run_post_hooks_sync(
                run_input=run_input,
                result=post_result,
                context=ctx,
            )
            output_text = str(post_result.content) if post_result.content is not None else ""
            resolved_output = post_result.raw_output if post_result.raw_output is not None else result
            signal = self._extract_error_signal_from_run_output(resolved_output)
            if self._status_is_error(resolved_output):
                self._raise_if_fatal_error_signal(signal)
                error_message = self._normalize_error_message(signal.get("message")) or "Model invocation failed."
                error_message = self._truncate_text(error_message, limit=_ERROR_MESSAGE_LIMIT)
                code = signal.get("error_id") or signal.get("error_type")
                self._emit_event_sync(
                    event_type="model.request.failed",
                    run_id=run_id,
                    context=ctx,
                    payload={"error": error_message, "code": code, "output_chars": len(output_text)},
                )
                self._emit_event_sync(
                    event_type="run.failed",
                    run_id=run_id,
                    context=ctx,
                    payload={"error": error_message, "code": code},
                )
                return resolved_output

            self._emit_event_sync(
                event_type="model.request.completed",
                run_id=run_id,
                context=ctx,
                payload={"output_chars": len(output_text)},
            )
            self._emit_event_sync(
                event_type="run.completed",
                run_id=run_id,
                context=ctx,
                payload={"output_chars": len(output_text)},
            )
            self._maybe_optimize_memories()
            return resolved_output
        except Exception as exc:
            if self._agent.system_message != base_prompt:
                self._agent.system_message = base_prompt
            harness_error = self._extract_harness_error(exc)
            error_code = harness_error.code if harness_error is not None else None
            self._emit_event_sync(
                event_type="run.failed",
                run_id=run_id,
                context=ctx,
                payload={"error": str(exc), "code": error_code},
            )
            if harness_error is not None:
                raise harness_error from exc
            raise HarnessError(
                code="MODEL_RUN_FAILED",
                category="model",
                message=str(exc),
                retryable=True,
            ) from exc
        finally:
            if not stream_cleanup_deferred:
                self._cleanup_tool_step_state(run_id)

    async def arun(
        self,
        message: str,
        *,
        stream: bool = False,
        stream_events: bool = False,
        max_turns: int | None = None,
        skill: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        context: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
        output_schema: type | None = None,
        **kwargs,
    ) -> RunOutput | AsyncIterator[RunOutputEvent]:
        """Async version of run()."""
        self._check_context_budget()

        run_id = f"run_{uuid4().hex}"
        effective_session = session_id or (context.session_id if context else None) or self._active_session_id(None)
        effective_user = user_id or (context.user_id if context else None) or self.user_id
        ctx = context.with_metadata(metadata) if context else self._build_execution_context(
            user_id=effective_user,
            session_id=effective_session,
            metadata=metadata,
        )
        run_input = RunInput(
            run_id=run_id,
            message=message,
            skill=skill,
            stream=stream,
            stream_events=stream_events,
            metadata=dict(metadata or {}),
        )
        if max_turns is not None:
            run_input.metadata.setdefault("max_turns", int(max_turns))
        base_prompt = self._agent.system_message
        stream_cleanup_deferred = False

        await self._emit_event_async(
            event_type="run.started",
            run_id=run_id,
            context=ctx,
            payload={
                "stream": stream,
                "stream_events": stream_events,
                "skill": skill,
                "max_turns": max_turns,
            },
        )

        try:
            run_input = await self._run_pre_hooks_async(run_input=run_input, context=ctx)

            before_run_decision = await self._run_policy_async(
                method_name="before_run",
                payload=run_input,
                run_input=run_input,
                context=ctx,
            )
            await self._enforce_policy_decision_async(
                decision=before_run_decision,
                checkpoint="before_run",
                run_id=run_id,
                context=ctx,
            )
            if before_run_decision.action == PolicyAction.ALLOW_WITH_REDACTION:
                run_input.message = apply_redactions(run_input.message, before_run_decision.redactions)

            skill_content: str | None = None
            if run_input.skill:
                await self._emit_event_async(
                    event_type="skill.load.started",
                    run_id=run_id,
                    context=ctx,
                    payload={"name": run_input.skill},
                )
                skill_decision = await self._run_policy_async(
                    method_name="before_skill_load",
                    payload=SkillLoadRequest(name=run_input.skill),
                    run_input=run_input,
                    context=ctx,
                )
                await self._enforce_policy_decision_async(
                    decision=skill_decision,
                    checkpoint="before_skill_load",
                    run_id=run_id,
                    context=ctx,
                )
                skill_content = self.skills.load_skill(run_input.skill)
                await self._emit_event_async(
                    event_type="skill.load.completed",
                    run_id=run_id,
                    context=ctx,
                    payload={"name": run_input.skill, "loaded": bool(skill_content)},
                )
                if skill_content:
                    self._set_system_prompt(skill_content=skill_content, session_id=effective_session)

            prompt = PromptEnvelope(
                system_prompt=self._agent.system_message,
                user_message=run_input.message,
                skill=run_input.skill,
            )
            prompt_decision = await self._run_policy_async(
                method_name="before_prompt_send",
                payload=prompt,
                run_input=run_input,
                context=ctx,
            )
            await self._enforce_policy_decision_async(
                decision=prompt_decision,
                checkpoint="before_prompt_send",
                run_id=run_id,
                context=ctx,
            )
            if prompt_decision.action == PolicyAction.ALLOW_WITH_REDACTION:
                prompt.user_message = apply_redactions(prompt.user_message, prompt_decision.redactions)
                prompt.system_prompt = apply_redactions(prompt.system_prompt, prompt_decision.redactions)
                self._agent.system_message = prompt.system_prompt

            await self._emit_event_async(
                event_type="prompt.built",
                run_id=run_id,
                context=ctx,
                payload={
                    "system_chars": len(prompt.system_prompt),
                    "user_chars": len(prompt.user_message),
                    "skill": run_input.skill,
                },
            )
            await self._emit_event_async(
                event_type="model.request.started",
                run_id=run_id,
                context=ctx,
                payload={"stream": stream, "stream_events": stream_events},
            )

            call_kwargs = dict(kwargs)
            extra_metadata = call_kwargs.pop("metadata", None)
            call_kwargs.pop("run_id", None)
            if output_schema is not None:
                call_kwargs["output_schema"] = output_schema
            if max_turns is not None:
                call_kwargs["max_turns"] = int(max_turns)
            agent_metadata = self._build_agent_run_metadata(
                context=ctx,
                run_input=run_input,
            )
            if isinstance(extra_metadata, dict):
                agent_metadata.update(extra_metadata)

            agno_call = self._agent.arun(
                prompt.user_message,
                stream=stream,
                stream_events=stream_events,
                session_id=effective_session,
                user_id=effective_user,
                run_id=run_id,
                metadata=agent_metadata,
                **call_kwargs,
            )

            # Agno's arun(stream=True) may return an async generator directly
            # (not a coroutine), so we can't blindly await it.
            if hasattr(agno_call, "__anext__") or hasattr(agno_call, "__aiter__"):
                result = agno_call
            else:
                result = await agno_call

            if self._agent.system_message != base_prompt:
                self._agent.system_message = base_prompt

            if stream and hasattr(result, "__aiter__"):

                async def _wrapped_stream() -> AsyncIterator[RunOutputEvent]:
                    collected: list[str] = []
                    cumulative = ""
                    stream_error_signal: dict[str, Any] | None = None
                    run_failed_emitted = False
                    try:
                        async for event in result:
                            source_event = self._event_name(event) or None
                            stream_summary = self._stream_event_summary(event)
                            stream_details = self._stream_event_details(event)
                            if source_event:
                                await self._emit_event_async(
                                    event_type="agno.event",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={
                                        "source_event": source_event,
                                        **stream_summary,
                                        "details": stream_details,
                                    },
                                )

                            mapped_event = self._map_agno_event_type(event)
                            if mapped_event and mapped_event not in _TOOL_LIFECYCLE_EVENT_TYPES:
                                await self._emit_event_async(
                                    event_type=mapped_event,
                                    run_id=run_id,
                                    context=ctx,
                                    payload={
                                        "source_event": source_event,
                                        **stream_summary,
                                        "details": stream_details,
                                    },
                                )

                            thinking = self._extract_thinking_content(event)
                            if thinking:
                                await self._emit_event_async(
                                    event_type="thinking",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={
                                        "content": thinking,
                                        "phase": self._thinking_phase(event),
                                        "source_event": self._event_name(event),
                                    },
                                )

                            error_signal = self._extract_error_signal_from_stream_event(event)
                            if error_signal is not None and stream_error_signal is None:
                                stream_error_signal = error_signal

                            text = self._extract_event_content(event)
                            if text:
                                collected.append(text)
                                cumulative += text
                                await self._emit_event_async(
                                    event_type="run.content",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={"chars": len(text)},
                                )
                                await self._emit_event_async(
                                    event_type="response_chunk",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={
                                        "content": text,
                                        "cumulative": cumulative,
                                        "is_final": False,
                                    },
                                )
                            yield event
                        await self._emit_event_async(
                            event_type="response_chunk",
                            run_id=run_id,
                            context=ctx,
                            payload={
                                "content": "",
                                "cumulative": cumulative,
                                "is_final": True,
                            },
                        )
                        post_result = RunResultEnvelope(
                            run_id=run_id,
                            content="".join(collected),
                            raw_output=None,
                            metadata=dict(run_input.metadata),
                        )
                        post_result = await self._run_post_hooks_async(
                            run_input=run_input,
                            result=post_result,
                            context=ctx,
                        )
                        output_text = str(post_result.content) if post_result.content is not None else ""
                        if stream_error_signal is not None:
                            error_message = (
                                self._normalize_error_message(stream_error_signal.get("message"))
                                or "Model invocation failed."
                            )
                            error_message = self._truncate_text(error_message, limit=_ERROR_MESSAGE_LIMIT)
                            code = stream_error_signal.get("error_id") or stream_error_signal.get("error_type")
                            await self._emit_event_async(
                                event_type="model.request.failed",
                                run_id=run_id,
                                context=ctx,
                                payload={"error": error_message, "code": code, "output_chars": len(output_text)},
                            )
                            await self._emit_event_async(
                                event_type="run.failed",
                                run_id=run_id,
                                context=ctx,
                                payload={"error": error_message, "code": code},
                            )
                            run_failed_emitted = True
                            self._raise_stream_error_signal(stream_error_signal)
                        await self._emit_event_async(
                            event_type="model.request.completed",
                            run_id=run_id,
                            context=ctx,
                            payload={"output_chars": len(output_text)},
                        )
                        await self._emit_event_async(
                            event_type="run.completed",
                            run_id=run_id,
                            context=ctx,
                            payload={"output_chars": len(output_text)},
                        )
                        self._maybe_optimize_memories()
                    except Exception as exc:
                        harness_error = self._extract_harness_error(exc)
                        error_code = harness_error.code if harness_error is not None else None
                        if not run_failed_emitted:
                            await self._emit_event_async(
                                event_type="run.failed",
                                run_id=run_id,
                                context=ctx,
                                payload={"error": str(exc), "code": error_code},
                            )
                        if harness_error is not None:
                            raise harness_error from exc
                        raise
                    finally:
                        self._cleanup_tool_step_state(run_id)

                stream_cleanup_deferred = True
                return _wrapped_stream()

            post_result = RunResultEnvelope(
                run_id=run_id,
                content=getattr(result, "content", result),
                raw_output=result,
                metadata=dict(run_input.metadata),
            )
            post_result = await self._run_post_hooks_async(
                run_input=run_input,
                result=post_result,
                context=ctx,
            )
            output_text = str(post_result.content) if post_result.content is not None else ""
            resolved_output = post_result.raw_output if post_result.raw_output is not None else result
            signal = self._extract_error_signal_from_run_output(resolved_output)
            if self._status_is_error(resolved_output):
                self._raise_if_fatal_error_signal(signal)
                error_message = self._normalize_error_message(signal.get("message")) or "Model invocation failed."
                error_message = self._truncate_text(error_message, limit=_ERROR_MESSAGE_LIMIT)
                code = signal.get("error_id") or signal.get("error_type")
                await self._emit_event_async(
                    event_type="model.request.failed",
                    run_id=run_id,
                    context=ctx,
                    payload={"error": error_message, "code": code, "output_chars": len(output_text)},
                )
                await self._emit_event_async(
                    event_type="run.failed",
                    run_id=run_id,
                    context=ctx,
                    payload={"error": error_message, "code": code},
                )
                return resolved_output

            await self._emit_event_async(
                event_type="model.request.completed",
                run_id=run_id,
                context=ctx,
                payload={"output_chars": len(output_text)},
            )
            await self._emit_event_async(
                event_type="run.completed",
                run_id=run_id,
                context=ctx,
                payload={"output_chars": len(output_text)},
            )
            self._maybe_optimize_memories()
            return resolved_output
        except Exception as exc:
            if self._agent.system_message != base_prompt:
                self._agent.system_message = base_prompt
            harness_error = self._extract_harness_error(exc)
            error_code = harness_error.code if harness_error is not None else None
            await self._emit_event_async(
                event_type="run.failed",
                run_id=run_id,
                context=ctx,
                payload={"error": str(exc), "code": error_code},
            )
            if harness_error is not None:
                raise harness_error from exc
            raise HarnessError(
                code="MODEL_RUN_FAILED",
                category="model",
                message=str(exc),
                retryable=True,
            ) from exc
        finally:
            if not stream_cleanup_deferred:
                self._cleanup_tool_step_state(run_id)

    def print_response(self, message: str, *, stream: bool = True, skill: str | None = None, **kwargs) -> None:
        """
        Run the agent through the full runtime pipeline and pretty-print the response.

        Unlike calling self._agent.print_response() directly, this routes through
        run() so that hooks, policy, events, permissions, and guardrails are enforced.
        """
        if stream:
            # Stream through run() pipeline → print each chunk
            response = self.run(message, stream=True, skill=skill, **kwargs)
            for event in response:
                content = self._extract_event_content(event)
                if content:
                    print(content, end="", flush=True)
            print()  # final newline
        else:
            result = self.run(message, stream=False, skill=skill, **kwargs)
            content = getattr(result, "content", result)
            if content:
                print(content)

    def enter_plan_mode(self) -> None:
        """
        Activate plan mode: injects plan mode instructions into the system prompt.

        In plan mode the agent is instructed to:
        - Only read/search — no writes, edits, or shell commands
        - Write a .plan.md file with the implementation plan
        - Wait for user approval before implementing

        Use exit_plan_mode() to return to normal operation.
        """
        self._plan_mode = True
        current = self._permission_controller.current_mode()
        if current != PermissionMode.PLAN:
            self._plan_mode_restore_permission_mode = current
            self._permission_controller.set_mode(PermissionMode.PLAN)
        self._set_system_prompt(session_id=self.session_id)

    def exit_plan_mode(self) -> None:
        """Deactivate plan mode: restores normal system prompt."""
        self._plan_mode = False
        restore = self._plan_mode_restore_permission_mode
        if restore is not None:
            self._permission_controller.set_mode(restore)
        else:
            self._permission_controller.set_mode(
                normalize_permission_mode(self.config.permission_mode)
            )
        self._plan_mode_restore_permission_mode = None
        self._set_system_prompt(session_id=self.session_id)

    def add_tool(self, tool) -> None:
        """Add a tool or toolkit to the agent."""
        self._attach_tool_runtime_hooks([tool])
        self._agent.add_tool(tool)

    def get_chat_history(self) -> list:
        """Return the chat history for the current session."""
        active_session = self.session_id or getattr(self._agent, "session_id", "")
        return self._agent.get_chat_history(active_session or "")

    def get_session_messages(self, session_id: str | None = None) -> list:
        """
        Return chat history for a specific session ID.

        Falls back to the currently active session when not provided.
        """
        target_session = session_id or self.session_id or getattr(self._agent, "session_id", "")
        if not target_session:
            return []
        messages = self._agent.get_chat_history(target_session)
        return list(messages or [])

    @staticmethod
    def _normalize_session_record(record: Any) -> dict[str, Any]:
        if isinstance(record, dict):
            data = dict(record)
        else:
            data = {}

        keys = (
            "session_id",
            "id",
            "user_id",
            "created_at",
            "updated_at",
            "summary",
            "run_count",
            "title",
        )
        for key in keys:
            if key in data:
                continue
            value = getattr(record, key, None)
            if value is not None:
                data[key] = value

        if "session_id" not in data and "id" in data and data["id"] is not None:
            data["session_id"] = str(data["id"])
        return data

    def list_sessions(self, *, user_id: str | None = None, limit: int | None = 50) -> list[dict[str, Any]]:
        """
        List known sessions from the configured storage backend.

        This is a best-effort adapter over storage backends with different
        method names/signatures.
        """
        db = getattr(self._agent, "db", None)
        if db is None:
            return []

        effective_user = user_id or self.user_id
        method_names = (
            "list_sessions",
            "get_sessions",
            "get_all_sessions",
        )

        raw = None
        for method_name in method_names:
            method = getattr(db, method_name, None)
            if not callable(method):
                continue

            attempts = []
            kwargs: dict[str, Any] = {}
            if effective_user is not None:
                kwargs["user_id"] = effective_user
            if limit is not None:
                kwargs["limit"] = int(limit)
            if kwargs:
                attempts.append(kwargs)
            attempts.append({})

            for call_kwargs in attempts:
                try:
                    raw = method(**call_kwargs)
                    break
                except TypeError:
                    continue
            if raw is not None:
                break

        if raw is None:
            return []

        if isinstance(raw, dict):
            if isinstance(raw.get("sessions"), list):
                records = raw["sessions"]
            elif isinstance(raw.get("items"), list):
                records = raw["items"]
            else:
                records = [raw]
        elif isinstance(raw, list | tuple):
            records = list(raw)
        else:
            records = [raw]

        normalized = [self._normalize_session_record(item) for item in records]
        normalized = [item for item in normalized if item.get("session_id")]

        if effective_user is not None:
            filtered: list[dict[str, Any]] = []
            for item in normalized:
                if item.get("user_id") in {None, effective_user}:
                    filtered.append(item)
            normalized = filtered

        if limit is not None and limit >= 0:
            normalized = normalized[: int(limit)]
        return normalized

    def _session_exists(self, session_id: str) -> bool:
        getter = getattr(self._agent, "get_session", None)
        if callable(getter):
            try:
                if getter(session_id):
                    return True
            except TypeError:
                pass
        return bool(self.get_session_messages(session_id))

    def resume_session(self, session_id: str, *, verify_exists: bool = False) -> str:
        """
        Activate an existing session ID for subsequent runs.
        """
        target = str(session_id or "").strip()
        if not target:
            raise ValueError("session_id is required")
        if verify_exists and not self._session_exists(target):
            raise HarnessError(
                code="SESSION_NOT_FOUND",
                category="session",
                message=f"Session not found: {target}",
                retryable=False,
                details={"session_id": target},
            )

        self.session_id = target
        if hasattr(self._agent, "session_id"):
            self._agent.session_id = target
        self._set_system_prompt(session_id=target)
        return target

    def clear_session_context(self, new_session_id: str | None = None) -> str:
        """
        Switch to a fresh session ID so subsequent turns start with empty history.

        Prior sessions remain stored in the database for audit/replay.
        """
        session = new_session_id or f"session-{uuid4().hex[:12]}"
        self.session_id = session
        if hasattr(self._agent, "session_id"):
            self._agent.session_id = session
        self._set_system_prompt(session_id=session)
        return session

    def save_session_summary(self, summary: str) -> None:
        """
        Persist a session summary to today's daily log in the workspace.

        Useful for context compaction: call this at the end of long sessions
        to preserve important context for future sessions.
        """
        self.workspace.write_session_summary(summary)

    def _check_context_budget(self) -> None:
        """
        Check token usage against max_context_tokens budget.

        Called before each run() when max_context_tokens is set.
        Logs a warning at 85% usage. At 95%, logs critical and the caller
        should consider calling compact_session().

        This is a best-effort check — token counting requires a model with
        count_tokens() support. Silently skips if unavailable.
        """
        if not self._max_context_tokens:
            return

        try:
            model = self._agent.model
            if model is None or not hasattr(model, "count_tokens"):
                return

            # Get session messages for token counting
            active_session = self.session_id or getattr(self._agent, "session_id", "")
            messages = self._agent.get_chat_history(active_session or "")
            if not messages:
                return

            tokens = model.count_tokens(messages)
            budget = self._max_context_tokens
            usage_pct = tokens / budget

            if usage_pct >= 0.95:
                logger.critical(
                    "Context at %d/%d tokens (%.0f%%) — approaching limit. "
                    "Call compact_session() to flush and compact.",
                    tokens, budget, usage_pct * 100,
                )
            elif usage_pct >= 0.85:
                logger.warning(
                    "Context at %d/%d tokens (%.0f%%) — consider compacting soon.",
                    tokens, budget, usage_pct * 100,
                )
        except Exception:
            pass  # Token counting is best-effort; don't break runs

    def _maybe_optimize_memories(self) -> None:
        """
        Periodically trigger LearningMachine's Curator to deduplicate
        and prune stale memories. Called after each run().

        Runs every _optimize_every_n_runs invocations. Silently skips
        if learning is not enabled or if the optimization fails.
        """
        self._run_count += 1
        if self._run_count % self._optimize_every_n_runs != 0:
            return

        learning = self._agent.learning
        if learning is None:
            return

        try:
            if hasattr(learning, "optimize_memories"):
                learning.optimize_memories()
                logger.debug(
                    "LearningMachine memory optimization triggered (run %d).",
                    self._run_count,
                )
        except Exception:
            pass  # Memory optimization is best-effort

    async def compact_session(self) -> None:
        """
        Pre-compaction memory flush (OpenClaw pattern).

        Before clearing old history, triggers a silent agent turn that writes
        important context to MEMORY.md. Then generates a session summary to
        preserve continuity. Call this when the context window is getting full.

        Fires on_compaction callback (if set) with the summary text so the
        platform can persist it externally (e.g. to a database).

        This is a manual escape hatch — Agno v2.5.x does not auto-compact.
        """
        # Step 1: Ask the agent to write important facts to memory
        # Route through harness's arun() so hooks/policy/events are enforced.
        flush_prompt = (
            "SYSTEM: Context compaction is about to occur. Before your conversation "
            "history is cleared, write any important facts, decisions, code locations, "
            "or context that should persist to MEMORY.md. Be concise — only preserve "
            "what a future session would need to continue your work effectively."
        )
        await self.arun(
            flush_prompt,
            session_id=self.session_id,
            user_id=self.user_id,
        )

        # Step 2: Generate and persist a session summary
        summary: str | None = None
        if self._agent.session_summary_manager:
            session = self._agent.get_session(self.session_id)
            if session:
                summary = self._agent.session_summary_manager.create_session_summary(session)

        # Step 3: Fire compaction callback so platform can persist the summary
        if self._on_compaction and summary:
            try:
                await self._on_compaction(summary)
            except Exception:
                logger.exception("on_compaction callback failed")

    async def end_session(self, generate_summary: bool = True) -> str | None:
        """
        End the current session.

        Optionally generates a conversation summary via a lightweight LLM call,
        fires the on_session_end callback so the platform can persist it, and
        returns the summary text.
        """
        summary = None
        if generate_summary:
            summary = await self._generate_session_summary()
        created_files = self._list_created_sandbox_files()
        try:
            if self._on_session_end and summary:
                try:
                    await self._emit_session_end_callback(
                        summary,
                        created_files=created_files,
                    )
                except Exception:
                    logger.exception("on_session_end callback failed")
        finally:
            self._cleanup_sandbox_dir()
        return summary

    def close(self) -> None:
        """Release owned resources held by this harness."""
        if self._closed:
            return
        self._closed = True
        if self._finalizer.alive:
            self._finalizer()

    async def aclose(self) -> None:
        """Async-compatible close alias."""
        self.close()

    def __enter__(self) -> AgentHarness:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    async def __aenter__(self) -> AgentHarness:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _generate_session_summary(self) -> str | None:
        """Generate a short summary of the current session via a cheap LLM call."""
        messages = self.get_chat_history()
        if not messages:
            return None

        # Take the last N messages to keep the summary call cheap
        recent = messages[-20:]
        history_text = "\n".join(
            f"{getattr(m, 'role', 'unknown')}: {getattr(m, 'content', str(m))}"
            for m in recent
            if getattr(m, "content", None)
        )
        if not history_text:
            return None

        prompt = (
            "Summarize this conversation in 3-5 bullets focusing on "
            "decisions made and artifacts produced. Be concise.\n\n"
            f"{history_text}"
        )
        result = await self.arun(prompt, session_id=self.session_id)
        return str(getattr(result, "content", result) or "")

    @property
    def underlying_agent(self) -> Agent:
        """
        Access the underlying Agno Agent.

        .. deprecated::
            Use narrow accessors (model_name, storage, chat_history, etc.)
            instead. Direct access bypasses all harness protections (hooks,
            policy, events, permissions, guardrails). Will be removed in v1.0.
        """
        warnings.warn(
            "underlying_agent is deprecated — use narrow accessors (model_name, "
            "storage, chat_history, etc.) instead. Direct access bypasses harness "
            "protections.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._agent

    # ── Narrow accessors (preferred over underlying_agent) ────────────────

    @property
    def model_name(self) -> str:
        """Return the resolved model string (provider:model_id)."""
        return self._model

    @property
    def storage(self):
        """Return the storage backend (SqliteDb or PostgresDb)."""
        return self._agent.db

    @property
    def chat_history(self) -> list:
        """Return chat history for the current session."""
        return self.get_chat_history()

    @property
    def system_prompt(self) -> str:
        """Return the current system prompt."""
        return self._agent.system_message or ""

    def remove_tool(self, tool_name: str) -> bool:
        """
        Remove a tool by name from the agent's tool registry.

        Returns True if the tool was found and removed, False otherwise.
        """
        if not hasattr(self._agent, "_tools") or self._agent._tools is None:
            return False
        before = len(self._agent._tools)
        self._agent._tools = [
            t for t in self._agent._tools
            if getattr(t, "name", None) != tool_name
        ]
        return len(self._agent._tools) < before

    def remove_hook(self, hook, *, kind: str = "pre") -> bool:
        """
        Remove a pre-run or post-run hook.

        Args:
            hook: The hook function/callable to remove.
            kind: "pre" or "post".

        Returns True if the hook was found and removed, False otherwise.
        """
        target = self._pre_run_hooks if kind == "pre" else self._post_run_hooks
        try:
            target.remove(hook)
            return True
        except ValueError:
            return False


# Backward-compatible alias
HarnessAgent = AgentHarness
