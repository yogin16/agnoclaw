"""
Cookbook 9: SDK Ergonomics (create + session.send)

Demonstrates:
- AgentHarness.create() — async factory that runs provider setup
- session() — lightweight per-call SDK session facade
- session.send() — returns a HarnessRun wrapper
- HarnessRun.events() — iterate over stream events
- Session-level user_id, session_id, metadata

Run: uv run python cookbook/09_sdk_ergonomics.py
"""

import asyncio

from agnoclaw import AgentHarness


async def main():
    # ── AgentHarness.create() — async factory ──────────────────────────────
    # Runs async provider setup hooks (asetup_context_providers).
    # Use this when you have context providers that need async initialization.
    harness = await AgentHarness.create(
        model="ollama:llama3.2",
        name="sdk-ergonomics-demo",
        session_id="cookbook-sdk-ergonomics",
    )

    try:
        # ── session().send() — per-call SDK facade ─────────────────────────
        session = harness.session(
            session_id="cookbook-sdk-session-1",
            user_id="demo-user",
            metadata={"source": "cookbook-sdk-ergonomics"},
        )

        # Non-streaming send
        run = await session.send("What files are in the current directory?")
        if run.result:
            print(f"Response: {str(run.result)[:200]}")

        # ── Streaming send ─────────────────────────────────────────────────
        run_stream = await session.send(
            "Tell me a short joke about programming.",
            stream=True,
        )
        async for event in run_stream.events():
            print(f"  event: {event}")

        # ── Multiple sessions, same harness ────────────────────────────────
        session2 = harness.session(
            session_id="cookbook-sdk-session-2",
            user_id="another-user",
        )
        run2 = await session2.send("What is 2+2?")
        print(f"\nSession 2 response: {str(run2.result)[:100]}")

    finally:
        await harness.aclose()


if __name__ == "__main__":
    asyncio.run(main())
