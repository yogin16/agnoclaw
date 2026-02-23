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
  │  user_profile · entity_memory · learned_knowledge     │
  │  decision_log · session_context                       │
  └───────────────────────────────────────────────────────┘
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

    from .agent import _make_model

    model = _make_model(model_id, provider)

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
):
    """
    Build an Agno LearningMachine for institutional cross-user memory.

    LearningMachine accumulates patterns, conventions, and insights that
    persist across ALL sessions and ALL users — forming the agent's
    institutional memory. Unlike MemoryManager (per-user facts), learnings
    are shared globally (or within a namespace).

    Components stored:
    - user_profile     — aggregated user behavior and preference patterns
    - entity_memory    — facts about named entities (people, projects, tools)
    - learned_knowledge — reusable insights discovered through experience
    - decision_log     — record of consequential decisions and their outcomes
    - session_context  — cross-session contextual patterns

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
        mode: Learning mode: 'always' | 'agentic' | 'propose' | 'hitl'.

    Returns:
        Configured LearningMachine instance.

    Example:
        # Shared institutional memory (all agents learn from all interactions)
        lm = build_learning_machine(db=my_db)

        # Isolated by role (research agent learns separately from code agent)
        research_lm = build_learning_machine(db=my_db, namespace="research")
        code_lm = build_learning_machine(db=my_db, namespace="code-review")

        agent = HarnessAgent(learning_machine=research_lm)
    """
    from agno.learn import LearningMachine, LearningMode
    from .agent import _make_model

    mode_map = {
        "always": LearningMode.ALWAYS,
        "agentic": LearningMode.AGENTIC,
        "propose": LearningMode.PROPOSE,
        "hitl": LearningMode.HITL,
    }
    learning_mode = mode_map.get(mode, LearningMode.AGENTIC)

    if db is None:
        from pathlib import Path
        from agno.db.sqlite import SqliteDb
        db_path = Path.home() / ".agnoclaw" / "sessions.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = SqliteDb(db_file=str(db_path))

    model = _make_model(model_id, provider)

    return LearningMachine(
        db=db,
        model=model,
        namespace=namespace,
    )


def build_culture_manager(
    db=None,
    model_id: str = "claude-haiku-4-5-20251001",
    provider: str = "anthropic",
    extra_instructions: Optional[str] = None,
):
    """
    Build an Agno CultureManager for team-level cultural knowledge.

    CultureManager captures team norms, conventions, and shared context
    that apply across all agents in a team. Think of it as the team's
    "operating principles" that emerge and evolve through usage.

    Best used with multi-agent teams (research_team, code_team) to
    accumulate team-specific conventions:
    - Code style decisions ("we use snake_case for all variables")
    - Review standards ("always check for N+1 queries")
    - Research protocols ("always cite primary sources")
    - Communication norms ("be direct, no fluff")

    Args:
        db: Agno database backend.
        model_id: Model for culture extraction.
        provider: Model provider.
        extra_instructions: Domain-specific culture capture instructions.

    Returns:
        Configured CultureManager instance.
    """
    from agno.culture import CultureManager
    from .agent import _make_model

    if db is None:
        from pathlib import Path
        from agno.db.sqlite import SqliteDb
        db_path = Path.home() / ".agnoclaw" / "sessions.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = SqliteDb(db_file=str(db_path))

    model = _make_model(model_id, provider)

    instructions = (extra_instructions or "") + """
Capture team norms, conventions, and shared principles:
- Technical standards (language choices, patterns, anti-patterns to avoid)
- Process conventions (how we structure reviews, how we document decisions)
- Communication norms (tone, format, level of detail expected)
- Quality standards (what constitutes "done", review checklist items)

Do NOT capture:
- Individual user preferences (use MemoryManager for that)
- Session-specific decisions (use decision_log in LearningMachine for that)
- Project-specific facts (use knowledge base for that)
"""

    return CultureManager(
        model=model,
        db=db,
        culture_capture_instructions=instructions,
    )
