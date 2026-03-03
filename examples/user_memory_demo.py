"""
Example: Per-User Memory (MemoryManager)

Demonstrates:
- Per-user memory extraction and retrieval
- User preferences stored and recalled across sessions
- Multiple users with isolated memories
- Combined MemoryManager + LearningMachine
"""

from _utils import detect_model
from agnoclaw import AgentHarness

MODEL = detect_model()

# ── Single user with persistent memory ───────────────────────────────────
# The MemoryManager extracts and stores facts about "alice" in the DB.
# On the next session (same user_id), those facts are auto-injected.

print("=== User Memory: alice ===\n")

alice_agent = AgentHarness(
    name="personal-assistant",
    model=MODEL,
    user_id="alice",
    enable_user_memory=True,  # enables MemoryManager (Tier 2)
)

# First interaction — alice shares preferences
alice_agent.print_response(
    "I prefer concise bullet points over paragraphs. "
    "I'm a senior Python developer working on a distributed data pipeline. "
    "I use pytest for testing and black for formatting. My timezone is PST.",
    stream=True,
)

print("\n--- Second interaction (memory should be recalled) ---\n")

# Second interaction — agent recalls alice's preferences
alice_agent2 = AgentHarness(
    name="personal-assistant",
    model=MODEL,
    user_id="alice",
    enable_user_memory=True,
)

alice_agent2.print_response(
    "What do you know about my preferences and current project?",
    stream=True,
)


# ── Multiple isolated users ───────────────────────────────────────────────

print("\n\n=== User Memory: bob (isolated from alice) ===\n")

bob_agent = AgentHarness(
    name="personal-assistant",
    model=MODEL,
    user_id="bob",
    enable_user_memory=True,
)

bob_agent.print_response(
    "I'm a data scientist working with pandas and polars. "
    "I prefer verbose explanations with examples. "
    "My stack: Python 3.12, Jupyter, DuckDB.",
    stream=True,
)

# Bob's agent has no knowledge of alice's preferences
bob_agent2 = AgentHarness(
    name="personal-assistant",
    model=MODEL,
    user_id="bob",
    enable_user_memory=True,
)

bob_agent2.print_response(
    "What tools do I use?",
    stream=True,
)


# ── Combined: per-user memory + institutional learning ───────────────────
# Best of both worlds: alice and bob each have private preferences (Tier 2)
# while the agent accumulates shared patterns/insights (Tier 3)

print("\n\n=== Combined Memory + Learning ===\n")

combined_agent = AgentHarness(
    name="combined",
    model=MODEL,
    user_id="charlie",
    enable_user_memory=True,    # per-user preferences (private)
    enable_learning=True,       # institutional patterns (shared)
    learning_namespace="shared-insights",
)

combined_agent.print_response(
    "I'm a DevOps engineer. Explain the tradeoffs between Kubernetes and "
    "Docker Compose for a team of 5 engineers.",
    stream=True,
)
