"""Internal profile routing for first-party harness clients."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, cast

from ..config import RuntimeProfile
from .presentation import LiveRunPresentation
from .run_handle import HarnessRun

if TYPE_CHECKING:
    from ..agent import AgentHarness


_LIFECYCLE_PROFILES = frozenset(
    {RuntimeProfile.QUICK, RuntimeProfile.DURABLE, RuntimeProfile.SERVICE}
)


def uses_lifecycle_route(agent: AgentHarness) -> bool:
    """Return whether unattended work must enter the controllable lifecycle."""
    try:
        profile = RuntimeProfile(agent.profile)
    except (AttributeError, TypeError, ValueError):
        return False
    return profile in _LIFECYCLE_PROFILES


async def first_party_run(
    agent: AgentHarness,
    message: str,
    **kwargs: Any,
) -> HarnessRun:
    """Route one non-streaming first-party call into the existing run facade.

    Every explicit 0.12 profile enters ``AgentHarness.start()`` so callers can
    retain logical identity across waiter cancellation. Only the named legacy
    profile and duck-typed compatibility clients retain direct ``arun()`` behavior.
    """
    if kwargs.get("stream"):
        raise ValueError("first_party_run() only supports non-streaming execution")
    if uses_lifecycle_route(agent):
        return await agent.start(message, **kwargs)
    return HarnessRun(result=await agent.arun(message, **kwargs))


async def _empty_events() -> AsyncIterator[Any]:
    if False:  # pragma: no cover - preserves the async-generator shape
        yield None


async def first_party_stream(
    agent: AgentHarness,
    message: str,
    **kwargs: Any,
) -> tuple[HarnessRun, AsyncIterator[Any]]:
    """Open one first-party live presentation stream around the existing run facade.

    Explicit-profile work enters ``start()`` and receives a bounded process-local
    presentation attachment. Slow or disconnected displays detach without applying
    backpressure or cancellation to the logical run. Quick/legacy clients retain the
    existing raw Agno stream.
    """
    kwargs.pop("stream", None)
    if uses_lifecycle_route(agent):
        presentation = LiveRunPresentation()
        try:
            run = await agent.start(
                message,
                _presentation=presentation,
                **kwargs,
            )
        except BaseException:
            presentation.finish()
            raise
        presentation.bind(run.run_id)
        return run, presentation.events()

    response = await agent.arun(message, stream=True, **kwargs)
    if hasattr(response, "__aiter__"):
        events = cast(AsyncIterator[Any], response)
        return HarnessRun(stream=events), events
    return HarnessRun(result=response), _empty_events()


__all__ = ["first_party_run", "first_party_stream", "uses_lifecycle_route"]
