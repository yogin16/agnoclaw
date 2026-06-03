"""
Cookbook 3: Elevated Command Execution

Demonstrates:
- Session-wide elevated modes (off/ask/on/full)
- Programmatic elevated command execution with reason capture
- ElevatedCommandRequest/Result types
- Audit event emission during elevated execution

Run: uv run python cookbook/03_elevated_commands.py
"""

from agnoclaw import AgentHarness
from agnoclaw.runtime import (
    ElevatedCommandResult,
    ElevatedSessionMode,
    InMemoryEventSink,
)


def main():
    # ── Capture events to see elevated command audit trail ──────────────────
    sink = InMemoryEventSink()

    harness = AgentHarness(
        model="ollama:llama3.2",
        name="elevated-demo",
        session_id="cookbook-elevated",
        event_sink=sink,
    )

    # ── Set session-wide elevated mode ─────────────────────────────────────
    print(f"Default elevated mode: {harness.elevated_mode}")

    harness.set_elevated_mode("on")
    print(f"After set_elevated_mode('on'): {harness.elevated_mode}")

    # All elevated mode values
    for mode in ElevatedSessionMode:
        harness.set_elevated_mode(mode)
        assert harness.elevated_mode == mode.value
    print("All elevated session modes validated.\n")

    # ── Run elevated commands programmatically ──────────────────────────────
    print("=" * 60)

    # Requires an explicit reason — audit trail
    result: ElevatedCommandResult = harness.run_elevated_command(
        "echo 'Hello from elevated context'",
        reason="Demonstrate elevated command execution",
        timeout_seconds=10,
    )
    print(f"exit_code={result.exit_code}, stdout={result.stdout.strip()}")

    # With working directory and metadata
    result = harness.run_elevated_command(
        "pwd && whoami",
        reason="Verify working directory and user identity",
        metadata={"env": "demo", "tier": "testing"},
    )
    print(f"stdout={result.stdout.strip()}, exit_code={result.exit_code}")

    # ── Inspect audit events ───────────────────────────────────────────────
    elevated_events = [
        e for e in sink.events
        if e.event_type.startswith("elevated.command.")
    ]
    print(f"\nElevated audit events emitted: {len(elevated_events)}")
    for event in elevated_events:
        print(f"  [{event.event_type}] run_id={event.run_id}")
        if "reason" in event.payload:
            print(f"    reason: {event.payload['reason']}")

    # ── Async variant ──────────────────────────────────────────────────────
    import asyncio

    async def async_demo():
        result = await harness.arun_elevated_command(
            "echo 'Async elevated command'",
            reason="Async elevated command demo",
        )
        print(f"\nAsync result: {result.stdout.strip()}")

    asyncio.run(async_demo())


if __name__ == "__main__":
    main()
