"""
Basic agent example — single agent with default tools.

Run: uv run python examples/basic_agent.py
"""

from _utils import detect_model

from agnoclaw import AgentHarness

# Create agent with auto-detected model (SQLite storage, ~/.agnoclaw/workspace)
agent = AgentHarness(
    name="my-agent",
    model=detect_model(),
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
