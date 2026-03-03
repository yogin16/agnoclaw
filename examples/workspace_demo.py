"""
Example: Workspace Configuration Demo

Demonstrates:
- Workspace file creation (AGENTS.md, SOUL.md, IDENTITY.md, TOOLS.md, BOOT.md)
- Custom workspace directory per project
- Reading and writing workspace files
- Daily memory logs
"""

import tempfile
from pathlib import Path

from _utils import detect_model
from agnoclaw import AgentHarness
from agnoclaw.workspace import Workspace


# ── Create a project-specific workspace ──────────────────────────────────

with tempfile.TemporaryDirectory(prefix="agnoclaw-workspace-") as tmpdir:
    workspace_path = Path(tmpdir) / "my-project-workspace"

    ws = Workspace(workspace_path)
    ws.initialize()

    print(f"Workspace created at: {ws.path}")
    print(f"Files: {[f.name for f in ws.path.iterdir()]}\n")

    # ── Write custom AGENTS.md ────────────────────────────────────────────
    ws.write_file("agents", """# Agent Guidelines — My Project

You are a specialized agent for the MyProject Python library.

- Always use our internal error types (see src/exceptions.py)
- Tests live in tests/unit/ and tests/integration/
- We use pytest with pytest-asyncio for async tests
- All new functions need type hints and docstrings
- Check MEMORY.md for ongoing work context
""")

    # ── Write custom SOUL.md ──────────────────────────────────────────────
    ws.write_file("soul", """# Soul — My Project Agent

You are a pragmatic, no-nonsense engineering assistant.
- Prefer working code over theoretical explanations
- Show examples, not just prose
- Flag security issues immediately — don't wait to be asked
- Be terse. A 5-line answer beats a 20-line essay.
""")

    # ── Write IDENTITY.md (OpenClaw-style) ───────────────────────────────
    ws.write_file("identity", """# Identity

I am the MyProject engineering assistant. My capabilities:

**I excel at:**
- Python code review and refactoring
- Writing and fixing pytest test suites
- Database query optimization (PostgreSQL)
- Debugging async/await issues

**I do NOT do:**
- Frontend work (redirect to the UI team)
- Infrastructure changes (require DevOps approval)
- Production database modifications without explicit confirmation

**My limitations:**
- I can't access internal Confluence docs (use the wiki tool)
- My knowledge cutoff is 2025-08 — check for newer library versions
""")

    # ── Write TOOLS.md (OpenClaw-style tool config) ───────────────────────
    ws.write_file("tools", """# Tool Configuration

## Bash
- Allowed: pytest, git, pip, uv, ruff, black, mypy
- Require confirmation: rm, cp -r, git push, docker

## File Operations
- Source root: src/
- Tests root: tests/
- Config: pyproject.toml, .env.example (never .env)

## Web Search
- Prefer: docs.python.org, pypi.org, github.com/[repo]
- Avoid: w3schools, tutorialspoint (low quality)
""")

    # ── Write BOOT.md (startup protocol) ─────────────────────────────────
    ws.write_file("boot", """# Boot Protocol

At session start, always:

1. Run `git status` to check for uncommitted changes
2. Read MEMORY.md for context from previous sessions
3. Check if any TODO items in MEMORY.md are still open
4. Greet the user with a one-line status: "Ready. [N] pending TODOs."

Do NOT run tests or make changes during boot — only gather context.
""")

    # ── Read back context files ───────────────────────────────────────────
    context = ws.context_files()
    print(f"Loaded context files: {list(context.keys())}\n")

    # ── Append to memory ─────────────────────────────────────────────────
    ws.append_to_memory("## 2026-02-23\n- Started workspace demo\n- Set up custom guidelines")

    # ── Daily log ────────────────────────────────────────────────────────
    ws.log_to_daily("Ran workspace demo. All files created successfully.")

    # ── Session summary (for compaction) ─────────────────────────────────
    ws.write_session_summary(
        "Demonstrated workspace setup. Key findings: BOOT.md protocol works. "
        "TOOLS.md helps constrain agent tool usage."
    )

    # ── Use the workspace with an agent ──────────────────────────────────
    agent = AgentHarness(
        name="project-agent",
        model=detect_model(),
        workspace_dir=workspace_path,
    )

    result = agent.run("What are your behavioral guidelines for this project?")
    print("=== Agent with Custom Workspace ===")
    print(result.content)
