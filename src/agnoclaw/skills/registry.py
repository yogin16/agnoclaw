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

Install support (metadata.openclaw.install):
  When a skill declares install specs, the registry runs them before
  loading the skill if the required binary/package is missing.
  Supports: uv, pip, brew, npm, go.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .loader import Skill, SkillInstaller, load_skill_from_path

logger = logging.getLogger("agnoclaw.skills")


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

        Runs any declared install specs before loading if dependencies are missing.

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
        # Run any declared installers (only installs if dependency is missing)
        self._run_install(skill)
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

    # ── Gate checks ────────────────────────────────────────────────────────────

    def _passes_gates(self, skill: Skill) -> bool:
        """
        Check OpenClaw-style gating: required binaries, env vars, OS.
        Always returns True if skill has always=True or no gates configured.
        """
        if skill.meta.always:
            return True

        # OS restriction
        if skill.meta.os_platforms:
            current_os = platform.system().lower()
            mapping = {"darwin": "darwin", "linux": "linux", "windows": "win32"}
            current = mapping.get(current_os, current_os)
            if current not in skill.meta.os_platforms:
                return False

        # Required binaries (all must exist)
        for bin_name in skill.meta.requires_bins:
            if not shutil.which(bin_name):
                return False

        # anyBins (at least one must exist)
        if skill.meta.requires_any_bins:
            if not any(shutil.which(b) for b in skill.meta.requires_any_bins):
                return False

        # Required env vars
        for env_var in skill.meta.requires_env:
            if not os.environ.get(env_var):
                return False

        return True

    # ── Install support ────────────────────────────────────────────────────────

    def _run_install(self, skill: Skill) -> None:
        """
        Run declared install specs for a skill.

        Only installs if the package/binary is not already present.
        Logs warnings on failure but does not raise — skill loading continues.

        Supported types: uv, pip, brew, npm, go
        """
        if not skill.meta.install:
            return

        current_os = platform.system().lower()
        os_map = {"darwin": "darwin", "linux": "linux", "windows": "win32"}
        current_platform = os_map.get(current_os, current_os)

        for installer in skill.meta.install:
            # Platform filter
            if installer.os and current_platform not in installer.os:
                continue

            if not self._needs_install(installer):
                continue

            pkg = installer.package
            if installer.version:
                pkg = f"{pkg}=={installer.version}" if installer.type in ("uv", "pip") else f"{pkg}@{installer.version}"

            cmd = self._build_install_cmd(installer.type, pkg)
            if cmd is None:
                logger.warning("Skill '%s': unknown installer type '%s'", skill.name, installer.type)
                continue

            logger.info("Skill '%s': installing %s (%s)", skill.name, pkg, installer.type)
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode != 0:
                    logger.warning(
                        "Skill '%s': install failed (exit %d): %s",
                        skill.name,
                        result.returncode,
                        result.stderr[:200],
                    )
            except FileNotFoundError:
                logger.warning(
                    "Skill '%s': installer '%s' not found on PATH", skill.name, installer.type
                )
            except subprocess.TimeoutExpired:
                logger.warning("Skill '%s': install timed out for %s", skill.name, pkg)
            except Exception as e:
                logger.warning("Skill '%s': install error: %s", skill.name, e)

    def _needs_install(self, installer: SkillInstaller) -> bool:
        """
        Check if an installer's package/binary is already present.
        Returns True if install should run, False if already satisfied.
        """
        itype = installer.type
        pkg = installer.package

        if itype in ("uv", "pip"):
            # Check if importable (covers most Python package checks)
            import_name = pkg.replace("-", "_").split("[")[0].split("==")[0]
            try:
                __import__(import_name)
                return False  # already installed
            except ImportError:
                return True

        elif itype == "brew":
            return shutil.which(pkg) is None

        elif itype == "npm":
            return shutil.which(pkg) is None

        elif itype == "go":
            # Go binaries land in $GOPATH/bin or $HOME/go/bin
            return shutil.which(pkg.split("/")[-1]) is None

        return True  # unknown type — try anyway

    @staticmethod
    def _build_install_cmd(itype: str, pkg: str) -> Optional[list[str]]:
        """Build the install command for the given installer type."""
        if itype == "uv":
            return [sys.executable, "-m", "uv", "pip", "install", pkg]
        elif itype == "pip":
            return [sys.executable, "-m", "pip", "install", "--quiet", pkg]
        elif itype == "brew":
            return ["brew", "install", pkg]
        elif itype == "npm":
            return ["npm", "install", "-g", pkg]
        elif itype == "go":
            return ["go", "install", pkg]
        return None

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _get_skill(self, name: str) -> Optional[Skill]:
        """Find a skill by name, checking cache first then scanning dirs."""
        if name in self._cache:
            return self._cache[name]

        for skills_dir in self._dirs:
            skill_md = skills_dir / name / "SKILL.md"
            skill = load_skill_from_path(skill_md)
            if skill:
                self._cache[skill.name] = skill
                return skill

            for skill_md in skills_dir.glob("*/SKILL.md"):
                skill = load_skill_from_path(skill_md)
                if skill and skill.name == name:
                    self._cache[skill.name] = skill
                    return skill

        return None

    @staticmethod
    def _find_bundled_skills_dir() -> Optional[Path]:
        """Find the bundled skills/ directory shipped with the package."""
        candidates = [
            Path(__file__).parent.parent.parent.parent / "skills",  # src layout
            Path(sys.prefix) / "share" / "agnoclaw" / "skills",
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.is_dir():
                return candidate
        return None
