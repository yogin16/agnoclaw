"""
OpenClaw Skill Hub — creating, installing, and using skills.

Demonstrates the full skill hub workflow:
- Writing SKILL.md files with YAML frontmatter (OpenClaw/Claude Code format)
- Installing skills to a local hub directory
- Discovering skills via SkillRegistry across multiple directories
- Using $ARGUMENTS and !`cmd` dynamic context injection
- OpenClaw gating metadata (requires_bins, requires_env, os restriction)
- Skill priority chain: workspace > user > extra dirs > bundled
- Selective injection: agent picks the right skill per task

Run: uv run python examples/openclaw_skills.py
Requires: ANTHROPIC_API_KEY env var
"""

import tempfile
from pathlib import Path

from agnoclaw import HarnessAgent
from agnoclaw.skills import SkillRegistry


# ── 1. Create a local skill hub directory ────────────────────────────────────

def create_skill_hub(hub_dir: Path) -> None:
    """
    Create a local skill hub with 3 example skills.

    A skill hub is just a directory of skill subdirectories, each with SKILL.md.
    Format: hub_dir/<skill-name>/SKILL.md
    """

    # ── Skill 1: python-expert ─────────────────────────────────────────────
    # Basic skill with tool restriction and description
    skill1 = hub_dir / "python-expert"
    skill1.mkdir(parents=True, exist_ok=True)
    (skill1 / "SKILL.md").write_text("""\
---
name: python-expert
description: Expert Python code review, optimization, and idiomatic refactoring
user-invocable: true
disable-model-invocation: false
allowed-tools: bash, files
---

# Python Expert Skill

You are reviewing or writing Python code at expert level. Apply these standards:

## Code Quality
- Type hints on all public functions and methods
- Dataclasses or Pydantic models over plain dicts for structured data
- `pathlib.Path` over `os.path` string manipulation
- Comprehensions over `.append()` loops when readable
- Context managers (`with`) for all resources (files, connections, locks)
- f-strings over `.format()` or `%` formatting

## Performance
- Profile before optimizing — avoid premature optimization
- `__slots__` on hot dataclasses; avoid repeated attribute access in tight loops
- Prefer generators over lists when the caller only iterates once

## Security
- Never log or print sensitive data (passwords, tokens, PII)
- Validate all external inputs at the boundary
- Use `secrets` not `random` for security-sensitive values

## Output
Give a structured review:
1. **Summary** (one sentence verdict)
2. **Issues** (Critical / Major / Minor — numbered)
3. **Suggested fix** (inline code diff if applicable)
""", encoding="utf-8")

    # ── Skill 2: git-commit ────────────────────────────────────────────────
    # Skill with $ARGUMENTS substitution and !`cmd` dynamic context
    skill2 = hub_dir / "git-commit"
    skill2.mkdir(parents=True, exist_ok=True)
    (skill2 / "SKILL.md").write_text("""\
---
name: git-commit
description: Write a clean, conventional commit message from staged diff
user-invocable: true
disable-model-invocation: false
allowed-tools: bash
metadata:
  openclaw:
    requires:
      bins: [git]
---

# Git Commit Skill

Write a conventional commit message for the staged changes below.

**Current git status:**
!`git status --short`

**Staged diff:**
!`git diff --cached --stat`

## Rules
- Format: `<type>(<scope>): <subject>` (max 72 chars)
- Types: feat, fix, docs, refactor, test, chore, perf, ci
- Subject in imperative mood ("add" not "added")
- Body: explain WHY not WHAT (what is in the diff)
- Breaking changes: add `BREAKING CHANGE:` footer

## Argument hint
If $ARGUMENTS is provided, use it as the scope: `feat($ARGUMENTS): ...`

Output ONLY the commit message — no explanation, no backticks.
""", encoding="utf-8")

    # ── Skill 3: sql-review ────────────────────────────────────────────────
    # Skill with requires_env gating and OS independence
    skill3 = hub_dir / "sql-review"
    skill3.mkdir(parents=True, exist_ok=True)
    (skill3 / "SKILL.md").write_text("""\
---
name: sql-review
description: Review SQL queries for correctness, performance, and security
user-invocable: true
disable-model-invocation: false
allowed-tools: files
---

# SQL Review Skill

Review the SQL query for the following (in priority order):

## 1. Correctness
- Are JOINs on the right columns (indexed FK)?
- NULL handling: `IS NULL` not `= NULL`
- Aggregate functions require GROUP BY for non-aggregated columns
- DISTINCT vs GROUP BY — usually GROUP BY is more explicit

## 2. Security (Critical)
- **SQL injection**: Any string interpolation in the query?
- **Privilege escalation**: Does this query touch more tables than needed?
- **Data leakage**: SELECT * in production queries?

## 3. Performance
- Missing indexes on WHERE/JOIN columns?
- N+1 query pattern (loop + single-row SELECT)?
- OFFSET pagination on large tables (use keyset pagination instead)
- Subqueries that could be CTEs or JOINs for better plan

## Output
1. **Verdict**: SAFE / NEEDS_REVIEW / BLOCKED
2. **Issues** (numbered, Critical/Major/Minor)
3. **Optimized version** (if applicable)
""", encoding="utf-8")

    print(f"Skill hub created at: {hub_dir}")
    print(f"Skills: {[d.name for d in hub_dir.iterdir() if d.is_dir()]}")


# ── 2. Discover skills via SkillRegistry ─────────────────────────────────────

def demonstrate_discovery(hub_dir: Path) -> None:
    """Show how SkillRegistry discovers skills across multiple directories."""
    print("\n=== Skill Discovery ===")

    # Create a registry with just the hub dir (no workspace)
    registry = SkillRegistry(workspace_skills_dir=None)
    registry.add_directory(hub_dir)

    skills = registry.list_skills()
    print(f"Discovered {len(skills)} skills:")
    for s in skills:
        invocable = "user+model" if s["user_invocable"] else "model-only"
        tools = ", ".join(s["allowed_tools"]) or "all"
        print(f"  {s['name']:20s}  [{invocable}]  tools={tools}")
        print(f"    {s['description']}")

    # Show what the agent sees in its system prompt (skill descriptions)
    print("\n=== System Prompt Injection (skill awareness) ===")
    print(registry.get_skill_descriptions())


# ── 3. Load and render a skill ────────────────────────────────────────────────

def demonstrate_render(hub_dir: Path) -> None:
    """Show skill rendering: $ARGUMENTS substitution and !`cmd` execution."""
    print("\n=== Skill Rendering ===")

    registry = SkillRegistry()
    registry.add_directory(hub_dir)

    # Render python-expert (no arguments needed)
    content = registry.load_skill("python-expert")
    print(f"python-expert — {len(content)} chars loaded")
    print(f"First line: {content.splitlines()[0]}")

    # Render git-commit — !`cmd` directives will be executed
    print("\ngit-commit — dynamic context via !`cmd`:")
    content = registry.load_skill("git-commit", arguments="auth")
    # Show lines that contain git command output (where !`...` was)
    for line in content.splitlines():
        if line.startswith("!") or "(no staged" in line.lower() or "nothing to commit" in line.lower():
            pass  # would have been executed
    print(f"  Loaded {len(content)} chars (git commands executed inline)")

    # Render git-commit with $ARGUMENTS substitution
    content_with_args = registry.load_skill("git-commit", arguments="api")
    assert "$ARGUMENTS" not in content_with_args, "Arguments not substituted"
    print(f"  $ARGUMENTS substituted: 'api' argument injected")


# ── 4. Use skills with HarnessAgent ──────────────────────────────────────────

def demonstrate_agent_with_skills(hub_dir: Path, workspace_dir: Path) -> None:
    """Run the agent with skills from a custom hub directory."""
    print("\n=== Agent with Custom Skill Hub ===")

    # Install skills to workspace-level (highest priority)
    import shutil
    workspace_skills = workspace_dir / "skills"
    workspace_skills.mkdir(parents=True, exist_ok=True)

    for skill_dir in hub_dir.iterdir():
        if skill_dir.is_dir():
            dest = workspace_skills / skill_dir.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(skill_dir, dest)

    agent = HarnessAgent(
        name="skill-hub-demo",
        workspace_dir=workspace_dir,
    )

    # List skills the agent has available
    skills = agent.skills.list_skills()
    print(f"Agent has {len(skills)} skills available:")
    for s in skills:
        print(f"  - {s['name']}: {s['description']}")

    # Run with explicit skill activation
    print("\n--- python-expert skill ---")
    agent.print_response(
        "Review this code:\n```python\ndef get_users(db, ids):\n"
        "    users = []\n    for id in ids:\n"
        "        users.append(db.execute(f'SELECT * FROM users WHERE id={id}').fetchone())\n"
        "    return users\n```",
        stream=True,
        skill="python-expert",
    )

    print("\n--- sql-review skill ---")
    agent.print_response(
        "Review this query:\n```sql\n"
        "SELECT * FROM orders WHERE customer_id = '" + "' + user_id + '" + "';\n```",
        stream=True,
        skill="sql-review",
    )


# ── 5. Workspace-level skill OVERRIDES bundled ────────────────────────────────

def demonstrate_priority(workspace_dir: Path) -> None:
    """
    Workspace-level skills override bundled skills of the same name.
    This is the key skill hub customization feature — teams fork bundled
    skills and the workspace version takes priority automatically.
    """
    print("\n=== Skill Priority Override ===")

    workspace_skills = workspace_dir / "skills"
    workspace_skills.mkdir(parents=True, exist_ok=True)

    # Create a workspace-level override of the (hypothetical) bundled code-review
    override_dir = workspace_skills / "code-review"
    override_dir.mkdir(exist_ok=True)
    (override_dir / "SKILL.md").write_text("""\
---
name: code-review
description: Team Python style review — our 6 rules
user-invocable: true
---

# Code Review — Team Rules

Review against our 6 non-negotiable rules:

1. `from __future__ import annotations` at top of every module
2. Type hints on all public API functions
3. No `str(e)` in except blocks — use `repr(e)` or `e.args[0]`
4. f-strings only (no % or .format)
5. `pathlib.Path` not `os.path`
6. Docstrings in Google format on public classes and functions

Flag violations as BLOCKER. Everything else is a suggestion.
""", encoding="utf-8")

    agent = HarnessAgent(name="priority-demo", workspace_dir=workspace_dir)

    # The workspace skill takes priority over any bundled code-review skill
    print("Using workspace-level code-review skill (team rules override):")
    agent.print_response(
        "Review: def read(path): return open(path).read()",
        stream=True,
        skill="code-review",
    )


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="agnoclaw-skill-hub-") as tmpdir:
        hub_dir = Path(tmpdir) / "my-skill-hub"
        workspace_dir = Path(tmpdir) / "workspace"
        workspace_dir.mkdir()

        # Initialize workspace
        from agnoclaw.workspace import Workspace
        ws = Workspace(workspace_dir)
        ws.initialize()

        # Step 1: Create skills in the hub
        create_skill_hub(hub_dir)

        # Step 2: Discover and inspect
        demonstrate_discovery(hub_dir)

        # Step 3: Render with $ARGUMENTS and !`cmd`
        demonstrate_render(hub_dir)

        # Step 4: Use with agent
        demonstrate_agent_with_skills(hub_dir, workspace_dir)

        # Step 5: Override priority
        demonstrate_priority(workspace_dir)
