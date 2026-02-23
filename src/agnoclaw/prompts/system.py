"""
System prompt assembler.

Builds the final system prompt by layering sections in order:
  1. Identity (with workspace path injected)
  2. Tone & Style
  3. Doing Tasks
  4. Tool Guidelines
  5. Security
  6. Git Protocol
  7. Memory Instructions
  8. Skill Instructions
  9. Workspace context files (AGENTS.md, SOUL.md, USER.md, MEMORY.md)
 10. Active skill content (selective — one at a time)
 11. Runtime reminders (date, session info)

This layered approach lets any section be overridden per-workspace or per-skill.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from .sections import (
    DOING_TASKS,
    GIT_PROTOCOL,
    IDENTITY,
    LEARNING_INSTRUCTIONS,
    MEMORY_INSTRUCTIONS,
    PLAN_MODE,
    SECURITY,
    SKILL_INSTRUCTIONS,
    TONE_AND_STYLE,
    TOOL_GUIDELINES,
)


class SystemPromptBuilder:
    """Assembles the full system prompt from layered sections."""

    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
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
            session_id: Active session ID (injected into runtime context).
        """
        parts: list[str] = []

        # 1-8: Core sections
        parts.append(IDENTITY.format(workspace_dir=self.workspace_dir))
        parts.append(TONE_AND_STYLE)
        parts.append(DOING_TASKS)
        parts.append(TOOL_GUIDELINES)
        parts.append(SECURITY)
        parts.append(GIT_PROTOCOL)
        parts.append(MEMORY_INSTRUCTIONS)
        parts.append(SKILL_INSTRUCTIONS)

        # 9: Plan mode (optional — enabled when entering plan mode)
        if include_plan_mode:
            parts.append(PLAN_MODE)

        # 10: Learning instructions (only injected when LearningMachine is active)
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
        for filename, label in files:
            path = self.workspace_dir / filename
            if path.exists():
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    loaded.append(f"## {label}\n\n{content}")

        if not loaded:
            return None

        return "# Workspace Context\n\n" + "\n\n".join(loaded)
