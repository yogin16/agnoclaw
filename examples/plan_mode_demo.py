"""
Example: Plan Mode Demo

Demonstrates:
- Entering plan mode (research-only, no code changes)
- Agent writes a .plan.md file
- User approves and agent implements
- Exiting plan mode
"""

from pathlib import Path

from _utils import detect_model
from agnoclaw import AgentHarness

MODEL = detect_model()

agent = AgentHarness(name="planner", model=MODEL)

# ── Enter plan mode ───────────────────────────────────────────────────────
# Agent will only read/search — no writes, edits, or shell commands
agent.enter_plan_mode()

print("=== Plan Mode Active ===")
print("Agent will research and write a plan. No code changes will be made.\n")

agent.print_response(
    "Plan how to add Redis caching to a FastAPI application that currently uses "
    "an in-memory dictionary. The app has 3 endpoints: GET /users, POST /users, "
    "GET /users/{id}. Cache invalidation on POST is required.",
    stream=True,
)

# Check if a plan file was written
plan_file = Path("cache-plan.md")
if plan_file.exists():
    print(f"\nPlan written to: {plan_file}")
    print("\n=== Plan Contents ===")
    print(plan_file.read_text())

# ── User approves the plan ────────────────────────────────────────────────
try:
    user_input = input("\nApprove this plan? [y/n]: ").strip().lower()
except (EOFError, KeyboardInterrupt):
    user_input = "y"  # auto-approve in non-interactive mode

if user_input == "y":
    # Exit plan mode — agent can now make changes
    agent.exit_plan_mode()
    print("\n=== Plan Mode Exited — Implementing ===\n")

    agent.print_response(
        "The plan is approved. Please implement it.",
        stream=True,
    )
else:
    print("\nPlan rejected. Staying in plan mode.")
    agent.print_response(
        "The user rejected the plan. Please revise it with more focus on "
        "cache TTL configuration and error handling.",
        stream=True,
    )


# ── Programmatic plan mode check ──────────────────────────────────────────
# You can use plan mode without the interactive prompt by just calling
# enter_plan_mode() and exit_plan_mode() directly in your automation.

def automated_plan_and_implement(task: str, auto_approve: bool = False):
    """Run a task through plan-then-implement workflow."""
    a = AgentHarness(name="auto-planner", model=MODEL)

    a.enter_plan_mode()
    plan_result = a.run(f"Plan how to: {task}")

    if auto_approve:
        a.exit_plan_mode()
        impl_result = a.run("The plan is approved. Implement it.")
        return plan_result, impl_result

    return plan_result, None


# Example usage (non-interactive):
# plan, impl = automated_plan_and_implement(
#     "add input validation to the login endpoint",
#     auto_approve=True
# )
