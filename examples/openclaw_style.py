"""
OpenClaw-style agent setup — full persona, workspace, and heartbeat.

Demonstrates the full OpenClaw-inspired feature set:
- Rich workspace: SOUL.md (persona), IDENTITY.md (capabilities),
  USER.md (user context), TOOLS.md (tool policy), BOOT.md (startup)
- Heartbeat daemon with a custom checklist
- Skills (custom workspace-level skill)
- ProgressToolkit for multi-session project tracking

Run: uv run python examples/openclaw_style.py
Requires: ANTHROPIC_API_KEY env var

This is the "everything wired up" reference. For a minimal example,
see examples/basic_agent.py.
"""

import asyncio
import tempfile
from pathlib import Path

from agnoclaw import AgentHarness
from agnoclaw.heartbeat import HeartbeatDaemon
from agnoclaw.workspace import Workspace


def setup_workspace(workspace_path: Path) -> Workspace:
    """Configure a workspace with full OpenClaw-style files."""
    ws = Workspace(workspace_path)
    ws.initialize()

    # ── SOUL.md — persona and tone ────────────────────────────────────────
    ws.write_file("soul", """# Soul

You are a disciplined, pragmatic engineering assistant.

**Tone:** Direct and confident. Use bullet points over paragraphs.
Use code examples over prose explanations. Flag risks immediately.

**Values:**
- Correctness over cleverness
- Reversible actions over irreversible ones
- Ask once rather than guess repeatedly
- "I don't know" beats a confident wrong answer

**Anti-patterns to avoid:**
- Long preambles before the actual answer
- Restating the question before answering it
- Hedging everything with "it depends" without elaborating
""")

    # ── IDENTITY.md — capabilities declaration ────────────────────────────
    ws.write_file("identity", """# Identity

I am a full-stack Python engineering assistant. My capabilities:

**Specialized in:**
- Python 3.12+ (async/await, type hints, dataclasses, protocols)
- Testing: pytest, pytest-asyncio, hypothesis, factory_boy
- Code quality: ruff, mypy, black, pre-commit
- Databases: PostgreSQL + SQLAlchemy, SQLite, Redis
- APIs: FastAPI, httpx, pydantic v2

**I will NOT do without explicit confirmation:**
- `git push` to remote branches
- `rm -rf` operations
- Database schema migrations in production
- Changes to .env or secrets files

**Knowledge cutoff:** August 2025. Always check PyPI for newer versions.
""")

    # ── USER.md — user context ────────────────────────────────────────────
    ws.write_file("user", """# User

**Name:** Developer
**Timezone:** UTC
**Stack:** Python 3.12, FastAPI, PostgreSQL, Docker
**Preferences:**
- Short answers first, details on request
- Show the diff, not the whole file
- pytest for all tests (no unittest)
- Type hints on all public functions
""")

    # ── TOOLS.md — tool policy ────────────────────────────────────────────
    ws.write_file("tools", """# Tool Configuration

## Bash
Allowed without confirmation:
- git status, git diff, git log, git add, git commit
- pytest, ruff, mypy, black
- uv pip install, uv run

Require confirmation:
- git push, git reset --hard, git branch -D
- rm -rf, find . -delete
- docker system prune

## File Operations
- Source root: src/
- Tests: tests/
- Config: pyproject.toml (never .env directly)

## Web
- Prioritize: docs.python.org, pypi.org, github.com
""")

    # ── BOOT.md — startup protocol ────────────────────────────────────────
    ws.write_file("boot", """# Boot Protocol

At the start of EVERY session:

1. Run `git status` — summarize any uncommitted changes in one line
2. Check MEMORY.md for open TODOs from previous sessions
3. Read features.md if it exists (project completion checklist)
4. Report: "Ready. [N open TODOs, M features pending]"

Skip tests and analysis during boot — just gather state.
""")

    # ── HEARTBEAT.md — periodic checklist ─────────────────────────────────
    ws.write_file("heartbeat", """# Heartbeat Checklist

Check each item and surface anything that needs attention:

- [ ] Any failing CI checks on the current branch?
- [ ] Unaddressed review comments on open PRs?
- [ ] Dependencies with known security vulnerabilities (check pip audit)?
- [ ] TODO/FIXME comments added in the last 24 hours?

If nothing needs attention, reply HEARTBEAT_OK.
""")

    # ── Workspace-level skill ─────────────────────────────────────────────
    skill_dir = ws.skills_dir() / "code-review"
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / "SKILL.md").write_text("""---
name: code-review
description: Review code for correctness, style, security, and test coverage.
user_invocable: true
model_invocable: true
allowed_tools: [files, bash]
---

# Code Review Skill

You are conducting a thorough code review. For each file or change:

## Checklist
- [ ] **Correctness**: Does the logic do what it claims? Edge cases handled?
- [ ] **Types**: Are type hints complete and accurate?
- [ ] **Tests**: Are there tests? Do they cover the main paths and error cases?
- [ ] **Security**: Any injection risks, insecure defaults, or exposed secrets?
- [ ] **Performance**: Any obvious O(n²) issues or unnecessary I/O in hot paths?
- [ ] **Style**: Does it match the project's conventions?

## Output Format
1. Summary (1-2 sentences)
2. Issues (numbered list, Critical/Major/Minor)
3. Suggestions (optional improvements)
4. Verdict: APPROVE / REQUEST_CHANGES
""", encoding="utf-8")

    print(f"Workspace initialized: {ws.path}")
    return ws


async def demo_heartbeat(agent: AgentHarness, ws: Workspace) -> None:
    """Trigger a single heartbeat check to demonstrate the system."""

    def on_alert(msg: str) -> None:
        print(f"\n{'=' * 50}")
        print("HEARTBEAT ALERT:")
        print(msg)
        print("=" * 50)

    daemon = HeartbeatDaemon(agent, on_alert=on_alert, workspace=ws)

    print("\n--- Heartbeat check ---")
    result = await daemon.trigger_now()
    if result:
        print(f"Alert: {result}")
    else:
        print("HEARTBEAT_OK — nothing needs attention.")


def demo_agent_with_skill(agent: AgentHarness) -> None:
    """Run the agent with the code-review skill activated."""
    print("\n--- Agent with code-review skill ---")
    agent.print_response(
        "Review this function for correctness and style:\n\n"
        "```python\n"
        "def divide(a, b):\n"
        "    return a / b\n"
        "```",
        stream=True,
        skill="code-review",
    )


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="agnoclaw-openclaw-") as tmpdir:
        workspace_path = Path(tmpdir) / "workspace"

        # 1. Configure workspace with full OpenClaw-style files
        ws = setup_workspace(workspace_path)

        # 2. Boot agent — workspace context injected into system prompt automatically
        agent = AgentHarness(
            name="openclaw-demo",
            session_id="openclaw-demo-session",
            workspace_dir=workspace_path,
        )
        print(f"\nAgent ready. Workspace context loaded: {list(agent.workspace.context_files().keys())}")

        # 3. Ask the agent about its guidelines (it reads from AGENTS.md + SOUL.md)
        print("\n--- Agent identity check ---")
        agent.print_response(
            "In one sentence: what are your core behavioral constraints?",
            stream=True,
        )

        # 4. Demonstrate skill activation
        demo_agent_with_skill(agent)

        # 5. Trigger heartbeat check
        await demo_heartbeat(agent, ws)

        # 6. Session end: save progress to MEMORY.md
        ws.append_to_memory(
            "## Session: OpenClaw demo\n"
            "- Demonstrated full workspace setup (SOUL/IDENTITY/USER/TOOLS/BOOT)\n"
            "- code-review skill activated and tested\n"
            "- Heartbeat daemon triggered once\n"
        )
        ws.write_session_summary(
            "Full OpenClaw-style demo session completed. "
            "All workspace files written and agent loaded context correctly."
        )
        print("\nSession summary saved to MEMORY.md and daily log.")


if __name__ == "__main__":
    asyncio.run(main())
