"""
Deep research example — single agent with deep-research skill.

Demonstrates skill activation, streaming events, and structured output.

Run: uv run python examples/deep_research.py
Requires: ANTHROPIC_API_KEY env var
"""

import asyncio
from pydantic import BaseModel, Field
from typing import List

from agnoclaw import AgentHarness
from agno.run.agent import RunEvent


# Optional: structured output
class ResearchReport(BaseModel):
    title: str
    summary: str = Field(..., description="2-3 sentence executive summary")
    key_findings: List[str] = Field(..., min_length=3)
    sources: List[str] = Field(..., description="URLs of key sources")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence in findings 0-1")


# ── Option A: Simple streaming with skill ─────────────────────────────────────
print("Option A: Streaming with deep-research skill")
print("-" * 50)

agent = AgentHarness(session_id="deep-research-example")
agent.print_response(
    "What are the most significant AI agent breakthroughs in early 2026?",
    stream=True,
    skill="deep-research",
)


# ── Option B: Async with streaming events ─────────────────────────────────────
async def research_with_events():
    print("\nOption B: Async with streaming events")
    print("-" * 50)

    agent = AgentHarness(session_id="deep-research-async")
    skill_content = agent.skills.load_skill("deep-research")

    async for event in agent.arun(
        "Research the competitive landscape of open-source LLM frameworks",
        stream=True,
        stream_events=True,
        skill="deep-research",
    ):
        match event.event:
            case RunEvent.run_content:
                print(event.content, end="", flush=True)
            case RunEvent.tool_call_started:
                print(f"\n[tool: {event.tool.tool_name}]", flush=True)
            case RunEvent.run_completed:
                metrics = event.run_output.metrics
                print(f"\n\n[tokens: {metrics}]")


asyncio.run(research_with_events())
