"""
Deep research example — single agent with deep-research skill.

Demonstrates skill activation and structured output.

Run: uv run python examples/deep_research.py
"""

from pydantic import BaseModel, Field
from typing import List

from _utils import detect_model
from agnoclaw import AgentHarness


# Optional: structured output
class ResearchReport(BaseModel):
    title: str
    summary: str = Field(..., description="2-3 sentence executive summary")
    key_findings: List[str] = Field(..., min_length=3)
    sources: List[str] = Field(..., description="URLs of key sources")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence in findings 0-1")


MODEL = detect_model()

# ── Option A: Simple streaming with skill ─────────────────────────────────────
print("Option A: Streaming with deep-research skill")
print("-" * 50)

agent = AgentHarness(model=MODEL, session_id="deep-research-example")
agent.print_response(
    "What are the most significant AI agent breakthroughs in early 2026?",
    stream=True,
    skill="deep-research",
)


# ── Option B: Synchronous run with skill ──────────────────────────────────────
print("\nOption B: Synchronous run")
print("-" * 50)

agent2 = AgentHarness(model=MODEL, session_id="deep-research-sync")
response = agent2.run(
    "Research the competitive landscape of open-source LLM frameworks",
    skill="deep-research",
)
print(response.content)
