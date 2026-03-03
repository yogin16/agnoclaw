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
import logging
import warnings
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Optional, Union
from uuid import uuid4

from agno.agent import Agent
from agno.exceptions import AgentRunException
from agno.run.agent import RunOutput, RunOutputEvent
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from .config import HarnessConfig, get_config
from .prompts.system import SystemPromptBuilder
from .runtime import (
    AllowAllPolicyEngine,
    EventSink,
    EventSinkMode,
    ExecutionContext,
    HarnessError,
    NullEventSink,
    PolicyAction,
    PolicyDecision,
    PolicyEngine,
    PostRunHook,
    PreRunHook,
    PromptEnvelope,
    RunInput,
    RunResultEnvelope,
    SkillLoadRequest,
    ToolCallRequest,
    ToolCallResult,
    PermissionApprover,
    PermissionController,
    PermissionMode,
    normalize_permission_mode,
    RuntimeGuardrails,
    apply_redactions,
    build_event,
)
from .skills.registry import SkillRegistry
from .tools import get_default_tools
from .workspace import Workspace

logger = logging.getLogger("agnoclaw.agent")

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


def _resolve_model(model: Optional[str], provider: Optional[str], config: HarnessConfig) -> str:
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
        tools: Additional tools to add alongside the defaults.
        instructions: Additional instructions appended to the system prompt.
        config: HarnessConfig override. Loaded from env/TOML if not provided.
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
        provider: Provider name — only needed when model is not in "provider:model_id"
                  format. Accepts "anthropic", "openai", "ollama", "groq", "google",
                  "aws"/"bedrock", "mistral", "xai"/"grok", "deepseek", "litellm".
        permission_mode: Runtime permission mode for tool calls:
                         "bypass", "default", "accept_edits", "plan", "dont_ask".
        permission_approver: Optional approval callback used in non-bypass modes.
        permission_require_approver: If True, deny approval-required calls when no approver exists.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        *,
        provider: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_dir: Optional[str | Path] = None,
        tools: Optional[list] = None,
        instructions: Optional[str] = None,
        config: Optional[HarnessConfig] = None,
        name: str = "agnoclaw",
        agent_id: Optional[str] = None,
        debug: bool = False,
        # Subagents
        subagents: Optional[dict] = None,
        # Memory options
        enable_user_memory: bool = False,
        enable_learning: Optional[bool] = None,
        learning_mode: Optional[str] = None,
        learning_namespace: Optional[str] = None,
        # Context management
        enable_compression: Optional[bool] = None,
        compress_token_limit: Optional[int] = None,
        enable_session_summary: Optional[bool] = None,
        num_history_runs: Optional[int] = None,
        num_history_messages: Optional[int] = None,
        max_tool_calls_from_history: Optional[int] = None,
        max_context_tokens: Optional[int] = None,
        # v0.2 runtime contracts
        event_sink: Optional[EventSink] = None,
        event_sink_mode: Optional[str] = None,
        policy_engine: Optional[PolicyEngine] = None,
        policy_fail_open: Optional[bool] = None,
        permission_mode: Optional[str] = None,
        permission_approver: Optional[PermissionApprover] = None,
        permission_require_approver: Optional[bool] = None,
        permission_preapproved_tools: Optional[list[str] | tuple[str, ...]] = None,
        permission_preapproved_categories: Optional[list[str] | tuple[str, ...]] = None,
        pre_run_hooks: Optional[list[PreRunHook]] = None,
        post_run_hooks: Optional[list[PostRunHook]] = None,
        tenant_id: Optional[str] = None,
        org_id: Optional[str] = None,
        team_id: Optional[str] = None,
        roles: Optional[list[str] | tuple[str, ...]] = None,
        scopes: Optional[list[str] | tuple[str, ...]] = None,
        request_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        context_metadata: Optional[dict[str, Any]] = None,
        # Legacy compat — use model + provider instead
        model_id: Optional[str] = None,
        extra_tools: Optional[list] = None,
        extra_instructions: Optional[str] = None,
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
        self.skills = SkillRegistry(self.workspace.skills_dir())

        # System prompt builder
        self._prompt_builder = SystemPromptBuilder(self.workspace.path)

        # Context budget monitoring
        self._max_context_tokens = max_context_tokens

        # Memory optimization: run Curator periodically to deduplicate/prune
        self._run_count = 0
        self._optimize_every_n_runs = 10  # trigger Curator every N runs

        # Build tool list (pass through named subagent definitions)
        _all_tools = get_default_tools(self.config, subagents=subagents)
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
                logger.info("Loaded %d plugin(s): %s", len(manifests), ", ".join(m.name for m in manifests))

        self._attach_tool_runtime_hooks(_all_tools)

        # Resolve learning flags before building system prompt
        _enable_learning = enable_learning if enable_learning is not None else self.config.enable_learning
        _learning_mode = learning_mode or self.config.learning_mode

        # Persist prompt options so per-run skill injection can be one-shot
        self._extra_instructions = _instructions
        self._include_learning = _enable_learning
        self._plan_mode = False

        # Assemble system prompt (skills are injected per-run, then reset)
        system_prompt = self._build_system_prompt(session_id=session_id)

        # Storage backend
        db = _make_db(self.config)

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

    def _build_system_prompt(
        self,
        *,
        skill_content: Optional[str] = None,
        session_id: Optional[str] = None,
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
        skill_content: Optional[str] = None,
        session_id: Optional[str] = None,
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

    def set_event_sink(self, sink: EventSink, mode: Optional[str] = None) -> None:
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

        def runtime_post_hook(agent=None, team=None, run_context=None, fc=None):
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
            arguments = dict(getattr(fc, "arguments", None) or {})
            request = ToolCallRequest(
                run_id=run_id,
                tool_name=tool_name,
                arguments=arguments,
                metadata={"tool_call_id": self._tool_call_id(fc)},
            )

            self._emit_event_sync(
                event_type="tool.call.started",
                run_id=run_id,
                context=context,
                payload={
                    "tool_name": tool_name,
                    "tool_call_id": self._tool_call_id(fc),
                    "argument_keys": sorted(arguments.keys()),
                },
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

            if decision.action == PolicyAction.ALLOW_WITH_REDACTION and getattr(fc, "arguments", None):
                fc.arguments = self._apply_redactions_to_object(dict(fc.arguments), decision.redactions)
        except HarnessError as exc:
            self._raise_agent_run_exception(exc)

    def _handle_tool_post_hook(self, *, fc, run_context) -> None:
        if fc is None or getattr(fc, "function", None) is None:
            return

        try:
            run_id = self._run_id_from_tool_hook(run_context=run_context, fc=fc)
            context = self._context_from_run_context(run_context)
            tool_name = getattr(fc.function, "name", "unknown_tool")
            result = ToolCallResult(
                run_id=run_id,
                tool_name=tool_name,
                arguments=dict(getattr(fc, "arguments", None) or {}),
                output=getattr(fc, "result", None),
                error=getattr(fc, "error", None),
                metadata={"tool_call_id": self._tool_call_id(fc)},
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

            event_type = "tool.call.failed" if getattr(fc, "error", None) else "tool.call.completed"
            payload = {
                "tool_name": tool_name,
                "tool_call_id": self._tool_call_id(fc),
                "error": getattr(fc, "error", None),
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

    def _active_session_id(self, override: Optional[str]) -> Optional[str]:
        if override is not None:
            return override
        return self.session_id or getattr(self._agent, "session_id", None)

    def _build_execution_context(
        self,
        *,
        user_id: Optional[str],
        session_id: Optional[str],
        metadata: Optional[dict[str, Any]] = None,
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
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        event = build_event(
            event_type=event_type,
            run_id=run_id,
            context=context,
            payload=payload,
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
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        event = build_event(
            event_type=event_type,
            run_id=run_id,
            context=context,
            payload=payload,
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
        content = getattr(event, "content", None)
        if content is not None:
            return str(content)
        if isinstance(event, dict) and "content" in event:
            return str(event["content"])
        return ""

    @staticmethod
    def _map_agno_event_type(event: Any) -> str | None:
        raw_event = getattr(event, "event", None)
        if raw_event is None and isinstance(event, dict):
            raw_event = event.get("event")
        if raw_event is None:
            return None
        mapping = {
            "ToolCallStarted": "tool.call.started",
            "ToolCallCompleted": "tool.call.completed",
            "ToolCallError": "tool.call.failed",
            "ReasoningStarted": "reasoning.started",
            "ReasoningStep": "reasoning.step",
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
        skill: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        context: Optional[ExecutionContext] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> Union[RunOutput, Iterator[RunOutputEvent]]:
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
        base_prompt = self._agent.system_message

        self._emit_event_sync(
            event_type="run.started",
            run_id=run_id,
            context=ctx,
            payload={
                "stream": stream,
                "stream_events": stream_events,
                "skill": skill,
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

            skill_content: Optional[str] = None
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
                        fork_result = _run_subagent(
                            task=run_input.message,
                            instructions=skill_content,
                            model_id=skill_obj.meta.model or self._model,
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
                    try:
                        for event in result:
                            mapped_event = self._map_agno_event_type(event)
                            if mapped_event:
                                self._emit_event_sync(
                                    event_type=mapped_event,
                                    run_id=run_id,
                                    context=ctx,
                                    payload={"source_event": getattr(event, "event", None)},
                                )
                            text = self._extract_event_content(event)
                            if text:
                                collected.append(text)
                                self._emit_event_sync(
                                    event_type="run.content",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={"chars": len(text)},
                                )
                            yield event
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
                        self._emit_event_sync(
                            event_type="run.failed",
                            run_id=run_id,
                            context=ctx,
                            payload={"error": str(exc), "code": error_code},
                        )
                        if harness_error is not None:
                            raise harness_error from exc
                        raise

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
            return post_result.raw_output if post_result.raw_output is not None else result
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

    async def arun(
        self,
        message: str,
        *,
        stream: bool = False,
        stream_events: bool = False,
        skill: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        context: Optional[ExecutionContext] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> Union[RunOutput, AsyncIterator[RunOutputEvent]]:
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
        base_prompt = self._agent.system_message

        await self._emit_event_async(
            event_type="run.started",
            run_id=run_id,
            context=ctx,
            payload={
                "stream": stream,
                "stream_events": stream_events,
                "skill": skill,
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

            skill_content: Optional[str] = None
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
                    try:
                        async for event in result:
                            mapped_event = self._map_agno_event_type(event)
                            if mapped_event:
                                await self._emit_event_async(
                                    event_type=mapped_event,
                                    run_id=run_id,
                                    context=ctx,
                                    payload={"source_event": getattr(event, "event", None)},
                                )
                            text = self._extract_event_content(event)
                            if text:
                                collected.append(text)
                                await self._emit_event_async(
                                    event_type="run.content",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={"chars": len(text)},
                                )
                            yield event
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
                        await self._emit_event_async(
                            event_type="run.failed",
                            run_id=run_id,
                            context=ctx,
                            payload={"error": str(exc), "code": error_code},
                        )
                        if harness_error is not None:
                            raise harness_error from exc
                        raise

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
            return post_result.raw_output if post_result.raw_output is not None else result
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

    def print_response(self, message: str, *, stream: bool = True, skill: Optional[str] = None, **kwargs) -> None:
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

    def clear_session_context(self, new_session_id: Optional[str] = None) -> str:
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
        if self._agent.session_summary_manager:
            session = self._agent.get_session(self.session_id)
            if session:
                self._agent.session_summary_manager.create_session_summary(session)

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
