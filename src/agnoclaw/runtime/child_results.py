"""Deterministic, lossless handoff of direct-child results to their parent host."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .artifacts import ArtifactReference
from .children import ChildRunContractError
from .lifecycle import TERMINAL_RUN_STATES, RunState
from .operations import OperationState
from .security import freeze_data, thaw_data
from .store import OperationNotFoundError, RunOwner, RuntimeStore

CHILD_RESULT_SCHEMA_VERSION = "1.0"
MAX_CHILD_RESULT_ARTIFACTS = 100
MIN_SYNTHESIS_INLINE_CHARS = 256
MAX_SYNTHESIS_INLINE_CHARS = 1_000_000


@dataclass(frozen=True)
class ChildArtifactHandoff:
    """Content-minimized reference a parent may use to retrieve child evidence."""

    artifact_id: str
    purpose: str
    media_type: str
    encoding: str
    checksum: str
    size_bytes: int

    def __post_init__(self) -> None:
        for field_name in ("artifact_id", "purpose", "media_type", "encoding", "checksum"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"child artifact handoff requires {field_name}")
        if isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ValueError("child artifact handoff size must be non-negative")

    @classmethod
    def from_reference(cls, reference: ArtifactReference) -> ChildArtifactHandoff:
        return cls(
            artifact_id=reference.artifact_id,
            purpose=reference.purpose,
            media_type=reference.media_type,
            encoding=reference.encoding,
            checksum=reference.checksum,
            size_bytes=reference.size_bytes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "purpose": self.purpose,
            "media_type": self.media_type,
            "encoding": self.encoding,
            "checksum": self.checksum,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, value: Any) -> ChildArtifactHandoff:
        if not isinstance(value, Mapping) or set(value) != {
            "artifact_id",
            "purpose",
            "media_type",
            "encoding",
            "checksum",
            "size_bytes",
        }:
            raise ValueError("child artifact handoff fields are invalid")
        return cls(**dict(value))


@dataclass(frozen=True)
class ChildRunOutcome:
    """One direct child's typed state, terminal projection, and artifact handoff."""

    child_run_id: str
    delegation_id: str
    purpose_code: str
    state: RunState
    result: Any = None
    safe_error: Any = None
    artifacts: tuple[ChildArtifactHandoff, ...] = ()
    result_artifact: ChildArtifactHandoff | None = None
    parent_step_id: str | None = None
    parent_tool_call_id: str | None = None
    schema_version: str = CHILD_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CHILD_RESULT_SCHEMA_VERSION:
            raise ValueError("unsupported child-result schema version")
        for field_name in ("child_run_id", "delegation_id", "purpose_code"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ValueError(f"child run outcome requires bounded {field_name}")
        for field_name in ("parent_step_id", "parent_tool_call_id"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str) or not value or len(value) > 512
            ):
                raise ValueError(f"child run outcome has invalid {field_name}")
        object.__setattr__(self, "state", RunState(self.state))
        if self.state is RunState.COMPLETED and self.safe_error is not None:
            raise ValueError("a successful child outcome cannot contain an error")
        if self.state is not RunState.COMPLETED and self.result is not None:
            raise ValueError("only a successful child outcome can contain a result")
        if self.state not in TERMINAL_RUN_STATES and (
            self.result is not None or self.safe_error is not None
        ):
            raise ValueError("a pending child outcome cannot contain a terminal projection")
        if len(self.artifacts) > MAX_CHILD_RESULT_ARTIFACTS + 1:
            raise ValueError("child result contains too many artifact handoffs")
        if self.result_artifact is not None and self.result_artifact not in self.artifacts:
            raise ValueError("the result artifact must be present in the artifact handoff set")
        object.__setattr__(self, "result", freeze_data(self.result))
        object.__setattr__(self, "safe_error", freeze_data(self.safe_error))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_RUN_STATES

    @property
    def succeeded(self) -> bool:
        return self.state is RunState.COMPLETED

    def to_dict(self, *, include_result: bool = True) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "child_run_id": self.child_run_id,
            "delegation_id": self.delegation_id,
            "purpose_code": self.purpose_code,
            "state": self.state.value,
            "terminal": self.terminal,
            "succeeded": self.succeeded,
            "safe_error": thaw_data(self.safe_error),
            "artifacts": [item.to_dict() for item in self.artifacts],
            "result_artifact": (
                self.result_artifact.to_dict() if self.result_artifact is not None else None
            ),
            "parent_step_id": self.parent_step_id,
            "parent_tool_call_id": self.parent_tool_call_id,
        }
        if include_result:
            value["result"] = thaw_data(self.result)
        return value

    @classmethod
    def from_dict(cls, value: Any) -> ChildRunOutcome:
        if not isinstance(value, Mapping):
            raise ValueError("child run outcome must be an object")
        required = {
            "schema_version",
            "child_run_id",
            "delegation_id",
            "purpose_code",
            "state",
            "terminal",
            "succeeded",
            "safe_error",
            "artifacts",
            "result_artifact",
            "parent_step_id",
            "parent_tool_call_id",
            "result",
        }
        if set(value) != required or not isinstance(value["artifacts"], list):
            raise ValueError("child run outcome fields are invalid")
        artifacts = tuple(ChildArtifactHandoff.from_dict(item) for item in value["artifacts"])
        result_artifact = value["result_artifact"]
        parsed_result_artifact = (
            ChildArtifactHandoff.from_dict(result_artifact)
            if result_artifact is not None
            else None
        )
        outcome = cls(
            child_run_id=value["child_run_id"],
            delegation_id=value["delegation_id"],
            purpose_code=value["purpose_code"],
            state=value["state"],
            result=value["result"],
            safe_error=value["safe_error"],
            artifacts=artifacts,
            result_artifact=parsed_result_artifact,
            parent_step_id=value["parent_step_id"],
            parent_tool_call_id=value["parent_tool_call_id"],
            schema_version=value["schema_version"],
        )
        if value["terminal"] is not outcome.terminal or value["succeeded"] is not outcome.succeeded:
            raise ValueError("child run outcome derived state is inconsistent")
        return outcome


@dataclass(frozen=True)
class ChildResultSet:
    """Bounded direct-child outcomes with explicit partial-failure semantics."""

    parent_run_id: str
    outcomes: tuple[ChildRunOutcome, ...] = field(default_factory=tuple)
    schema_version: str = CHILD_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CHILD_RESULT_SCHEMA_VERSION:
            raise ValueError("unsupported child-result-set schema version")
        if (
            not isinstance(self.parent_run_id, str)
            or not self.parent_run_id
            or len(self.parent_run_id) > 512
        ):
            raise ValueError("child result set requires a bounded parent_run_id")
        if len(self.outcomes) > 64:
            raise ValueError("child result set cannot exceed the direct-child fan-out bound")
        child_ids = [item.child_run_id for item in self.outcomes]
        delegation_ids = [item.delegation_id for item in self.outcomes]
        if len(set(child_ids)) != len(child_ids):
            raise ValueError("child result set cannot contain duplicate child runs")
        if len(set(delegation_ids)) != len(delegation_ids):
            raise ValueError("child result set cannot contain duplicate delegations")
        object.__setattr__(self, "outcomes", tuple(self.outcomes))

    @property
    def pending(self) -> tuple[ChildRunOutcome, ...]:
        return tuple(item for item in self.outcomes if not item.terminal)

    @property
    def successful(self) -> tuple[ChildRunOutcome, ...]:
        return tuple(item for item in self.outcomes if item.succeeded)

    @property
    def failed(self) -> tuple[ChildRunOutcome, ...]:
        return tuple(item for item in self.outcomes if item.terminal and not item.succeeded)

    @property
    def all_terminal(self) -> bool:
        return not self.pending

    @property
    def all_succeeded(self) -> bool:
        return self.all_terminal and not self.failed

    def require_all_terminal(self) -> ChildResultSet:
        if self.pending:
            raise ChildRunContractError(
                code="CHILD_RESULTS_PENDING",
                message="Direct-child results are not all terminal yet.",
                details={"parent_run_id": self.parent_run_id, "pending_count": len(self.pending)},
            )
        return self

    def require_all_succeeded(self) -> ChildResultSet:
        self.require_all_terminal()
        if self.failed:
            raise ChildRunContractError(
                code="CHILD_RESULTS_FAILED",
                message="One or more direct children did not complete successfully.",
                details={"parent_run_id": self.parent_run_id, "failed_count": len(self.failed)},
            )
        return self

    def to_dict(self, *, include_results: bool = True) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "parent_run_id": self.parent_run_id,
            "all_terminal": self.all_terminal,
            "all_succeeded": self.all_succeeded,
            "pending_count": len(self.pending),
            "failed_count": len(self.failed),
            "outcomes": [
                item.to_dict(include_result=include_results) for item in self.outcomes
            ],
        }

    @classmethod
    def from_dict(cls, value: Any) -> ChildResultSet:
        if not isinstance(value, Mapping) or set(value) != {
            "schema_version",
            "parent_run_id",
            "all_terminal",
            "all_succeeded",
            "pending_count",
            "failed_count",
            "outcomes",
        }:
            raise ValueError("child result set fields are invalid")
        raw_outcomes = value["outcomes"]
        if not isinstance(raw_outcomes, list):
            raise ValueError("child result outcomes must be an array")
        result = cls(
            parent_run_id=value["parent_run_id"],
            outcomes=tuple(ChildRunOutcome.from_dict(item) for item in raw_outcomes),
            schema_version=value["schema_version"],
        )
        expected = (
            result.all_terminal,
            result.all_succeeded,
            len(result.pending),
            len(result.failed),
        )
        supplied = (
            value["all_terminal"],
            value["all_succeeded"],
            value["pending_count"],
            value["failed_count"],
        )
        if (
            not isinstance(value["all_terminal"], bool)
            or not isinstance(value["all_succeeded"], bool)
            or isinstance(value["pending_count"], bool)
            or not isinstance(value["pending_count"], int)
            or isinstance(value["failed_count"], bool)
            or not isinstance(value["failed_count"], int)
            or supplied != expected
        ):
            raise ValueError("child result set derived state is inconsistent")
        return result

    def synthesis_payload(self, *, max_inline_result_chars: int = 8_000) -> dict[str, Any]:
        """Return deterministic model input without silently truncating a child result."""
        if (
            isinstance(max_inline_result_chars, bool)
            or not MIN_SYNTHESIS_INLINE_CHARS
            <= max_inline_result_chars
            <= MAX_SYNTHESIS_INLINE_CHARS
        ):
            raise ValueError(
                "max_inline_result_chars must be between "
                f"{MIN_SYNTHESIS_INLINE_CHARS} and {MAX_SYNTHESIS_INLINE_CHARS}"
            )
        outcomes: list[dict[str, Any]] = []
        for item in self.outcomes:
            value = item.to_dict(include_result=False)
            result = thaw_data(item.result)
            if result is None:
                value["result"] = None
            else:
                rendered = json.dumps(
                    result,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                if len(rendered) <= max_inline_result_chars:
                    value["result"] = result
                elif item.result_artifact is not None:
                    value["result"] = {
                        "type": "agnoclaw.child_result_artifact",
                        "inline": False,
                        "rendered_chars": len(rendered),
                        "artifact": item.result_artifact.to_dict(),
                    }
                else:
                    raise ChildRunContractError(
                        code="CHILD_RESULT_ARTIFACT_REQUIRED",
                        message=(
                            "A child result exceeds the synthesis inline bound without a "
                            "lossless result artifact."
                        ),
                        details={"child_run_id": item.child_run_id},
                    )
            outcomes.append(value)
        payload = self.to_dict(include_results=False)
        payload["outcomes"] = outcomes
        return payload


def collect_child_results(
    store: RuntimeStore,
    parent_run_id: str,
    *,
    owner: RunOwner | None,
    limit: int = 64,
    artifact_limit: int = 16,
) -> ChildResultSet:
    """Build one owner-authorized, deterministic direct-child result snapshot."""
    if isinstance(artifact_limit, bool) or not 0 <= artifact_limit <= MAX_CHILD_RESULT_ARTIFACTS:
        raise ValueError(
            f"artifact_limit must be between 0 and {MAX_CHILD_RESULT_ARTIFACTS}"
        )
    snapshots = store.list_children(parent_run_id, limit=limit, owner=owner)
    outcomes: list[ChildRunOutcome] = []
    for snapshot in snapshots:
        spec = store.get_child_spec(snapshot.run_id, owner=owner)
        terminal = store.get_terminal(snapshot.run_id, owner=owner) if snapshot.terminal else None
        if snapshot.terminal and terminal is None:
            raise ChildRunContractError(
                code="CHILD_RESULT_TERMINAL_MISSING",
                message="A terminal child is missing its authoritative terminal projection.",
                details={"child_run_id": snapshot.run_id},
            )

        references = (
            store.list_artifacts(snapshot.run_id, limit=artifact_limit, owner=owner)
            if artifact_limit
            else []
        )
        result_reference: ArtifactReference | None = None
        try:
            operation = store.get_operation(f"{snapshot.run_id}:model:1", owner=owner)
        except OperationNotFoundError:
            operation = None
        if (
            operation is not None
            and operation.state is OperationState.SUCCEEDED
            and operation.settlement is not None
            and operation.settlement.result_reference is not None
            and operation.settlement.result_reference.startswith("artifact:")
        ):
            result_reference = store.get_artifact(
                operation.settlement.result_reference,
                owner=owner,
            )
            if all(
                item.artifact_id != result_reference.artifact_id for item in references
            ):
                references.append(result_reference)

        for reference in references:
            if reference.scope.run_id != snapshot.run_id:
                raise ChildRunContractError(
                    code="CHILD_ARTIFACT_SCOPE_MISMATCH",
                    message="A child artifact handoff does not belong to that child run.",
                    details={"child_run_id": snapshot.run_id},
                )
        links = tuple(ChildArtifactHandoff.from_reference(item) for item in references)
        result_link = (
            ChildArtifactHandoff.from_reference(result_reference)
            if result_reference is not None
            else None
        )
        outcomes.append(
            ChildRunOutcome(
                child_run_id=snapshot.run_id,
                delegation_id=spec.delegation_id,
                purpose_code=spec.purpose_code,
                state=snapshot.state,
                result=thaw_data(terminal.value) if terminal is not None else None,
                safe_error=thaw_data(terminal.error) if terminal is not None else None,
                artifacts=links,
                result_artifact=result_link,
                parent_step_id=spec.parent_step_id,
                parent_tool_call_id=spec.parent_tool_call_id,
            )
        )
    return ChildResultSet(parent_run_id=parent_run_id, outcomes=tuple(outcomes))


__all__ = [
    "CHILD_RESULT_SCHEMA_VERSION",
    "ChildArtifactHandoff",
    "ChildResultSet",
    "ChildRunOutcome",
    "collect_child_results",
]
