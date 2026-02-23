"""
Example: Skills System Demo

Demonstrates:
- Listing available skills
- Activating a skill per-run
- Skill override priority (workspace > user > bundled)
- Multiple skills in a session
- Skill metadata inspection

Run: uv run python examples/skill_demo.py
Requires: ANTHROPIC_API_KEY env var
"""

import shutil
from agnoclaw import AgentHarness


# ── Inspect available skills ──────────────────────────────────────────────

agent = AgentHarness(name="skill-demo")
registry = agent.skills

print("=== Available Skills ===")
for skill in registry.list_skills():
    # list_skills() returns dicts: {name, description, user_invocable, ...}
    print(f"  {skill['name']:20s}  {skill['description'] or '(no description)'}")

print()

# ── Activate a skill per-run ──────────────────────────────────────────────
# The skill's SKILL.md is injected into context for this run only.
# Next run reverts to the base system prompt.

print("=== Code Review Skill ===")
agent.print_response(
    "Review this function for issues:\n```python\ndef process(data):\n    result = []\n    for i in range(len(data)):\n        result.append(data[i] * 2)\n    return result\n```",
    stream=True,
    skill="code-review",
)

print("\n=== Deep Research Skill ===")
agent.print_response(
    "What is the current state of quantum computing error correction?",
    stream=True,
    skill="deep-research",
)

print("\n=== Git Workflow Skill ===")
agent.print_response(
    "I need to squash my last 3 commits and write a clean commit message",
    stream=True,
    skill="git-workflow",
)


# ── Skill metadata inspection ─────────────────────────────────────────────

print("\n=== Skill Details (from system prompt injection) ===")
descriptions = registry.get_skill_descriptions()
print(descriptions[:500] + "..." if len(descriptions) > 500 else descriptions)


# ── Workspace-level skill override ───────────────────────────────────────
# Create a workspace-level skill that overrides the bundled version.
# Workspace skills have the highest priority in the loading chain.

workspace_skills_dir = agent.workspace.skills_dir()
custom_review_dir = workspace_skills_dir / "code-review"
custom_review_dir.mkdir(parents=True, exist_ok=True)

custom_skill_md = """\
---
name: code-review
description: Custom code review focused on our team's Python style guide
---

# Code Review — Team Python Style

Review code against our specific standards:

1. Type hints required on all public functions
2. Docstrings in Google format
3. No bare `except:` clauses
4. f-strings preferred over .format() or %
5. Pathlib over os.path
6. `from __future__ import annotations` at top of every module

Focus on these 5 rules above all else. Be concise.
"""

(custom_review_dir / "SKILL.md").write_text(custom_skill_md, encoding="utf-8")

# Reload registry — workspace skill now takes priority
agent2 = AgentHarness(name="skill-demo-2")
print("\n=== Custom Workspace Code Review Skill ===")
agent2.print_response(
    "Review: def add(a, b): return a + b",
    stream=True,
    skill="code-review",
)

# Cleanup
shutil.rmtree(custom_review_dir, ignore_errors=True)
