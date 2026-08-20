"""Agno-native model subjects for frozen paired improvement experiments.

This adapter executes a fresh host-supplied Agno Agent for one evaluation case and
normalizes only its JSON-like content plus bounded RunMetrics. It does not choose cases,
score outputs, edit components, write the learning ledger, or promote a candidate.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from .compat import AgnoFeature, inspect_agno_compatibility
from .improvement_runner import EvaluationCase, EvaluationRollout, SubjectFactory
from .models.ownership import OwnedAgnoModelResource
from .runtime.errors import HarnessError
from .runtime.security import freeze_data, thaw_data

EvaluationInputBuilder = Callable[[EvaluationCase], Any]
EvaluationOutputBuilder = Callable[[Any], Any]


def _error(code: str, message: str) -> HarnessError:
    return HarnessError(
        code=code,
        category="evaluation",
        message=message,
        retryable=False,
    )


def _default_input(case: EvaluationCase) -> str:
    payload = thaw_data(case.payload)
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _default_output(content: Any) -> Any:
    model_dump = getattr(content, "model_dump", None)
    if callable(model_dump):
        content = model_dump(mode="json")
    return thaw_data(freeze_data(content))


def _metric_int(metrics: Any, name: str) -> int:
    value = getattr(metrics, name, 0) if metrics is not None else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _error(
            "IMPROVEMENT_AGNO_METRICS_INVALID",
            "Agno returned an invalid non-negative integer evaluation metric.",
        )
    return value


def _metric_cost(metrics: Any) -> float:
    value = getattr(metrics, "cost", None) if metrics is not None else None
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(
            "IMPROVEMENT_AGNO_METRICS_INVALID",
            "Agno returned an invalid evaluation cost metric.",
        )
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise _error(
            "IMPROVEMENT_AGNO_METRICS_INVALID",
            "Agno returned an invalid evaluation cost metric.",
        )
    return normalized


class AgnoEvaluationSubject:
    """One fresh Agno Agent adapted to the runner's async subject contract."""

    def __init__(
        self,
        agent: Any,
        *,
        input_builder: EvaluationInputBuilder = _default_input,
        output_builder: EvaluationOutputBuilder = _default_output,
        session_prefix: str = "agnoclaw-improvement-eval",
        close_agent: bool = False,
    ) -> None:
        inspect_agno_compatibility().require(AgnoFeature.MODEL_EVALUATION_SUBJECT)
        if not callable(getattr(agent, "arun", None)):
            raise TypeError("agent must provide Agno's async arun method")
        if not callable(input_builder) or not callable(output_builder):
            raise TypeError("input_builder and output_builder must be callable")
        if (
            not isinstance(session_prefix, str)
            or not session_prefix.strip()
            or len(session_prefix) > 128
        ):
            raise ValueError("session_prefix must contain 1 to 128 characters")
        if not isinstance(close_agent, bool):
            raise TypeError("close_agent must be a boolean")
        self._agent = agent
        self._input_builder = input_builder
        self._output_builder = output_builder
        self._session_prefix = session_prefix
        self._close_agent = close_agent
        self._model_resource = (
            OwnedAgnoModelResource(getattr(agent, "model", None)) if close_agent else None
        )

    async def __call__(self, case: EvaluationCase) -> EvaluationRollout:
        if not isinstance(case, EvaluationCase):
            raise TypeError("case must be an EvaluationCase")
        model_input = self._input_builder(case)
        if inspect.isawaitable(model_input):
            raise TypeError("input_builder must be synchronous")
        case_digest = hashlib.sha256(case.case_id.encode()).hexdigest()[:24]
        response = await self._agent.arun(
            input=model_input,
            session_id=f"{self._session_prefix}:{case_digest}:{uuid4().hex}",
            stream=False,
        )
        if response is None or not hasattr(response, "content"):
            raise _error(
                "IMPROVEMENT_AGNO_RESPONSE_INVALID",
                "Agno returned no evaluation response contract.",
            )
        output = self._output_builder(response.content)
        if inspect.isawaitable(output):
            raise TypeError("output_builder must be synchronous")
        metrics = getattr(response, "metrics", None)
        total_tokens = _metric_int(metrics, "total_tokens")
        if total_tokens == 0:
            total_tokens = _metric_int(metrics, "input_tokens") + _metric_int(
                metrics,
                "output_tokens",
            )
        return EvaluationRollout(
            output=output,
            tokens=total_tokens,
            cost_usd=_metric_cost(metrics),
        )

    async def aclose(self) -> None:
        if not self._close_agent:
            return
        try:
            async_close = getattr(self._agent, "aclose", None)
            if callable(async_close):
                result = async_close()
                if not inspect.isawaitable(result):
                    raise TypeError("agent aclose() must return an awaitable")
                await result
                return
            close = getattr(self._agent, "close", None)
            if callable(close):
                result = await asyncio.to_thread(close)
                if inspect.isawaitable(result):
                    await result
        finally:
            if self._model_resource is not None:
                await self._model_resource.aclose()


def agno_evaluation_subject_factory(
    agent_factory: Callable[[], Any],
    *,
    input_builder: EvaluationInputBuilder = _default_input,
    output_builder: EvaluationOutputBuilder = _default_output,
    session_prefix: str = "agnoclaw-improvement-eval",
    close_agent: bool = False,
) -> SubjectFactory:
    """Return a runner factory that asks the host for one fresh Agent per rollout."""
    if not callable(agent_factory):
        raise TypeError("agent_factory must be callable")
    inspect_agno_compatibility().require(AgnoFeature.MODEL_EVALUATION_SUBJECT)

    def build() -> AgnoEvaluationSubject:
        return AgnoEvaluationSubject(
            agent_factory(),
            input_builder=input_builder,
            output_builder=output_builder,
            session_prefix=session_prefix,
            close_agent=close_agent,
        )

    return build


__all__ = [
    "AgnoEvaluationSubject",
    "EvaluationInputBuilder",
    "EvaluationOutputBuilder",
    "agno_evaluation_subject_factory",
]
