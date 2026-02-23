"""
Example: Multi-Agent Teams with Institutional Learning

Demonstrates:
- research_team with learning enabled
- code_team with learning enabled
- Namespace isolation (research team vs code team don't share learnings)
- Streaming team responses
- Session ID for team continuity
"""

from agnoclaw.teams import research_team, code_team, data_team


# ── Research Team with Learning ───────────────────────────────────────────
# The research team accumulates learnings about:
# - Which sources are reliable for which topics
# - Effective search strategies
# - Source quality patterns

print("=== Research Team (with learning) ===\n")

r_team = research_team(
    model_id="claude-haiku-4-5-20251001",  # cheaper for demo
    session_id="research-session-001",
    enable_learning=True,  # namespaced to "research-team"
)

r_team.print_response(
    "What are the key differences between RAG and fine-tuning for LLMs in production?",
    stream=True,
)


# ── Code Team with Learning ───────────────────────────────────────────────
# The code team accumulates learnings about:
# - Architecture decisions and their outcomes
# - Code review patterns that catch the most bugs
# - Testing conventions

print("\n\n=== Code Team (with learning) ===\n")

c_team = code_team(
    model_id="claude-haiku-4-5-20251001",
    session_id="code-session-001",
    enable_learning=True,  # namespaced to "code-team" (isolated from research)
)

c_team.print_response(
    "Design and implement a simple rate limiter in Python using token bucket algorithm. "
    "Write tests. Keep it under 100 lines.",
    stream=True,
)


# ── Data Team (no learning — stateless analysis) ─────────────────────────
# data_team doesn't support learning by default — pure analysis

print("\n\n=== Data Team ===\n")

d_team = data_team(
    model_id="claude-haiku-4-5-20251001",
    session_id="data-session-001",
)

d_team.print_response(
    "Analyze: what's the time complexity of sorting 1M vs 100M records? "
    "Give me specific numbers assuming 1μs per comparison.",
    stream=True,
)


# ── Continuing a Team Session ─────────────────────────────────────────────
# Re-use the same session_id to continue where you left off

print("\n\n=== Research Team (continued session) ===\n")

r_team_continued = research_team(
    session_id="research-session-001",  # same session
    enable_learning=True,
)

r_team_continued.print_response(
    "Based on our previous research, which approach would you recommend for a startup "
    "with limited ML expertise?",
    stream=True,
)
