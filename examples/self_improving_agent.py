"""
Self-improving agent example.

Demonstrates agnoclaw's self-improving-agent skill — an OpenClaw-inspired pattern
for capturing corrections, errors, and feature requests across sessions.

How OpenClaw "self-learning" actually works (researched Feb 2026):
  - NOT a built-in engine or magic AI feature
  - It's a community SKILL.md pattern that writes structured entries to .learnings/
  - Entries get promoted to workspace files (AGENTS.md, SOUL.md, TOOLS.md) when stable
  - Pre-compaction flush: agent consolidates learnings before context window truncates
  - This is separate from the memory system (MEMORY.md) and LearningMachine (SQLite)

Three files in .learnings/:
  - LEARNINGS.md   — corrections, patterns, best practices (LRN-YYYYMMDD-NNN)
  - ERRORS.md      — command failures + workarounds (ERR-YYYYMMDD-NNN)
  - FEATURE_REQUESTS.md — capability gaps (FEAT-YYYYMMDD-NNN)

Promotion targets: stable patterns → AGENTS.md, SOUL.md, TOOLS.md, IDENTITY.md

Run:
    uv run python examples/self_improving_agent.py

No API key needed — uses Ollama (qwen3:0.6b) by default.
Set AGNOCLAW_TEST_PROVIDER=anthropic for cloud inference.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

PROVIDER = os.environ.get("AGNOCLAW_TEST_PROVIDER", "ollama")
MODEL = os.environ.get(
    "AGNOCLAW_TEST_MODEL",
    "qwen3:0.6b" if PROVIDER == "ollama" else "claude-haiku-4-5-20251001",
)


def _check_ollama() -> bool:
    if PROVIDER != "ollama":
        return True
    try:
        import httpx
        r = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


# ── Part 1: Show skill metadata (no API) ──────────────────────────────────────

def demo_skill_discovery():
    """Discover the self-improving-agent skill without running it."""
    from agnoclaw.skills.registry import SkillRegistry

    print("=== Skill Discovery ===")
    registry = SkillRegistry()
    skills = registry.list_skills()
    sip = next((s for s in skills if s["name"] == "self-improving-agent"), None)

    if sip:
        print(f"  name:       {sip['name']}")
        print(f"  description:{sip['description']}")
        print(f"  user-invocable: {sip['user_invocable']}")
        print(f"  model-invocable: {sip['model_invocable']}")
        print(f"  allowed-tools: {', '.join(sip['allowed_tools'])}")
    else:
        print("  Skill not found (run from repo root or check skills/ dir)")
    print()


# ── Part 2: .learnings/ file format ───────────────────────────────────────────

def demo_learnings_format(tmp: Path):
    """Create example .learnings/ files showing the entry format."""
    print("=== .learnings/ File Format ===")

    learnings_dir = tmp / ".learnings"
    learnings_dir.mkdir()

    # Example LEARNINGS.md entry
    (learnings_dir / "LEARNINGS.md").write_text("""# Learnings

Corrections, patterns, and best practices discovered through experience.
See ERRORS.md for command failures, FEATURE_REQUESTS.md for capability gaps.

### LRN-20260223-001: Always use absolute paths in file tools

- **id**: LRN-20260223-001
- **timestamp**: 2026-02-23T14:30:00Z
- **priority**: high
- **status**: pending
- **area**: tools
- **source**: user correction ("that edit failed because you used a relative path")
- **related_files**: src/agnoclaw/tools/files.py

**Summary**: File tools require absolute paths — relative paths silently fail.

**Details**: When calling read_file() or edit_file(), always expand to absolute
paths. Relative paths like "src/foo.py" resolve relative to the process cwd,
not the workspace, causing "file not found" errors that look like the file
doesn't exist.

**Suggested action**: Add to TOOLS.md: "Always use absolute paths in file tools."
""", encoding="utf-8")

    # Example ERRORS.md entry
    (learnings_dir / "ERRORS.md").write_text("""# Errors

Command failures, unexpected tool behavior, and their workarounds.

### ERR-20260223-001: pip install fails in uv-managed project

- **id**: ERR-20260223-001
- **timestamp**: 2026-02-23T15:00:00Z
- **priority**: medium
- **status**: pending
- **area**: workflow
- **source**: command failure (exit code 1)

**Summary**: In projects managed by uv, `pip install X` fails. Use `uv add X`.

**Details**: Running `pip install X` in a uv-managed virtualenv raises
"target environment is managed by uv" error. The correct command is `uv add X`
(adds to pyproject.toml) or `uv pip install X` for one-off installs.

**Suggested action**: Add to TOOLS.md: "Use 'uv add X' not 'pip install X'."
""", encoding="utf-8")

    # Example FEATURE_REQUESTS.md entry
    (learnings_dir / "FEATURE_REQUESTS.md").write_text("""# Feature Requests

Capabilities requested by users but not yet available.

### FEAT-20260223-001: Support for reading PDF files

- **id**: FEAT-20260223-001
- **timestamp**: 2026-02-23T16:00:00Z
- **priority**: low
- **status**: pending
- **area**: tools

**Summary**: User asked to read a PDF — read_file() doesn't handle binary.

**Suggested action**: Add PDF reading support to FilesToolkit (use pypdf).
""", encoding="utf-8")

    print(f"  Created: {learnings_dir}/")
    for f in sorted(learnings_dir.iterdir()):
        lines = f.read_text(encoding="utf-8").count("\n")
        print(f"    {f.name}: {lines} lines")
    print()
    return learnings_dir


# ── Part 3: Trigger with agent (live) ────────────────────────────────────────

def demo_with_agent(tmp: Path, learnings_dir: Path):
    """
    Run the self-improving-agent skill with a live agent.
    Ask it to review pending learnings and summarize.
    """
    from agnoclaw import AgentHarness

    print("=== Live Agent Demo: Review Learnings ===")

    agent = AgentHarness(
        provider=PROVIDER,
        model_id=MODEL,
        workspace_dir=tmp,
        session_id="self-improving-demo",
    )

    # Tell the agent about the learnings and ask for a review
    prompt = f"""I want to review my agent learnings.

The .learnings/ directory is at: {learnings_dir}

Please:
1. Read all three files (LEARNINGS.md, ERRORS.md, FEATURE_REQUESTS.md)
2. List the pending entries with their IDs and priorities
3. Identify which entries are ready to promote to workspace files
4. For each promotable entry, say which workspace file it should go to

Keep your response concise."""

    response = agent.run(prompt, skill="self-improving-agent")
    print(f"  {str(response.content)[:500]}...")
    print()


# ── Part 4: Promotion pattern ─────────────────────────────────────────────────

def demo_promotion_pattern():
    """Show how learnings get promoted to workspace files."""
    print("=== Promotion Pattern ===")
    print()
    print("  When a learning is confirmed stable (seen 2+ times or high priority),")
    print("  it gets promoted to a workspace file:")
    print()
    print("  Learning area → Promotion target:")
    print("  ─────────────────────────────────────────────")
    print("  Behavioral rules ('always X', 'never Y') → AGENTS.md")
    print("  Tool usage patterns                       → TOOLS.md")
    print("  Identity/persona adjustments              → SOUL.md")
    print("  Capability/knowledge updates              → IDENTITY.md")
    print()
    print("  Example promotion command:")
    print('  agent.run("promote LRN-20260223-001 to TOOLS.md", skill="self-improving-agent")')
    print()
    print("  After promotion, the entry status changes from 'pending' → 'promoted'")
    print("  and the workspace file gains a new bullet under the relevant section.")
    print()


# ── Part 5: Comparison with LearningMachine ──────────────────────────────────

def demo_comparison():
    """Clarify when to use self-improving-agent vs LearningMachine."""
    print("=== Self-improving vs LearningMachine ===")
    print()
    print("  Two different memory mechanisms — complement each other:")
    print()
    print("  self-improving-agent skill:")
    print("    - Writes Markdown files (.learnings/*.md)")
    print("    - Human-readable, git-trackable")
    print("    - Agent-controlled: records when it makes/observes mistakes")
    print("    - Promotes to workspace files (AGENTS.md etc.)")
    print("    - Good for: corrections, workflow patterns, capability gaps")
    print()
    print("  LearningMachine (Agno, SQLite-backed):")
    print("    - Writes structured records to SQLite")
    print("    - Cross-session, cross-user institutional memory")
    print("    - Three stores: entity_memory, learned_knowledge, decision_log")
    print("    - Auto-retrieved and injected into context per session")
    print("    - Good for: project entities, technical patterns, decision rationale")
    print()
    print("  Use both: skill for workspace evolution, LearningMachine for deep patterns.")
    print()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("agnoclaw Self-Improving Agent Demo")
    print("=" * 50)
    print()
    print("Based on OpenClaw's community self-improving-agent + agent-reflect skills.")
    print("'Self-learning' = structured .learnings/ capture + workspace file promotion.")
    print()

    demo_skill_discovery()

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)

        # Part 2 — no API needed
        learnings_dir = demo_learnings_format(tmp)

        # Part 3 — live agent (Ollama or cloud)
        if _check_ollama() or PROVIDER != "ollama":
            demo_with_agent(tmp, learnings_dir)
        else:
            print("=== Live Agent Demo ===")
            print("  (Skipped: Ollama not running. Start with: ollama serve)")
            print()

    # Parts 4 + 5 — informational
    demo_promotion_pattern()
    demo_comparison()

    print("Done.")


if __name__ == "__main__":
    main()
