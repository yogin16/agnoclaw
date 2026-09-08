"""Durable, owner-scoped Agno human-input continuation checkpoints."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .artifacts import ArtifactScope, ArtifactStore
from .checkpoints import canonical_request_value
from .errors import HarnessError
from .gateway import OperationGateway
from .lifecycle import RunSnapshot, RunState
from .operations import EffectClass, OperationIntent, OperationKind, OperationRecord, OperationState
from .security import canonical_json_digest, freeze_data, thaw_data
from .store import OperationNotFoundError, RunOwner, RuntimeStore

AGNO_REQUIREMENT_CHECKPOINT_SCHEMA_VERSION = "1.0"
AGNO_REQUIREMENT_RESPONSE_SCHEMA_VERSION = "1.0"
AGNO_REQUIREMENT_CHECKPOINT_PURPOSE = "run_agno_requirement_checkpoint"
AGNO_REQUIREMENT_RESPONSE_PURPOSE = "run_agno_requirement_response"
AGNO_REQUIREMENT_CHECKPOINT_TARGET = "agnoclaw.runtime.agno_requirement_checkpoint"
AGNO_REQUIREMENT_RESPONSE_TARGET = "agnoclaw.runtime.agno_requirement_response"
MAX_AGNO_REQUIREMENTS = 64
MAX_AGNO_REQUIREMENT_BYTES = 1_000_000
MAX_AGNO_INTERACTION_OPERATION_SCAN = 1_000
_REQUEST_ID = re.compile(r"^agno_input_(\d{6})_([0-9a-f]{40})$")
_RESERVED_RESPONSE_FIELDS = frozenset(
    {
        "confirmation",
        "confirmation_note",
        "user_input",
        "user_feedback",
        "external_execution_result",
    }
)


def _required_text(value: Any, *, field_name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} cannot exceed {maximum} characters")
    return normalized


def _require_generation(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 999_999:
        raise ValueError("generation must be between 1 and 999999")
    return value


def _require_bound(value: Any, *, field_name: str) -> Any:
    normalized = canonical_request_value(value, path=field_name)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_AGNO_REQUIREMENT_BYTES:
        raise HarnessError(
            code="RUN_REQUIREMENT_TOO_LARGE",
            category="lifecycle",
            message="The paused Agno requirement exceeds the durable continuation bound.",
            retryable=False,
            details={"field": field_name, "maximum_bytes": MAX_AGNO_REQUIREMENT_BYTES},
        )
    return normalized


def _requirement_id(requirement: Any, raw: dict[str, Any]) -> str:
    candidate = getattr(requirement, "id", None) or raw.get("id")
    if isinstance(candidate, str) and candidate.strip():
        return _required_text(candidate, field_name="requirement_id")
    tool = getattr(requirement, "tool_execution", None)
    call_id = getattr(tool, "tool_call_id", None)
    if isinstance(call_id, str) and call_id.strip():
        return _required_text(call_id, field_name="requirement_id")
    return f"requirement_{canonical_json_digest(raw).split(':', 1)[1][:40]}"


def _requirement_kinds(requirement: Any) -> tuple[str, ...]:
    checks = (
        ("confirmation", "needs_confirmation"),
        ("user_input", "needs_user_input"),
        ("user_feedback", "needs_user_feedback"),
        ("external_execution", "needs_external_execution"),
    )
    return tuple(kind for kind, attribute in checks if bool(getattr(requirement, attribute, False)))


def _schema_value(items: Any) -> tuple[Any, ...]:
    if not isinstance(items, (list, tuple)):
        return ()
    normalized: list[Any] = []
    for item in items:
        to_dict = getattr(item, "to_dict", None)
        value = to_dict() if callable(to_dict) else item
        normalized.append(_require_bound(value, field_name="requirement.schema"))
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class PendingRunRequirement:
    """Owner-visible question derived from one exact persisted Agno requirement."""

    request_id: str
    run_id: str
    generation: int
    requirement_id: str
    kinds: tuple[str, ...]
    tool_name: str | None
    tool_arguments: Any
    user_input_schema: tuple[Any, ...] = ()
    user_feedback_schema: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _required_text(self.request_id, field_name="request_id"),
        )
        object.__setattr__(self, "run_id", _required_text(self.run_id, field_name="run_id"))
        object.__setattr__(self, "generation", _require_generation(self.generation))
        object.__setattr__(
            self,
            "requirement_id",
            _required_text(self.requirement_id, field_name="requirement_id"),
        )
        allowed = {"confirmation", "user_input", "user_feedback", "external_execution"}
        normalized_kinds = tuple(str(item) for item in self.kinds)
        if not normalized_kinds or len(set(normalized_kinds)) != len(normalized_kinds):
            raise ValueError("kinds must contain unique unresolved requirement kinds")
        if any(item not in allowed for item in normalized_kinds):
            raise ValueError("kinds contains an unsupported Agno requirement kind")
        object.__setattr__(self, "kinds", normalized_kinds)
        if self.tool_name is not None:
            object.__setattr__(
                self,
                "tool_name",
                _required_text(self.tool_name, field_name="tool_name"),
            )
        object.__setattr__(
            self,
            "tool_arguments",
            freeze_data(_require_bound(self.tool_arguments, field_name="tool_arguments")),
        )
        object.__setattr__(
            self,
            "user_input_schema",
            tuple(freeze_data(item) for item in self.user_input_schema),
        )
        object.__setattr__(
            self,
            "user_feedback_schema",
            tuple(freeze_data(item) for item in self.user_feedback_schema),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "generation": self.generation,
            "requirement_id": self.requirement_id,
            "kinds": list(self.kinds),
            "tool_name": self.tool_name,
            "tool_arguments": thaw_data(self.tool_arguments),
            "user_input_schema": [thaw_data(item) for item in self.user_input_schema],
            "user_feedback_schema": [thaw_data(item) for item in self.user_feedback_schema],
        }


def _request_view(
    requirement: Any,
    raw: dict[str, Any],
    *,
    run_id: str,
    generation: int,
) -> PendingRunRequirement | None:
    kinds = _requirement_kinds(requirement)
    if not kinds:
        return None
    requirement_id = _requirement_id(requirement, raw)
    identity = canonical_json_digest(
        {
            "schema": AGNO_REQUIREMENT_CHECKPOINT_SCHEMA_VERSION,
            "run_id": run_id,
            "generation": generation,
            "requirement_id": requirement_id,
            "kinds": kinds,
        }
    ).split(":", 1)[1][:40]
    request_id = f"agno_input_{generation:06d}_{identity}"
    tool = getattr(requirement, "tool_execution", None)
    tool_name = getattr(tool, "tool_name", None)
    tool_arguments = getattr(tool, "tool_args", None)
    return PendingRunRequirement(
        request_id=request_id,
        run_id=run_id,
        generation=generation,
        requirement_id=requirement_id,
        kinds=kinds,
        tool_name=(str(tool_name) if tool_name else None),
        tool_arguments=(dict(tool_arguments) if isinstance(tool_arguments, dict) else {}),
        user_input_schema=_schema_value(getattr(requirement, "user_input_schema", None)),
        user_feedback_schema=_schema_value(
            getattr(requirement, "user_feedback_schema", None)
        ),
    )


def _raw_requirement(requirement: Any) -> dict[str, Any]:
    to_dict = getattr(requirement, "to_dict", None)
    if not callable(to_dict):
        raise HarnessError(
            code="RUN_REQUIREMENT_SERIALIZATION_UNAVAILABLE",
            category="compatibility",
            message="This Agno requirement cannot be durably serialized.",
            retryable=False,
        )
    raw = _require_bound(to_dict(), field_name="requirement")
    if not isinstance(raw, dict):
        raise HarnessError(
            code="RUN_REQUIREMENT_SERIALIZATION_INVALID",
            category="compatibility",
            message="Agno serialized a paused requirement as a non-object value.",
            retryable=False,
        )
    return raw


def _restore_requirements(raw_requirements: tuple[Any, ...]) -> list[Any]:
    try:
        from agno.run.requirement import RunRequirement
    except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover - compatibility gate
        raise HarnessError(
            code="RUN_REQUIREMENT_CONTINUATION_UNAVAILABLE",
            category="compatibility",
            message="The installed Agno version cannot restore paused requirements.",
            retryable=False,
        ) from exc
    restored: list[Any] = []
    for value in raw_requirements:
        try:
            restored.append(RunRequirement.from_dict(thaw_data(value)))
        except (TypeError, ValueError) as exc:
            raise HarnessError(
                code="RUN_REQUIREMENT_CHECKPOINT_INVALID",
                category="recovery",
                message="A persisted Agno requirement failed schema validation.",
                retryable=False,
            ) from exc
    return restored


@dataclass(frozen=True, slots=True)
class AgnoRequirementCheckpoint:
    run_id: str
    generation: int
    requirements: tuple[Any, ...]
    pending: tuple[PendingRunRequirement, ...]
    schema_version: str = AGNO_REQUIREMENT_CHECKPOINT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != AGNO_REQUIREMENT_CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported Agno requirement checkpoint schema")
        object.__setattr__(self, "run_id", _required_text(self.run_id, field_name="run_id"))
        object.__setattr__(self, "generation", _require_generation(self.generation))
        if not 1 <= len(self.requirements) <= MAX_AGNO_REQUIREMENTS:
            raise HarnessError(
                code="RUN_REQUIREMENT_COUNT_INVALID",
                category="lifecycle",
                message="A paused Agno run must contain a bounded requirement set.",
                retryable=False,
                details={"maximum": MAX_AGNO_REQUIREMENTS},
            )
        normalized = tuple(
            freeze_data(_require_bound(item, field_name="requirement"))
            for item in self.requirements
        )
        object.__setattr__(self, "requirements", normalized)
        object.__setattr__(self, "pending", tuple(self.pending))
        if not self.pending:
            raise HarnessError(
                code="RUN_REQUIREMENT_UNRESOLVED_MISSING",
                category="lifecycle",
                message="Agno paused without an unresolved requirement.",
                retryable=False,
                details={"run_id": self.run_id},
            )
        if any(
            item.run_id != self.run_id or item.generation != self.generation
            for item in self.pending
        ):
            raise ValueError("pending requirements do not match their checkpoint")
        encoded = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_AGNO_REQUIREMENT_BYTES:
            raise HarnessError(
                code="RUN_REQUIREMENT_TOO_LARGE",
                category="lifecycle",
                message="The paused Agno requirement checkpoint exceeds its size bound.",
                retryable=False,
                details={"maximum_bytes": MAX_AGNO_REQUIREMENT_BYTES},
            )

    @property
    def active(self) -> PendingRunRequirement:
        return self.pending[0]

    @property
    def digest(self) -> str:
        return canonical_json_digest(self.to_dict())

    @classmethod
    def from_requirements(
        cls,
        *,
        run_id: str,
        generation: int,
        requirements: list[Any] | tuple[Any, ...],
    ) -> AgnoRequirementCheckpoint:
        raw = tuple(_raw_requirement(item) for item in requirements)
        pending = tuple(
            view
            for requirement, value in zip(requirements, raw, strict=True)
            if (
                view := _request_view(
                    requirement,
                    value,
                    run_id=run_id,
                    generation=generation,
                )
            )
            is not None
        )
        return cls(
            run_id=run_id,
            generation=generation,
            requirements=raw,
            pending=pending,
        )

    def restore_requirements(self) -> list[Any]:
        return _restore_requirements(self.requirements)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "agnoclaw.agno_requirement_checkpoint",
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "generation": self.generation,
            "requirements": [thaw_data(item) for item in self.requirements],
            "pending": [item.to_dict() for item in self.pending],
        }

    @classmethod
    def from_dict(cls, value: Any) -> AgnoRequirementCheckpoint:
        if not isinstance(value, dict) or value.get("type") != (
            "agnoclaw.agno_requirement_checkpoint"
        ):
            raise ValueError("artifact is not an Agno requirement checkpoint")
        requirements = value.get("requirements")
        pending = value.get("pending")
        if not isinstance(requirements, list) or not isinstance(pending, list):
            raise ValueError("Agno requirement checkpoint collections are invalid")
        checkpoint = cls.from_requirements(
            run_id=_required_text(value.get("run_id"), field_name="run_id"),
            generation=_require_generation(value.get("generation")),
            requirements=_restore_requirements(tuple(requirements)),
        )
        if value.get("schema_version") != checkpoint.schema_version:
            raise ValueError("unsupported Agno requirement checkpoint schema")
        if checkpoint.to_dict()["pending"] != pending:
            raise ValueError("Agno requirement checkpoint projection mismatch")
        return checkpoint


@dataclass(frozen=True, slots=True)
class AgnoRequirementResponse:
    run_id: str
    request_id: str
    command_id: str
    checkpoint_digest: str
    payload: Any
    schema_version: str = AGNO_REQUIREMENT_RESPONSE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != AGNO_REQUIREMENT_RESPONSE_SCHEMA_VERSION:
            raise ValueError("unsupported Agno requirement response schema")
        for name in ("run_id", "request_id", "command_id"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), field_name=name),
            )
        if not isinstance(self.checkpoint_digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", self.checkpoint_digest
        ):
            raise ValueError("checkpoint_digest must be a canonical sha256 digest")
        object.__setattr__(
            self,
            "payload",
            freeze_data(
                _require_bound(
                    thaw_data(self.payload),
                    field_name="response.payload",
                )
            ),
        )

    @property
    def digest(self) -> str:
        # command_id is audit provenance, not semantic response identity. A host
        # may retry the same response with a fresh command object after a crash;
        # the request/checkpoint/payload tuple remains the at-most-once key.
        return canonical_json_digest(
            {
                "type": "agnoclaw.agno_requirement_response",
                "schema_version": self.schema_version,
                "run_id": self.run_id,
                "request_id": self.request_id,
                "checkpoint_digest": self.checkpoint_digest,
                "payload": thaw_data(self.payload),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "agnoclaw.agno_requirement_response",
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "command_id": self.command_id,
            "checkpoint_digest": self.checkpoint_digest,
            "payload": thaw_data(self.payload),
        }

    @classmethod
    def from_dict(cls, value: Any) -> AgnoRequirementResponse:
        if not isinstance(value, dict) or value.get("type") != (
            "agnoclaw.agno_requirement_response"
        ):
            raise ValueError("artifact is not an Agno requirement response")
        return cls(
            run_id=_required_text(value.get("run_id"), field_name="run_id"),
            request_id=_required_text(value.get("request_id"), field_name="request_id"),
            command_id=_required_text(value.get("command_id"), field_name="command_id"),
            checkpoint_digest=_required_text(
                value.get("checkpoint_digest"),
                field_name="checkpoint_digest",
            ),
            payload=value.get("payload"),
            schema_version=_required_text(
                value.get("schema_version"),
                field_name="schema_version",
            ),
        )


@dataclass(frozen=True, slots=True)
class ResolvedAgnoInteraction:
    checkpoint: AgnoRequirementCheckpoint
    response: AgnoRequirementResponse
    requirements: tuple[Any, ...]


def agno_result_requirements(result: Any) -> tuple[Any, ...] | None:
    """Return unresolved requirements only when an Agno output is genuinely paused."""
    status = getattr(result, "status", None)
    status_value = str(getattr(status, "value", status) or "").upper()
    if status_value != "PAUSED":
        return None
    requirements = getattr(result, "requirements", None)
    if not isinstance(requirements, (list, tuple)):
        raise HarnessError(
            code="RUN_REQUIREMENT_UNAVAILABLE",
            category="compatibility",
            message="Agno paused without exposing serializable requirements.",
            retryable=False,
        )
    unresolved = tuple(
        item
        for item in requirements
        if callable(getattr(item, "is_resolved", None)) and not item.is_resolved()
    )
    if not unresolved:
        raise HarnessError(
            code="RUN_REQUIREMENT_UNRESOLVED_MISSING",
            category="compatibility",
            message="Agno reported PAUSED without an unresolved requirement.",
            retryable=False,
        )
    return tuple(requirements)


def validate_agno_requirement_checkpoint_result(
    result: Any,
    checkpoint: AgnoRequirementCheckpoint,
) -> None:
    """Prove that Agno's persisted pause is the one the host answered."""
    requirements = agno_result_requirements(result)
    run_id = getattr(result, "run_id", None)
    if requirements is None or run_id != checkpoint.run_id:
        raise HarnessError(
            code="RUN_REQUIREMENT_AGNO_CHECKPOINT_MISMATCH",
            category="recovery",
            message="Agno's persisted paused run does not match the durable checkpoint.",
            retryable=False,
            details={"run_id": checkpoint.run_id},
        )
    raw = tuple(freeze_data(_raw_requirement(item)) for item in requirements)
    if raw != checkpoint.requirements:
        raise HarnessError(
            code="RUN_REQUIREMENT_AGNO_CHECKPOINT_MISMATCH",
            category="recovery",
            message="Agno's persisted requirements differ from the answered checkpoint.",
            retryable=False,
            details={"run_id": checkpoint.run_id},
        )


def requirement_checkpoint_operation_id(run_id: str, generation: int) -> str:
    return f"{run_id}:checkpoint:agno-input:{_require_generation(generation):06d}"


def requirement_response_operation_id(run_id: str, request_id: str) -> str:
    suffix = canonical_json_digest(
        {"run_id": run_id, "request_id": request_id}
    ).split(":", 1)[1][:40]
    return f"{run_id}:response:agno-input:{suffix}"


async def persist_agno_requirement_checkpoint(
    checkpoint: AgnoRequirementCheckpoint,
    *,
    store: RuntimeStore,
    artifact_store: ArtifactStore | None,
    worker_id: str,
) -> None:
    if artifact_store is None:
        raise HarnessError(
            code="RUN_REQUIREMENT_ARTIFACT_STORE_REQUIRED",
            category="configuration",
            message="Durable Agno input continuation requires an ArtifactStore.",
            retryable=False,
            details={"run_id": checkpoint.run_id},
        )
    gateway = OperationGateway(
        store,
        worker_id=worker_id,
        artifact_store=artifact_store,
        artifact_purpose=AGNO_REQUIREMENT_CHECKPOINT_PURPOSE,
        result_cache_size=0,
    )
    await gateway.execute(
        OperationIntent(
            operation_id=requirement_checkpoint_operation_id(
                checkpoint.run_id, checkpoint.generation
            ),
            run_id=checkpoint.run_id,
            attempt_id=(
                f"{checkpoint.run_id}:checkpoint:agno-input:{checkpoint.generation:06d}"
            ),
            kind=OperationKind.CAPABILITY,
            target=AGNO_REQUIREMENT_CHECKPOINT_TARGET,
            request_digest=checkpoint.digest,
            effect_class=EffectClass.READ_ONLY,
            metadata={
                "schema_version": checkpoint.schema_version,
                "generation": checkpoint.generation,
                "request_id": checkpoint.active.request_id,
            },
        ),
        checkpoint.to_dict,
    )


async def persist_agno_requirement_response(
    response: AgnoRequirementResponse,
    *,
    store: RuntimeStore,
    artifact_store: ArtifactStore | None,
    worker_id: str,
) -> None:
    if artifact_store is None:
        raise HarnessError(
            code="RUN_REQUIREMENT_ARTIFACT_STORE_REQUIRED",
            category="configuration",
            message="Durable Agno input continuation requires an ArtifactStore.",
            retryable=False,
            details={"run_id": response.run_id},
        )
    gateway = OperationGateway(
        store,
        worker_id=worker_id,
        artifact_store=artifact_store,
        artifact_purpose=AGNO_REQUIREMENT_RESPONSE_PURPOSE,
        result_cache_size=0,
    )
    await gateway.execute(
        OperationIntent(
            operation_id=requirement_response_operation_id(
                response.run_id, response.request_id
            ),
            run_id=response.run_id,
            attempt_id=f"{response.run_id}:response:{response.request_id}",
            kind=OperationKind.CAPABILITY,
            target=AGNO_REQUIREMENT_RESPONSE_TARGET,
            request_digest=response.digest,
            effect_class=EffectClass.READ_ONLY,
            metadata={
                "schema_version": response.schema_version,
                "request_id": response.request_id,
                "checkpoint_digest": response.checkpoint_digest,
            },
        ),
        response.to_dict,
    )


async def _load_operation_artifact(
    operation: OperationRecord,
    *,
    snapshot: RunSnapshot,
    owner: RunOwner,
    store: RuntimeStore,
    artifact_store: ArtifactStore | None,
    purpose: str,
) -> Any:
    reference_id = (
        operation.settlement.result_reference
        if operation.state is OperationState.SUCCEEDED and operation.settlement is not None
        else None
    )
    if artifact_store is None or reference_id is None:
        raise HarnessError(
            code="RUN_REQUIREMENT_CHECKPOINT_UNAVAILABLE",
            category="recovery",
            message="The Agno input continuation artifact is not durably readable.",
            retryable=False,
            details={"run_id": snapshot.run_id},
        )
    reference = store.get_artifact(reference_id, owner=owner)
    metadata = thaw_data(reference.metadata)
    if (
        reference.scope
        != ArtifactScope(
            run_id=snapshot.run_id,
            tenant_id=snapshot.tenant_id,
            user_id=snapshot.user_id,
        )
        or reference.purpose != purpose
        or not isinstance(metadata, dict)
        or metadata.get("operation_id") != operation.intent.operation_id
        or metadata.get("attempt_id") != operation.intent.attempt_id
        or metadata.get("kind") != operation.intent.kind.value
    ):
        raise HarnessError(
            code="RUN_REQUIREMENT_CHECKPOINT_SCOPE_MISMATCH",
            category="authorization",
            message="The Agno input continuation artifact is not bound to this run.",
            retryable=False,
            details={"run_id": snapshot.run_id},
        )
    return await artifact_store.load_json(reference)


def _checkpoint_generation_from_request(request_id: str) -> int:
    match = _REQUEST_ID.fullmatch(request_id)
    if match is None:
        raise HarnessError(
            code="RUN_RESPONSE_REQUEST_MISMATCH",
            category="lifecycle",
            message="The response does not identify an Agno input checkpoint.",
            retryable=False,
            details={"request_id": request_id},
        )
    return int(match.group(1))


async def load_agno_requirement_checkpoint(
    *,
    store: RuntimeStore,
    artifact_store: ArtifactStore | None,
    snapshot: RunSnapshot,
    owner: RunOwner,
    request_id: str,
) -> AgnoRequirementCheckpoint:
    generation = _checkpoint_generation_from_request(request_id)
    try:
        operation = store.get_operation(
            requirement_checkpoint_operation_id(snapshot.run_id, generation),
            owner=owner,
        )
    except OperationNotFoundError as exc:
        raise HarnessError(
            code="RUN_REQUIREMENT_CHECKPOINT_UNAVAILABLE",
            category="recovery",
            message="This run has no durable Agno input checkpoint for the request.",
            retryable=False,
            details={"run_id": snapshot.run_id, "request_id": request_id},
        ) from exc
    metadata = thaw_data(operation.intent.metadata)
    if (
        operation.intent.run_id != snapshot.run_id
        or operation.intent.kind is not OperationKind.CAPABILITY
        or operation.intent.target != AGNO_REQUIREMENT_CHECKPOINT_TARGET
        or operation.intent.effect_class is not EffectClass.READ_ONLY
        or not isinstance(metadata, dict)
        or metadata
        != {
            "schema_version": AGNO_REQUIREMENT_CHECKPOINT_SCHEMA_VERSION,
            "generation": generation,
            "request_id": request_id,
        }
    ):
        raise HarnessError(
            code="RUN_REQUIREMENT_CHECKPOINT_INVALID",
            category="recovery",
            message="The Agno input checkpoint operation failed validation.",
            retryable=False,
            details={"run_id": snapshot.run_id},
        )
    try:
        raw = await _load_operation_artifact(
            operation,
            snapshot=snapshot,
            owner=owner,
            store=store,
            artifact_store=artifact_store,
            purpose=AGNO_REQUIREMENT_CHECKPOINT_PURPOSE,
        )
        checkpoint = AgnoRequirementCheckpoint.from_dict(raw)
    except (TypeError, ValueError) as exc:
        raise HarnessError(
            code="RUN_REQUIREMENT_CHECKPOINT_INVALID",
            category="recovery",
            message="The Agno input checkpoint artifact failed schema validation.",
            retryable=False,
            details={"run_id": snapshot.run_id},
        ) from exc
    if (
        checkpoint.run_id != snapshot.run_id
        or checkpoint.generation != generation
        or checkpoint.active.request_id != request_id
        or checkpoint.digest != operation.intent.request_digest
    ):
        raise HarnessError(
            code="RUN_REQUIREMENT_CHECKPOINT_INVALID",
            category="recovery",
            message="The Agno input checkpoint does not match its operation intent.",
            retryable=False,
            details={"run_id": snapshot.run_id},
        )
    return checkpoint


def _normalize_response_payload(
    requirement: Any,
    pending: PendingRunRequirement,
    payload: Any,
) -> dict[str, Any]:
    value = thaw_data(payload)
    if isinstance(value, bool) and pending.kinds == ("confirmation",):
        return {"confirmation": value}
    if isinstance(value, str) and pending.kinds == ("external_execution",):
        return {"external_execution_result": value}
    if isinstance(value, dict) and pending.kinds == ("user_input",) and not (
        set(value) & _RESERVED_RESPONSE_FIELDS
    ):
        return {"user_input": value}
    if not isinstance(value, dict):
        raise HarnessError(
            code="RUN_RESPONSE_PAYLOAD_INVALID",
            category="validation",
            message="The Agno input response has an invalid payload shape.",
            retryable=False,
            details={"request_id": pending.request_id, "expected_kinds": pending.kinds},
        )
    unknown = set(value) - _RESERVED_RESPONSE_FIELDS
    if unknown:
        raise HarnessError(
            code="RUN_RESPONSE_PAYLOAD_INVALID",
            category="validation",
            message="The Agno input response contains unsupported fields.",
            retryable=False,
            details={"request_id": pending.request_id, "unknown_fields": sorted(unknown)},
        )
    return value


def resolve_agno_requirements(
    checkpoint: AgnoRequirementCheckpoint,
    *,
    request_id: str,
    payload: Any,
) -> tuple[Any, ...]:
    pending_by_id = {item.request_id: item for item in checkpoint.pending}
    pending = pending_by_id.get(request_id)
    if pending is None or checkpoint.active.request_id != request_id:
        raise HarnessError(
            code="RUN_RESPONSE_REQUEST_MISMATCH",
            category="lifecycle",
            message="The response does not match the run's active Agno request.",
            retryable=False,
            details={"request_id": request_id},
        )
    requirements = checkpoint.restore_requirements()
    match = next(
        (
            item
            for item, raw in zip(requirements, checkpoint.requirements, strict=True)
            if _requirement_id(item, thaw_data(raw)) == pending.requirement_id
        ),
        None,
    )
    if match is None:
        raise HarnessError(
            code="RUN_REQUIREMENT_CHECKPOINT_INVALID",
            category="recovery",
            message="The active Agno requirement is absent from its checkpoint.",
            retryable=False,
            details={"request_id": request_id},
        )
    normalized = _normalize_response_payload(match, pending, payload)
    expected_fields = {
        "confirmation": "confirmation",
        "user_input": "user_input",
        "user_feedback": "user_feedback",
        "external_execution": "external_execution_result",
    }
    missing = [kind for kind in pending.kinds if expected_fields[kind] not in normalized]
    if missing:
        raise HarnessError(
            code="RUN_RESPONSE_PAYLOAD_INCOMPLETE",
            category="validation",
            message="The Agno input response does not resolve every requested field.",
            retryable=False,
            details={"request_id": request_id, "missing_kinds": missing},
        )
    try:
        if "confirmation" in pending.kinds:
            confirmation = normalized.get("confirmation")
            if not isinstance(confirmation, bool):
                raise TypeError("confirmation must be a boolean")
            if confirmation:
                match.confirm()
            else:
                note = normalized.get("confirmation_note")
                if note is not None and not isinstance(note, str):
                    raise TypeError("confirmation_note must be a string")
                match.reject(note=note)
        if "user_input" in pending.kinds:
            values = normalized.get("user_input")
            if not isinstance(values, dict):
                raise TypeError("user_input must be an object")
            match.provide_user_input(values)
        if "user_feedback" in pending.kinds:
            selections = normalized.get("user_feedback")
            if not isinstance(selections, dict) or any(
                not isinstance(key, str)
                or not isinstance(items, list)
                or any(not isinstance(item, str) for item in items)
                for key, items in selections.items()
            ):
                raise TypeError("user_feedback must map questions to string lists")
            match.provide_user_feedback(selections)
        if "external_execution" in pending.kinds:
            external = normalized.get("external_execution_result")
            if not isinstance(external, str):
                raise TypeError("external_execution_result must be a string")
            match.set_external_execution_result(external)
    except (TypeError, ValueError) as exc:
        raise HarnessError(
            code="RUN_RESPONSE_PAYLOAD_INVALID",
            category="validation",
            message="Agno rejected the input response for this requirement.",
            retryable=False,
            details={"request_id": request_id},
        ) from exc
    if not match.is_resolved():
        raise HarnessError(
            code="RUN_RESPONSE_PAYLOAD_INCOMPLETE",
            category="validation",
            message="The Agno input response left the active requirement unresolved.",
            retryable=False,
            details={"request_id": request_id},
        )
    return tuple(requirements)


async def load_agno_requirement_response(
    *,
    store: RuntimeStore,
    artifact_store: ArtifactStore | None,
    snapshot: RunSnapshot,
    owner: RunOwner,
    checkpoint: AgnoRequirementCheckpoint,
) -> ResolvedAgnoInteraction:
    request_id = checkpoint.active.request_id
    try:
        operation = store.get_operation(
            requirement_response_operation_id(snapshot.run_id, request_id),
            owner=owner,
        )
    except OperationNotFoundError as exc:
        raise HarnessError(
            code="RUN_REQUIREMENT_RESPONSE_UNAVAILABLE",
            category="recovery",
            message="The resumed run has no durable response for its Agno request.",
            retryable=False,
            details={"run_id": snapshot.run_id, "request_id": request_id},
        ) from exc
    metadata = thaw_data(operation.intent.metadata)
    if (
        operation.intent.run_id != snapshot.run_id
        or operation.intent.kind is not OperationKind.CAPABILITY
        or operation.intent.target != AGNO_REQUIREMENT_RESPONSE_TARGET
        or operation.intent.effect_class is not EffectClass.READ_ONLY
        or not isinstance(metadata, dict)
        or metadata
        != {
            "schema_version": AGNO_REQUIREMENT_RESPONSE_SCHEMA_VERSION,
            "request_id": request_id,
            "checkpoint_digest": checkpoint.digest,
        }
    ):
        raise HarnessError(
            code="RUN_REQUIREMENT_RESPONSE_INVALID",
            category="recovery",
            message="The Agno input response operation failed validation.",
            retryable=False,
            details={"run_id": snapshot.run_id},
        )
    try:
        raw = await _load_operation_artifact(
            operation,
            snapshot=snapshot,
            owner=owner,
            store=store,
            artifact_store=artifact_store,
            purpose=AGNO_REQUIREMENT_RESPONSE_PURPOSE,
        )
        response = AgnoRequirementResponse.from_dict(raw)
    except (TypeError, ValueError) as exc:
        raise HarnessError(
            code="RUN_REQUIREMENT_RESPONSE_INVALID",
            category="recovery",
            message="The Agno input response artifact failed schema validation.",
            retryable=False,
            details={"run_id": snapshot.run_id},
        ) from exc
    if (
        response.run_id != snapshot.run_id
        or response.request_id != request_id
        or response.checkpoint_digest != checkpoint.digest
        or response.digest != operation.intent.request_digest
    ):
        raise HarnessError(
            code="RUN_REQUIREMENT_RESPONSE_INVALID",
            category="recovery",
            message="The Agno input response does not match its operation intent.",
            retryable=False,
            details={"run_id": snapshot.run_id},
        )
    requirements = resolve_agno_requirements(
        checkpoint,
        request_id=request_id,
        payload=response.payload,
    )
    return ResolvedAgnoInteraction(
        checkpoint=checkpoint,
        response=response,
        requirements=requirements,
    )


async def load_latest_resolved_agno_interaction(
    *,
    store: RuntimeStore,
    artifact_store: ArtifactStore | None,
    snapshot: RunSnapshot,
    owner: RunOwner,
) -> ResolvedAgnoInteraction | None:
    records = store.list_run_operations(
        snapshot.run_id,
        limit=MAX_AGNO_INTERACTION_OPERATION_SCAN,
        owner=owner,
    )
    if len(records) == MAX_AGNO_INTERACTION_OPERATION_SCAN:
        raise HarnessError(
            code="RUN_REQUIREMENT_OPERATION_SCAN_INCOMPLETE",
            category="recovery",
            message="The bounded operation scan cannot prove the latest Agno response.",
            retryable=False,
            details={
                "run_id": snapshot.run_id,
                "maximum_operations": MAX_AGNO_INTERACTION_OPERATION_SCAN,
            },
        )
    generations = [
        int(metadata["generation"])
        for record in records
        if record.intent.target == AGNO_REQUIREMENT_CHECKPOINT_TARGET
        and isinstance((metadata := thaw_data(record.intent.metadata)), dict)
        and isinstance(metadata.get("generation"), int)
    ]
    if not generations:
        return None
    generation = max(generations)
    checkpoint_record = next(
        record
        for record in records
        if record.intent.operation_id
        == requirement_checkpoint_operation_id(snapshot.run_id, generation)
    )
    metadata = thaw_data(checkpoint_record.intent.metadata)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("request_id"), str):
        raise HarnessError(
            code="RUN_REQUIREMENT_CHECKPOINT_INVALID",
            category="recovery",
            message="The latest Agno input checkpoint has invalid metadata.",
            retryable=False,
            details={"run_id": snapshot.run_id},
        )
    checkpoint = await load_agno_requirement_checkpoint(
        store=store,
        artifact_store=artifact_store,
        snapshot=snapshot,
        owner=owner,
        request_id=metadata["request_id"],
    )
    return await load_agno_requirement_response(
        store=store,
        artifact_store=artifact_store,
        snapshot=snapshot,
        owner=owner,
        checkpoint=checkpoint,
    )


async def pending_agno_requirements(
    *,
    store: RuntimeStore,
    artifact_store: ArtifactStore | None,
    snapshot: RunSnapshot,
    owner: RunOwner,
) -> tuple[PendingRunRequirement, ...]:
    if snapshot.state is not RunState.WAITING_FOR_INPUT or snapshot.pending_request_id is None:
        return ()
    checkpoint = await load_agno_requirement_checkpoint(
        store=store,
        artifact_store=artifact_store,
        snapshot=snapshot,
        owner=owner,
        request_id=snapshot.pending_request_id,
    )
    return checkpoint.pending


__all__ = [
    "AGNO_REQUIREMENT_CHECKPOINT_SCHEMA_VERSION",
    "AGNO_REQUIREMENT_RESPONSE_SCHEMA_VERSION",
    "AgnoRequirementCheckpoint",
    "AgnoRequirementResponse",
    "PendingRunRequirement",
    "ResolvedAgnoInteraction",
    "agno_result_requirements",
    "load_agno_requirement_checkpoint",
    "load_agno_requirement_response",
    "load_latest_resolved_agno_interaction",
    "pending_agno_requirements",
    "persist_agno_requirement_checkpoint",
    "persist_agno_requirement_response",
    "requirement_checkpoint_operation_id",
    "requirement_response_operation_id",
    "resolve_agno_requirements",
    "validate_agno_requirement_checkpoint_result",
]
