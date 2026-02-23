"""
Memory management utilities.

Agnoclaw uses two memory layers:
  1. Workspace files — plain Markdown, human-readable, cross-session
     (AGENTS.md, SOUL.md, USER.md, MEMORY.md, daily logs)

  2. Agno MemoryManager — structured extraction + SQLite/Postgres storage,
     per-user fact extraction, auto-injection into context

This module provides helpers for building Agno MemoryManager instances
and for reading/writing workspace memory files.
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
    Build an Agno MemoryManager for structured cross-session memory.

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
