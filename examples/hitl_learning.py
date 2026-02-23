"""
Example: HITL Learning Mode (Human-in-the-Loop)

Demonstrates:
- LearningMachine with hitl mode — agent proposes learnings for human approval
- propose mode — learnings submitted to a review queue
- agentic mode — agent decides autonomously (default)
- Namespace isolation between agents
"""

from agnoclaw import AgentHarness


# ── HITL mode: human approves each learning ───────────────────────────────
# Best for: high-stakes environments, regulated industries, personal agents
# where you want full control over what gets persisted.

hitl_agent = AgentHarness(
    name="hitl-agent",
    enable_learning=True,
    learning_mode="hitl",
    learning_namespace="code-review",
)

print("=== HITL Learning Mode ===")
print("Agent will propose learnings for your approval before storing them.\n")
hitl_agent.print_response(
    "Review this code pattern: always use context managers for file operations in Python",
    stream=True,
)


# ── Propose mode: learnings go to a review queue ──────────────────────────
# Best for: team settings where a lead reviews before institutional knowledge
# is committed. Learnings are queued, not auto-applied.

propose_agent = AgentHarness(
    name="propose-agent",
    enable_learning=True,
    learning_mode="propose",
    learning_namespace="research",
)

print("\n=== Propose Mode ===")
print("Agent will propose learnings — review queue, not auto-stored.\n")
propose_agent.print_response(
    "I've found that arXiv papers from 2023+ are more reliable for LLM benchmarks "
    "than conference proceedings due to faster publication cycles.",
    stream=True,
)


# ── Agentic mode: agent decides autonomously ──────────────────────────────
# Best for: general use. The agent uses judgment about what's worth storing.
# Most balanced cost/coverage trade-off.

agentic_agent = AgentHarness(
    name="agentic-agent",
    enable_learning=True,
    learning_mode="agentic",
    learning_namespace="general",
)

print("\n=== Agentic Mode ===")
print("Agent autonomously decides when to record learnings.\n")
agentic_agent.print_response(
    "Complete this analysis task: compare REST vs GraphQL for a mobile app backend",
    stream=True,
)


# ── Namespace isolation: agents don't share learnings ────────────────────
# research-agent and code-agent have completely isolated institutional memory

research_agent = AgentHarness(
    name="research",
    enable_learning=True,
    learning_namespace="research-v2",
)

code_agent = AgentHarness(
    name="code",
    enable_learning=True,
    learning_namespace="code-v2",
)

# These agents learn independently — no cross-contamination
print("\n=== Namespaced Agents ===")
print("research-v2 and code-v2 namespaces are completely isolated.\n")
