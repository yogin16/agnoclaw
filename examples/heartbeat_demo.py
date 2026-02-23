"""
Heartbeat daemon example.

Demonstrates the heartbeat system — periodic agent check-ins.

Run: uv run python examples/heartbeat_demo.py
Requires: ANTHROPIC_API_KEY env var

The example:
1. Initializes the workspace with a HEARTBEAT.md checklist
2. Starts the heartbeat daemon (1-minute interval for demo)
3. Triggers one immediate heartbeat check
4. Runs for 3 minutes then exits
"""

import asyncio
from pathlib import Path

from agnoclaw import AgentHarness
from agnoclaw.heartbeat import HeartbeatDaemon
from agnoclaw.workspace import Workspace


async def main():
    # Set up workspace with a demo HEARTBEAT.md
    ws = Workspace()
    ws.initialize()
    ws.write_file("heartbeat", """# Heartbeat Checklist

- Check if any git repositories have uncommitted changes
- Verify that the tmp/ directory doesn't have files older than 24 hours
- Check system disk usage (alert if > 80%)

If nothing needs attention, reply HEARTBEAT_OK.
""")

    print(f"Workspace: {ws.path}")
    print("HEARTBEAT.md written with demo checklist")
    print()

    # Create agent
    agent = AgentHarness(
        session_id="heartbeat-demo",
        workspace_dir=ws.path,
    )

    # Alert handler
    def on_alert(message: str):
        print(f"\n{'='*50}")
        print("HEARTBEAT ALERT:")
        print(message)
        print('='*50)

    # Create daemon with 1-minute interval for demo
    from agnoclaw.config import get_config
    cfg = get_config()
    cfg.heartbeat.interval_minutes = 1
    cfg.heartbeat.enabled = True

    daemon = HeartbeatDaemon(agent, on_alert=on_alert, workspace=ws)

    # Trigger one immediate check
    print("Triggering immediate heartbeat check...")
    result = await daemon.trigger_now()
    if result:
        print(f"Alert: {result}")
    else:
        print("HEARTBEAT_OK — nothing needs attention")

    # Start daemon for 3 minutes
    print("\nStarting daemon (runs every 1 minute for 3 minutes)...")
    daemon.start()

    try:
        await asyncio.sleep(180)
    except KeyboardInterrupt:
        print("\nInterrupted")
    finally:
        daemon.stop()
        print("Daemon stopped.")


if __name__ == "__main__":
    asyncio.run(main())
