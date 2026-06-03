"""
Cookbook 1: Context Providers

Demonstrates:
- Extending Agno's ContextProvider ABC (query, aquery, status, astatus)
- Registering context providers with AgentHarness
- Using AgentHarness.create() for async provider setup

Run: uv run python cookbook/01_context_providers.py
"""

import asyncio
import os
import tempfile

from agno.context.provider import Answer, ContextProvider, Status

from agnoclaw import AgentHarness


# ── Context provider extending Agno's ABC ────────────────────────────────────
class ProjectContextProvider(ContextProvider):
    """Provides project-aware tools for browsing a codebase."""

    def __init__(self, project_root: str):
        super().__init__(
            id="project_context",
            name="Project Context",
            query_tool_name="list_project_files",
            read=True,
            write=False,
        )
        self._root = project_root

    def query(self, question: str, *, run_context=None) -> Answer:
        """List files in the project directory."""
        matches = []
        for root, _dirs, files in os.walk(self._root):
            for f in files:
                matches.append(os.path.join(root, f))
        result = "\n".join(sorted(matches)) if matches else "No files found."
        return Answer(answer=result)

    async def aquery(self, question: str, *, run_context=None) -> Answer:
        return self.query(question, run_context=run_context)

    def status(self) -> Status:
        return Status(available=True, details=f"Watching {self._root}")

    async def astatus(self) -> Status:
        return self.status()

    def instructions(self) -> str:
        return (
            f"Use `{self.query_tool_name}(question)` to list files "
            f"in the project at {self._root}."
        )

    async def asetup(self) -> None:
        print(f"[Provider] Setting up project context: {self._root}")

    async def aclose(self) -> None:
        print(f"[Provider] Closing project context: {self._root}")


class WeatherContextProvider(ContextProvider):
    """Simulated weather data provider."""

    def __init__(self):
        super().__init__(
            id="weather",
            name="Weather Service",
            query_tool_name="get_weather",
            read=True,
            write=False,
        )

    def query(self, question: str, *, run_context=None) -> Answer:
        data = {
            "New York": "22°C, partly cloudy",
            "London": "15°C, light rain",
            "Tokyo": "28°C, sunny",
            "Dubai": "40°C, clear",
        }
        for city, weather in data.items():
            if city.lower() in question.lower():
                return Answer(answer=f"{city}: {weather}")
        return Answer(answer=f"No weather data found for: {question}")

    async def aquery(self, question: str, *, run_context=None) -> Answer:
        return self.query(question, run_context=run_context)

    def status(self) -> Status:
        return Status(available=True, details="Weather service ready")

    async def astatus(self) -> Status:
        return self.status()

    def instructions(self) -> str:
        return f"You have `{self.query_tool_name}(question)` for checking weather."


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
