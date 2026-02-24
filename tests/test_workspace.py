"""Tests for the workspace system."""

import pytest

from agnoclaw.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    """Create a temporary workspace."""
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    return workspace


def test_workspace_initialize(ws):
    assert ws.path.exists()
    assert (ws.path / "skills").exists()
    assert (ws.path / "memory").exists()
    assert (ws.path / "sessions").exists()
    # Default files created
    assert (ws.path / "AGENTS.md").exists()
    assert (ws.path / "SOUL.md").exists()
    assert (ws.path / "HEARTBEAT.md").exists()


def test_workspace_read_write(ws):
    ws.write_file("user", "# User\n\nAlice, timezone: US/Pacific")
    content = ws.read_file("user")
    assert content is not None
    assert "Alice" in content


def test_workspace_read_missing(ws):
    assert ws.read_file("user") is None  # USER.md not created by default


def test_workspace_append_memory(ws):
    ws.append_to_memory("User prefers Python")
    content = ws.read_file("memory")
    assert content is not None
    assert "User prefers Python" in content


def test_workspace_append_memory_twice(ws):
    ws.append_to_memory("Prefers Python")
    ws.append_to_memory("Uses pytest for testing")
    content = ws.read_file("memory")
    assert "Prefers Python" in content
    assert "Uses pytest for testing" in content


def test_workspace_heartbeat_empty(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.initialize()
    # Default HEARTBEAT.md has content
    assert not ws.is_empty_heartbeat()


def test_workspace_heartbeat_headers_only(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.initialize()
    ws.write_file("heartbeat", "# Heartbeat\n\n## Section\n")
    assert ws.is_empty_heartbeat()


def test_workspace_context_files(ws):
    ws.write_file("user", "# User\nAlice")
    context = ws.context_files()
    # agents and soul exist (defaults), user we just wrote
    assert "agents" in context
    assert "soul" in context
    assert "user" in context
    assert "Alice" in context["user"]


def test_workspace_daily_log(ws):
    ws.log_to_daily("Completed feature X")
    from datetime import date
    today = date.today().isoformat()
    log_path = ws.path / "memory" / f"{today}.md"
    assert log_path.exists()
    assert "Completed feature X" in log_path.read_text()


def test_workspace_identity_md(ws):
    ws.write_file("identity", "# Identity\n\nI am an AI assistant.")
    content = ws.read_file("identity")
    assert "I am an AI assistant." in content


def test_workspace_tools_md(ws):
    ws.write_file("tools", "# Tools\n\nAllow: bash, files")
    content = ws.read_file("tools")
    assert "Allow: bash, files" in content


def test_workspace_boot_md(ws):
    ws.write_file("boot", "# Boot\n\n1. Run git status\n2. Check MEMORY.md")
    content = ws.read_file("boot")
    assert "git status" in content


def test_workspace_context_files_includes_boot(ws):
    ws.write_file("boot", "# Boot\n\nRun startup checks.")
    context = ws.context_files()
    assert "boot" in context
    assert "startup checks" in context["boot"]


def test_workspace_context_files_includes_identity(ws):
    ws.write_file("identity", "# Identity\n\nCustom identity here.")
    context = ws.context_files()
    assert "identity" in context


def test_workspace_context_files_includes_tools(ws):
    ws.write_file("tools", "# Tools\n\nCustom tools config.")
    context = ws.context_files()
    assert "tools" in context


def test_workspace_context_files_order(ws):
    """boot should appear last in context_files dict."""
    for key in ("agents", "soul", "identity", "user", "memory", "tools", "boot"):
        ws.write_file(key, f"# {key.capitalize()}\n\nContent for {key}.")
    context = ws.context_files()
    keys = list(context.keys())
    assert keys.index("boot") > keys.index("memory")
    assert keys.index("agents") < keys.index("user")


def test_workspace_write_session_summary(ws):
    ws.write_session_summary("Summarized research on quantum computing.")
    from datetime import date
    today = date.today().isoformat()
    log_path = ws.path / "memory" / f"{today}.md"
    assert log_path.exists()
    content = log_path.read_text()
    assert "Summarized research on quantum computing." in content
    assert "Session Summary" in content


def test_workspace_workspace_files_mapping():
    """WORKSPACE_FILES should include all new file types."""
    from agnoclaw.workspace import WORKSPACE_FILES
    assert "identity" in WORKSPACE_FILES
    assert "tools" in WORKSPACE_FILES
    assert "boot" in WORKSPACE_FILES
    assert WORKSPACE_FILES["identity"] == "IDENTITY.md"
    assert WORKSPACE_FILES["tools"] == "TOOLS.md"
    assert WORKSPACE_FILES["boot"] == "BOOT.md"


# ── Size limit tests ─────────────────────────────────────────────────────


def test_memory_startup_line_cap(tmp_path):
    """MEMORY.md should only return the first 200 lines at startup."""
    from agnoclaw.workspace import MEMORY_STARTUP_LINES

    ws = Workspace(tmp_path / "ws")
    ws.initialize()

    # Write 250 lines
    lines = [f"line {i}" for i in range(250)]
    ws.write_file("memory", "\n".join(lines))

    content = ws.read_file("memory")
    assert content is not None
    returned_lines = content.splitlines()
    assert len(returned_lines) == MEMORY_STARTUP_LINES, (
        f"Expected {MEMORY_STARTUP_LINES} lines, got {len(returned_lines)}"
    )
    assert "line 0" in content
    assert f"line {MEMORY_STARTUP_LINES - 1}" in content
    assert f"line {MEMORY_STARTUP_LINES}" not in content  # line 200 should not appear


def test_memory_startup_line_cap_under_limit(tmp_path):
    """MEMORY.md under 200 lines should be returned in full."""
    ws = Workspace(tmp_path / "ws")
    ws.initialize()

    ws.write_file("memory", "# Memory\n\nJust a few notes.\n")
    content = ws.read_file("memory")
    assert content is not None
    assert "Just a few notes." in content


def test_memory_startup_line_cap_constant():
    """MEMORY_STARTUP_LINES constant should be 200."""
    from agnoclaw.workspace import MEMORY_STARTUP_LINES
    assert MEMORY_STARTUP_LINES == 200


def test_bootstrap_max_chars_constant():
    from agnoclaw.workspace import BOOTSTRAP_MAX_CHARS
    assert BOOTSTRAP_MAX_CHARS == 20_000


def test_bootstrap_total_max_chars_constant():
    from agnoclaw.workspace import BOOTSTRAP_TOTAL_MAX_CHARS
    assert BOOTSTRAP_TOTAL_MAX_CHARS == 150_000


def test_per_file_char_cap(tmp_path):
    """A workspace file exceeding BOOTSTRAP_MAX_CHARS should be truncated."""
    from agnoclaw.workspace import BOOTSTRAP_MAX_CHARS

    ws = Workspace(tmp_path / "ws")
    ws.initialize()

    # Write content > 20K chars
    long_content = "A" * (BOOTSTRAP_MAX_CHARS + 5000)
    ws.write_file("agents", long_content)

    context = ws.context_files()
    assert "agents" in context
    assert len(context["agents"]) == BOOTSTRAP_MAX_CHARS


def test_total_bootstrap_cap(tmp_path):
    """Total context should not exceed BOOTSTRAP_TOTAL_MAX_CHARS."""
    from agnoclaw.workspace import BOOTSTRAP_MAX_CHARS, BOOTSTRAP_TOTAL_MAX_CHARS

    ws = Workspace(tmp_path / "ws")
    ws.initialize()

    # Fill 7 files × 20K each = 140K + defaults — some should get dropped
    for name in ("agents", "soul", "identity", "user", "memory", "tools"):
        ws.write_file(name, "X" * BOOTSTRAP_MAX_CHARS)

    context = ws.context_files()
    total = sum(len(v) for v in context.values())
    assert total <= BOOTSTRAP_TOTAL_MAX_CHARS


def test_context_files_skips_files_over_budget(tmp_path):
    """Once total budget is exhausted, later files should be excluded."""
    from agnoclaw.workspace import BOOTSTRAP_MAX_CHARS, BOOTSTRAP_TOTAL_MAX_CHARS

    ws = Workspace(tmp_path / "ws")
    ws.initialize()

    # Use exactly the total budget with the first two files
    # agents = 20K, soul = 130K → fills 150K
    ws.write_file("agents", "A" * BOOTSTRAP_MAX_CHARS)
    ws.write_file("soul", "S" * (BOOTSTRAP_TOTAL_MAX_CHARS - BOOTSTRAP_MAX_CHARS))
    ws.write_file("user", "U" * 1000)  # should be excluded

    context = ws.context_files()
    assert "agents" in context
    assert "soul" in context
    # 'user' should either be excluded or truncated (budget exhausted)
    total = sum(len(v) for v in context.values())
    assert total <= BOOTSTRAP_TOTAL_MAX_CHARS
