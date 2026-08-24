"""Content-minimized provider usage evidence and child-budget assessment."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import math
from collections.abc import MutableMapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from .children import ChildRunBudget, ChildRunContractError, ChildRunSpec
from .concurrency import drain_thread_call
from .lifecycle import (
    LifecycleTransition,
    RunRevisionConflictError,
    RunState,
    TransitionKind,
)
from .operations import OperationSettlement, OperationSettlementEvidence
from .security import freeze_data, thaw_data
from .store import RunOwner, RuntimeEventInput, RuntimeStore

_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "audio_input_tokens",
    "audio_output_tokens",
    "audio_total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _cost_microusd(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not decimal.is_finite() or decimal < 0:
        return None
    return int((decimal * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))


def agno_result_settlement_evidence(result: Any) -> OperationSettlementEvidence:
    """Extract stable Agno 2.6/2.8 usage without persisting provider-private data."""
    metrics = _field(result, "metrics")
    usage: dict[str, Any] = {"source": "agno.run.metrics", "reported": False}
    if metrics is not None:
        for name in _TOKEN_FIELDS:
            normalized = _nonnegative_int(_field(metrics, name))
            if normalized is not None:
                usage[name] = normalized
        total = usage.get("total_tokens", 0)
        if total == 0:
            total = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            if total > 0:
                usage["total_tokens"] = total
        usage["reported"] = bool(total > 0)
        duration = _field(metrics, "duration")
        if (
            isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and math.isfinite(float(duration))
            and duration >= 0
        ):
            usage["duration_milliseconds"] = math.ceil(float(duration) * 1000)

    cost_value = _cost_microusd(_field(metrics, "cost")) if metrics is not None else None
    cost: dict[str, Any] = {
        "currency": "USD",
        "source": "agno.run.metrics",
        "reported": cost_value is not None,
    }
    if cost_value is not None:
        cost["microusd"] = cost_value

    provider_request_id = None
    provider_data = _field(result, "model_provider_data")
    if isinstance(provider_data, dict):
        candidate = provider_data.get("request_id")
        if isinstance(candidate, str) and 0 < len(candidate) <= 512:
            provider_request_id = candidate
    return OperationSettlementEvidence(
        provider_request_id=provider_request_id,
        usage=usage,
        cost=cost,
    )


def agno_model_response_settlement_evidence(result: Any) -> OperationSettlementEvidence:
    """Extract content-free evidence from one provider response or response stream."""
    candidates = result if isinstance(result, (list, tuple)) else (result,)
    selected = None
    for candidate in candidates:
        if candidate is None:
            continue
        if _field(candidate, "response_usage") is not None or _field(
            candidate, "provider_data"
        ) is not None:
            selected = candidate
    if selected is None and candidates:
        selected = candidates[-1]

    metrics = _field(selected, "response_usage") if selected is not None else None
    usage: dict[str, Any] = {"source": "agno.model.response_usage", "reported": False}
    for name in _TOKEN_FIELDS:
        normalized = _nonnegative_int(_field(metrics, name))
        if normalized is None and selected is not None:
            normalized = _nonnegative_int(_field(selected, name))
        if normalized is not None:
            usage[name] = normalized
    total = usage.get("total_tokens", 0)
    if total == 0:
        total = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        if total > 0:
            usage["total_tokens"] = total
    usage["reported"] = bool(total > 0)

    cost_value = _cost_microusd(_field(metrics, "cost")) if metrics is not None else None
    cost: dict[str, Any] = {
        "currency": "USD",
        "source": "agno.model.response_usage",
        "reported": cost_value is not None,
    }
    if cost_value is not None:
        cost["microusd"] = cost_value

    provider_request_id = None
    provider_data = _field(selected, "provider_data") if selected is not None else None
    if isinstance(provider_data, dict):
        for key in ("request_id", "response_id", "id"):
            candidate = provider_data.get(key)
            if isinstance(candidate, str) and 0 < len(candidate) <= 512:
                provider_request_id = candidate
                break
    return OperationSettlementEvidence(
        provider_request_id=provider_request_id,
        usage=usage,
        cost=cost,
    )


@dataclass(frozen=True)
class ChildBudgetAssessment:
    """Post-dispatch comparison of reported provider usage to one child grant."""

    measured: Any
    exceeded_dimensions: tuple[str, ...]
    unverified_dimensions: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "measured", freeze_data(self.measured))
        for values in (self.exceeded_dimensions, self.unverified_dimensions):
            if len(set(values)) != len(values):
                raise ValueError("budget dimensions must be unique")

    @property
    def exceeded(self) -> bool:
        return bool(self.exceeded_dimensions)

    @property
    def fully_verified(self) -> bool:
        return not self.unverified_dimensions

    def event_payload(self, *, budget: ChildRunBudget, spec_digest: str) -> dict[str, Any]:
        return {
            "child_spec_digest": spec_digest,
            "limits": {
                "max_tokens": budget.max_tokens,
                "max_cost_microusd": budget.max_cost_microusd,
            },
            "measured": thaw_data(self.measured),
            "exceeded_dimensions": list(self.exceeded_dimensions),
            "unverified_dimensions": list(self.unverified_dimensions),
            "fully_verified": self.fully_verified,
        }


def assess_child_budget(
    budget: ChildRunBudget,
    settlement: OperationSettlement,
) -> ChildBudgetAssessment:
    usage = thaw_data(settlement.usage)
    cost = thaw_data(settlement.cost)
    measured: dict[str, int] = {}
    exceeded: list[str] = []
    unverified: list[str] = []

    total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    normalized_tokens = _nonnegative_int(total_tokens)
    usage_reported = usage.get("reported") is True if isinstance(usage, dict) else False
    if usage_reported and normalized_tokens is not None:
        measured["total_tokens"] = normalized_tokens
        if normalized_tokens > budget.max_tokens:
            exceeded.append("tokens")
    else:
        unverified.append("tokens")

    microusd = cost.get("microusd") if isinstance(cost, dict) else None
    normalized_cost = _nonnegative_int(microusd)
    cost_reported = cost.get("reported") is True if isinstance(cost, dict) else False
    if cost_reported and normalized_cost is not None:
        measured["cost_microusd"] = normalized_cost
        if normalized_cost > budget.max_cost_microusd:
            exceeded.append("cost")
    else:
        unverified.append("cost")

    return ChildBudgetAssessment(
        measured=measured,
        exceeded_dimensions=tuple(exceeded),
        unverified_dimensions=tuple(unverified),
    )


def record_child_budget_assessment(
    *,
    store: RuntimeStore,
    owner: RunOwner,
    spec: ChildRunSpec,
    settlement: OperationSettlement,
) -> ChildBudgetAssessment:
    """Append one idempotent usage observation before child terminal settlement."""
    assessment = assess_child_budget(spec.budget, settlement)
    identity = hashlib.sha256(spec.child_run_id.encode()).hexdigest()[:24]
    store.append_runtime_event(
        RuntimeEventInput(
            event_id=f"evt_child_budget_{identity}",
            run_id=spec.child_run_id,
            event_type="run.child.budget.observed",
            occurred_at=settlement.settled_at,
            attempt_id=f"{spec.child_run_id}:attempt:1",
            payload=assessment.event_payload(
                budget=spec.budget,
                spec_digest=spec.digest,
            ),
        ),
        owner=owner,
    )
    return assessment


def enforce_child_budget(
    *,
    store: RuntimeStore,
    owner: RunOwner,
    spec: ChildRunSpec,
    settlement: OperationSettlement,
) -> ChildBudgetAssessment:
    """Record reported usage and reject a successful child that exceeded its grant."""
    assessment = record_child_budget_assessment(
        store=store,
        owner=owner,
        spec=spec,
        settlement=settlement,
    )
    if assessment.exceeded:
        raise ChildRunContractError(
            code="CHILD_RESOURCE_BUDGET_EXCEEDED",
            message="Reported child usage exceeded its declared resource grant.",
            details={"exceeded_dimensions": list(assessment.exceeded_dimensions)},
        )
    return assessment


async def supervise_child_deadline(
    *,
    store: RuntimeStore,
    run_id: str,
    spec: ChildRunSpec,
    worker_task: asyncio.Task[Any],
    failures: MutableMapping[str, BaseException],
) -> None:
    """Convert a finite child wall grant into an authoritative cancel request."""
    try:
        await asyncio.sleep(spec.budget.timeout_seconds)
        for _attempt in range(3):
            snapshot, cancelled, error = await drain_thread_call(
                lambda: store.get_run(run_id)
            )
            if cancelled:
                raise asyncio.CancelledError
            if error is not None:
                raise error
            if snapshot.terminal or snapshot.state in {
                RunState.CANCELLING,
                RunState.WAITING_FOR_RECONCILIATION,
            }:
                return
            expected_revision = snapshot.revision
            _decision, cancelled, error = await drain_thread_call(
                functools.partial(
                    store.apply_transition,
                    LifecycleTransition(
                        run_id=run_id,
                        kind=TransitionKind.REQUEST_CANCEL,
                        transition_id=f"{run_id}:child-timeout",
                        reason_code="CHILD_TIMEOUT_EXCEEDED",
                        payload={"child_spec_digest": spec.digest},
                    ),
                    expected_revision=expected_revision,
                )
            )
            if cancelled:
                raise asyncio.CancelledError
            if error is None:
                worker_task.cancel()
                return
            if not isinstance(error, RunRevisionConflictError):
                raise error
        raise ChildRunContractError(
            code="CHILD_TIMEOUT_ENFORCEMENT_CONFLICT",
            message="Child deadline enforcement exhausted its revision retries.",
            details={"run_id": run_id},
        )
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        failures[run_id] = exc
        worker_task.cancel()


__all__ = [
    "ChildBudgetAssessment",
    "agno_model_response_settlement_evidence",
    "agno_result_settlement_evidence",
    "assess_child_budget",
    "enforce_child_budget",
    "record_child_budget_assessment",
    "supervise_child_deadline",
]
