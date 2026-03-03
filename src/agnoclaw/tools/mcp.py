"""
MCP (Model Context Protocol) integration toolkit.

Connects to MCP servers (stdio or SSE transport) and exposes their tools
as Agno-compatible tools that AgentHarness can use.

Optional extra: agnoclaw[mcp] → mcp>=1.0.0

Config example (.agnoclaw.toml):
    [[mcp_servers]]
    name = "filesystem"
    command = ["npx", "-y", "@anthropic-ai/mcp-filesystem", "/path/to/dir"]

    [[mcp_servers]]
    name = "brave-search"
    url = "http://localhost:3001/sse"
    env = { BRAVE_API_KEY = "..." }

Usage:
    from agnoclaw.tools.mcp import MCPToolkit

    toolkit = MCPToolkit(command=["npx", "-y", "@anthropic-ai/mcp-filesystem", "."])
    toolkit.connect()  # Discovers tools from the MCP server
    # toolkit now has registered tools that can be passed to AgentHarness
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from agno.tools.toolkit import Toolkit

logger = logging.getLogger("agnoclaw.tools.mcp")


def _check_mcp():
    """Check if the mcp package is importable."""
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:
        return False


class MCPToolkit(Toolkit):
    """
    Connects to an MCP server and exposes its tools as Agno toolkit methods.

    Supports two transports:
      - stdio: Launch a subprocess via `command` (e.g., ["npx", "-y", "@anthropic-ai/mcp-filesystem", "."])
      - SSE: Connect to a running server via `url` (e.g., "http://localhost:3001/sse")

    Args:
        name: Toolkit name (used as prefix for tool names).
        command: Command to launch MCP server via stdio transport.
        url: URL for SSE transport.
        env: Environment variables to set for the MCP server process.
    """

    def __init__(
        self,
        name: str = "mcp",
        command: Optional[list[str]] = None,
        url: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
    ):
        super().__init__(name=name)
        self._command = command
        self._url = url
        self._env = env or {}
        self._session = None
        self._transport = None
        self._connected = False
        self._tool_schemas: list[dict] = []

    def connect(self) -> list[str]:
        """
        Connect to the MCP server and discover available tools.

        Returns:
            List of discovered tool names.
        """
        if not _check_mcp():
            raise ImportError(
                "MCP package is required for MCP tools. "
                "Install with: pip install agnoclaw[mcp]"
            )

        import asyncio

        # Run the async connection in a sync context
        try:
            loop = asyncio.get_running_loop()
            # If there's already a loop, we can't use asyncio.run
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                tool_names = loop.run_in_executor(pool, self._connect_sync)
                return asyncio.get_event_loop().run_until_complete(tool_names)
        except RuntimeError:
            return asyncio.run(self._aconnect())

    async def _aconnect(self) -> list[str]:
        """Async connect to MCP server."""
        from mcp import ClientSession

        if self._command:
            from mcp.client.stdio import StdioServerParameters, stdio_client

            params = StdioServerParameters(
                command=self._command[0],
                args=self._command[1:] if len(self._command) > 1 else [],
                env=self._env if self._env else None,
            )
            self._transport = stdio_client(params)
        elif self._url:
            from mcp.client.sse import sse_client

            self._transport = sse_client(self._url)
        else:
            raise ValueError("MCPToolkit requires either 'command' or 'url'")

        read, write = await self._transport.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()

        # Discover tools
        tools_result = await self._session.list_tools()
        tool_names = []

        for tool_schema in tools_result.tools:
            tool_name = tool_schema.name
            tool_names.append(tool_name)
            self._tool_schemas.append({
                "name": tool_name,
                "description": tool_schema.description or f"MCP tool: {tool_name}",
                "input_schema": tool_schema.inputSchema if hasattr(tool_schema, 'inputSchema') else {},
            })

            # Create a wrapper function for this MCP tool
            self._register_mcp_tool(tool_name, tool_schema)

        self._connected = True
        logger.info(
            "Connected to MCP server '%s': %d tools discovered",
            self.name, len(tool_names),
        )
        return tool_names

    def _connect_sync(self) -> list[str]:
        """Synchronous wrapper for connection."""
        import asyncio
        return asyncio.run(self._aconnect())

    def _register_mcp_tool(self, tool_name: str, tool_schema) -> None:
        """Register a single MCP tool as an Agno toolkit method."""
        session = self  # capture reference

        def mcp_tool_caller(**kwargs) -> str:
            """Call the MCP tool with given arguments."""
            return session._call_tool_sync(tool_name, kwargs)

        # Set metadata for Agno
        mcp_tool_caller.__name__ = tool_name
        mcp_tool_caller.__doc__ = (
            tool_schema.description or f"MCP tool: {tool_name}"
        )

        self.register(mcp_tool_caller)

    def _call_tool_sync(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Synchronously call an MCP tool."""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # We're in an async context, use a thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, self._acall_tool(tool_name, arguments))
                return future.result(timeout=120)
        else:
            return asyncio.run(self._acall_tool(tool_name, arguments))

    async def _acall_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Async call to an MCP tool."""
        if not self._session:
            return f"[error] MCP session not connected. Call connect() first."

        try:
            result = await self._session.call_tool(tool_name, arguments)
            # Extract text content from result
            if hasattr(result, 'content') and result.content:
                parts = []
                for content_item in result.content:
                    if hasattr(content_item, 'text'):
                        parts.append(content_item.text)
                    elif hasattr(content_item, 'data'):
                        parts.append(str(content_item.data))
                return "\n".join(parts) if parts else str(result)
            return str(result)
        except Exception as e:
            return f"[error] MCP tool '{tool_name}' failed: {e}"

    @property
    def tool_schemas(self) -> list[dict]:
        """Return the raw tool schemas from the MCP server."""
        return list(self._tool_schemas)

    @property
    def connected(self) -> bool:
        return self._connected

    async def aclose(self) -> None:
        """Async cleanup."""
        if self._session:
            await self._session.__aexit__(None, None, None)
            self._session = None
        if self._transport:
            await self._transport.__aexit__(None, None, None)
            self._transport = None
        self._connected = False

    def close(self) -> None:
        """Synchronous cleanup."""
        import asyncio
        try:
            asyncio.run(self.aclose())
        except Exception as e:
            logger.debug("MCP cleanup error: %s", e)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
