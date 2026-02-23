"""
Skill registry — discovery, loading, and selective injection.

Skill precedence (highest → lowest):
  1. Workspace skills dir   (~/.agnoclaw/workspace/skills/)
  2. User skills dir        (~/.agnoclaw/skills/)
  3. Extra configured dirs  (from config.skills_dirs)
  4. Bundled skills         (shipped with agnoclaw package)

Selective injection principle (from OpenClaw):
  Before responding, the agent scans available skill descriptions.
  If exactly one skill clearly applies: load its SKILL.md and follow it.
  If multiple could apply: choose the most specific one.
  If none apply: don't load any.
  Never load more than one skill per turn.

This keeps context lean and avoids prompt bloat.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from .loader import Skill, load_skill_from_path


class SkillRegistry:
    """
    Discovers and manages skills from multiple directories.

    Skills are loaded lazily (on demand) to avoid prompt bloat.
    Only the content of the selected skill is injected per turn.
    """

    def __init__(self, workspace_skills_dir: Optional[Path] = None):
        self._dirs: list[Path] = []
        self._cache: dict[str, Skill] = {}

        # Build search path (highest → lowest priority)
        if workspace_skills_dir:
            self._dirs.append(workspace_skills_dir)

        user_skills = Path.home() / ".agnoclaw" / "skills"
        if user_skills.exists():
            self._dirs.append(user_skills)

        # Bundled skills (relative to this package)
        bundled = self._find_bundled_skills_dir()
        if bundled:
            self._dirs.append(bundled)

    def add_directory(self, path: str | Path) -> None:
        """Add an additional skills directory (appended at lowest priority)."""
        p = Path(path).expanduser().resolve()
        if p.exists() and p not in self._dirs:
            self._dirs.append(p)

    def discover_all(self) -> list[Skill]:
        """
        Discover all available skills across all directories.
        Higher-priority dirs win for skills with the same name.
        """
        seen_names: set[str] = set()
        skills: list[Skill] = []

        for skills_dir in self._dirs:
            if not skills_dir.exists():
                continue
            for skill_md in skills_dir.glob("*/SKILL.md"):
                skill = load_skill_from_path(skill_md)
                if skill and skill.name not in seen_names:
                    seen_names.add(skill.name)
                    skills.append(skill)
                    self._cache[skill.name] = skill

        return skills

    def load_skill(self, name: str, arguments: str = "") -> Optional[str]:
        """
        Load a skill by name and return its rendered content for injection.

        Args:
            name: Skill name (matches the directory name or `name` frontmatter field).
            arguments: Arguments to substitute into the skill content.

        Returns:
            Rendered skill content string, or None if skill not found.
        """
        skill = self._get_skill(name)
        if skill is None:
            return None
        if not self._passes_gates(skill):
            return None
        return skill.render(arguments)

    def list_skills(self) -> list[dict]:
        """
        List all available skills with metadata (for CLI display).

        Returns:
            List of dicts with name, description, user_invocable, source_dir.
        """
        self.discover_all()
        result = []
        for skill in self._cache.values():
            result.append({
                "name": skill.name,
                "description": skill.description or "(no description)",
                "user_invocable": skill.meta.user_invocable,
                "model_invocable": not skill.meta.disable_model_invocation,
                "source": str(skill.path.parent.parent),
                "allowed_tools": skill.meta.allowed_tools,
            })
        return sorted(result, key=lambda s: s["name"])

    def get_skill_descriptions(self) -> str:
        """
        Return a compact description of all model-invocable skills.

        This is injected into the system prompt for selective injection awareness.
        The agent uses these descriptions to decide which skill (if any) to activate.
        """
        skills = [s for s in self.discover_all() if not s.meta.disable_model_invocation]
        if not skills:
            return ""

        lines = ["# Available Skills\n"]
        lines.append(
            "Before responding, scan these skill descriptions. "
            "If exactly one clearly applies to the user's request, "
            "activate it by requesting it. If multiple could apply, "
            "choose the most specific. If none clearly apply, proceed normally.\n"
        )
        for skill in skills:
            inv = "(user-only)" if not skill.meta.user_invocable else ""
            lines.append(f"- **{skill.name}** {inv}: {skill.description}")

        return "\n".join(lines)

    def _get_skill(self, name: str) -> Optional[Skill]:
        """Find a skill by name, checking cache first then scanning dirs."""
        if name in self._cache:
            return self._cache[name]

        # Scan all dirs for this specific skill
        for skills_dir in self._dirs:
            # Try exact directory match
            skill_md = skills_dir / name / "SKILL.md"
            skill = load_skill_from_path(skill_md)
            if skill:
                self._cache[skill.name] = skill
                return skill

            # Try scanning all subdirs for a skill with this name in frontmatter
            for skill_md in skills_dir.glob("*/SKILL.md"):
                skill = load_skill_from_path(skill_md)
                if skill and skill.name == name:
                    self._cache[skill.name] = skill
                    return skill

        return None

    def _passes_gates(self, skill: Skill) -> bool:
        """
        Check OpenClaw-style gating: required binaries, env vars, OS.
        Always returns True if skill has always=True or no gates configured.
        """
        if skill.meta.always:
            return True

        # OS restriction (list of platforms: "darwin", "linux", "win32")
        if skill.meta.os_platforms:
            import platform
            current_os = platform.system().lower()
            mapping = {"darwin": "darwin", "linux": "linux", "windows": "win32"}
            current = mapping.get(current_os, current_os)
            if current not in skill.meta.os_platforms:
                return False

        # Required binaries (all must exist)
        for bin_name in skill.meta.requires_bins:
            if not self._which(bin_name):
                return False

        # anyBins (at least one must exist)
        if skill.meta.requires_any_bins:
            if not any(self._which(b) for b in skill.meta.requires_any_bins):
                return False

        # Required env vars
        for env_var in skill.meta.requires_env:
            if not os.environ.get(env_var):
                return False

        return True

    @staticmethod
    def _which(name: str) -> bool:
        """Check if a binary is available on PATH."""
        import shutil
        return shutil.which(name) is not None

    @staticmethod
    def _find_bundled_skills_dir() -> Optional[Path]:
        """Find the bundled skills/ directory shipped with the package."""
        # When installed, skills/ is at the project root (two levels up from this file)
        candidates = [
            Path(__file__).parent.parent.parent.parent / "skills",  # src layout
            Path(sys.prefix) / "share" / "agnoclaw" / "skills",
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.is_dir():
                return candidate
        return None
