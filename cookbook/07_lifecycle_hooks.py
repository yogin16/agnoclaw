"""
Cookbook 7: Workspace Lifecycle Hooks

Demonstrates:
- Registering lifecycle hooks on named checkpoints
- Sync and async hook callables
- Mutating lifecycle events via hook return values
- Known lifecycle event types
- Inspecting audit trail from hooks

Run: uv run python cookbook/07_lifecycle_hooks.py
"""

from agnoclaw import AgentHarness
from agnoclaw.runtime import InMemoryEventSink, LifecycleHookRequest


def main():
    sink = InMemoryEventSink()

    har = AgentHarness(
        model="ollama:llama3.2",
        name="lifecycle-hooks-demo",
        session_id="cookbook-lifecycle",
        event_sink=sink,
    )

    # ── Register a sync lifecycle hook ──────────────────────────────────────
    def on_run_started(event: LifecycleHookRequest, context) -> LifecycleHookRequest | None:
        print(f"  [hook] run started: event_type={event.event_type}")
        event.metadata["checked_by"] = "lifecycle-hook-demo"
        return event  # returning the (optionally mutated) event is allowed

    def on_run_completed(event: LifecycleHookRequest, context) -> LifecycleHookRequest | None:
        print(f"  [hook] run completed: event_type={event.event_type}")
        event.metadata["completed"] = True
        return event

    har.add_lifecycle_hook("run.started", on_run_started)
    har.add_lifecycle_hook("run.completed", on_run_completed)

    # ── Register an async lifecycle hook ────────────────────────────────────
    import asyncio

    async def on_session_created(
        event: LifecycleHookRequest, context
    ) -> LifecycleHookRequest | None:
        await asyncio.sleep(0.01)  # simulate some IO
        print(f"  [hook] session created: event_type={event.event_type}")
        event.metadata["session_note"] = "created via cookbook demo"
        return event

    har.add_lifecycle_hook("session.created", on_session_created)

    # ── Run the agent to trigger hooks ──────────────────────────────────────
    print("Running agent (triggers run.started, run.completed, session.created)...")
    har.print_response("Say hello briefly.", stream=True)

    # ── Inspect lifecycle events in the audit trail ─────────────────────────
    lifecycle_events = [
        e for e in sink.events
        if e.event_type.startswith("run.") or e.event_type.startswith("session.")
    ]
    print(f"\nLifecycle audit events: {len(lifecycle_events)}")
    for ev in lifecycle_events:
        print(f"  [{ev.event_type}] run_id={ev.run_id}")


if __name__ == "__main__":
    main()
