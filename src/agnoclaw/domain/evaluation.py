"""Small offline regression fixtures; model-quality studies use the host's graders."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from agnoclaw.runtime.security import canonical_json_digest

from ._contracts import Record, digest_string, frozen_json, identifier
from .verification import CheckStatus


@dataclass(frozen=True)
class EvaluationCase(Record):
    case_id: str
    inputs: Mapping[str, Any]
    expected: Mapping[str, Any]

    def __post_init__(self) -> None:
        identifier(self.case_id, "case_id")
        object.__setattr__(self, "inputs", frozen_json(self.inputs))
        object.__setattr__(self, "expected", frozen_json(self.expected))


@dataclass(frozen=True)
class CaseResult(Record):
    case_id: str
    status: CheckStatus
    elapsed_seconds: float
    actual_digest: str | None

    def __post_init__(self) -> None:
        identifier(self.case_id, "case_id")
        object.__setattr__(self, "status", CheckStatus(self.status))
        if not math.isfinite(self.elapsed_seconds) or self.elapsed_seconds < 0:
            raise ValueError("elapsed time must be finite and non-negative")
        if self.actual_digest is not None:
            digest_string(self.actual_digest)


@dataclass(frozen=True)
class EvaluationSummary(Record):
    results: tuple[CaseResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))
        if not self.results or len({item.case_id for item in self.results}) != len(self.results):
            raise ValueError("evaluation summary requires non-empty, unique results")

    @property
    def pass_rate(self) -> float:
        return sum(result.status is CheckStatus.PASS for result in self.results) / len(self.results)


async def evaluate_cases(
    cases: Sequence[EvaluationCase],
    run: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
    *,
    timeout_seconds: float = 30,
) -> EvaluationSummary:
    """Exact structured-result regression, bounded per case; never call a model implicitly."""
    if not cases or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("evaluation requires non-empty, uniquely identified cases")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("evaluation timeout must be positive and finite")
    results = []
    for case in cases:
        started = perf_counter()
        actual_digest = None
        try:
            async with asyncio.timeout(timeout_seconds):
                actual = frozen_json(await run(case.inputs))
            actual_digest = canonical_json_digest(actual)
            status = (
                CheckStatus.PASS
                if actual_digest == canonical_json_digest(case.expected)
                else CheckStatus.FAIL
            )
        except Exception:
            status = CheckStatus.ERROR
        results.append(CaseResult(case.case_id, status, perf_counter() - started, actual_digest))
    return EvaluationSummary(tuple(results))
