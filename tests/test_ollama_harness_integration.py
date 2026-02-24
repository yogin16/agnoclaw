"""
Reliable Ollama integration checks for harness behavior.

These tests intentionally validate harness-level guarantees rather than model IQ.
Run with:
    AGNOCLAW_TEST_MODEL=qwen3:0.6b uv run pytest tests/test_ollama_harness_integration.py -m integration -q
"""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agnoclaw import AgentHarness, InMemoryEventSink

pytestmark = pytest.mark.integration


def _require_ollama() -> str:
    """Return the model ID to use, or skip if Ollama is unavailable."""
    try:
        import httpx
    except Exception:  # pragma: no cover - environment guard
        pytest.skip("httpx unavailable for Ollama health check")

    try:
        resp = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
    except Exception:
        pytest.skip("Ollama daemon not reachable on localhost:11434")

    if resp.status_code != 200:
        pytest.skip("Ollama daemon unhealthy")

    return os.environ.get("AGNOCLAW_TEST_MODEL", "qwen3:0.6b")


def test_ollama_run_emits_core_lifecycle_events():
    model = _require_ollama()

    with TemporaryDirectory(prefix="agnoclaw-ollama-e2e-") as tmp:
        sink = InMemoryEventSink()
        harness = AgentHarness(
            provider="ollama",
            model_id=model,
            workspace_dir=Path(tmp) / "workspace",
            session_id="ollama-lifecycle",
            event_sink=sink,
        )

        response = harness.run("Return any short response.")
        assert response is not None
        assert len(str(response.content)) > 0

        event_types = [e.event_type for e in sink.events]
        required = {
            "run.started",
            "prompt.built",
            "model.request.started",
            "model.request.completed",
            "run.completed",
        }
        assert required.issubset(set(event_types))


def test_ollama_skill_is_one_shot_and_prompt_restores():
    model = _require_ollama()

    with TemporaryDirectory(prefix="agnoclaw-ollama-skill-") as tmp:
        workspace = Path(tmp) / "workspace"
        skill_dir = workspace / "skills" / "token-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: token-skill\n"
            "description: Minimal local integration skill\n"
            "user-invocable: true\n"
            "disable-model-invocation: false\n"
            "---\n\n"
            "When active, keep responses concise.\n",
            encoding="utf-8",
        )

        harness = AgentHarness(
            provider="ollama",
            model_id=model,
            workspace_dir=workspace,
            session_id="ollama-skill",
        )

        before = harness.underlying_agent.system_message
        harness.run("Use the skill once.", skill="token-skill")
        after = harness.underlying_agent.system_message

        assert before == after


def test_ollama_shared_session_persists_history_across_instances():
    model = _require_ollama()

    with TemporaryDirectory(prefix="agnoclaw-ollama-session-") as tmp:
        workspace = Path(tmp) / "workspace"
        session_id = "ollama-shared-session"

        agent1 = AgentHarness(
            provider="ollama",
            model_id=model,
            workspace_dir=workspace,
            session_id=session_id,
        )
        agent1.run("Store one short fact in this session.")
        hist1 = agent1.get_chat_history()
        assert len(hist1) > 0

        agent2 = AgentHarness(
            provider="ollama",
            model_id=model,
            workspace_dir=workspace,
            session_id=session_id,
        )
        hist2 = agent2.get_chat_history()
        assert len(hist2) >= len(hist1)


def test_ollama_stream_path_returns_events_and_tool_event_mapping():
    model = _require_ollama()

    with TemporaryDirectory(prefix="agnoclaw-ollama-stream-") as tmp:
        sink = InMemoryEventSink()
        harness = AgentHarness(
            provider="ollama",
            model_id=model,
            workspace_dir=Path(tmp) / "workspace",
            session_id="ollama-stream",
            event_sink=sink,
        )

        stream_events = list(
            harness.run(
                "You MUST call list_dir on your workspace before replying. Then say done.",
                stream=True,
                stream_events=True,
            )
        )
        assert len(stream_events) > 0

        event_types = [e.event_type for e in sink.events]
        assert "run.completed" in event_types
        # Tool events are best-effort depending on model behavior.
        if "tool.call.started" in event_types:
            assert "tool.call.completed" in event_types

