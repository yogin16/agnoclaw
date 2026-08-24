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

import hashlib
import logging
import platform
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backends import (
    AutoApproveSkillInstallApprover,
    InteractiveSkillInstallApprover,
    LocalSkillRuntimeBackend,
    SkillInstallApprover,
    SkillRuntimeBackend,
    build_install_command,
)
from .loader import Skill, SkillInstaller, load_skill_from_path

logger = logging.getLogger("agnoclaw.skills")

# ── Security: package name validation ──────────────────────────────────────────

# Characters that should never appear in a package name
# Note: <>=. are allowed for version constraints (e.g., requests>=2.31)
# Subprocess calls use list form (not shell=True), so these are safe.
_DANGEROUS_CHARS = re.compile(r"[;&|$`()\[\]{}!#\\\n\r]")

# Patterns that indicate a URL-based install (supply chain risk)
_URL_PATTERNS = re.compile(r"^(https?://|git\+|git://|svn\+|ssh://|ftp://)")

_MAX_MODEL_SKILL_NAME_CHARS = 128
_MAX_MODEL_SKILL_ARGUMENT_BYTES = 16_384
_MAX_MODEL_SKILL_CONTENT_BYTES = 131_072


@dataclass(frozen=True)
class ModelSkillActivation:
    """A bounded, side-effect-free skill disclosure for one model run."""

    name: str
    description: str
    content: str
    trust: str
    allowed_tools: tuple[str, ...] | None
    content_digest: str


class ModelSkillActivationError(ValueError):
    """A recoverable reason a model cannot activate a requested skill inline."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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

    def __init__(
        self,
        workspace_skills_dir: Path | None = None,
        *,
        auto_approve_installs: bool = False,
        runtime_backend: SkillRuntimeBackend | None = None,
        install_approver: SkillInstallApprover | None = None,
        working_dir: str | Path | None = None,
    ):
        self._dirs: list[Path] = []
        self._cache: dict[str, Skill] = {}
        self._bundled_dir: Path | None = None
        self._local_dirs: list[Path] = []
        self._directory_trust: dict[Path, str] = {}
        self._auto_approve_installs = auto_approve_installs
        self._working_dir = (
            Path(working_dir).expanduser().resolve() if working_dir is not None else None
        )
        self._runtime_backend = runtime_backend or LocalSkillRuntimeBackend(
            working_dir=self._working_dir
        )
        self._install_approver = install_approver or (
            AutoApproveSkillInstallApprover()
            if auto_approve_installs
            else InteractiveSkillInstallApprover()
        )

        # Build search path (highest → lowest priority). Remote provenance remains
        # below every local/builtin root even when stored underneath one of them.
        community_dirs: list[Path] = []
        if workspace_skills_dir:
            workspace_root = Path(workspace_skills_dir).expanduser().resolve()
            self._register_directory(workspace_root, trust="local")
            if workspace_root.joinpath(".community").exists():
                community_dirs.append(workspace_root / ".community")

        user_skills = Path.home() / ".agnoclaw" / "skills"
        if user_skills.exists():
            self._register_directory(user_skills, trust="local")
            if user_skills.joinpath(".community").exists():
                community_dirs.append(user_skills / ".community")

        # Bundled skills (relative to this package)
        self._bundled_dir = self._find_bundled_skills_dir()
        if self._bundled_dir:
            self._register_directory(self._bundled_dir, trust="builtin")

        global_community = Path.home() / ".agnoclaw" / "community-skills"
        if global_community.exists():
            community_dirs.append(global_community)
        for community_dir in community_dirs:
            self._register_directory(community_dir, trust="community")

    def add_directory(self, path: str | Path, *, trust: str = "community") -> None:
        """
        Add an additional skills directory (appended at lowest priority).

        Args:
            path: Directory containing skill subdirectories.
            trust: Trust level for skills from this directory.
                   "local" allows inline !`cmd` execution.
                   "community" (default) blocks inline execution.
        """
        if trust not in {"local", "community"}:
            raise ValueError("skill directory trust must be 'local' or 'community'")
        p = Path(path).expanduser().resolve()
        if p.exists():
            self._register_directory(p, trust=trust)

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

    def load_skill(self, name: str, arguments: str = "") -> str | None:
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
        runtime_backend = self._runtime_backend if trust in ("builtin", "local") else None
        return skill.render(
            arguments,
            allow_exec=False,
            runtime_backend=runtime_backend,
            working_dir=self._working_dir,
        )

    def inspect_skill(self, name: str) -> Skill | None:
        """Return parsed skill metadata/content without gates, installs, or execution."""
        return self._get_skill(name)

    def activate_for_model(self, name: str, arguments: str = "") -> ModelSkillActivation:
        """Disclose one trusted model-invocable skill without executing host effects.

        Model activation intentionally does not run install specifications or inline
        commands. Declarations that require changing the model, forking context,
        dispatching a command directly, or rewriting tool schemas still require the
        caller-owned ``skill=...`` activation path so their semantics cannot be
        silently weakened mid-run.
        """
        normalized_name = str(name).strip()
        if not normalized_name or len(normalized_name) > _MAX_MODEL_SKILL_NAME_CHARS:
            raise ModelSkillActivationError(
                "SKILL_NAME_INVALID",
                "A bounded non-empty skill name is required.",
            )
        encoded_arguments = str(arguments).encode("utf-8")
        if len(encoded_arguments) > _MAX_MODEL_SKILL_ARGUMENT_BYTES:
            raise ModelSkillActivationError(
                "SKILL_ARGUMENTS_TOO_LARGE",
                "Skill arguments exceed the inline activation budget.",
            )
        skill = self._get_skill(normalized_name)
        if skill is None:
            raise ModelSkillActivationError(
                "SKILL_NOT_FOUND",
                f"Skill '{normalized_name}' is not available.",
            )
        trust = self._trust_level(skill)
        if trust not in {"builtin", "local"} or skill.meta.disable_model_invocation:
            raise ModelSkillActivationError(
                "SKILL_MODEL_ACTIVATION_DENIED",
                f"Skill '{normalized_name}' is not eligible for model activation.",
            )
        if not self._passes_gates(skill):
            raise ModelSkillActivationError(
                "SKILL_REQUIREMENTS_UNMET",
                f"Skill '{normalized_name}' has unmet runtime requirements.",
            )
        unsupported = self._model_activation_unsupported_fields(skill)
        if unsupported:
            raise ModelSkillActivationError(
                "SKILL_EXPLICIT_ACTIVATION_REQUIRED",
                (
                    f"Skill '{normalized_name}' requires caller-owned activation for: "
                    + ", ".join(unsupported)
                    + "."
                ),
            )
        if self._model_activation_needs_install(skill):
            raise ModelSkillActivationError(
                "SKILL_INSTALL_APPROVAL_REQUIRED",
                (
                    f"Skill '{normalized_name}' has unmet install specifications; "
                    "activate it explicitly after review."
                ),
            )

        # Dynamic !`cmd` fragments stay literal. A model may request an allowed
        # governed tool afterward, but loading instructions itself has no host effect.
        content = skill.render(str(arguments), allow_exec=False, runtime_backend=None)
        if len(content.encode("utf-8")) > _MAX_MODEL_SKILL_CONTENT_BYTES:
            raise ModelSkillActivationError(
                "SKILL_CONTENT_TOO_LARGE",
                f"Skill '{normalized_name}' exceeds the inline activation budget.",
            )
        return ModelSkillActivation(
            name=skill.name,
            description=skill.description,
            content=content,
            trust=trust,
            allowed_tools=(tuple(skill.meta.allowed_tools) if skill.meta.allowed_tools else None),
            content_digest=(
                "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
            ),
        )

    @staticmethod
    def _model_activation_unsupported_fields(skill: Skill) -> list[str]:
        unsupported = []
        if skill.meta.model:
            unsupported.append("model")
        if skill.meta.context:
            unsupported.append("context")
        if skill.meta.command_dispatch or skill.meta.command_tool:
            unsupported.append("command-dispatch")
        if skill.meta.tool_schemas:
            unsupported.append("tool-schemas")
        if skill.meta.tool_arg_bindings:
            unsupported.append("tool-arg-bindings")
        return unsupported

    def _model_activation_needs_install(self, skill: Skill) -> bool:
        current_os = platform.system().lower()
        current_platform = {
            "darwin": "darwin",
            "linux": "linux",
            "windows": "win32",
        }.get(current_os, current_os)
        return any(
            self._needs_install(installer)
            for installer in skill.meta.install
            if not installer.os or current_platform in installer.os
        )

    def _is_model_activatable(self, skill: Skill) -> bool:
        return bool(
            self._trust_level(skill) in {"builtin", "local"}
            and not skill.meta.disable_model_invocation
            and self._passes_gates(skill)
            and not self._model_activation_unsupported_fields(skill)
            and not self._model_activation_needs_install(skill)
        )

    def list_skills(self) -> list[dict]:
        """
        List all available skills with metadata (for CLI display).

        Returns:
            List of dicts with name, description, user_invocable, source_dir.
        """
        self.discover_all()
        result = []
        for skill in self._cache.values():
            trust = self._trust_level(skill)
            result.append(
                {
                    "name": skill.name,
                    "description": skill.description or "(no description)",
                    "user_invocable": skill.meta.user_invocable,
                    "model_invocable": (
                        self._is_model_activatable(skill)
                    ),
                    "declared_model_invocable": not skill.meta.disable_model_invocation,
                    "trust": trust,
                    "source": str(skill.path.parent.parent),
                    "allowed_tools": skill.meta.allowed_tools,
                }
            )
        return sorted(result, key=lambda s: s["name"])

    def get_skill_descriptions(self) -> str:
        """
        Return a compact description of all model-invocable skills.

        This is injected into the system prompt for selective injection awareness.
        The agent uses these descriptions to decide which skill (if any) to activate.
        """
        skills = [
            skill
            for skill in self.discover_all()
            if self._is_model_activatable(skill)
        ]
        if not skills:
            return ""

        lines = ["# Available Skills\n"]
        lines.append(
            "Before responding, scan these skill descriptions. "
            "If exactly one clearly applies to the user's request, "
            "call get_skill_instructions with its exact name as a standalone tool "
            "step, then follow the returned instructions. If multiple could apply, "
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
            if not self._runtime_backend_for_call().has_binary(bin_name):
                return False

        # anyBins (at least one must exist)
        if skill.meta.requires_any_bins:
            if not any(
                self._runtime_backend_for_call().has_binary(b) for b in skill.meta.requires_any_bins
            ):
                return False

        # Required env vars
        for env_var in skill.meta.requires_env:
            if not self._runtime_backend_for_call().has_env_var(env_var):
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

        return self._directory_trust.get(skill_dir_resolved, "community")

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
                    skill.name,
                    installer.package,
                    reason,
                )
                continue

            pkg = installer.package
            if installer.version:
                if installer.type in ("uv", "pip"):
                    pkg = f"{pkg}=={installer.version}"
                else:
                    pkg = f"{pkg}@{installer.version}"
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
            logger.info("Skill '%s': installing %s (%s)", skill.name, pkg, installer.type)
            result = self._runtime_backend_for_call().run_install(
                installer_type=installer.type,
                package_spec=pkg,
                timeout_seconds=120,
            )
            if not result.success:
                detail = result.message or result.stderr[:200] or result.stdout[:200]
                logger.warning(
                    "Skill '%s': install failed (%s): %s",
                    skill.name,
                    result.exit_code if result.exit_code is not None else "no-exit-code",
                    detail,
                )

    def _prompt_install_approval(
        self,
        skill: Skill,
        pending: list[tuple[SkillInstaller, str]],
    ) -> bool:
        """
        Display pending installs and prompt the user for approval.

        Returns True if the user approves, False otherwise.
        """
        return self._install_approver.approve(skill, pending)

    def _needs_install(self, installer: SkillInstaller) -> bool:
        """
        Check if an installer's package/binary is already present.
        Returns True if install should run, False if already satisfied.
        """
        itype = installer.type
        pkg = installer.package

        if itype in ("uv", "pip"):
            dist_name = pkg.split("[")[0].split("==")[0].split(">=")[0].split("<=")[0]
            return not self._runtime_backend_for_call().has_python_distribution(dist_name)

        if itype in ("brew", "npm"):
            return not self._runtime_backend_for_call().has_binary(pkg)

        if itype == "go":
            return not self._runtime_backend_for_call().has_binary(pkg.split("/")[-1])

        return True  # unknown type — try anyway

    @staticmethod
    def _build_install_cmd(itype: str, pkg: str) -> list[str] | None:
        """Build the install command for the given installer type."""
        return build_install_command(itype, pkg)

    # ── ClawHub integration ──────────────────────────────────────────────────

    def install_from_hub(
        self,
        name: str,
        hub_url: str = "https://clawhub.ai",
        cache_dir: str = "~/.agnoclaw/cache/hub",
        *,
        network_policy: Any | None = None,
    ) -> Path | None:
        """
        Download and install a skill from ClawHub to a quarantined community directory.

        Hub provenance never inherits local trust merely because the destination is
        underneath a local workspace. Installed skills are explicitly loadable, but
        inline execution is blocked and their metadata is excluded from the automatic
        system-prompt catalog.

        Args:
            name: Skill name on ClawHub.
            hub_url: ClawHub API base URL.
            cache_dir: Cache directory for ClawHub metadata.

        Returns:
            Path to the installed skill directory, or None on failure.
        """
        from .hub import ClawHubClient

        # Keep remote provenance in a distinct search root. The surrounding local
        # directory is only a storage choice; it must not confer executable trust.
        if self._local_dirs:
            dest_dir = self._local_dirs[0] / ".community"
        else:
            dest_dir = Path.home() / ".agnoclaw" / "community-skills"

        dest_dir.mkdir(parents=True, exist_ok=True)
        self._register_directory(dest_dir, trust="community")

        client = ClawHubClient(
            base_url=hub_url,
            cache_dir=cache_dir,
            network_policy=network_policy,
        )
        try:
            skill_dir = client.download(name, dest_dir)
        finally:
            client.close()

        if skill_dir:
            # Invalidate cache so the new skill is discovered
            self._cache.pop(name, None)
            logger.info("Installed skill '%s' from ClawHub to %s", name, skill_dir)

        return skill_dir

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _register_directory(self, path: str | Path, *, trust: str) -> None:
        p = Path(path).expanduser().resolve()
        existing = self._directory_trust.get(p)
        if existing is not None and existing != trust:
            raise ValueError(
                f"skill directory {p} is already registered with {existing!r} trust"
            )
        self._directory_trust[p] = trust
        if p not in self._dirs:
            self._dirs.append(p)
        if trust == "local" and p not in self._local_dirs:
            self._local_dirs.append(p)
        self._cache.clear()

    def _get_skill(self, name: str) -> Skill | None:
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

    def _runtime_backend_for_call(self) -> SkillRuntimeBackend:
        backend = getattr(self, "_runtime_backend", None)
        if backend is not None:
            return backend
        working_dir = getattr(self, "_working_dir", None)
        return LocalSkillRuntimeBackend(working_dir=working_dir)

    @staticmethod
    def _find_bundled_skills_dir() -> Path | None:
        """Find the bundled skills/ directory shipped with the package."""
        candidates = [
            Path(__file__).parent.parent.parent.parent / "skills",  # src layout
            Path(sys.prefix) / "share" / "agnoclaw" / "skills",
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.is_dir():
                return candidate
        return None
