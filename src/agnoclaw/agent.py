"""
HarnessAgent — the central class of agnoclaw.

Wraps Agno's Agent with:
  - Claude Code-inspired system prompt (layered, assembled at runtime)
  - Workspace awareness (AGENTS.md, SOUL.md, USER.md, MEMORY.md)
  - Skill injection (selective — one SKILL.md at a time)
  - Default tool suite (bash, files, web, tasks, subagent)
  - Persistent session storage (SQLite or Postgres)
  - Multi-provider model support (any Agno-supported model)
  - Three-tier memory: workspace files → MemoryManager → LearningMachine

Memory tiers:
  1. Workspace files (Markdown) — human-readable, per-workspace context
  2. MemoryManager — structured per-user facts, extracted and stored in SQL
  3. LearningMachine — institutional cross-user knowledge that accumulates
     over time (patterns, conventions, learnings from experience)

Usage:
    from agnoclaw import HarnessAgent

    # Basic
    agent = HarnessAgent()
    agent.print_response("Find and fix the bug in src/auth.py")

    # With learning enabled (institutional memory)
    agent = HarnessAgent(enable_learning=True, learning_mode="agentic")

    # With per-user memory + learning
    agent = HarnessAgent(
        user_id="alice",
        enable_user_memory=True,
        enable_learning=True,
        learning_namespace="code-review",
    )
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Optional, Union

from agno.agent import Agent
from agno.run.agent import RunOutput, RunOutputEvent

from .config import HarnessConfig, get_config
from .prompts.system import SystemPromptBuilder
from .skills.registry import SkillRegistry
from .tools import get_default_tools
from .workspace import Workspace


def _make_model(model_id: str, provider: str) -> Any:
    """
    Instantiate an Agno model from provider name and model ID.
    Returns the appropriate Agno model class instance.
    """
    provider = provider.lower()

    if provider == "anthropic":
        from agno.models.anthropic import Claude
        return Claude(id=model_id)
    elif provider == "openai":
        from agno.models.openai import OpenAIChat
        return OpenAIChat(id=model_id)
    elif provider == "google":
        from agno.models.google import Gemini
        return Gemini(id=model_id)
    elif provider == "groq":
        from agno.models.groq import Groq
        return Groq(id=model_id)
    elif provider == "ollama":
        from agno.models.ollama import Ollama
        return Ollama(id=model_id)
    elif provider == "aws" or provider == "bedrock":
        from agno.models.aws.bedrock import AwsBedrock
        return AwsBedrock(id=model_id)
    elif provider == "mistral":
        from agno.models.mistral import MistralChat
        return MistralChat(id=model_id)
    elif provider == "xai" or provider == "grok":
        from agno.models.xai import xAI
        return xAI(id=model_id)
    elif provider == "deepseek":
        from agno.models.deepseek import DeepSeek
        return DeepSeek(id=model_id)
    elif provider == "litellm":
        from agno.models.litellm import LiteLLM
        return LiteLLM(id=model_id)
    else:
        raise ValueError(
            f"Unknown provider '{provider}'. "
            f"Supported: anthropic, openai, google, groq, ollama, aws, mistral, xai, deepseek, litellm"
        )


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


class HarnessAgent:
    """
    A hackable, model-agnostic agent harness built on Agno.

    HarnessAgent is the primary public interface. It wires together:
    - System prompt assembly (Claude Code-inspired, layered)
    - Workspace (persistent Markdown context files)
    - Skills (SKILL.md selective injection)
    - Default tools (bash, files, web, tasks, subagent)
    - Agno Agent (model invocation, tool calling, storage, streaming)

    Args:
        model_id: Model identifier (e.g. "claude-sonnet-4-6", "gpt-4o", "llama3.2").
        provider: Model provider name (e.g. "anthropic", "openai", "ollama").
        session_id: Session ID for persistence. Auto-generated if not provided.
        user_id: User identifier for per-user memory.
        workspace_dir: Workspace path override. Defaults to ~/.agnoclaw/workspace.
        extra_tools: Additional tools to add alongside the defaults.
        extra_instructions: Additional instructions appended to the system prompt.
        config: HarnessConfig override. Loaded from env/TOML if not provided.
        name: Agent name (cosmetic).
        agent_id: Stable agent ID (cosmetic, used in logs).
        debug: Enable debug mode (show tool calls, verbose output).
    """

    def __init__(
        self,
        model_id: Optional[str] = None,
        provider: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_dir: Optional[str | Path] = None,
        extra_tools: Optional[list] = None,
        extra_instructions: Optional[str] = None,
        config: Optional[HarnessConfig] = None,
        name: str = "agnoclaw",
        agent_id: Optional[str] = None,
        debug: bool = False,
        # Memory options
        enable_user_memory: bool = False,
        enable_learning: Optional[bool] = None,
        learning_mode: Optional[str] = None,
        learning_namespace: Optional[str] = None,
        enable_culture: Optional[bool] = None,
    ):
        self.config = config or get_config()
        self.name = name
        self.user_id = user_id
        self.session_id = session_id

        # Resolve model
        _model_id = model_id or self.config.default_model
        _provider = provider or self.config.default_provider
        self._model = _make_model(_model_id, _provider)

        # Workspace
        _ws_dir = workspace_dir or self.config.workspace_dir
        self.workspace = Workspace(_ws_dir)
        self.workspace.initialize()

        # Skills registry
        self.skills = SkillRegistry(self.workspace.skills_dir())

        # System prompt builder
        self._prompt_builder = SystemPromptBuilder(self.workspace.path)

        # Build tool list
        tools = get_default_tools(self.config)
        if extra_tools:
            tools.extend(extra_tools)

        # Resolve learning flags before building system prompt
        _enable_learning = enable_learning if enable_learning is not None else self.config.enable_learning
        _learning_mode = learning_mode or self.config.learning_mode
        _enable_culture = enable_culture if enable_culture is not None else self.config.enable_culture

        # Assemble system prompt (initial — skills injected dynamically)
        system_prompt = self._prompt_builder.build(
            extra_context=extra_instructions,
            include_learning=_enable_learning,
        )

        # Storage backend
        db = _make_db(self.config)

        # ── Memory tier 2: per-user MemoryManager ─────────────────────────
        memory_manager = None
        if enable_user_memory:
            from .memory import build_memory_manager
            memory_manager = build_memory_manager(db=db)

        # ── Memory tier 3: LearningMachine ────────────────────────────────
        # Institutional cross-user knowledge — patterns, conventions, insights
        learning = None
        if _enable_learning:
            from .memory import build_learning_machine
            learning = build_learning_machine(
                db=db,
                namespace=learning_namespace or name,
                mode=_learning_mode,
            )

        culture_manager = None
        if _enable_culture:
            from .memory import build_culture_manager
            culture_manager = build_culture_manager(db=db)

        # Core Agno Agent
        self._agent = Agent(
            model=self._model,
            name=name,
            id=agent_id,
            system_message=system_prompt,
            tools=tools,
            db=db,
            session_id=session_id,
            user_id=user_id,
            add_history_to_context=True,
            num_history_runs=self.config.session_history_runs,
            markdown=True,
            debug_mode=debug or self.config.debug,
            # Memory tier 2: per-user structured facts
            memory_manager=memory_manager,
            enable_agentic_memory=enable_user_memory,
            add_memories_to_context=enable_user_memory,
            # Memory tier 3: institutional cross-user knowledge
            learning=learning,
            add_learnings_to_context=_enable_learning,
            culture_manager=culture_manager,
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
        # Inject skill content if specified
        if skill:
            skill_content = self.skills.load_skill(skill)
            if skill_content:
                # Rebuild system prompt with active skill injected
                system_prompt = self._prompt_builder.build(skill_content=skill_content)
                self._agent.system_message = system_prompt

        return self._agent.run(
            message,
            stream=stream,
            stream_events=stream_events,
            session_id=session_id or self.session_id,
            user_id=user_id or self.user_id,
            **kwargs,
        )

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
        if skill:
            skill_content = self.skills.load_skill(skill)
            if skill_content:
                system_prompt = self._prompt_builder.build(skill_content=skill_content)
                self._agent.system_message = system_prompt

        return await self._agent.arun(
            message,
            stream=stream,
            stream_events=stream_events,
            session_id=session_id or self.session_id,
            user_id=user_id or self.user_id,
            **kwargs,
        )

    def print_response(self, message: str, *, stream: bool = True, skill: Optional[str] = None, **kwargs) -> None:
        """Run the agent and pretty-print the response to the terminal."""
        if skill:
            skill_content = self.skills.load_skill(skill)
            if skill_content:
                system_prompt = self._prompt_builder.build(skill_content=skill_content)
                self._agent.system_message = system_prompt
        self._agent.print_response(
            message,
            stream=stream,
            session_id=self.session_id,
            user_id=self.user_id,
            **kwargs,
        )

    def add_tool(self, tool) -> None:
        """Add a tool or toolkit to the agent."""
        self._agent.add_tool(tool)

    def get_chat_history(self) -> list:
        """Return the chat history for the current session."""
        return self._agent.get_chat_history(self.session_id or "")

    @property
    def underlying_agent(self) -> Agent:
        """Access the underlying Agno Agent for advanced use cases."""
        return self._agent
