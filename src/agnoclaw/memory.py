"""
Memory management utilities.

Agnoclaw uses two memory layers (plus in-context workspace files):

  1. Workspace files — plain Markdown, human-readable, cross-session
     (AGENTS.md, SOUL.md, USER.md, MEMORY.md, daily logs)
     Scope: per-workspace / per-user / per-project

  2. Agno LearningMachine — unified memory system handling both per-user
     facts AND institutional cross-user knowledge. Introduced in Agno v2.3.25
     as the successor to the older MemoryManager pattern.
     Scope: per-user AND global (namespaced per agent role)

Memory Hierarchy:
  ┌───────────────────────────────────────────────────────┐
  │ Session memory (in-context — current run only)        │
  ├───────────────────────────────────────────────────────┤
  │ Workspace files (Markdown — per-workspace)            │
  │  AGENTS.md · SOUL.md · USER.md · MEMORY.md           │
  ├───────────────────────────────────────────────────────┤
  │ LearningMachine (SQL — unified memory)                │
  │  Per-user stores (when enable_user_memory=True):      │
  │    user_profile     — structured profile fields       │
  │    user_memory      — unstructured observations       │
  │  Institutional stores (when enable_learning=True):    │
  │    learned_knowledge — reusable insights/patterns     │
  │    entity_memory     — facts about projects/tools     │
  │    decision_log      — consequential decisions        │
  │  Optional:                                            │
  │    session_context   — goals, plans, progress         │
  └───────────────────────────────────────────────────────┘

LearningMachine Store Roles:
  - user_profile: Structured per-user fields (name, preferences, custom).
  - user_memory: Unstructured per-user observations (conversations, habits).
    These two replace the older MemoryManager with better extraction prompts
    and the Curator for memory maintenance.
  - learned_knowledge: Institutional patterns — "always validate X before Y".
  - entity_memory: Named entities — projects, tools, APIs, people.
  - decision_log: WHY decisions were made. Prevents re-litigating.
  - session_context: Cross-session goals/plans. Higher noise, opt-in.

LearningMachine API Notes:
  LearningMachine is a dataclass — each store is configured individually
  via its own config object (EntityMemoryConfig, LearnedKnowledgeConfig,
  DecisionLogConfig, SessionContextConfig). There is NO global mode= param;
  mode is set per-store through the individual config objects.
"""

from __future__ import annotations

from typing import Optional


def build_memory_manager(
    model_id: str = "claude-haiku-4-5-20251001",
    provider: str = "anthropic",
    db=None,
    extra_instructions: Optional[str] = None,
):
    """
    Build an Agno MemoryManager for structured per-user cross-session memory.

    .. deprecated::
        Use ``build_learning_machine(enable_user_memory=True)`` instead.
        LearningMachine's user_memory and user_profile stores are a strict
        superset of MemoryManager with better extraction prompts and the
        Curator for memory maintenance.

    Args:
        model_id: Model to use for memory extraction.
        provider: Model provider.
        db: Agno database backend (SqliteDb or PostgresDb).
        extra_instructions: Additional instructions for what to remember.

    Returns:
        Configured MemoryManager instance.
    """
    import warnings
    warnings.warn(
        "build_memory_manager() is deprecated. Use build_learning_machine("
        "enable_user_memory=True) instead — LearningMachine's user_memory "
        "store is a strict superset with better prompts and memory maintenance.",
        DeprecationWarning,
        stacklevel=2,
    )

    from agno.memory import MemoryManager

    from .agent import _resolve_model
    from .config import get_config
    model = _resolve_model(model_id, provider, get_config())

    instructions = """
Capture the following types of information:
- User preferences (communication style, language choices, tool preferences)
- Project-specific conventions (testing patterns, naming conventions, architecture decisions)
- Goals and priorities (what the user is working toward)
- Important context (timezone, team structure, recurring workflows)

Do NOT capture:
- Session-specific task state (what was done in this session)
- Information easily found in the codebase
- Temporary context that won't be relevant next session
"""
    if extra_instructions:
        instructions += f"\n\nAdditional instructions:\n{extra_instructions}"

    kwargs = {"model": model, "additional_instructions": instructions}
    if db is not None:
        kwargs["db"] = db

    return MemoryManager(**kwargs)


def build_learning_machine(
    db=None,
    model_id: str = "claude-haiku-4-5-20251001",
    provider: str = "anthropic",
    namespace: Optional[str] = None,
    mode: str = "agentic",
    enable_user_memory: bool = False,
    enable_session_context: bool = False,
):
    """
    Build an Agno LearningMachine — the unified memory system.

    LearningMachine handles both per-user memory (user_profile, user_memory)
    and institutional cross-user knowledge (learned_knowledge, entity_memory,
    decision_log). It replaces the older MemoryManager with better extraction
    prompts and the Curator for memory maintenance.

    Each store is configured individually with its own LearningMode.
    There is NO global mode parameter — modes are set per-store.

    Institutional stores (always enabled):
    - learned_knowledge — reusable insights discovered through experience
    - entity_memory     — facts about named entities (projects, tools, APIs)
    - decision_log      — record of consequential decisions and their rationale

    Per-user stores (opt-in via enable_user_memory=True):
    - user_profile      — structured profile fields (name, preferences)
    - user_memory       — unstructured observations about users

    Optional:
    - session_context   — goals, plans, progress; opt in via enable_session_context

    Learning Modes:
    - 'always'  — extract and store learnings after every run (highest coverage)
    - 'agentic' — agent decides when to record learnings (balanced, recommended)
    - 'propose' — learnings proposed to human for review before storing
    - 'hitl'    — human must approve each learning (maximum control)

    Args:
        db: Agno database backend. Uses SqliteDb if not provided.
        model_id: Model for learning extraction (cheap model recommended).
        provider: Model provider.
        namespace: Optional namespace to isolate learnings by agent role
                   (e.g. "research", "code-review", "heartbeat").
                   Prevents cross-contamination between different agent purposes.
        mode: Learning mode applied to entity_memory and decision_log stores:
              'always' | 'agentic' | 'propose' | 'hitl'.
              learned_knowledge always uses 'agentic' (knowledge is selective).
        enable_user_memory: Enable per-user stores (user_profile + user_memory).
                           Replaces the deprecated MemoryManager.
        enable_session_context: Include session_context store (higher noise,
                                useful for long-running agents).

    Returns:
        Configured LearningMachine instance.

    Example:
        # Institutional memory only (cross-user knowledge)
        lm = build_learning_machine(db=my_db)

        # Full memory: institutional + per-user
        lm = build_learning_machine(db=my_db, enable_user_memory=True)

        # Via AgentHarness (recommended)
        agent = AgentHarness(
            enable_learning=True,
            enable_user_memory=True,
            learning_namespace="code-review",
        )
    """
    from agno.learn import LearningMachine, LearningMode
    from agno.learn.config import (
        DecisionLogConfig,
        EntityMemoryConfig,
        LearnedKnowledgeConfig,
    )
    from agno.models.utils import get_model

    from .agent import _resolve_model
    from .config import get_config

    mode_map = {
        "always": LearningMode.ALWAYS,
        "agentic": LearningMode.AGENTIC,
        "propose": LearningMode.PROPOSE,
        "hitl": LearningMode.HITL,
    }
    learning_mode = mode_map.get(mode)
    if learning_mode is None:
        import logging as _logging
        _logging.getLogger("agnoclaw.memory").warning(
            "Unknown learning mode '%s', falling back to 'agentic'. "
            "Valid modes: %s",
            mode, ", ".join(sorted(mode_map.keys())),
        )
        learning_mode = LearningMode.AGENTIC
    _namespace = namespace or "global"

    if db is None:
        from pathlib import Path

        from agno.db.sqlite import SqliteDb

        cfg = get_config()
        db_path = Path(cfg.storage.sqlite_path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = SqliteDb(db_file=str(db_path))

    model_ref = _resolve_model(model_id, provider, get_config())
    model = get_model(model_ref)

    # ── Per-store configs ──────────────────────────────────────────────────
    # entity_memory: tracks named entities with configurable mode
    entity_config = EntityMemoryConfig(
        db=db,
        model=model,
        mode=learning_mode,
        namespace=_namespace,
        enable_agent_tools=True,
    )

    # learned_knowledge: always AGENTIC — knowledge capture is selective by nature
    # Note: LearnedKnowledgeConfig doesn't take db= (uses LearningMachine's db)
    learned_config = LearnedKnowledgeConfig(
        model=model,
        mode=LearningMode.AGENTIC,
        namespace=_namespace,
        enable_agent_tools=True,
    )

    # decision_log: tracks WHY decisions were made, configurable mode
    decision_config = DecisionLogConfig(
        db=db,
        model=model,
        mode=learning_mode,
        enable_agent_tools=True,
    )

    kwargs = dict(
        db=db,
        model=model,
        namespace=_namespace,
        # Per-user stores — enabled when enable_user_memory=True
        user_profile=enable_user_memory,
        user_memory=enable_user_memory,
        # Institutional stores
        entity_memory=entity_config,
        learned_knowledge=learned_config,
        decision_log=decision_config,
    )

    if enable_session_context:
        from agno.learn.config import SessionContextConfig

        kwargs["session_context"] = SessionContextConfig(
            db=db,
            model=model,
            mode=LearningMode.ALWAYS,
            enable_planning=True,
        )

    return LearningMachine(**kwargs)
