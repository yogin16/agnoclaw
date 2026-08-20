"""Contracts for profile-aware first-party execution."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agnoclaw.config import RuntimeProfile
from agnoclaw.runtime.first_party import (
    first_party_run,
    first_party_stream,
    uses_lifecycle_route,
)
from agnoclaw.runtime.presentation import (
    LiveRunPresentation,
    RunPresentationDetached,
)

ROOT = Path(__file__).resolve().parents[1]


class _RawAgentAccessVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.classes: list[str] = []
        self.functions: list[str] = []
        self.accesses: set[tuple[str, str, str, str]] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.classes.append(node.name)
        self.generic_visit(node)
        self.classes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        value = node.value
        if (
            node.attr in {"run", "arun", "print_response"}
            and isinstance(value, ast.Attribute)
            and value.attr == "_agent"
            and isinstance(value.value, ast.Name)
            and value.value.id == "self"
        ):
            self.accesses.add(
                (
                    self.relative_path,
                    self.classes[-1] if self.classes else "",
                    self.functions[-1] if self.functions else "",
                    node.attr,
                )
            )
        self.generic_visit(node)


def test_raw_agno_execution_boundaries_are_explicitly_allowlisted() -> None:
    accesses: set[tuple[str, str, str, str]] = set()
    for path in (ROOT / "src" / "agnoclaw").rglob("*.py"):
        visitor = _RawAgentAccessVisitor(str(path.relative_to(ROOT)))
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        accesses.update(visitor.accesses)

    assert accesses == {
        ("src/agnoclaw/agent.py", "AgentHarness", "run", "run"),
        ("src/agnoclaw/agent.py", "AgentHarness", "arun", "arun"),
        (
            "src/agnoclaw/improvement_agno.py",
            "AgnoEvaluationSubject",
            "__call__",
            "arun",
        ),
    }


@pytest.mark.parametrize(
    "profile",
    [RuntimeProfile.QUICK, RuntimeProfile.DURABLE, RuntimeProfile.SERVICE],
)
def test_explicit_profiles_use_lifecycle_route(profile):
    agent = MagicMock(profile=profile)

    assert uses_lifecycle_route(agent) is True


def test_named_legacy_profile_keeps_direct_route():
    profile = RuntimeProfile.LEGACY
    agent = MagicMock(profile=profile)

    assert uses_lifecycle_route(agent) is False


def test_unknown_adapter_profile_fails_to_compatibility_route():
    agent = MagicMock()
    del agent.profile

    assert uses_lifecycle_route(agent) is False


@pytest.mark.asyncio
async def test_first_party_run_returns_durable_handle_without_waiting():
    handle = MagicMock(run_id="run_durable")
    agent = MagicMock(profile=RuntimeProfile.DURABLE)
    agent.start = AsyncMock(return_value=handle)
    agent.arun = AsyncMock()

    result = await first_party_run(agent, "work", skill="review")

    assert result is handle
    agent.start.assert_awaited_once_with("work", skill="review")
    agent.arun.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_party_run_wraps_legacy_result_in_existing_facade():
    response = MagicMock(content="done")
    agent = MagicMock(profile=RuntimeProfile.LEGACY)
    agent.arun = AsyncMock(return_value=response)
    agent.start = AsyncMock()

    run = await first_party_run(agent, "work")

    assert run.run_id is None
    assert await run.wait() is response
    agent.arun.assert_awaited_once_with("work")
    agent.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_party_run_rejects_streaming_instead_of_miswrapping_iterator():
    agent = MagicMock(profile=RuntimeProfile.LEGACY)

    with pytest.raises(ValueError, match="non-streaming"):
        await first_party_run(agent, "work", stream=True)

    agent.arun.assert_not_called()


@pytest.mark.asyncio
async def test_first_party_stream_attaches_durable_presentation_to_existing_handle():
    handle = MagicMock(run_id="run_durable")
    event = SimpleNamespace(event="RunContent", content="hello")

    async def start(_message, **kwargs):
        presentation = kwargs.pop("_presentation")
        presentation.publish(event)
        presentation.finish()
        assert kwargs == {"skill": "review"}
        return handle

    agent = MagicMock(profile=RuntimeProfile.DURABLE)
    agent.start = AsyncMock(side_effect=start)
    agent.arun = AsyncMock()

    run, events = await first_party_stream(agent, "work", skill="review")

    assert run is handle
    assert [item async for item in events] == [event]
    agent.arun.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_party_stream_preserves_legacy_raw_stream():
    event = SimpleNamespace(event="RunContent", content="hello")

    async def raw_events():
        yield event

    stream = raw_events()
    agent = MagicMock(profile=RuntimeProfile.LEGACY)
    agent.arun = AsyncMock(return_value=stream)
    agent.start = AsyncMock()

    run, events = await first_party_stream(agent, "work", skill="review")

    assert run.run_id is None
    assert [item async for item in events] == [event]
    agent.arun.assert_awaited_once_with("work", stream=True, skill="review")
    agent.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_presentation_detaches_slow_consumer_without_backpressure():
    presentation = LiveRunPresentation(capacity=1)
    presentation.bind("run_slow")

    assert presentation.publish("first") is True
    assert presentation.publish("second") is False

    events = [item async for item in presentation.events()]
    assert len(events) == 1
    detached = events[0]
    assert isinstance(detached, RunPresentationDetached)
    assert detached.run_id == "run_slow"
    assert detached.reason_code == "PRESENTATION_SLOW_CONSUMER"
    assert detached.published_events == 1


@pytest.mark.asyncio
async def test_live_presentation_is_single_consumer_and_normal_finish_is_exact():
    presentation = LiveRunPresentation(capacity=2)
    presentation.publish("first")
    presentation.finish()

    assert [item async for item in presentation.events()] == ["first"]
    with pytest.raises(RuntimeError, match="one consumer"):
        async for _ in presentation.events():
            pass
