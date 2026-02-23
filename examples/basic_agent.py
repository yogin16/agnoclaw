"""
Basic agent example — single agent with default tools.

Run: uv run python examples/basic_agent.py
Requires: ANTHROPIC_API_KEY env var
"""

from agnoclaw import HarnessAgent

# Create agent with defaults (Claude Sonnet, SQLite storage, ~/.agnoclaw/workspace)
agent = HarnessAgent(
    name="my-agent",
    session_id="basic-example",  # persistent session
)

# One-shot response
agent.print_response(
    "What are the files in the current directory? Give me a brief summary.",
    stream=True,
)

# Streaming with skill activation
agent.print_response(
    "Research the latest developments in Agno framework",
    stream=True,
    skill="deep-research",
)
