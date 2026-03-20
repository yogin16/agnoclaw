"""
SKILL.md parser.

Implements the AgentSkills open standard, compatible with Claude Code skills
and OpenClaw/ClawHub skill format.

A skill is a directory containing a SKILL.md file with YAML frontmatter:

    ---
    name: deep-research
    description: Perform deep multi-source research on any topic
    user-invocable: true
    disable-model-invocation: false
    allowed-tools: bash, web_search, web_fetch, spawn_subagent
    argument-hint: "[topic]"
    metadata:
      openclaw:
        emoji: 🔍
        os: [darwin, linux]
        requires:
          bins: [git]
          anyBins: [brew, apt]
          env: [GITHUB_TOKEN]
        install:
          - type: uv
            package: httpx
          - type: brew
            package: gh
            os: [darwin]
    ---

    ## Deep Research Skill

    When performing deep research:
    1. Start with a broad web search to map the landscape
    2. Identify 3-5 authoritative sources
    ...

Special syntax in SKILL.md content:
  $ARGUMENTS    — all arguments passed at invocation
  $ARGUMENTS[N] — nth argument (0-indexed)
  !`cmd`        — shell command run before content is injected (dynamic context)

Installer types (metadata.openclaw.install):
  uv      — Python package (uv add / uv pip install)
  pip     — Python package (pip install) — use uv when possible
  brew    — Homebrew formula (macOS)
  npm     — Node.js package (npm install -g)
  go      — Go binary (go install)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import frontmatter

from .backends import LocalSkillRuntimeBackend, SkillRuntimeBackend

logger = logging.getLogger("agnoclaw.skills")


@dataclass
class SkillInstaller:
    """A single installer spec from metadata.openclaw.install."""

    type: str                           # "uv", "pip", "brew", "npm", "go"
    package: str                        # package/formula name
    os: list[str] = field(default_factory=list)  # platform filter (empty = all)
    version: Optional[str] = None      # optional version constraint


@dataclass
class SkillMeta:
    """Parsed metadata from a SKILL.md frontmatter."""

    name: str
    description: str = ""
    user_invocable: bool = True
    disable_model_invocation: bool = False
    allowed_tools: list[str] = field(default_factory=list)
    model: Optional[str] = None
    context: Optional[str] = None       # "fork" for isolated subagent
    argument_hint: Optional[str] = None
    homepage: Optional[str] = None
    # OpenClaw gating
    requires_bins: list[str] = field(default_factory=list)    # all must be on PATH
    requires_any_bins: list[str] = field(default_factory=list) # at least one must exist
    requires_env: list[str] = field(default_factory=list)      # all must be set
    os_platforms: list[str] = field(default_factory=list)      # ["darwin", "linux", "win32"]
    always: bool = False                # skip all gate checks
    # OpenClaw install — runs before the skill is loaded if dependency is missing
    install: list[SkillInstaller] = field(default_factory=list)
    # Command dispatch (OpenClaw: bypass model, run tool directly)
    command_dispatch: Optional[str] = None   # "tool"
    command_tool: Optional[str] = None
    # Display
    emoji: Optional[str] = None


@dataclass
class Skill:
    """A fully loaded and parsed skill."""

    meta: SkillMeta
    content: str   # The SKILL.md body (instructions)
    path: Path     # Path to the SKILL.md file

    @property
    def name(self) -> str:
        return self.meta.name

    @property
    def description(self) -> str:
        return self.meta.description

    def render(
        self,
        arguments: str = "",
        *,
        allow_exec: bool = False,
        runtime_backend: SkillRuntimeBackend | None = None,
        working_dir: str | Path | None = None,
    ) -> str:
        """
        Render the skill content with argument substitution.

        Substitutes:
          $ARGUMENTS    → full arguments string
          $ARGUMENTS[N] → nth space-split argument
          !`cmd`        → output of shell command (only if allow_exec=True or backend provided)

        Security: Inline shell execution (!`cmd`) is disabled by default.
        Pass allow_exec=True only for trusted (builtin/local) skills.
        Untrusted skills get the raw !`cmd` syntax preserved as-is.

        Args:
            arguments: Arguments to substitute into $ARGUMENTS placeholders.
            allow_exec: If True, execute !`cmd` inline shell commands using a local backend
                        when no runtime backend is supplied.
            runtime_backend: Optional backend for inline command execution.
            working_dir: Optional working directory for inline command execution.
        """
        content = self.content

        # Substitute $ARGUMENTS[N]
        args_list = arguments.split() if arguments else []

        def replace_arg_n(m: re.Match) -> str:
            idx = int(m.group(1))
            return args_list[idx] if idx < len(args_list) else ""

        content = re.sub(r"\$ARGUMENTS\[(\d+)\]", replace_arg_n, content)
        content = content.replace("$ARGUMENTS", arguments)

        # Execute inline shell commands: !`cmd` — only if explicitly allowed
        inline_backend = runtime_backend
        if inline_backend is None and allow_exec:
            inline_backend = LocalSkillRuntimeBackend(working_dir=working_dir)

        if inline_backend is not None:
            def run_inline(m: re.Match) -> str:
                cmd = m.group(1)
                logger.debug("Skill '%s': executing inline command: %s", self.name, cmd)
                return inline_backend.run_inline_command(
                    command=cmd,
                    timeout_seconds=10,
                    working_dir=(
                        str(Path(working_dir).expanduser().resolve())
                        if working_dir is not None
                        else None
                    ),
                )

            content = re.sub(r"!`([^`]+)`", run_inline, content)
        else:
            # Count how many commands would have been executed
            inline_cmds = re.findall(r"!`([^`]+)`", content)
            if inline_cmds:
                logger.info(
                    "Skill '%s': %d inline command(s) skipped (allow_exec=False)",
                    self.name, len(inline_cmds),
                )

        return content


def load_skill_from_path(skill_md_path: Path) -> Optional[Skill]:
    """
    Parse a SKILL.md file into a Skill object.

    Returns None if the file doesn't exist or can't be parsed.
    """
    if not skill_md_path.exists():
        return None

    try:
        post = frontmatter.load(str(skill_md_path))
    except Exception as e:
        logger.warning("Failed to parse SKILL.md at %s: %s", skill_md_path, e)
        return None

    metadata = post.metadata
    content = post.content.strip()

    # Derive name from directory if not in frontmatter
    name = metadata.get("name") or skill_md_path.parent.name

    # Parse allowed-tools (can be string or list)
    allowed_tools_raw = metadata.get("allowed-tools", metadata.get("allowed_tools", []))
    if isinstance(allowed_tools_raw, str):
        allowed_tools = [t.strip() for t in allowed_tools_raw.split(",") if t.strip()]
    else:
        allowed_tools = list(allowed_tools_raw)

    # Parse OpenClaw gating metadata.
    # Accepts aliases: metadata.openclaw, metadata.clawdbot, metadata.clawdis
    raw_meta = metadata.get("metadata") or {}
    if isinstance(raw_meta, str):
        try:
            import json as _json
            raw_meta = _json.loads(raw_meta)
        except Exception:
            raw_meta = {}
    openclaw_meta = (
        raw_meta.get("openclaw")
        or raw_meta.get("clawdbot")
        or raw_meta.get("clawdis")
        or {}
    )
    requires = openclaw_meta.get("requires", {})

    # os_platforms: normalize string or list → list of strings
    raw_os = openclaw_meta.get("os", [])
    if isinstance(raw_os, str):
        os_platforms = [raw_os] if raw_os else []
    else:
        os_platforms = list(raw_os)

    # Parse install specs
    install: list[SkillInstaller] = []
    for entry in openclaw_meta.get("install", []):
        if not isinstance(entry, dict):
            continue
        installer_type = entry.get("type", "")
        package = entry.get("package", "")
        if not installer_type or not package:
            continue
        installer_os_raw = entry.get("os", [])
        installer_os = (
            [installer_os_raw] if isinstance(installer_os_raw, str) else list(installer_os_raw)
        )
        install.append(SkillInstaller(
            type=installer_type.lower(),
            package=package,
            os=installer_os,
            version=entry.get("version"),
        ))

    meta = SkillMeta(
        name=name,
        description=metadata.get("description", ""),
        user_invocable=metadata.get("user-invocable", metadata.get("user_invocable", True)),
        disable_model_invocation=metadata.get(
            "disable-model-invocation",
            metadata.get("disable_model_invocation", False),
        ),
        allowed_tools=allowed_tools,
        model=metadata.get("model"),
        context=metadata.get("context"),
        argument_hint=metadata.get("argument-hint", metadata.get("argument_hint")),
        homepage=metadata.get("homepage"),
        requires_bins=requires.get("bins", []),
        requires_any_bins=requires.get("anyBins", requires.get("any_bins", [])),
        requires_env=requires.get("env", []),
        os_platforms=os_platforms,
        always=openclaw_meta.get("always", False),
        install=install,
        command_dispatch=metadata.get("command-dispatch", metadata.get("command_dispatch")),
        command_tool=metadata.get("command-tool", metadata.get("command_tool")),
        emoji=openclaw_meta.get("emoji"),
    )

    return Skill(meta=meta, content=content, path=skill_md_path)
