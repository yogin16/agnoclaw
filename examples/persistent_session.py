"""
Example: Persistent Session Continuation

Demonstrates:
- Session ID reuse across restarts
- Conversation history retrieval
- Session summary for context compaction
- Picking up exactly where you left off
"""

from _utils import detect_model
from agnoclaw import AgentHarness

MODEL = detect_model()

# ── First run: start a session ─────────────────────────────────────────────

SESSION_ID = "my-project-session-001"

agent = AgentHarness(model=MODEL, session_id=SESSION_ID, name="ProjectAgent")

# Run the first task
result = agent.run("Create a TODO list for a Python web API project")
print("=== Run 1 ===")
print(result.content)

# Save a summary for context continuity before long-running work
agent.save_session_summary(
    "User is building a Python web API. Created initial TODO list. "
    "Next: scaffold project structure."
)

# ── Second run: resume the same session ───────────────────────────────────
# In a real scenario this would be a new process. Here we re-create the agent
# with the same session_id — the storage backend restores history.

agent2 = AgentHarness(model=MODEL, session_id=SESSION_ID, name="ProjectAgent")

result2 = agent2.run("What was on the TODO list from our last conversation?")
print("\n=== Run 2 (resumed) ===")
print(result2.content)

# ── Inspect chat history ──────────────────────────────────────────────────

history = agent2.get_chat_history()
print(f"\nChat history has {len(history)} messages")
