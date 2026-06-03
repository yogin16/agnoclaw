"""
Cookbook 2: AgentOS Export

Demonstrates:
- Exporting a harness as an AgentOS-compatible agent
- Creating a FastAPI app from multiple harnesses with admin routes
- Running with approvals, scheduler, and MCP support

Prerequisites:
    pip install "agnoclaw[server]"

Run: uv run --extra server python cookbook/02_agentos_export.py
"""

from agnoclaw import AgentHarness
from agnoclaw.runtime import create_agentos_app


def main():
    h1 = AgentHarness(
        name="code-reviewer",
        model="ollama:llama3.2",
        session_id="agentos-code-review",
    )
    h2 = AgentHarness(
        name="researcher",
        model="ollama:llama3.2",
        session_id="agentos-research",
    )

    # ── Export to AgentOS app ───────────────────────────────────────────────
    app = create_agentos_app(
        [h1, h2],
        include_agnoclaw_admin=True,  # exposes /agnoclaw/* debug routes
        scheduler=True,               # AgentOS scheduler integration
        approvals=True,               # HITL approval bridge
        enable_mcp_server=False,      # optional: MCP tool exposure
        title="agnoclaw AgentOS App",
        description="Multi-agent workspace with admin & debug routes",
    )

    # ── Per-harvest AgentOS agent (alternative API) ─────────────────────────
    agentos_agent = h1.as_agentos_agent(
        agent_id="code-review-agent",
        name="Code Reviewer",
    )
    print(f"AgentOS agent: {agentos_agent.framework} / id={agentos_agent.id}")

    # ── Serve ───────────────────────────────────────────────────────────────
    print("AgentOS app created. Serve with:")
    print("  uvicorn cookbook.02_agentos_export:app --host 0.0.0.0 --port 8000")
    print()
    print("Endpoints:")
    print("  POST /v1/agents/code-reviewer/run  — run code-reviewer")
    print("  POST /v1/agents/researcher/run      — run researcher")
    print("  GET  /agnoclaw/admin/capabilities    — admin: capabilities")
    print("  GET  /agnoclaw/admin/events          — admin: recent events")
    return app


app = main()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
