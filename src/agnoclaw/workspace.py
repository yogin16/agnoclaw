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
  IDENTITY.md  — detailed agent identity, capabilities, and self-description
  TOOLS.md     — workspace-specific tool configuration and overrides
  HEARTBEAT.md — heartbeat checklist (what to check on each heartbeat)
  BOOT.md      — startup sequence: commands to run and checks to perform
  skills/      — workspace-specific skill overrides (highest priority)
  memory/      — daily memory logs (YYYY-MM-DD.md)
  sessions/    — session transcripts (optional local backup)

Context loading order (loaded into system prompt):
  AGENTS.md → SOUL.md → IDENTITY.md → USER.md → MEMORY.md → TOOLS.md → BOOT.md

Size limits (matching Claude Code / OpenClaw conventions):
  MEMORY.md — only first MEMORY_STARTUP_LINES (200) lines loaded at startup.
              Content beyond line 200 is not injected into context automatically.
              Use separate topic files (debugging.md, patterns.md) for detailed notes.
  Per file — capped at BOOTSTRAP_MAX_CHARS (20,000 chars) before injection.
  Total    — all files combined capped at BOOTSTRAP_TOTAL_MAX_CHARS (150,000 chars).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional


# ── Context injection size limits ──────────────────────────────────────────
# Matching Claude Code auto-memory and OpenClaw bootstrap conventions.

MEMORY_STARTUP_LINES: int = 200
"""Only the first 200 lines of MEMORY.md are loaded at startup.
Keep MEMORY.md as an index; move detailed notes into topic files."""

BOOTSTRAP_MAX_CHARS: int = 20_000
"""Maximum characters for any single workspace file before injection."""

BOOTSTRAP_TOTAL_MAX_CHARS: int = 150_000
"""Maximum total characters for all workspace context files combined."""


WORKSPACE_FILES = {
    "agents": "AGENTS.md",
    "soul": "SOUL.md",
    "identity": "IDENTITY.md",
    "user": "USER.md",
    "memory": "MEMORY.md",
    "tools": "TOOLS.md",
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
    Represents an agent workspace directory with hierarchical parent chain.

    The workspace contains context files (AGENTS.md, SOUL.md, USER.md, MEMORY.md)
    that shape agent behavior, plus a skills/ subdirectory for workspace-level skill overrides.

    Hierarchy (child files override parent):
      global (~/.agnoclaw/global) → project (.agnoclaw/) → workspace (~/.agnoclaw/workspace)

    When reading context files, the workspace checks its own directory first,
    then falls through to the project-level directory, then to the global directory.
    This enables organizations to set global defaults, projects to override them,
    and individual workspaces to further customize.
    """

    def __init__(
        self,
        path: Optional[str | Path] = None,
        global_dir: Optional[str | Path] = None,
        project_dir: Optional[str | Path] = None,
    ):
        if path is None:
            path = Path.home() / ".agnoclaw" / "workspace"
        self.path = Path(path).expanduser().resolve()

        # Hierarchical parent chain
        self._global_dir: Optional[Path] = None
        self._project_dir: Optional[Path] = None

        if global_dir:
            gd = Path(global_dir).expanduser().resolve()
            if gd.exists():
                self._global_dir = gd

        if project_dir:
            pd = Path(project_dir).expanduser().resolve()
            if pd.exists():
                self._project_dir = pd

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
        """Read a workspace file by logical name or filename. Returns None if not found.

        Checks the hierarchical chain: workspace → project → global.
        The first directory that contains the file wins.

        MEMORY.md is automatically capped at MEMORY_STARTUP_LINES (200) lines on read.
        This matches Claude Code's auto-memory behaviour: the first 200 lines of MEMORY.md
        are loaded at startup; content beyond line 200 is not injected into context.
        Keep MEMORY.md concise — move detailed notes into separate topic files.
        """
        filename = WORKSPACE_FILES.get(name, name)

        # Check hierarchy: workspace (highest) → project → global (lowest)
        search_dirs = [self.path]
        if self._project_dir:
            search_dirs.append(self._project_dir)
        if self._global_dir:
            search_dirs.append(self._global_dir)

        for search_dir in search_dirs:
            path = search_dir / filename
            if path.exists():
                content = path.read_text(encoding="utf-8")
                # MEMORY.md startup cap: first 200 lines only
                if name == "memory" or filename == "MEMORY.md":
                    lines = content.splitlines()
                    if len(lines) > MEMORY_STARTUP_LINES:
                        content = "\n".join(lines[:MEMORY_STARTUP_LINES])
                content = content.strip()
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
        """
        Load all existing workspace context files. Returns {logical_name: content}.

        Loading order: AGENTS.md → SOUL.md → IDENTITY.md → USER.md → MEMORY.md → TOOLS.md → BOOT.md
        BOOT.md is returned last so the agent acts on it at session start.

        Size limits applied (matching OpenClaw bootstrap conventions):
        - Per file: capped at BOOTSTRAP_MAX_CHARS (20,000 chars)
        - Total: capped at BOOTSTRAP_TOTAL_MAX_CHARS (150,000 chars)
        - MEMORY.md: additionally capped at MEMORY_STARTUP_LINES (200) lines
          by read_file()

        Files that would exceed the total budget are skipped (lower-priority files
        are loaded first; later files drop out if budget is exhausted).
        """
        result = {}
        total_chars = 0

        for name in ("agents", "soul", "identity", "user", "memory", "tools", "boot"):
            content = self.read_file(name)
            if not content:
                continue
            # Per-file cap
            if len(content) > BOOTSTRAP_MAX_CHARS:
                content = content[:BOOTSTRAP_MAX_CHARS]
            # Total budget check
            if total_chars + len(content) > BOOTSTRAP_TOTAL_MAX_CHARS:
                # Remaining budget
                remaining = BOOTSTRAP_TOTAL_MAX_CHARS - total_chars
                if remaining > 0:
                    result[name] = content[:remaining]
                break  # budget exhausted
            result[name] = content
            total_chars += len(content)

        return result

    def write_session_summary(self, summary: str) -> None:
        """Write a session summary to today's daily log (used for context compaction)."""
        from datetime import datetime
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_to_daily(f"## Session Summary [{timestamp}]\n\n{summary}")

    def __repr__(self) -> str:
        return f"Workspace({self.path})"
