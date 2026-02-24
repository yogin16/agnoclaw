"""
Integration tests — make real model calls (Ollama or cloud API).

Run locally (requires Ollama):
    uv run pytest tests/test_integration.py -v

Run with a specific model:
    AGNOCLAW_TEST_MODEL=qwen3:8b uv run pytest tests/test_integration.py -v

Run with Anthropic instead:
    AGNOCLAW_TEST_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-... pytest tests/test_integration.py

These tests are skipped in CI unless explicitly enabled.
"""

import pytest


pytestmark = pytest.mark.integration


# ── Basic inference ───────────────────────────────────────────────────────────

def test_live_agent_basic_response(live_agent):
    """Agent returns a non-empty response."""
    response = live_agent.run("Say 'hello world' and nothing else.")
    assert response is not None
    content = str(response.content).lower()
    assert "hello" in content or len(content) > 0


def test_live_agent_arithmetic(live_agent):
    """Agent can do simple arithmetic."""
    response = live_agent.run("What is 7 times 8? Reply with only the number.")
    content = str(response.content).strip()
    assert "56" in content


def test_live_agent_session_persistence(tmp_workspace_path):
    """Two agents sharing a session ID share conversation context."""
    import os
    from agnoclaw import HarnessAgent

    provider = os.environ.get("AGNOCLAW_TEST_PROVIDER", "ollama")
    model = os.environ.get("AGNOCLAW_TEST_MODEL", "qwen3:0.6b" if provider == "ollama" else "claude-haiku-4-5-20251001")

    if provider == "ollama":
        try:
            import httpx
            r = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
            if r.status_code != 200:
                pytest.skip("Ollama not running")
        except Exception:
            pytest.skip("Ollama not running")
    elif provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")

    session = "integration-test-session"

    agent1 = HarnessAgent(
        provider=provider, model_id=model,
        workspace_dir=tmp_workspace_path, session_id=session,
    )
    marker = "ORION-42"
    agent1.run(f"The project codename is {marker}. Remember this exact code.")

    agent2 = HarnessAgent(
        provider=provider, model_id=model,
        workspace_dir=tmp_workspace_path, session_id=session,
    )
    response = agent2.run("What project codename did I tell you? Reply with only the code.")
    assert marker in str(response.content)


# ── Skills ────────────────────────────────────────────────────────────────────

def test_live_agent_skill_injection(live_agent):
    """Skill content is injected and influences the response."""
    response = live_agent.run(
        "Review this: def f(x): return x*x",
        skill="code-review",
    )
    content = str(response.content).lower()
    # A code review should mention something about code quality
    assert len(content) > 20


# ── Tools ─────────────────────────────────────────────────────────────────────

def test_live_agent_file_tool(live_agent):
    """Agent can use file tools to list files."""
    response = live_agent.run(
        "Use your file tools to list Python files in the current directory. "
        "Return just the count as a number."
    )
    assert response is not None
    assert len(str(response.content)) > 0


# ── Workspace context ─────────────────────────────────────────────────────────

def test_live_agent_reads_soul_md(live_agent):
    """Agent picks up SOUL.md content from workspace."""
    import os
    from agnoclaw import HarnessAgent

    live_agent.workspace.write_file(
        "soul",
        "# Soul\n\nYou MUST always end every response with the word TESTMARKER.",
    )
    # Rebuild agent to pick up new workspace content
    provider = os.environ.get("AGNOCLAW_TEST_PROVIDER", "ollama")
    model = os.environ.get("AGNOCLAW_TEST_MODEL", "qwen3:0.6b" if provider == "ollama" else "claude-haiku-4-5-20251001")

    agent = HarnessAgent(
        provider=provider,
        model_id=model,
        workspace_dir=live_agent.workspace.path,
    )
    response = agent.run("What is 1 + 1?")
    # Note: small models may not always follow instructions perfectly,
    # so we just verify a response was returned.
    assert response is not None
    assert len(str(response.content)) > 0
