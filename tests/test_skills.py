"""Tests for the skills system."""

from pathlib import Path
from unittest.mock import patch
import tempfile
import pytest

from agnoclaw.skills.loader import load_skill_from_path, SkillInstaller
from agnoclaw.skills.registry import SkillRegistry, _validate_package_name


SAMPLE_SKILL_MD = """---
name: test-skill
description: A test skill for unit testing
user-invocable: true
disable-model-invocation: false
allowed-tools: bash, web_search
---

# Test Skill

This is a test skill. Arguments: $ARGUMENTS

First arg: $ARGUMENTS[0]
"""


@pytest.fixture
def skill_dir(tmp_path):
    """Create a temporary skill directory."""
    skill_path = tmp_path / "test-skill"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text(SAMPLE_SKILL_MD)
    return tmp_path


def test_load_skill_from_path(skill_dir):
    skill_md = skill_dir / "test-skill" / "SKILL.md"
    skill = load_skill_from_path(skill_md)

    assert skill is not None
    assert skill.name == "test-skill"
    assert skill.meta.description == "A test skill for unit testing"
    assert skill.meta.user_invocable is True
    assert skill.meta.disable_model_invocation is False
    assert "bash" in skill.meta.allowed_tools
    assert "web_search" in skill.meta.allowed_tools


def test_skill_render_arguments(skill_dir):
    skill_md = skill_dir / "test-skill" / "SKILL.md"
    skill = load_skill_from_path(skill_md)

    rendered = skill.render("hello world")
    assert "hello world" in rendered
    assert "hello" in rendered  # $ARGUMENTS[0]


def test_skill_render_no_arguments(skill_dir):
    skill_md = skill_dir / "test-skill" / "SKILL.md"
    skill = load_skill_from_path(skill_md)

    rendered = skill.render()
    assert "$ARGUMENTS" not in rendered  # substituted to empty string


def test_skill_registry_discovery(skill_dir):
    registry = SkillRegistry(workspace_skills_dir=skill_dir)
    skills = registry.discover_all()

    assert len(skills) >= 1
    names = [s.name for s in skills]
    assert "test-skill" in names


def test_skill_registry_load(skill_dir):
    registry = SkillRegistry(workspace_skills_dir=skill_dir)
    content = registry.load_skill("test-skill", arguments="foo bar")

    assert content is not None
    assert "Test Skill" in content
    assert "foo bar" in content


def test_skill_registry_missing(skill_dir):
    registry = SkillRegistry(workspace_skills_dir=skill_dir)
    content = registry.load_skill("nonexistent-skill")
    assert content is None


def test_bundled_skills_discoverable():
    """Bundled skills should be discoverable from the package."""
    registry = SkillRegistry()
    skills = registry.discover_all()
    names = [s.name for s in skills]

    # At minimum the bundled skills should be found
    expected = ["deep-research", "code-review", "git-workflow", "daily-standup", "memory-manage"]
    for expected_name in expected:
        assert expected_name in names, f"Bundled skill '{expected_name}' not found. Found: {names}"


# ── Security: inline execution gating ──────────────────────────────────────────

SKILL_WITH_INLINE_CMD = """---
name: exec-test
description: Skill with inline shell commands
---

# Exec Test

Git status: !`echo hello-from-shell`
Date: !`date`
"""


@pytest.fixture
def exec_skill_dir(tmp_path):
    """Create a skill with !`cmd` inline commands."""
    skill_path = tmp_path / "exec-test"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text(SKILL_WITH_INLINE_CMD)
    return tmp_path


def test_render_blocks_inline_exec_by_default(exec_skill_dir):
    """By default, !`cmd` should NOT be executed — syntax preserved as-is."""
    skill_md = exec_skill_dir / "exec-test" / "SKILL.md"
    skill = load_skill_from_path(skill_md)

    rendered = skill.render()
    # The !`cmd` syntax should be preserved as-is
    assert "!`echo hello-from-shell`" in rendered
    assert "!`date`" in rendered
    # Verify the inline command pattern count matches expectations
    import re
    inline_cmds = re.findall(r"!`([^`]+)`", rendered)
    assert len(inline_cmds) == 2, f"Expected 2 preserved !`cmd` blocks, got {len(inline_cmds)}"


def test_render_allows_inline_exec_when_enabled(exec_skill_dir):
    """With allow_exec=True, !`cmd` should run and substitute output."""
    skill_md = exec_skill_dir / "exec-test" / "SKILL.md"
    skill = load_skill_from_path(skill_md)

    rendered = skill.render(allow_exec=True)
    # echo output should be substituted
    assert "hello-from-shell" in rendered
    # The !`cmd` syntax should be gone (replaced by output)
    assert "!`echo hello-from-shell`" not in rendered


# ── Security: package name validation ──────────────────────────────────────────

def test_validate_package_name_valid():
    """Normal package names should pass validation."""
    assert _validate_package_name("httpx", "uv") == (True, "")
    assert _validate_package_name("requests>=2.31", "pip") == (True, "")
    assert _validate_package_name("@octokit/cli", "npm") == (True, "")
    assert _validate_package_name("github.com/cli/cli/v2/cmd/gh@latest", "go") == (True, "")
    assert _validate_package_name("gh", "brew") == (True, "")


def test_validate_package_name_shell_metacharacters():
    """Package names with shell metacharacters should be blocked."""
    bad_names = [
        "httpx; rm -rf /",
        "httpx && evil",
        "httpx | cat /etc/passwd",
        "httpx`whoami`",
        "httpx$(id)",
        "httpx\nmalicious",
    ]
    for name in bad_names:
        valid, reason = _validate_package_name(name, "pip")
        assert not valid, f"Should have rejected: {name!r}"
        assert "metacharacters" in reason or "empty" in reason


def test_validate_package_name_url_blocked():
    """URL-based installs should be blocked."""
    urls = [
        "https://evil.com/backdoor.tar.gz",
        "git+https://github.com/evil/pkg",
        "git://github.com/evil/pkg",
        "ssh://evil.com/pkg",
    ]
    for url in urls:
        valid, reason = _validate_package_name(url, "pip")
        assert not valid, f"Should have rejected URL: {url!r}"
        assert "URL-based" in reason


def test_validate_package_name_path_traversal():
    """Path traversal should be blocked (except for go packages)."""
    valid, reason = _validate_package_name("../../etc/passwd", "pip")
    assert not valid
    assert "path traversal" in reason

    # Go packages with dots are fine (e.g., github.com/user/repo)
    valid, _ = _validate_package_name("github.com/user/repo", "go")
    assert valid


def test_validate_package_name_empty():
    """Empty package names should be rejected."""
    assert _validate_package_name("", "pip")[0] is False
    assert _validate_package_name("   ", "pip")[0] is False


def test_validate_package_name_too_long():
    """Excessively long package names should be rejected."""
    valid, reason = _validate_package_name("a" * 201, "pip")
    assert not valid
    assert "too long" in reason


# ── Security: trust levels ─────────────────────────────────────────────────────

def test_trust_level_local(skill_dir):
    """Skills in workspace_skills_dir should be 'local' trust."""
    registry = SkillRegistry(workspace_skills_dir=skill_dir)
    registry.discover_all()
    skill = registry._get_skill("test-skill")
    assert skill is not None
    assert registry._trust_level(skill) == "local"


def test_trust_level_builtin():
    """Bundled skills should be 'builtin' trust."""
    registry = SkillRegistry()
    registry.discover_all()
    skill = registry._get_skill("deep-research")
    assert skill is not None
    assert registry._trust_level(skill) == "builtin"


def test_trust_level_community(tmp_path):
    """Skills from unknown dirs should be 'community' trust."""
    # Create a skill in an arbitrary directory (not workspace, not bundled)
    external_dir = tmp_path / "external-skills"
    skill_path = external_dir / "sus-skill"
    skill_path.mkdir(parents=True)
    (skill_path / "SKILL.md").write_text("""---
name: sus-skill
description: A suspicious skill
---
# Suspicious
Do things: !`evil_command`
""")
    # Use a different workspace dir so external_dir is NOT local
    workspace = tmp_path / "workspace-skills"
    workspace.mkdir()
    registry = SkillRegistry(workspace_skills_dir=workspace)
    registry.add_directory(external_dir)
    registry.discover_all()
    skill = registry._get_skill("sus-skill")
    assert skill is not None
    assert registry._trust_level(skill) == "community"


# ── Security: install approval gate ────────────────────────────────────────────

SKILL_WITH_INSTALL = """---
name: install-test
description: Skill with install specs
metadata:
  openclaw:
    install:
      - type: pip
        package: httpx
---

# Install Test
Use httpx for HTTP calls.
"""


@pytest.fixture
def install_skill_dir(tmp_path):
    skill_path = tmp_path / "install-test"
    skill_path.mkdir()
    (skill_path / "SKILL.md").write_text(SKILL_WITH_INSTALL)
    return tmp_path


def test_install_approval_auto_approve(install_skill_dir):
    """auto_approve_installs=True should skip the prompt."""
    registry = SkillRegistry(
        workspace_skills_dir=install_skill_dir,
        auto_approve_installs=True,
    )
    # Should load without prompting (httpx is likely already installed)
    content = registry.load_skill("install-test")
    assert content is not None
    assert "Install Test" in content


def test_install_approval_declined(install_skill_dir):
    """Declining install should still render the skill (without the dependency)."""
    registry = SkillRegistry(workspace_skills_dir=install_skill_dir)
    # Mock the approval to decline
    with patch.object(SkillRegistry, '_prompt_install_approval', return_value=False):
        content = registry.load_skill("install-test")
    assert content is not None  # skill still loads, install just skipped


# ── Security: community skill inline exec blocked ─────────────────────────────

def test_community_skill_inline_exec_blocked(tmp_path):
    """Community skills should NOT have !`cmd` executed."""
    external_dir = tmp_path / "external"
    skill_path = external_dir / "evil-skill"
    skill_path.mkdir(parents=True)
    (skill_path / "SKILL.md").write_text("""---
name: evil-skill
description: Skill trying to execute commands
---
# Evil Skill
Output: !`echo pwned`
""")
    workspace = tmp_path / "workspace-skills"
    workspace.mkdir()
    registry = SkillRegistry(workspace_skills_dir=workspace, auto_approve_installs=True)
    registry.add_directory(external_dir)
    content = registry.load_skill("evil-skill")
    assert content is not None
    # !`cmd` should NOT have been executed
    assert "!`echo pwned`" in content
    assert "pwned" not in content.replace("!`echo pwned`", "")


# ── ClawHub / OpenClaw skill format compatibility ─────────────────────────────

# Realistic ClawHub-format SKILL.md (based on actual ClawHub community skills)
CLAWHUB_SKILL_MD = """---
name: coding-agent
description: "Autonomous coding agent — reads code, plans changes, writes diffs, runs tests"
user-invocable: true
disable-model-invocation: false
allowed-tools: bash, read_file, write_file, edit_file, glob, grep, web_search
model: claude-sonnet-4-6
context: fork
argument-hint: "[task description]"
homepage: https://clawhub.ai/skills/coding-agent
metadata:
  openclaw:
    emoji: "\U0001F4BB"
    os: [darwin, linux]
    always: false
    requires:
      bins: [git]
      anyBins: [brew, apt]
      env: []
    install:
      - type: uv
        package: httpx
      - type: brew
        package: gh
        os: [darwin]
      - type: npm
        package: "@anthropic-ai/sdk"
        version: "0.39.0"
---

# Coding Agent

You are an autonomous coding agent. Your task: $ARGUMENTS

## Workflow

1. **Understand**: Read relevant files, understand the codebase structure
2. **Plan**: Create a step-by-step plan before writing any code
3. **Implement**: Write clean, well-tested code
4. **Verify**: Run tests and linters to validate changes

## Context

Working directory: !`pwd`
Git branch: !`git branch --show-current`
Recent changes: !`git log --oneline -3`

## Rules

- Never commit directly to main
- Always run tests before marking complete
- Follow existing code style and conventions
"""

# ClawHub skill using clawdbot alias (older format)
CLAWDBOT_SKILL_MD = """---
name: pr-reviewer
description: "Review pull requests with structured feedback"
user-invocable: true
allowed-tools: bash, read_file
metadata:
  clawdbot:
    emoji: "\U0001F50D"
    os: [darwin, linux, win32]
    always: true
    requires:
      bins: [gh]
---

# PR Reviewer

Review the PR: $ARGUMENTS

Use `gh pr diff` to get the changes, then provide structured feedback.
"""

# Minimal ClawHub skill (just frontmatter basics)
MINIMAL_CLAWHUB_SKILL = """---
name: quick-fix
description: "Quick code fix agent"
metadata:
  openclaw:
    emoji: "\u26A1"
---

Fix this: $ARGUMENTS
"""


@pytest.fixture
def clawhub_skill_dir(tmp_path):
    """Create a directory with multiple ClawHub-format skills."""
    for name, content in [
        ("coding-agent", CLAWHUB_SKILL_MD),
        ("pr-reviewer", CLAWDBOT_SKILL_MD),
        ("quick-fix", MINIMAL_CLAWHUB_SKILL),
    ]:
        skill_path = tmp_path / name
        skill_path.mkdir()
        (skill_path / "SKILL.md").write_text(content)
    return tmp_path


def test_clawhub_full_skill_parse(clawhub_skill_dir):
    """Full ClawHub skill with all OpenClaw metadata should parse correctly."""
    skill_md = clawhub_skill_dir / "coding-agent" / "SKILL.md"
    skill = load_skill_from_path(skill_md)

    assert skill is not None
    assert skill.name == "coding-agent"
    assert "autonomous coding agent" in skill.meta.description.lower()
    assert skill.meta.user_invocable is True
    assert skill.meta.disable_model_invocation is False
    assert skill.meta.model == "claude-sonnet-4-6"
    assert skill.meta.context == "fork"
    assert skill.meta.argument_hint == "[task description]"
    assert skill.meta.homepage == "https://clawhub.ai/skills/coding-agent"

    # allowed-tools parsed from comma-separated string
    assert "bash" in skill.meta.allowed_tools
    assert "read_file" in skill.meta.allowed_tools
    assert "web_search" in skill.meta.allowed_tools

    # OpenClaw metadata
    assert skill.meta.emoji == "\U0001F4BB"
    assert skill.meta.os_platforms == ["darwin", "linux"]
    assert skill.meta.always is False
    assert skill.meta.requires_bins == ["git"]
    assert skill.meta.requires_any_bins == ["brew", "apt"]

    # Install specs
    assert len(skill.meta.install) == 3
    uv_install = skill.meta.install[0]
    assert uv_install.type == "uv"
    assert uv_install.package == "httpx"

    brew_install = skill.meta.install[1]
    assert brew_install.type == "brew"
    assert brew_install.package == "gh"
    assert brew_install.os == ["darwin"]

    npm_install = skill.meta.install[2]
    assert npm_install.type == "npm"
    assert npm_install.package == "@anthropic-ai/sdk"
    assert npm_install.version == "0.39.0"


def test_clawhub_content_render(clawhub_skill_dir):
    """ClawHub skill content should render $ARGUMENTS correctly."""
    skill_md = clawhub_skill_dir / "coding-agent" / "SKILL.md"
    skill = load_skill_from_path(skill_md)

    rendered = skill.render("implement user authentication")
    assert "implement user authentication" in rendered
    assert "autonomous coding agent" in rendered.lower() or "Coding Agent" in rendered
    # !`cmd` should NOT be executed (default allow_exec=False)
    assert "!`pwd`" in rendered
    assert "!`git branch --show-current`" in rendered


def test_clawdbot_alias_parse(clawhub_skill_dir):
    """Skills using metadata.clawdbot alias should parse identically."""
    skill_md = clawhub_skill_dir / "pr-reviewer" / "SKILL.md"
    skill = load_skill_from_path(skill_md)

    assert skill is not None
    assert skill.name == "pr-reviewer"
    assert skill.meta.emoji == "\U0001F50D"
    assert skill.meta.os_platforms == ["darwin", "linux", "win32"]
    assert skill.meta.always is True
    assert skill.meta.requires_bins == ["gh"]


def test_minimal_clawhub_skill(clawhub_skill_dir):
    """Minimal ClawHub skill with just name+description+emoji should work."""
    skill_md = clawhub_skill_dir / "quick-fix" / "SKILL.md"
    skill = load_skill_from_path(skill_md)

    assert skill is not None
    assert skill.name == "quick-fix"
    assert skill.meta.emoji == "\u26A1"
    # Defaults
    assert skill.meta.user_invocable is True
    assert skill.meta.always is False
    assert skill.meta.install == []
    assert skill.meta.requires_bins == []


def test_clawhub_registry_integration(clawhub_skill_dir):
    """ClawHub-format skills should be discoverable and loadable via SkillRegistry."""
    registry = SkillRegistry(workspace_skills_dir=clawhub_skill_dir, auto_approve_installs=True)
    skills = registry.discover_all()

    names = [s.name for s in skills]
    assert "coding-agent" in names
    assert "pr-reviewer" in names
    assert "quick-fix" in names

    # Load with arguments
    content = registry.load_skill("coding-agent", arguments="fix the login bug")
    assert content is not None
    assert "fix the login bug" in content


def test_clawhub_skill_descriptions(clawhub_skill_dir):
    """Skill descriptions for system prompt should include ClawHub skills."""
    registry = SkillRegistry(workspace_skills_dir=clawhub_skill_dir)
    descriptions = registry.get_skill_descriptions()

    assert "coding-agent" in descriptions
    assert "pr-reviewer" in descriptions
    assert "quick-fix" in descriptions
    assert "Available Skills" in descriptions


def test_clawhub_skill_trust_as_local(clawhub_skill_dir):
    """ClawHub skills in workspace dir should be 'local' trust (exec allowed)."""
    registry = SkillRegistry(workspace_skills_dir=clawhub_skill_dir, auto_approve_installs=True)
    registry.discover_all()

    skill = registry._get_skill("coding-agent")
    assert registry._trust_level(skill) == "local"

    # When loaded, !`cmd` should be executed (local trust = allow_exec)
    content = registry.load_skill("coding-agent")
    # !`pwd` should have been executed and replaced with actual output
    assert "!`pwd`" not in content


def test_clawhub_skill_trust_as_community(tmp_path):
    """ClawHub skills added via add_directory (external) should be 'community' trust."""
    # Simulate: skill downloaded from clawhub to a non-workspace directory
    clawhub_cache = tmp_path / "clawhub-cache"
    skill_path = clawhub_cache / "coding-agent"
    skill_path.mkdir(parents=True)
    (skill_path / "SKILL.md").write_text(CLAWHUB_SKILL_MD)

    workspace = tmp_path / "workspace-skills"
    workspace.mkdir()
    registry = SkillRegistry(workspace_skills_dir=workspace, auto_approve_installs=True)
    registry.add_directory(clawhub_cache)

    content = registry.load_skill("coding-agent", arguments="test task")
    assert content is not None
    # Community trust: !`cmd` should NOT have been executed
    assert "!`pwd`" in content
    assert "test task" in content


def test_wheel_shared_data_includes_bundled_skills():
    """Wheel build config should include repo-level bundled skills."""
    import tomllib

    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    data = tomllib.loads(pyproject)
    shared = data["tool"]["hatch"]["build"]["targets"]["wheel"]["shared-data"]
    assert shared["skills"] == "share/agnoclaw/skills"
