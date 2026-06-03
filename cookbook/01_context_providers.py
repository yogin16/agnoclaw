"""
Cookbook 1: Context Providers

Demonstrates:
- Creating a duck-typed context provider with get_tools() and optional lifecycle
- Registering context providers with AgentHarness
- Using AgentHarness.create() for async provider setup

Run: uv run python cookbook/01_context_providers.py
"""

import asyncio
import os
import tempfile

from agnoclaw import AgentHarness


# ── Duck-typed context provider ─────────────────────────────────────────────
# No base class needed — just implement get_tools() and optional lifecycle hooks.
class ProjectContextProvider:
    """Provides project-aware tools for browsing a codebase."""

    def __init__(self, project_root: str):
        self.id = "project_context"
        self.name = "Project Context"
        self.query_tool_name = "list_project_files"
        self.update_tool_name = None  # read-only provider
        self._root = project_root

    def get_tools(self) -> list:
        """Return Agno tool functions for this provider."""
        from agno.tools import tool

        @tool(name=self.query_tool_name)
        def list_project_files(extension: str | None = None) -> str:
            """List files in the project directory, optionally filtered by extension."""
            matches = []
            for root, _dirs, files in os.walk(self._root):
                for f in files:
                    if extension is None or f.endswith(extension):
                        matches.append(os.path.join(root, f))
            if not matches:
                return "No files found."
            return "\n".join(sorted(matches))

        return [list_project_files]

    def instructions(self) -> str:
        return (
            f"You have access to `{self.query_tool_name}` for listing files "
            f"in the project at {self._root}. Use it when asked about the codebase."
        )

    async def asetup(self) -> None:
        """Async initialization — called by AgentHarness.create()."""
        print(f"[Provider] Setting up project context: {self._root}")

    async def aclose(self) -> None:
        """Async cleanup — called by AgentHarness.aclose()."""
        print(f"[Provider] Closing project context: {self._root}")


class WeatherContextProvider:
    """Simulated weather data provider — demonstrates bounded tools + instructions."""

    def __init__(self):
        self.id = "weather"
        self.name = "Weather Service"
        self.query_tool_name = "get_weather"
        self.update_tool_name = None

    def get_tools(self) -> list:
        from agno.tools import tool

        @tool(name=self.query_tool_name)
        def get_weather(city: str) -> str:
            """Get current weather for a city (simulated)."""
            data = {
                "New York": "22°C, partly cloudy",
                "London": "15°C, light rain",
                "Tokyo": "28°C, sunny",
                "Dubai": "40°C, clear",
            }
            return data.get(city, f"No data for {city}")

        return [get_weather]

    def instructions(self) -> str:
        return "You have a `get_weather` tool for checking weather conditions."


async def main():
    # Create a temp project to demonstrate
    with tempfile.TemporaryDirectory() as tmpdir:
        for fname in ["main.py", "utils.py", "README.md"]:
            (p := os.path.join(tmpdir, fname)) and open(p, "w").write(f"# {fname}\n")

        # Use AgentHarness.create() — runs async provider setup hooks
        harness = await AgentHarness.create(
            model="ollama:llama3.2",
            name="context-provider-demo",
            context_providers=[
                ProjectContextProvider(project_root=tmpdir),
                WeatherContextProvider(),
            ],
            session_id="cookbook-context-providers",
        )

        try:
            print("=" * 60)
            harness.print_response(
                "List all .py files in the project and tell me what they contain.",
                stream=True,
            )
            print("=" * 60)
            harness.print_response(
                "What's the weather in Tokyo and New York?",
                stream=True,
            )
        finally:
            await harness.aclose()


if __name__ == "__main__":
    asyncio.run(main())
