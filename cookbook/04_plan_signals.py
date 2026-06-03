"""
Cookbook 4: Structured Plan UX Signals

Demonstrates:
- Entering plan mode (read-only research phase)
- AskUserQuestion — structured questions with options for decision-making
- ExitPlanMode — structured plan completion signal
- PlanQuestionSignal / PlanExitSignal data types
- Programmatic signal recording and retrieval

Run: uv run python cookbook/04_plan_signals.py
"""

from agnoclaw import AgentHarness
from agnoclaw.runtime import PlanExitSignal, PlanQuestionSignal


def main():
    harness = AgentHarness(
        model="ollama:llama3.2",
        name="plan-signals-demo",
        session_id="cookbook-plan-signals",
    )

    # ── Enter plan mode ─────────────────────────────────────────────────────
    # Agent switches to read-only mode — no writes, edits, or shell commands
    harness.enter_plan_mode()
    print("=== Plan Mode Active ===")

    # ── Ask a structured question ──────────────────────────────────────────
    # The LLM can call AskUserQuestion tool, but you can also do it
    # programmatically via the SDK:
    signal: PlanQuestionSignal = harness.ask_user_question(
        "Which database should we use for the user service?",
        options=["PostgreSQL", "SQLite", "MySQL", "MongoDB"],
        allow_freeform=True,
        metadata={"phase": "design", "component": "user-service"},
    )
    print(f"\nQuestion emitted: [{signal.signal_id}] {signal.question}")
    print(f"Options: {signal.options}")
    print(f"Allow freeform: {signal.allow_freeform}")

    # ── Signal plan completion ──────────────────────────────────────────────
    # Also triggers automatic exit from plan mode
    exit_signal: PlanExitSignal = harness.signal_plan_completion(
        summary="Use PostgreSQL with SQLAlchemy for user service. "
                "Cache with Redis. Deploy via Docker Compose.",
        plan_path="plan-user-service.md",
        ready_for_approval=True,
        metadata={"pages": 3, "services": ["users", "cache", "db"]},
    )
    print(f"\nPlan completed: [{exit_signal.signal_id}]")
    print(f"Summary: {exit_signal.summary}")
    print(f"Plan file: {exit_signal.plan_path}")
    print(f"Ready for approval: {exit_signal.ready_for_approval}")

    # ── Retrieve all captured signals ──────────────────────────────────────
    all_signals = harness.plan_signals()
    print(f"\nTotal signals captured: {len(all_signals)}")
    for s in all_signals:
        if isinstance(s, PlanQuestionSignal):
            print(f"  QUESTION: {s.question}")
        elif isinstance(s, PlanExitSignal):
            print(f"  EXIT: {s.summary[:60]}...")

    harness.clear_plan_signals()
    print("\nSignals cleared.")


if __name__ == "__main__":
    main()
