"""Tests for the workspace system."""

import pytest
from pathlib import Path

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
