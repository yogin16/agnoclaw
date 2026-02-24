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

import logging
from pathlib import Path
from typing import Any, Iterator, Optional, Union
from uuid import uuid4

from agno.agent import Agent
from agno.run.agent import RunOutput, RunOutputEvent

from .config import HarnessConfig, get_config
from .prompts.system import SystemPromptBuilder
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

    # Already in "provider:model_id" format?
    if ":" in model_str:
        parts = model_str.split(":", 1)
        p = _PROVIDER_ALIASES.get(parts[0].lower(), parts[0].lower())
        return f"{p}:{parts[1]}"

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
        # Legacy compat — use model + provider instead
        model_id: Optional[str] = None,
        extra_tools: Optional[list] = None,
        extra_instructions: Optional[str] = None,
    ):
        self.config = config or get_config()
        self.name = name
        self.user_id = user_id
        self.session_id = session_id

        # Legacy compat: model_id / extra_tools / extra_instructions
        _model = model or model_id
        _tools = tools or extra_tools
        _instructions = instructions or extra_instructions

        # Resolve model → Agno-native "provider:model_id" string
        self._model = _resolve_model(_model, provider, self.config)

        # Workspace
        _ws_dir = workspace_dir or self.config.workspace_dir
        self.workspace = Workspace(_ws_dir)
        self.workspace.initialize()

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
        return self._prompt_builder.build(
            skill_content=skill_content,
            extra_context=self._extra_instructions,
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

    def run(
        self,
        message: str,
        *,
        stream: bool = False,
        stream_events: bool = False,
        skill: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        **kwargs,
    ) -> Union[RunOutput, Iterator[RunOutputEvent]]:
        """
        Run the agent on a message.

        Args:
            message: The user message or task description.
            stream: If True, return a streaming iterator.
            stream_events: If True (and stream=True), yield full RunOutputEvent objects.
            skill: Skill name to activate for this run (loads SKILL.md content).
            session_id: Override session ID for this run.
            user_id: Override user ID for this run.
            **kwargs: Additional kwargs passed to Agno Agent.run().

        Returns:
            RunOutput (or Iterator[RunOutputEvent] if stream=True).
        """
        self._check_context_budget()

        skill_content = None
        if skill:
            skill_content = self.skills.load_skill(skill)
            if skill_content:
                self._set_system_prompt(
                    skill_content=skill_content,
                    session_id=session_id,
                )

        try:
            result = self._agent.run(
                message,
                stream=stream,
                stream_events=stream_events,
                session_id=session_id or self.session_id,
                user_id=user_id or self.user_id,
                **kwargs,
            )
        finally:
            if skill_content:
                self._set_system_prompt(session_id=self.session_id)

        self._maybe_optimize_memories()
        return result

    async def arun(
        self,
        message: str,
        *,
        stream: bool = False,
        stream_events: bool = False,
        skill: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        **kwargs,
    ):
        """Async version of run()."""
        self._check_context_budget()

        skill_content = None
        if skill:
            skill_content = self.skills.load_skill(skill)
            if skill_content:
                self._set_system_prompt(
                    skill_content=skill_content,
                    session_id=session_id,
                )

        try:
            result = await self._agent.arun(
                message,
                stream=stream,
                stream_events=stream_events,
                session_id=session_id or self.session_id,
                user_id=user_id or self.user_id,
                **kwargs,
            )
        finally:
            if skill_content:
                self._set_system_prompt(session_id=self.session_id)

        self._maybe_optimize_memories()
        return result

    def print_response(self, message: str, *, stream: bool = True, skill: Optional[str] = None, **kwargs) -> None:
        """Run the agent and pretty-print the response to the terminal."""
        skill_content = None
        if skill:
            skill_content = self.skills.load_skill(skill)
            if skill_content:
                self._set_system_prompt(skill_content=skill_content)

        try:
            self._agent.print_response(
                message,
                stream=stream,
                session_id=self.session_id,
                user_id=self.user_id,
                **kwargs,
            )
        finally:
            if skill_content:
                self._set_system_prompt(session_id=self.session_id)

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
        self._set_system_prompt(session_id=self.session_id)

    def exit_plan_mode(self) -> None:
        """Deactivate plan mode: restores normal system prompt."""
        self._plan_mode = False
        self._set_system_prompt(session_id=self.session_id)

    def add_tool(self, tool) -> None:
        """Add a tool or toolkit to the agent."""
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
        flush_prompt = (
            "SYSTEM: Context compaction is about to occur. Before your conversation "
            "history is cleared, write any important facts, decisions, code locations, "
            "or context that should persist to MEMORY.md. Be concise — only preserve "
            "what a future session would need to continue your work effectively."
        )
        await self._agent.arun(
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
        """Access the underlying Agno Agent for advanced use cases."""
        return self._agent


# Backward-compatible alias
HarnessAgent = AgentHarness
