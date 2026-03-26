"""
System prompt assembler.

Builds the final system prompt by layering sections in order:
  1. Identity (with workspace path injected)
  2. Tone & Style
  3. Communication Discipline (narration suppression)
  4. Doing Tasks
  5. Executing with Care (reversibility-based action policy)
  6. Blocked Approaches (anti-pattern directives)
  7. Tool Guidelines
  8. Security
  9. Git Protocol
 10. Memory Instructions
 11. Skill Instructions
 12. Plan Mode (optional)
 13. Heartbeat Context (optional)
 14. Learning Instructions (optional)
 15. Custom sections
 16. Workspace context files (AGENTS.md, SOUL.md, USER.md, MEMORY.md)
 17. Active skill content (selective — one at a time)
 18. Runtime reminders (date, session info)

This layered approach lets any section be overridden per-workspace or per-skill.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from ..workspace import BOOTSTRAP_MAX_CHARS, BOOTSTRAP_TOTAL_MAX_CHARS, MEMORY_STARTUP_LINES
from .sections import (
    BLOCKED_APPROACHES,
    DOING_TASKS,
    EXECUTING_WITH_CARE,
    GIT_PROTOCOL,
    HEARTBEAT_CONTEXT,
    IDENTITY,
    LEARNING_INSTRUCTIONS,
    MEMORY_INSTRUCTIONS,
    NARRATION,
    PLAN_MODE,
    SECURITY,
    SKILL_INSTRUCTIONS,
    TONE_AND_STYLE,
    TOOL_GUIDELINES,
)


class SystemPromptBuilder:
    """Assembles the full system prompt from layered sections."""

    def __init__(self, workspace_dir: Path, sandbox_dir: Path | None = None):
        self.workspace_dir = workspace_dir
        self.sandbox_dir = sandbox_dir
        self._custom_sections: list[str] = []

    def add_section(self, content: str) -> "SystemPromptBuilder":
        """Append a custom section (e.g. from enterprise config)."""
        self._custom_sections.append(content)
        return self

    def build(
        self,
        *,
        skill_content: Optional[str] = None,
        include_datetime: bool = True,
        extra_context: Optional[str] = None,
        include_learning: bool = False,
        include_plan_mode: bool = False,
        include_heartbeat: bool = False,
        session_id: Optional[str] = None,
    ) -> str:
        """
        Build the full system prompt string.

        Args:
            skill_content: Active skill's SKILL.md content (selective injection).
            include_datetime: Inject current date/time into context.
            extra_context: Additional instructions (enterprise config, project CLAUDE.md).
            include_learning: Include the Learning section (only when LearningMachine is active).
            include_plan_mode: Include plan mode instructions.
            include_heartbeat: Include heartbeat context instructions.
            session_id: Active session ID (injected into runtime context).
        """
        parts: list[str] = []

        # Core behavioral sections
        parts.append(IDENTITY.format(workspace_dir=self.workspace_dir))
        parts.append(TONE_AND_STYLE)
        parts.append(NARRATION)
        parts.append(DOING_TASKS)
        parts.append(EXECUTING_WITH_CARE)
        parts.append(BLOCKED_APPROACHES)
        parts.append(TOOL_GUIDELINES)
        parts.append(SECURITY)
        parts.append(GIT_PROTOCOL)
        parts.append(MEMORY_INSTRUCTIONS)
        parts.append(SKILL_INSTRUCTIONS)

        # Plan mode (optional — enabled when entering plan mode)
        if include_plan_mode:
            parts.append(PLAN_MODE)

        # Heartbeat context (prevents stale task carryover)
        if include_heartbeat:
            parts.append(HEARTBEAT_CONTEXT)

        # Learning instructions (only injected when LearningMachine is active)
        if include_learning:
            parts.append(LEARNING_INSTRUCTIONS)

        # 11: Custom enterprise/user sections
        parts.extend(self._custom_sections)

        # 12: Workspace context files (injected if they exist)
        # Order: AGENTS.md → SOUL.md → IDENTITY.md → USER.md → MEMORY.md → TOOLS.md → BOOT.md
        workspace_context = self._load_workspace_context()
        if workspace_context:
            parts.append(workspace_context)

        # 13: Active skill content
        if skill_content:
            parts.append(f"# Active Skill\n\n{skill_content}")

        # 14: Extra context (e.g. project CLAUDE.md contents)
        if extra_context:
            parts.append(f"# Project Context\n\n{extra_context}")

        # 15: Runtime reminders
        if include_datetime:
            now = datetime.now()
            runtime_lines = [
                f"Current date and time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
                f"Workspace: {self.workspace_dir}",
            ]
            if self.sandbox_dir is not None:
                runtime_lines.append(f"Session sandbox: {self.sandbox_dir}")
            if session_id:
                runtime_lines.append(f"Session ID: {session_id}")
            parts.append("# Runtime\n\n" + "\n".join(runtime_lines))

        return "\n\n---\n\n".join(parts)

    def _load_workspace_context(self) -> Optional[str]:
        """
        Load and concatenate workspace context files if they exist.

        Loading order: AGENTS.md → SOUL.md → IDENTITY.md → USER.md →
                       MEMORY.md → TOOLS.md → BOOT.md
        """
        files = [
            ("AGENTS.md", "Agent Guidelines (AGENTS.md)"),
            ("SOUL.md", "Persona (SOUL.md)"),
            ("IDENTITY.md", "Identity (IDENTITY.md)"),
            ("USER.md", "User Preferences (USER.md)"),
            ("MEMORY.md", "Long-term Memory (MEMORY.md)"),
            ("TOOLS.md", "Tool Configuration (TOOLS.md)"),
            ("BOOT.md", "Startup Protocol (BOOT.md)"),
        ]

        loaded: list[str] = []
        total_chars = 0
        for filename, label in files:
            path = self.workspace_dir / filename
            if path.exists():
                content = path.read_text(encoding="utf-8").strip()

                # Keep MEMORY.md as an index: only first N lines are injected.
                if filename == "MEMORY.md":
                    lines = content.splitlines()
                    if len(lines) > MEMORY_STARTUP_LINES:
                        content = "\n".join(lines[:MEMORY_STARTUP_LINES])

                # Hard per-file cap to avoid context blowups.
                if len(content) > BOOTSTRAP_MAX_CHARS:
                    content = content[:BOOTSTRAP_MAX_CHARS]

                # Enforce global workspace bootstrap budget.
                if total_chars + len(content) > BOOTSTRAP_TOTAL_MAX_CHARS:
                    remaining = BOOTSTRAP_TOTAL_MAX_CHARS - total_chars
                    if remaining <= 0:
                        break
                    content = content[:remaining]

                if content:
                    loaded.append(f"## {label}\n\n{content}")
                    total_chars += len(content)
                    if total_chars >= BOOTSTRAP_TOTAL_MAX_CHARS:
                        break

        if not loaded:
            return None

        return "# Workspace Context\n\n" + "\n\n".join(loaded)
