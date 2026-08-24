"""Declaration-bound validation for normalized child model output."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from ..capability_schema import (
    preflight_capability_arguments,
    validate_capability_arguments,
)
from .children import ChildRunContractError, ChildRunSpec
from .errors import HarnessError
from .operations import OperationSettlement
from .security import freeze_data, thaw_data
from .store import RunOwner, RuntimeEventInput, RuntimeStore
from .usage import enforce_child_budget


@dataclass(frozen=True)
class ChildOutputAssessment:
    """Content-free evidence that normalized output matched its declared schema."""

    schema_digest: str
    valid: bool
    mismatch: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "mismatch", freeze_data(self.mismatch))

    def event_payload(self, *, spec_digest: str) -> dict[str, Any]:
        return {
            "child_spec_digest": spec_digest,
            "result_schema_digest": self.schema_digest,
            "valid": self.valid,
            "mismatch": thaw_data(self.mismatch),
        }


def enforce_child_output_schema(
    *,
    store: RuntimeStore,
    owner: RunOwner,
    spec: ChildRunSpec,
    settlement: OperationSettlement,
    output: Any,
) -> ChildOutputAssessment | None:
    """Validate normalized content, persist evidence, and reject a mismatch."""
    schema_digest = spec.result_schema_digest
    if spec.result_schema is None or schema_digest is None:
        return None
    schema = thaw_data(spec.result_schema)
    mismatch: dict[str, Any] | None = None
    try:
        preflight_capability_arguments(
            output,
            capability=f"child-output:{spec.purpose_code}",
        )
        validate_capability_arguments(
            cast(Mapping[str, Any], schema),
            cast(Mapping[str, Any], output),
            capability=f"child-output:{spec.purpose_code}",
        )
    except HarnessError as exc:
        details = exc.details or {}
        mismatch = {
            key: thaw_data(details[key])
            for key in ("keyword", "schema_path")
            if key in details
        }
    assessment = ChildOutputAssessment(
        schema_digest=schema_digest,
        valid=mismatch is None,
        mismatch=mismatch,
    )
    identity = hashlib.sha256(spec.child_run_id.encode()).hexdigest()[:24]
    store.append_runtime_event(
        RuntimeEventInput(
            event_id=f"evt_child_output_{identity}",
            run_id=spec.child_run_id,
            event_type="run.child.output.validated",
            occurred_at=settlement.settled_at,
            attempt_id=f"{spec.child_run_id}:attempt:1",
            payload=assessment.event_payload(spec_digest=spec.digest),
        ),
        owner=owner,
    )
    if not assessment.valid:
        raise ChildRunContractError(
            code="CHILD_OUTPUT_SCHEMA_MISMATCH",
            message="Child output does not satisfy its declaration-bound result schema.",
            details=thaw_data(assessment.mismatch),
        )
    return assessment


def enforce_child_result_contracts(
    *,
    store: RuntimeStore,
    owner: RunOwner,
    spec: ChildRunSpec,
    settlement: OperationSettlement,
    result: Any,
) -> None:
    """Apply usage and output contracts before a child becomes terminal."""
    enforce_child_budget(store=store, owner=owner, spec=spec, settlement=settlement)
    output = result.get("content") if isinstance(result, dict) else result
    enforce_child_output_schema(
        store=store,
        owner=owner,
        spec=spec,
        settlement=settlement,
        output=output,
    )


__all__ = [
    "ChildOutputAssessment",
    "enforce_child_result_contracts",
    "enforce_child_output_schema",
]
