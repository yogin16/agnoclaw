"""
ProgressToolkit — multi-context-window project tracking.

Demonstrates the ProgressToolkit pattern for complex projects that span
multiple sessions or context windows:

- write_features: define all requirements as a pass/fail checklist upfront
- update_feature_status: mark features passing as they're implemented
- write_progress: save state before a session ends / context compacts
- read_progress: resume at the next session start

This pattern is inspired by Claude Code's progress.md and the
initializer-then-coder pattern for large, long-running projects.

Run: uv run python examples/progress_tracking.py
Requires: ANTHROPIC_API_KEY env var
"""

import json
import tempfile
from pathlib import Path

from agnoclaw.tools.tasks import ProgressToolkit


# ── Direct toolkit usage (no agent required) ──────────────────────────────────

def demo_features_checklist(project_dir: str) -> None:
    """Show the full features.md lifecycle: write → update → read."""
    print("=== Features Checklist ===\n")

    toolkit = ProgressToolkit(project_dir=project_dir)

    # 1. Write feature requirements at project start (all start as 'failing')
    features = [
        {"id": "auth-01", "description": "Users can register with email + password"},
        {"id": "auth-02", "description": "Users can log in and receive a JWT"},
        {"id": "auth-03", "description": "JWT validation middleware on protected routes"},
        {"id": "api-01", "description": "GET /users/{id} returns user profile"},
        {"id": "api-02", "description": "POST /users creates a new user"},
        {"id": "api-03", "description": "DELETE /users/{id} soft-deletes user (admin only)"},
        {"id": "test-01", "description": "Auth endpoints have 90%+ test coverage"},
        {"id": "test-02", "description": "Integration tests run against a test database"},
    ]

    result = toolkit.write_features(json.dumps(features))
    print(result)
    print()

    # 2. Read the initial checklist (all failing)
    print("Initial state (all failing):")
    print(toolkit.read_features())

    # 3. Simulate work — mark features as they're implemented
    print("\n--- Implementing auth features... ---\n")
    for fid in ("auth-01", "auth-02", "auth-03"):
        result = toolkit.update_feature_status(fid, "passing")
        print(result)

    print("\n--- Implementing API features... ---\n")
    for fid in ("api-01", "api-02"):
        result = toolkit.update_feature_status(fid, "passing")
        print(result)

    # 4. Read current state (mixed)
    print("\nProgress after first session:")
    print(toolkit.read_features())


def demo_progress_persistence(project_dir: str) -> None:
    """Show write_progress / read_progress across simulated sessions."""
    print("\n=== Cross-Session Progress Persistence ===\n")

    toolkit = ProgressToolkit(project_dir=project_dir)

    # Session 1: save state before ending
    print("--- End of Session 1 ---")
    result = toolkit.write_progress(
        summary=(
            "Completed authentication layer (auth-01 through auth-03). "
            "JWT middleware tested and working. "
            "Started GET /users/{id} endpoint — not yet complete."
        ),
        next_steps=(
            "1. Finish GET /users/{id} endpoint and write tests\n"
            "2. Implement POST /users with validation\n"
            "3. Add DELETE /users/{id} with admin role check\n"
            "4. Wire up integration tests against test DB"
        ),
        context=(
            "Key decisions:\n"
            "- JWT secret stored in env var JWT_SECRET (not hardcoded)\n"
            "- User model: id, email, password_hash, created_at, deleted_at\n"
            "- Soft delete: set deleted_at, filter in WHERE clause\n"
            "- Admin check: JWT claims['role'] == 'admin'\n"
            "\n"
            "Known issues:\n"
            "- Token refresh not yet designed — parking for v2\n"
            "- password_hash using bcrypt (12 rounds) — may need tuning"
        ),
    )
    print(result)

    # Session 2: read progress at start
    print("\n--- Start of Session 2 ---")
    print("Reading progress from previous session:\n")
    print(toolkit.read_progress())


def demo_with_agent(project_dir: str) -> None:
    """Show ProgressToolkit used by an agent during a task."""
    print("\n=== Agent Using ProgressToolkit ===\n")

    from agnoclaw import AgentHarness
    from agnoclaw.tools.tasks import ProgressToolkit

    # Pass a custom ProgressToolkit pointed at our project dir
    agent = AgentHarness(
        name="progress-demo-agent",
        tools=[ProgressToolkit(project_dir=project_dir)],
    )

    # Ask the agent to use the tools
    agent.print_response(
        "Use the progress toolkit to:\n"
        "1. Read the current features checklist\n"
        "2. Read the progress notes from the last session\n"
        "3. Tell me what features are still failing and what the next steps are",
        stream=True,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="agnoclaw-progress-") as project_dir:
        print(f"Project directory: {project_dir}\n")

        # Part 1: Feature checklist lifecycle
        demo_features_checklist(project_dir)

        # Part 2: Cross-session progress persistence
        demo_progress_persistence(project_dir)

        # Part 3: Agent using ProgressToolkit
        # (uncomment to make a real API call)
        # demo_with_agent(project_dir)

        print(f"\nFiles created:")
        for f in Path(project_dir).iterdir():
            print(f"  {f.name}: {f.stat().st_size} bytes")
