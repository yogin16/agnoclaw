"""
Cookbook 8: Remote Harness Client

Demonstrates:
- Connecting to an AgentOS-exported harness via RemoteHarnessClient
- Non-streaming and streaming (SSE) remote runs
- Context manager usage
- Custom agent_id, api_key, and timeout

Prerequisites:
    Run cookbook/02_agentos_export.py on localhost:8000 first.

Run: uv run python cookbook/08_remote_client.py
"""

import asyncio

from agnoclaw import RemoteHarnessClient


async def main():
    # ── Connect to a running AgentOS server ────────────────────────────────
    agent_id = "code-reviewer"  # must match a harness name from the server
    base_url = "http://localhost:8000"

    async with RemoteHarnessClient(
        base_url=base_url,
        agent_id=agent_id,
        api_key=None,  # set if the server requires auth
        timeout=60.0,
    ) as client:

        # ── Non-streaming run ──────────────────────────────────────────────
        print(f"Connecting to {base_url} agent='{agent_id}'...")
        run = await client.arun("Hello! What can you do?")
        if run.result:
            print(f"Response: {str(run.result)[:200]}")
        print()

        # ── Streaming run (SSE) ────────────────────────────────────────────
        print("Streaming response:")
        run_stream = await client.arun(
            "List 3 interesting facts about agnoclaw.",
            stream=True,
        )
        async for event in run_stream.events():
            print(f"  event: {event.get('event_type', '?')}")

        # ── Run with metadata ──────────────────────────────────────────────
        run = await client.arun(
            "What's the weather like?",
            session_id="remote-session-1",
            user_id="demo-user",
            metadata={"source": "cookbook-remote-client"},
        )
        print(f"\nSession run completed: {bool(run.result)}")


if __name__ == "__main__":
    asyncio.run(main())
