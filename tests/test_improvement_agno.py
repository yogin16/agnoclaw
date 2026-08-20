"""Agno-native subject adapter contracts for paired improvement evaluation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from agnoclaw import (
    AgnoEvaluationSubject,
    EvaluationCase,
    EvaluationSlice,
    HarnessError,
    agno_evaluation_subject_factory,
)


class _StructuredOutput(BaseModel):
    answer: str
    confidence: float


class _FakeAgent:
    def __init__(self, *, content=None, metrics=None) -> None:
        self.content = content if content is not None else {"answer": "safe"}
        self.metrics = metrics
        self.calls: list[dict[str, object]] = []
        self.closed = False

    async def arun(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=self.content,
            metrics=self.metrics,
            model_provider_data={"secret": "must-not-enter-rollout"},
        )

    async def aclose(self) -> None:
        self.closed = True


def _case() -> EvaluationCase:
    return EvaluationCase(
        case_id="private-case-name",
        slice=EvaluationSlice.HELD_OUT,
        task_class="incident-response",
        payload={"question": "What is the safe action?", "priority": 2},
    )


@pytest.mark.asyncio
async def test_agno_subject_normalizes_content_usage_cost_and_session_isolation() -> None:
    agent = _FakeAgent(
        content=_StructuredOutput(answer="isolate", confidence=0.9),
        metrics=SimpleNamespace(
            total_tokens=0,
            input_tokens=11,
            output_tokens=7,
            cost=0.004,
        ),
    )
    subject = AgnoEvaluationSubject(agent, close_agent=True)

    first = await subject(_case())
    second = await subject(_case())
    await subject.aclose()

    assert first.output == {"answer": "isolate", "confidence": 0.9}
    assert (first.tokens, first.cost_usd) == (18, 0.004)
    assert first == second
    assert agent.calls[0]["stream"] is False
    assert agent.calls[0]["input"] == '{"priority":2,"question":"What is the safe action?"}'
    assert agent.calls[0]["session_id"] != agent.calls[1]["session_id"]
    assert "private-case-name" not in str(agent.calls[0]["session_id"])
    assert "must-not-enter-rollout" not in str(first.to_dict())
    assert agent.closed is True


@pytest.mark.asyncio
async def test_agno_subject_factory_requests_one_fresh_agent_per_rollout() -> None:
    agents: list[_FakeAgent] = []

    def build_agent() -> _FakeAgent:
        agent = _FakeAgent(metrics=SimpleNamespace(total_tokens=3, cost=None))
        agents.append(agent)
        return agent

    factory = agno_evaluation_subject_factory(
        build_agent,
        input_builder=lambda case: str(case.payload["question"]),
    )
    first = factory()
    second = factory()

    assert first is not second
    assert len(agents) == 2
    assert (await first(_case())).tokens == 3
    assert (await second(_case())).tokens == 3


@pytest.mark.asyncio
async def test_agno_subject_closes_owned_agent_model_transport() -> None:
    transport = MagicMock()
    agent = _FakeAgent()
    agent.model = SimpleNamespace(
        client=SimpleNamespace(close=transport.close),
        async_client=None,
    )
    subject = AgnoEvaluationSubject(agent, close_agent=True)

    await subject(_case())
    await subject.aclose()
    await subject.aclose()

    assert agent.closed is True
    transport.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_agno_subject_fails_closed_on_invalid_metrics_or_opaque_output() -> None:
    bad_metrics = AgnoEvaluationSubject(
        _FakeAgent(metrics=SimpleNamespace(total_tokens=-1, cost=0)),
    )
    with pytest.raises(HarnessError) as metric_error:
        await bad_metrics(_case())
    assert metric_error.value.code == "IMPROVEMENT_AGNO_METRICS_INVALID"
    assert "private-case-name" not in str(metric_error.value)

    opaque = AgnoEvaluationSubject(_FakeAgent(content=object(), metrics=None))
    with pytest.raises(TypeError, match="JSON-like"):
        await opaque(_case())


def test_agno_subject_rejects_opaque_agent_and_unsafe_configuration() -> None:
    with pytest.raises(TypeError, match="arun"):
        AgnoEvaluationSubject(object())
    with pytest.raises(ValueError, match="session_prefix"):
        AgnoEvaluationSubject(_FakeAgent(), session_prefix="")
    with pytest.raises(TypeError, match="agent_factory"):
        agno_evaluation_subject_factory(None)  # type: ignore[arg-type]
