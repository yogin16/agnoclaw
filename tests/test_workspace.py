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
