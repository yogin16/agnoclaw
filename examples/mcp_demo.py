"""
Example: MCP Demo — connecting to MCP servers from AgentHarness

Demonstrates connecting to MCP (Model Context Protocol) servers and
using their tools as native Agno tools within AgentHarness.

Run: uv run --extra mcp python examples/mcp_demo.py
Requires: ANTHROPIC_API_KEY, mcp package, an MCP server to connect to

Example MCP servers:
  - Filesystem: npx -y @anthropic-ai/mcp-filesystem /path/to/dir
  - Brave Search: requires BRAVE_API_KEY
"""

from agnoclaw import AgentHarness


def main():
    print("=" * 60)
    print("MCP Toolkit Demo")
    print("=" * 60)

    # Check if MCP is available
    try:
        from agnoclaw.tools.mcp import MCPToolkit, _check_mcp
        if not _check_mcp():
            print("\nMCP package not installed. Install with:")
            print("  pip install agnoclaw[mcp]")
            _show_config_example()
            return
    except ImportError:
        print("\nMCP toolkit not available.")
        _show_config_example()
        return

    # Demo: connect to filesystem MCP server
    print("\nConnecting to MCP filesystem server...")
    print("(This requires: npx -y @anthropic-ai/mcp-filesystem /tmp)")

    try:
        toolkit = MCPToolkit(
            name="filesystem",
            command=["npx", "-y", "@anthropic-ai/mcp-filesystem", "/tmp"],
        )
        tool_names = toolkit.connect()
        print(f"Connected! Discovered {len(tool_names)} tools: {tool_names}")

        # Create agent with MCP tools
        agent = AgentHarness(
            name="mcp-agent",
            tools=[toolkit],
            instructions="You have filesystem tools via MCP. Use them to explore /tmp.",
        )

        agent.print_response("List the files in /tmp", stream=True)

        toolkit.close()

    except Exception as e:
        print(f"\nMCP connection failed: {e}")
        print("This is expected if the MCP server is not running.")
        _show_config_example()


def _show_config_example():
    """Show how to configure MCP servers in .agnoclaw.toml."""
    print("\n" + "=" * 60)
    print("MCP Configuration (.agnoclaw.toml):")
    print("=" * 60)
    print("""
# Stdio transport (launches subprocess)
[[mcp_servers]]
name = "filesystem"
command = ["npx", "-y", "@anthropic-ai/mcp-filesystem", "."]

# SSE transport (connects to running server)
[[mcp_servers]]
name = "brave-search"
url = "http://localhost:3001/sse"
env = { BRAVE_API_KEY = "..." }

# In Python:
from agnoclaw.tools.mcp import MCPToolkit

toolkit = MCPToolkit(
    name="my-server",
    command=["npx", "-y", "@my-org/mcp-server"],
)
toolkit.connect()
# toolkit now has registered tools
""")


if __name__ == "__main__":
    main()
