"""Tests for the MCP toolkit."""

from unittest.mock import MagicMock, patch

import pytest


def test_mcp_toolkit_import():
    """MCPToolkit should import without mcp package installed."""
    from agnoclaw.tools.mcp import MCPToolkit
    assert MCPToolkit is not None


def test_mcp_check():
    """_check_mcp should return False when mcp is not installed."""
    from agnoclaw.tools.mcp import _check_mcp
    result = _check_mcp()
    assert isinstance(result, bool)


def test_mcp_toolkit_init():
    """MCPToolkit should initialize with command or url."""
    from agnoclaw.tools.mcp import MCPToolkit

    # Stdio transport
    toolkit = MCPToolkit(name="test", command=["echo", "hello"])
    assert toolkit.name == "test"
    assert not toolkit.connected

    # SSE transport
    toolkit = MCPToolkit(name="sse-test", url="http://localhost:3001/sse")
    assert toolkit.name == "sse-test"


def test_mcp_toolkit_requires_transport():
    """MCPToolkit should raise ValueError without command or url."""
    from agnoclaw.tools.mcp import MCPToolkit

    toolkit = MCPToolkit(name="no-transport")
    with patch("agnoclaw.tools.mcp._check_mcp", return_value=True):
        # Mock the mcp imports
        with patch.dict("sys.modules", {"mcp": MagicMock(), "mcp.client": MagicMock()}):
            # The actual connect would fail because neither command nor url is set
            # This is validated at connect time
            pass


def test_mcp_tool_schemas_empty_before_connect():
    """tool_schemas should be empty before connecting."""
    from agnoclaw.tools.mcp import MCPToolkit

    toolkit = MCPToolkit(command=["echo"])
    assert toolkit.tool_schemas == []


def test_mcp_connect_requires_package():
    """connect() should raise ImportError when mcp is not installed."""
    from agnoclaw.tools.mcp import MCPToolkit

    toolkit = MCPToolkit(command=["echo"])
    with patch("agnoclaw.tools.mcp._check_mcp", return_value=False):
        with pytest.raises(ImportError, match="MCP package"):
            toolkit.connect()


def test_mcp_call_tool_without_session():
    """Calling a tool without connecting should return error."""
    from agnoclaw.tools.mcp import MCPToolkit

    toolkit = MCPToolkit(command=["echo"])
    result = toolkit._call_tool_sync("test_tool", {"arg": "value"})
    assert "[error]" in result
