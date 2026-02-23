"""
Multi-agent team example — research team and code team.

Run: uv run python examples/multi_agent_team.py
Requires: ANTHROPIC_API_KEY env var
"""

from agnoclaw.teams import code_team, research_team

# ── Research Team ─────────────────────────────────────────────────────────────
print("=" * 60)
print("RESEARCH TEAM")
print("=" * 60)

team = research_team(session_id="research-example")
team.print_response(
    "What is the current state of AI agent frameworks in 2026? "
    "Compare Agno, LangChain, CrewAI, and AutoGen.",
    stream=True,
)

# ── Code Team ─────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("CODE TEAM")
print("=" * 60)

team = code_team(session_id="code-example")
team.print_response(
    "Design and implement a Python function that validates email addresses "
    "using regex, with comprehensive tests.",
    stream=True,
)
