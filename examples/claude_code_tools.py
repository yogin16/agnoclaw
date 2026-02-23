"""
Claude Code tools comparison example.

Shows the tools agnoclaw provides, what's been added (MultiEdit), and
what's still missing vs Claude Code v2.1.50.

This is a reference/documentation example — most sections don't require
an API key and can be run for free.

Run:
    uv run python examples/claude_code_tools.py

No API key needed for most sections.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

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


# ── Part 1: Tool inventory ─────────────────────────────────────────────────────

def demo_tool_inventory():
    """Show all tools registered in a default AgentHarness."""
    print("=== agnoclaw Default Tool Inventory ===")
    print()

    from agnoclaw.tools import get_default_tools
    from agnoclaw.config import get_config

    cfg = get_config()
    tools = get_default_tools(cfg)

    # Group by toolkit
    names = []
    for t in tools:
        if hasattr(t, "functions"):
            # It's a Toolkit
            for fn in t.functions.values():
                names.append(fn.name)
        elif hasattr(t, "name"):
            names.append(t.name)

    print("  Registered tools:")
    for name in sorted(names):
        print(f"    {name}")
    print()


# ── Part 2: MultiEdit demo ─────────────────────────────────────────────────────

def demo_multi_edit(tmp: Path):
    """
    MultiEdit — apply multiple string replacements atomically.

    Claude Code calls this 'MultiEdit'. It solves the read/edit/read round-trip
    problem when you need to make several changes to one file.

    Key properties:
    - All edits validated BEFORE any are applied (fail-fast)
    - Each old_string must appear exactly once (same uniqueness rule as Edit)
    - Edits applied in order — earlier edits can change what later edits see
    """
    from agnoclaw.tools.files import FilesToolkit

    print("=== MultiEdit Tool ===")
    print()

    toolkit = FilesToolkit()
    file_path = str(tmp / "config.py")

    # Write example file
    (tmp / "config.py").write_text(
        'DEBUG = False\n'
        'DATABASE_URL = "sqlite:///dev.db"\n'
        'MAX_CONNECTIONS = 10\n'
        'TIMEOUT_SECONDS = 30\n',
        encoding="utf-8",
    )

    print("  Original file:")
    content = (tmp / "config.py").read_text(encoding="utf-8")
    for line in content.splitlines():
        print(f"    {line}")
    print()

    # Apply three edits atomically
    result = toolkit.multi_edit_file(file_path, [
        {"old_string": 'DEBUG = False',           "new_string": 'DEBUG = True'},
        {"old_string": '"sqlite:///dev.db"',       "new_string": '"postgresql://localhost/prod"'},
        {"old_string": 'MAX_CONNECTIONS = 10',     "new_string": 'MAX_CONNECTIONS = 100'},
    ])
    print(f"  Result: {result}")
    print()

    print("  After MultiEdit:")
    content = (tmp / "config.py").read_text(encoding="utf-8")
    for line in content.splitlines():
        print(f"    {line}")
    print()

    # Demonstrate fail-fast validation
    print("  Fail-fast: if ANY edit is invalid, NONE are applied")
    original = (tmp / "config.py").read_text(encoding="utf-8")
    result = toolkit.multi_edit_file(file_path, [
        {"old_string": 'DEBUG = True',       "new_string": 'DEBUG = False'},   # valid
        {"old_string": 'NOT_IN_FILE = True', "new_string": 'x = 1'},           # invalid
    ])
    after = (tmp / "config.py").read_text(encoding="utf-8")
    print(f"  Error result: {result[:80]}")
    print(f"  File unchanged: {original == after}")
    print()


# ── Part 3: Existing file tools comparison ────────────────────────────────────

def demo_file_tools(tmp: Path):
    """Quick tour of all file tools: Read, Write, Edit, MultiEdit, Glob, Grep, ListDir."""
    from agnoclaw.tools.files import FilesToolkit

    print("=== File Tools Suite ===")
    print()

    toolkit = FilesToolkit(workspace_dir=tmp)

    # Write
    path = str(tmp / "example.py")
    toolkit.write_file(path, "def greet(name):\n    return f'Hello, {name}!'\n\ndef farewell(name):\n    return f'Goodbye, {name}!'\n")
    print(f"  write_file: created example.py")

    # Read
    result = toolkit.read_file(path, limit=3)
    print(f"  read_file (first 3 lines):\n    {result.replace(chr(10), chr(10)+'    ')}")

    # Edit (single replacement)
    toolkit.edit_file(path, "'Hello, {name}!'", "'Hi there, {name}!'")
    print(f"  edit_file: replaced greeting")

    # MultiEdit (multiple replacements)
    toolkit.multi_edit_file(path, [
        {"old_string": "'Hi there, {name}!'",  "new_string": "'Hello, {name}!'"},
        {"old_string": "'Goodbye, {name}!'",   "new_string": "'See you, {name}!'"},
    ])
    print(f"  multi_edit_file: restored greeting, updated farewell")

    # Glob
    (tmp / "a.py").write_text("x=1")
    (tmp / "b.py").write_text("y=2")
    result = toolkit.glob_files("*.py", base_dir=str(tmp))
    count = len(result.splitlines())
    print(f"  glob_files('*.py'): {count} files found")

    # Grep
    result = toolkit.grep_files("def ", str(tmp), glob="*.py")
    matches = [l for l in result.splitlines() if "def " in l]
    print(f"  grep_files('def '): {len(matches)} function definitions found")

    # ListDir
    result = toolkit.list_dir(str(tmp))
    lines = [l for l in result.splitlines() if l.startswith(("f ", "d "))]
    print(f"  list_dir: {len(lines)} entries")
    print()


# ── Part 4: Gap analysis ───────────────────────────────────────────────────────

def demo_gap_analysis():
    """
    Side-by-side comparison of agnoclaw tools vs Claude Code v2.1.50.
    See docs/claude-code-gaps.md for full analysis.
    """
    print("=== Claude Code Tool Gaps (CC v2.1.50 vs agnoclaw) ===")
    print()

    implemented = [
        ("Read",        "read_file()",       "File reading with line offset/limit"),
        ("Write",       "write_file()",      "File writing (creates parent dirs)"),
        ("Edit",        "edit_file()",       "Single string replacement (unique match required)"),
        ("MultiEdit",   "multi_edit_file()", "Multiple replacements atomically — ADDED this session"),
        ("Glob",        "glob_files()",      "File pattern matching (sorted by mtime)"),
        ("Grep",        "grep_files()",      "Regex content search with context lines"),
        ("LS/ListDir",  "list_dir()",        "Directory listing with sizes"),
        ("Bash",        "bash()",            "Shell execution (timeout, description)"),
        ("WebSearch",   "web_search()",      "Web search with domain filtering"),
        ("WebFetch",    "web_fetch()",       "URL fetching with AI summarization"),
        ("TodoWrite",   "create_todo()",     "Task creation (CRUD vs CC's atomic write)"),
        ("TodoRead",    "list_todos()",      "Task listing"),
        ("Task/Spawn",  "spawn_subagent()",  "Subagent spawning (simpler than CC's named agents)"),
    ]

    missing = [
        ("NotebookRead",   "Read Jupyter notebook cells — data science use case"),
        ("NotebookEdit",   "Edit/insert/delete Jupyter cells with cell_id"),
        ("AskUserQuestion","Structured multi-choice prompts — critical for plan mode UX"),
        ("ExitPlanMode",   "Plan mode exit signal — agnoclaw uses text instruction only"),
        ("BashOutput",     "Retrieve output from background bash tasks by task ID"),
        ("KillShell",      "Kill a backgrounded shell process"),
        ("Skill (tool)",   "Model-invoked skill tool — CC has a Skill tool; agnoclaw injects via prompt"),
        ("TaskOutput",     "Retrieve output from background subagent by agent ID"),
        ("TaskUpdate",     "Shared task list update for agent teams"),
    ]

    print("  IMPLEMENTED (agnoclaw equivalent):")
    for cc_name, our_name, note in implemented:
        status = "NEW" if "ADDED" in note else "   "
        print(f"    [{status}] {cc_name:<16} → {our_name:<22} {note}")
    print()

    print("  MISSING from agnoclaw:")
    for cc_name, note in missing:
        print(f"    [ ? ] {cc_name:<20} {note}")
    print()

    print("  THINGS AGNOCLAW HAS THAT CC LACKS:")
    extras = [
        ("BOOT.md", "Startup sequence execution (CC uses SessionStart hooks instead)"),
        ("LearningMachine", "Institutional cross-user memory (CC has auto memory but not this)"),
        ("ProgressToolkit", "Multi-context-window feature tracking"),
        ("self-improving-agent skill", "Structured .learnings/ capture + workspace promotion"),
        ("CronJob scheduler", "Cron expression + interval scheduling (CC has no scheduler)"),
        ("install-service", "launchd/systemd registration for always-on operation"),
        ("3-tier memory", "Workspace files + MemoryManager + LearningMachine"),
        ("SOUL/IDENTITY/TOOLS.md", "Agnoclaw-specific workspace persona files"),
    ]
    for name, note in extras:
        print(f"    [+]  {name:<30} {note}")
    print()
    print("  Full analysis: docs/claude-code-gaps.md")
    print()


# ── Part 5: Live agent using MultiEdit ───────────────────────────────────────

def demo_agent_multi_edit(tmp: Path):
    """Show an agent using MultiEdit to refactor a file."""
    from agnoclaw import AgentHarness

    print("=== Agent Using MultiEdit ===")

    # Create a file with multiple issues to fix
    (tmp / "utils.py").write_text(
        "import os\nimport sys\n\n"
        "def get_home():\n    return os.path.expanduser('~')\n\n"
        "def get_cwd():\n    return os.getcwd()\n\n"
        "DEBUG_MODE = False\n",
        encoding="utf-8",
    )

    agent = AgentHarness(
        provider=PROVIDER,
        model_id=MODEL,
        workspace_dir=tmp,
        session_id="multi-edit-demo",
    )

    prompt = f"""Use multi_edit_file to make these two changes to {tmp}/utils.py in one call:
1. Change DEBUG_MODE = False to DEBUG_MODE = True
2. Change def get_home() to def get_home_dir()

Read the file first, then use multi_edit_file with both changes.
Confirm the changes were applied."""

    response = agent.run(prompt)
    print(f"  {str(response.content)[:400]}")
    print()

    # Show result
    final = (tmp / "utils.py").read_text(encoding="utf-8")
    has_debug = "DEBUG_MODE = True" in final
    has_rename = "def get_home_dir" in final
    print(f"  DEBUG_MODE changed: {has_debug}")
    print(f"  Function renamed:   {has_rename}")
    print()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("agnoclaw Tools Demo — Claude Code Comparison")
    print("=" * 50)
    print()

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)

        # No API needed
        demo_tool_inventory()
        demo_multi_edit(tmp)
        demo_file_tools(tmp)
        demo_gap_analysis()

        # Live agent demo
        if _check_ollama() or PROVIDER != "ollama":
            demo_agent_multi_edit(tmp)
        else:
            print("=== Agent MultiEdit Demo ===")
            print("  (Skipped: Ollama not running. Start with: ollama serve)")
            print()

    print("Done.")
    print()
    print("See docs/claude-code-gaps.md for the complete gap analysis.")


if __name__ == "__main__":
    main()
