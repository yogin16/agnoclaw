"""
agnoclaw — a hackable, model-agnostic agent harness built on Agno.

Quick start:
    from agnoclaw import HarnessAgent

    agent = HarnessAgent()
    agent.print_response("Summarize today's AI news")

Multi-agent:
    from agnoclaw.teams import research_team

    team = research_team()
    team.print_response("Research the state of fusion energy in 2026", stream=True)

With skills:
    agent = HarnessAgent()
    agent.print_response("Research quantum computing", skill="deep-research")

With custom model:
    agent = HarnessAgent(model_id="gpt-4o", provider="openai")
    agent = HarnessAgent(model_id="llama3.2", provider="ollama")
"""

from .agent import HarnessAgent
from .config import HarnessConfig, get_config
from .workspace import Workspace

__all__ = [
    "HarnessAgent",
    "HarnessConfig",
    "get_config",
    "Workspace",
]

__version__ = "0.1.0"
