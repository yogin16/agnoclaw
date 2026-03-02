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

Security model:
  Skills are classified by trust level based on their source directory:
    - builtin: shipped with agnoclaw — inline commands and installs auto-approved
    - local:   user's workspace or ~/.agnoclaw/skills/ — inline commands allowed,
               installs require interactive approval
    - community: external sources — inline commands blocked, installs require approval,
                 package names validated against dangerous patterns

  The !`cmd` syntax in SKILL.md is only executed for builtin/local skills.
  Install specs always display what will be installed and require confirmation
  (except for builtin skills).

Install support (metadata.openclaw.install):
  When a skill declares install specs, the registry validates package names,
  then prompts the user before running any installs.
  Supports: uv, pip, brew, npm, go.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .loader import Skill, SkillInstaller, load_skill_from_path

logger = logging.getLogger("agnoclaw.skills")

# ── Security: package name validation ──────────────────────────────────────────

# Characters that should never appear in a package name
# Note: <>=. are allowed for version constraints (e.g., requests>=2.31)
# Subprocess calls use list form (not shell=True), so these are safe.
_DANGEROUS_CHARS = re.compile(r'[;&|$`()\[\]{}!#\\\n\r]')

# Patterns that indicate a URL-based install (supply chain risk)
_URL_PATTERNS = re.compile(r'^(https?://|git\+|git://|svn\+|ssh://|ftp://)')


def _validate_package_name(pkg: str, installer_type: str) -> tuple[bool, str]:
    """
    Validate a package name for dangerous patterns.

    Returns (is_valid, reason) — reason is empty string if valid.
    """
    if not pkg or not pkg.strip():
        return False, "empty package name"

    if _DANGEROUS_CHARS.search(pkg):
        return False, f"contains shell metacharacters: {pkg!r}"

    if _URL_PATTERNS.match(pkg):
        return False, f"URL-based installs are blocked: {pkg!r}"

    # Go packages are paths like github.com/user/repo — allow slashes
    if installer_type != "go" and ".." in pkg:
        return False, f"path traversal in package name: {pkg!r}"

    # npm scoped packages start with @ — that's fine
    # But reject obviously suspicious patterns
    if len(pkg) > 200:
        return False, f"package name too long ({len(pkg)} chars)"

    return True, ""


class SkillRegistry:
    """
    Discovers and manages skills from multiple directories.

    Skills are loaded lazily (on demand) to avoid prompt bloat.
    Only the content of the selected skill is injected per turn.

    Trust model:
        Skills are assigned a trust level based on their source directory:
        - "builtin": shipped with agnoclaw — fully trusted
        - "local": user's workspace or ~/.agnoclaw/skills/ — trusted for exec, approval for installs
        - "community": external sources — exec blocked, installs require approval + validation
    """

    def __init__(self, workspace_skills_dir: Optional[Path] = None, *, auto_approve_installs: bool = False):
        self._dirs: list[Path] = []
        self._cache: dict[str, Skill] = {}
        self._bundled_dir: Optional[Path] = None
        self._local_dirs: list[Path] = []
        self._auto_approve_installs = auto_approve_installs

        # Build search path (highest → lowest priority)
        if workspace_skills_dir:
            self._dirs.append(workspace_skills_dir)
            self._local_dirs.append(workspace_skills_dir)

        user_skills = Path.home() / ".agnoclaw" / "skills"
        if user_skills.exists():
            self._dirs.append(user_skills)
            self._local_dirs.append(user_skills)

        # Bundled skills (relative to this package)
        self._bundled_dir = self._find_bundled_skills_dir()
        if self._bundled_dir:
            self._dirs.append(self._bundled_dir)

    def add_directory(self, path: str | Path, *, trust: str = "community") -> None:
        """
        Add an additional skills directory (appended at lowest priority).

        Args:
            path: Directory containing skill subdirectories.
            trust: Trust level for skills from this directory.
                   "local" allows inline !`cmd` execution.
                   "community" (default) blocks inline execution.
        """
        p = Path(path).expanduser().resolve()
        if p.exists() and p not in self._dirs:
            self._dirs.append(p)
            if trust == "local":
                self._local_dirs.append(p)
            # Invalidate cache so new skills are discovered
            self._cache.clear()

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

        Security behavior by trust level:
          - builtin: inline exec allowed, installs auto-approved
          - local: inline exec allowed, installs require user confirmation
          - community: inline exec blocked, installs require confirmation + validation

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

        trust = self._trust_level(skill)

        # Run any declared installers (validated + approval-gated)
        self._run_install(skill, trust)

        # Inline !`cmd` execution: only for builtin and local skills
        allow_exec = trust in ("builtin", "local")
        return skill.render(arguments, allow_exec=allow_exec)

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
            inv = "(model-only)" if not skill.meta.user_invocable else ""
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

    # ── Trust model ─────────────────────────────────────────────────────────────

    def _trust_level(self, skill: Skill) -> str:
        """
        Determine trust level for a skill based on its source directory.

        Returns: "builtin", "local", or "community"
        """
        skill_dir = skill.path.parent.parent  # skill_name/SKILL.md → parent dir
        try:
            skill_dir_resolved = skill_dir.resolve()
        except (OSError, ValueError):
            return "community"

        if self._bundled_dir:
            try:
                if skill_dir_resolved == self._bundled_dir.resolve():
                    return "builtin"
            except (OSError, ValueError):
                pass

        for local_dir in self._local_dirs:
            try:
                if skill_dir_resolved == local_dir.resolve():
                    return "local"
            except (OSError, ValueError):
                pass

        return "community"

    # ── Install support ────────────────────────────────────────────────────────

    def _run_install(self, skill: Skill, trust: str = "community") -> None:
        """
        Run declared install specs for a skill, with security validation.

        Security gates:
          1. Package names are validated against dangerous patterns
          2. Non-builtin skills require interactive user approval before install
          3. Install commands are logged

        Only installs if the package/binary is not already present.
        Logs warnings on failure but does not raise — skill loading continues.

        Supported types: uv, pip, brew, npm, go
        """
        if not skill.meta.install:
            return

        current_os = platform.system().lower()
        os_map = {"darwin": "darwin", "linux": "linux", "windows": "win32"}
        current_platform = os_map.get(current_os, current_os)

        # Collect pending installs (filter by platform and already-installed)
        pending: list[tuple[SkillInstaller, str]] = []
        for installer in skill.meta.install:
            if installer.os and current_platform not in installer.os:
                continue
            if not self._needs_install(installer):
                continue

            # Validate package name
            valid, reason = _validate_package_name(installer.package, installer.type)
            if not valid:
                logger.warning(
                    "Skill '%s': BLOCKED install of '%s' — %s",
                    skill.name, installer.package, reason,
                )
                continue

            pkg = installer.package
            if installer.version:
                pkg = f"{pkg}=={installer.version}" if installer.type in ("uv", "pip") else f"{pkg}@{installer.version}"
            pending.append((installer, pkg))

        if not pending:
            return

        # Approval gate: builtin skills auto-approve, others prompt
        if trust != "builtin" and not self._auto_approve_installs:
            if not self._prompt_install_approval(skill, pending):
                logger.info("Skill '%s': install declined by user", skill.name)
                return

        # Execute installs
        for installer, pkg in pending:
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

    @staticmethod
    def _prompt_install_approval(skill: Skill, pending: list[tuple[SkillInstaller, str]]) -> bool:
        """
        Display pending installs and prompt the user for approval.

        Returns True if the user approves, False otherwise.
        """
        print(f"\nSkill '{skill.name}' requires the following installations:")
        for installer, pkg in pending:
            print(f"  {installer.type}: {pkg}")
        print()
        try:
            answer = input("Proceed with installation? [y/N] ").strip().lower()
            return answer in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def _needs_install(self, installer: SkillInstaller) -> bool:
        """
        Check if an installer's package/binary is already present.
        Returns True if install should run, False if already satisfied.
        """
        itype = installer.type
        pkg = installer.package

        if itype in ("uv", "pip"):
            # Use importlib.metadata for accurate installed-package detection.
            # This handles cases where import name != package name
            # (e.g. Pillow→PIL, beautifulsoup4→bs4, python-dateutil→dateutil).
            dist_name = pkg.split("[")[0].split("==")[0].split(">=")[0].split("<=")[0]
            try:
                from importlib.metadata import distribution
                distribution(dist_name)
                return False  # already installed
            except Exception:
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
