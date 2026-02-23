"""
Workspace management.

The workspace is the agent's home directory — the single source of truth for
identity, memory, skills, and session state. Inspired by OpenClaw's workspace
design but plain Markdown, Python-native, and fully hackable.

Default location: ~/.agnoclaw/workspace/

Key files:
  AGENTS.md    — behavioral guidelines and memory usage instructions
  SOUL.md      — persona, tone, and identity boundaries
  USER.md      — user identity, timezone, communication preferences
  MEMORY.md    — long-term curated memory (cross-session)
  HEARTBEAT.md — heartbeat checklist (what to check on each heartbeat)
  BOOT.md      — optional startup sequence
  skills/      — workspace-specific skill overrides (highest priority)
  memory/      — daily memory logs (YYYY-MM-DD.md)
  sessions/    — session transcripts (optional local backup)
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional


WORKSPACE_FILES = {
    "agents": "AGENTS.md",
    "soul": "SOUL.md",
    "user": "USER.md",
    "memory": "MEMORY.md",
    "heartbeat": "HEARTBEAT.md",
    "boot": "BOOT.md",
}

DEFAULT_AGENTS_MD = """# Agent Guidelines

You are a capable, autonomous agent. Follow these behavioral guidelines:

- Read SOUL.md to understand your persona and tone.
- Read USER.md to understand the user's preferences and context.
- Read MEMORY.md for long-term context from previous sessions.
- Update MEMORY.md with important new information after each session.
- Keep your workspace organized and your memory files concise.
- Prefer reversible actions over destructive ones.
- When in doubt, ask the user rather than guessing.
"""

DEFAULT_SOUL_MD = """# Soul

You are a capable, direct, and thoughtful assistant. You:
- Give concise answers without unnecessary preamble
- Ask clarifying questions rather than making assumptions
- Acknowledge uncertainty rather than guessing confidently
- Respect the user's time and intelligence
"""

DEFAULT_HEARTBEAT_MD = """# Heartbeat Checklist

Check if any of the following need attention:
- [ ] Pending tasks or TODOs from previous sessions
- [ ] File system issues or disk usage concerns
- [ ] Any scheduled jobs or processes that may need attention

If nothing needs attention, reply HEARTBEAT_OK.
"""


class Workspace:
    """
    Represents an agent workspace directory.

    The workspace contains context files (AGENTS.md, SOUL.md, USER.md, MEMORY.md)
    that shape agent behavior, plus a skills/ subdirectory for workspace-level skill overrides.
    """

    def __init__(self, path: Optional[str | Path] = None):
        if path is None:
            path = Path.home() / ".agnoclaw" / "workspace"
        self.path = Path(path).expanduser().resolve()

    def initialize(self) -> None:
        """Create the workspace directory and default files if they don't exist."""
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "skills").mkdir(exist_ok=True)
        (self.path / "memory").mkdir(exist_ok=True)
        (self.path / "sessions").mkdir(exist_ok=True)

        # Create default files if missing
        self._create_if_missing("AGENTS.md", DEFAULT_AGENTS_MD)
        self._create_if_missing("SOUL.md", DEFAULT_SOUL_MD)
        self._create_if_missing("HEARTBEAT.md", DEFAULT_HEARTBEAT_MD)

    def _create_if_missing(self, filename: str, content: str) -> None:
        path = self.path / filename
        if not path.exists():
            path.write_text(content, encoding="utf-8")

    def read_file(self, name: str) -> Optional[str]:
        """Read a workspace file by logical name or filename. Returns None if not found."""
        filename = WORKSPACE_FILES.get(name, name)
        path = self.path / filename
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            return content if content else None
        return None

    def write_file(self, name: str, content: str) -> None:
        """Write a workspace file by logical name or filename."""
        filename = WORKSPACE_FILES.get(name, name)
        path = self.path / filename
        path.write_text(content, encoding="utf-8")

    def append_to_memory(self, content: str) -> None:
        """Append a note to MEMORY.md."""
        memory_path = self.path / "MEMORY.md"
        existing = memory_path.read_text(encoding="utf-8") if memory_path.exists() else "# Memory\n"
        memory_path.write_text(existing.rstrip() + "\n\n" + content.strip() + "\n", encoding="utf-8")

    def log_to_daily(self, content: str) -> None:
        """Write a log entry to today's daily memory file (memory/YYYY-MM-DD.md)."""
        today = date.today().isoformat()
        log_path = self.path / "memory" / f"{today}.md"
        existing = log_path.read_text(encoding="utf-8") if log_path.exists() else f"# {today}\n"
        log_path.write_text(existing.rstrip() + "\n\n" + content.strip() + "\n", encoding="utf-8")

    def skills_dir(self) -> Path:
        """Workspace-level skills directory (highest priority for skill loading)."""
        return self.path / "skills"

    def heartbeat_md(self) -> Optional[str]:
        """Read HEARTBEAT.md. Returns None if empty or only headers."""
        content = self.read_file("heartbeat")
        if content is None:
            return None
        # Skip if only whitespace and markdown headers
        meaningful = [
            line for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return content if meaningful else None

    def is_empty_heartbeat(self) -> bool:
        """Returns True if HEARTBEAT.md has no actionable content (skip to save cost)."""
        return self.heartbeat_md() is None

    def context_files(self) -> dict[str, str]:
        """Load all existing workspace context files. Returns {logical_name: content}."""
        result = {}
        for name in ("agents", "soul", "user", "memory"):
            content = self.read_file(name)
            if content:
                result[name] = content
        return result

    def __repr__(self) -> str:
        return f"Workspace({self.path})"
