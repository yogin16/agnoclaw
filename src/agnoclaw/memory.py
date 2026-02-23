"""
Memory management utilities.

Agnoclaw uses three memory layers:

  1. Workspace files — plain Markdown, human-readable, cross-session
     (AGENTS.md, SOUL.md, USER.md, MEMORY.md, daily logs)
     Scope: per-workspace / per-user / per-project

  2. Agno MemoryManager — structured fact extraction per user_id,
     stored in SQLite/Postgres, auto-injected into context.
     Scope: per-user, cross-session

  3. Agno LearningMachine — institutional, cross-user, cross-session
     knowledge. Patterns, conventions, insights that accumulate over time
     and benefit all users of the harness.
     Scope: global (or namespaced per agent role)

Memory Hierarchy:
  ┌───────────────────────────────────────────────────────┐
  │ Session memory (in-context — current run only)        │
  ├───────────────────────────────────────────────────────┤
  │ Workspace files (Markdown — per-workspace)            │
  │  AGENTS.md · SOUL.md · USER.md · MEMORY.md           │
  ├───────────────────────────────────────────────────────┤
  │ MemoryManager (SQL — per-user facts, preferences)     │
  ├───────────────────────────────────────────────────────┤
  │ LearningMachine (SQL — institutional / cross-user)    │
  │  Stores enabled by default:                           │
  │    learned_knowledge — reusable insights/patterns     │
  │    entity_memory     — facts about projects/tools     │
  │    decision_log      — consequential decisions        │
  │  Stores excluded (per-user, not institutional):       │
  │    user_profile      — use MemoryManager instead      │
  │    user_memory       — use MemoryManager instead      │
  └───────────────────────────────────────────────────────┘

LearningMachine Store Selection Rationale:
  - learned_knowledge: Core institutional store. Captures patterns like
    "always validate X before calling Y", "team prefers Z pattern". High
    value across all agent types.
  - entity_memory: Tracks named entities — projects, tools, APIs, people.
    Enables "we use pytest in this project", "Alice owns the auth service".
    Scoped globally or by namespace.
  - decision_log: Captures WHY decisions were made. "Chose PostgreSQL over
    SQLite because of concurrent writes". Prevents re-litigating decisions.
  - user_profile: EXCLUDED from LearningMachine. This is per-user data —
    use MemoryManager (Tier 2) for user preferences/facts.
  - user_memory: EXCLUDED — use MemoryManager instead.
  - session_context: Cross-session patterns. Useful but high noise.
    Omitted by default; opt in via enable_session_context=True.

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

    Uses a cheap model (Haiku by default) for memory extraction.
    Stores facts per user_id in the configured database.

    Args:
        model_id: Model to use for memory extraction.
        provider: Model provider.
        db: Agno database backend (SqliteDb or PostgresDb).
        extra_instructions: Additional instructions for what to remember.

    Returns:
        Configured MemoryManager instance.
    """
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
    enable_session_context: bool = False,
):
    """
    Build an Agno LearningMachine for institutional cross-user memory.

    LearningMachine accumulates patterns, conventions, and insights that
    persist across ALL sessions and ALL users — forming the agent's
    institutional memory. Unlike MemoryManager (per-user facts), learnings
    are shared globally (or within a namespace).

    Each store is configured individually with its own LearningMode.
    There is NO global mode parameter — modes are set per-store.

    Enabled stores (see module docstring for rationale):
    - learned_knowledge — reusable insights discovered through experience
    - entity_memory     — facts about named entities (projects, tools, APIs)
    - decision_log      — record of consequential decisions and their rationale

    Excluded stores:
    - user_profile      — per-user data; use MemoryManager (Tier 2) instead
    - user_memory       — per-user data; use MemoryManager (Tier 2) instead
    - session_context   — high noise; opt in via enable_session_context=True

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
        enable_session_context: Include session_context store (higher noise,
                                useful for long-running agents).

    Returns:
        Configured LearningMachine instance.

    Example:
        # Shared institutional memory (all agents learn from all interactions)
        lm = build_learning_machine(db=my_db)

        # Isolated by role (research agent learns separately from code agent)
        research_lm = build_learning_machine(db=my_db, namespace="research")
        code_lm = build_learning_machine(db=my_db, namespace="code-review")

        agent = HarnessAgent(enable_learning=True, learning_namespace="code-review")
    """
    from agno.learn import LearningMachine, LearningMode
    from agno.learn.config import (
        DecisionLogConfig,
        EntityMemoryConfig,
        LearnedKnowledgeConfig,
    )

    from .agent import _resolve_model
    from .config import get_config

    mode_map = {
        "always": LearningMode.ALWAYS,
        "agentic": LearningMode.AGENTIC,
        "propose": LearningMode.PROPOSE,
        "hitl": LearningMode.HITL,
    }
    learning_mode = mode_map.get(mode, LearningMode.AGENTIC)
    _namespace = namespace or "global"

    if db is None:
        from pathlib import Path

        from agno.db.sqlite import SqliteDb

        db_path = Path.home() / ".agnoclaw" / "sessions.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = SqliteDb(db_file=str(db_path))

    model = _resolve_model(model_id, provider, get_config())

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
        # Exclude per-user stores — handled by MemoryManager (Tier 2)
        user_profile=False,
        user_memory=False,
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
