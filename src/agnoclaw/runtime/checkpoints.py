"""Content-addressed pre-model checkpoints for truthful run continuation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from .artifacts import ArtifactScope, ArtifactStore
from .context import ExecutionContext
from .errors import HarnessError
from .gateway import OperationGateway
from .lifecycle import RunSnapshot
from .operations import (
    EffectClass,
    OperationIntent,
    OperationKind,
    OperationRecord,
    OperationState,
)
from .security import AdmissionEnvelope, IdentitySource, freeze_data, thaw_data
from .store import OperationNotFoundError, RunOwner, RuntimeStore

RUNTIME_REQUEST_CHECKPOINT_SCHEMA_VERSION = "1.0"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def canonical_request_value(value: Any, *, path: str = "request") -> Any:
    """Normalize lifecycle request data into finite deterministic JSON."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HarnessError(
                code="RUN_REQUEST_NOT_CANONICAL",
                category="validation",
                message=f"{path} contains a non-finite number.",
                retryable=False,
                details={"parameter": path},
            )
        return value
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise HarnessError(
                    code="RUN_REQUEST_NOT_CANONICAL",
                    category="validation",
                    message=f"{path} contains a non-string object key.",
                    retryable=False,
                    details={"parameter": path},
                )
            normalized[key] = canonical_request_value(item, path=f"{path}.{key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            canonical_request_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, type):
        return {"$type": f"{value.__module__}.{value.__qualname__}"}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return canonical_request_value(model_dump(mode="json"), path=path)
    raise HarnessError(
        code="RUN_REQUEST_NOT_CANONICAL",
        category="validation",
        message=(
            f"{path} contains an opaque {value.__class__.__name__}; supply a "
            "stable descriptor before using lifecycle idempotency."
        ),
        retryable=False,
        details={"parameter": path},
    )


def execution_context_value(context: ExecutionContext) -> dict[str, Any]:
    if not isinstance(context, ExecutionContext):
        raise TypeError("context must be an ExecutionContext")
    return canonical_request_value(
        {
            "user_id": context.user_id,
            "session_id": context.session_id,
            "workspace_id": context.workspace_id,
            "tenant_id": context.tenant_id,
            "org_id": context.org_id,
            "team_id": context.team_id,
            "roles": list(context.roles),
            "scopes": list(context.scopes),
            "request_id": context.request_id,
            "trace_id": context.trace_id,
            "trusted_permission_tools": list(context.trusted_permission_tools),
            "trusted_permission_categories": list(context.trusted_permission_categories),
            "metadata": context.metadata,
            "identity_source": context.identity_source.value,
            "admission": context.admission.to_dict() if context.admission else None,
        },
        path="context",
    )


def _optional_text(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string or null")
    return value


def _required_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _text_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"{field_name} must be a sequence of strings")
    return tuple(value)


def execution_context_from_value(value: Any) -> ExecutionContext:
    if not isinstance(value, dict):
        raise TypeError("checkpoint context must be an object")
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        raise TypeError("checkpoint context metadata must be an object")
    raw_admission = value.get("admission")
    if raw_admission is not None and not isinstance(raw_admission, dict):
        raise TypeError("checkpoint admission must be an object or null")
    return ExecutionContext.create(
        user_id=_optional_text(value.get("user_id"), field_name="user_id"),
        session_id=_optional_text(value.get("session_id"), field_name="session_id"),
        workspace_id=_optional_text(value.get("workspace_id"), field_name="workspace_id"),
        tenant_id=_optional_text(value.get("tenant_id"), field_name="tenant_id"),
        org_id=_optional_text(value.get("org_id"), field_name="org_id"),
        team_id=_optional_text(value.get("team_id"), field_name="team_id"),
        roles=_text_tuple(value.get("roles"), field_name="roles"),
        scopes=_text_tuple(value.get("scopes"), field_name="scopes"),
        request_id=_optional_text(value.get("request_id"), field_name="request_id"),
        trace_id=_optional_text(value.get("trace_id"), field_name="trace_id"),
        trusted_permission_tools=_text_tuple(
            value.get("trusted_permission_tools"),
            field_name="trusted_permission_tools",
        ),
        trusted_permission_categories=_text_tuple(
            value.get("trusted_permission_categories"),
            field_name="trusted_permission_categories",
        ),
        metadata=metadata,
        identity_source=IdentitySource(str(value.get("identity_source"))),
        admission=(
            AdmissionEnvelope.from_dict(raw_admission) if raw_admission is not None else None
        ),
    )


def runtime_request_digest(
    *,
    message: str,
    context: ExecutionContext,
    kwargs: dict[str, Any],
    harness_spec_digest: str,
) -> str:
    canonical = json.dumps(
        canonical_request_value(
            {
                "message": message,
                "context": execution_context_value(context),
                "harness_spec": harness_spec_digest,
                "kwargs": kwargs,
            }
        ),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def persisted_result_value(result: Any) -> dict[str, Any]:
    """Normalize terminal model content without retaining an opaque provider object."""
    content = getattr(result, "content", result)
    try:
        normalized = canonical_request_value(content, path="result.content")
    except HarnessError:
        normalized = {
            "$unavailable": True,
            "$type": f"{content.__class__.__module__}.{content.__class__.__qualname__}",
        }
    return {"content": normalized}


@dataclass(frozen=True, slots=True)
class RuntimeRequestCheckpoint:
    run_id: str
    request_digest: str
    harness_spec_digest: str
    message: str
    context: Any
    kwargs: Any
    schema_version: str = RUNTIME_REQUEST_CHECKPOINT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RUNTIME_REQUEST_CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported runtime request checkpoint schema")
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if not isinstance(self.message, str):
            raise TypeError("message must be a string")
        for field_name in ("request_digest", "harness_spec_digest"):
            if not _DIGEST_RE.fullmatch(getattr(self, field_name)):
                raise ValueError(f"{field_name} must be a canonical sha256 digest")
        normalized_context = canonical_request_value(self.context, path="context")
        normalized_kwargs = canonical_request_value(self.kwargs, path="kwargs")
        if not isinstance(normalized_context, dict):
            raise TypeError("context must be an object")
        if not isinstance(normalized_kwargs, dict):
            raise TypeError("kwargs must be an object")
        object.__setattr__(self, "context", freeze_data(normalized_context))
        object.__setattr__(self, "kwargs", freeze_data(normalized_kwargs))

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        message: str,
        context: ExecutionContext,
        kwargs: dict[str, Any],
        harness_spec_digest: str,
    ) -> RuntimeRequestCheckpoint:
        return cls(
            run_id=run_id,
            request_digest=runtime_request_digest(
                message=message,
                context=context,
                kwargs=kwargs,
                harness_spec_digest=harness_spec_digest,
            ),
            harness_spec_digest=harness_spec_digest,
            message=message,
            context=execution_context_value(context),
            kwargs=kwargs,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "agnoclaw.runtime_request_checkpoint",
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "request_digest": self.request_digest,
            "harness_spec_digest": self.harness_spec_digest,
            "message": self.message,
            "context": thaw_data(self.context),
            "kwargs": thaw_data(self.kwargs),
        }

    @classmethod
    def from_dict(cls, value: Any) -> RuntimeRequestCheckpoint:
        if not isinstance(value, dict) or value.get("type") != (
            "agnoclaw.runtime_request_checkpoint"
        ):
            raise ValueError("artifact is not a runtime request checkpoint")
        return cls(
            run_id=_required_text(value.get("run_id"), field_name="run_id"),
            request_digest=_required_text(value.get("request_digest"), field_name="request_digest"),
            harness_spec_digest=_required_text(
                value.get("harness_spec_digest"),
                field_name="harness_spec_digest",
            ),
            message=_required_text(value.get("message"), field_name="message"),
            context=value.get("context"),
            kwargs=value.get("kwargs"),
            schema_version=_required_text(value.get("schema_version"), field_name="schema_version"),
        )

    def restore(
        self,
        *,
        snapshot: RunSnapshot,
        harness_spec_digest: str,
    ) -> RecoveredRuntimeRequest:
        if self.run_id != snapshot.run_id:
            raise HarnessError(
                code="RUN_RECOVERY_CHECKPOINT_INVALID",
                category="recovery",
                message="The checkpoint belongs to another run.",
                retryable=False,
                details={"run_id": snapshot.run_id},
            )
        if self.harness_spec_digest != harness_spec_digest:
            raise HarnessError(
                code="RUN_RECOVERY_SPEC_MISMATCH",
                category="recovery",
                message="The current harness specification differs from the checkpoint.",
                retryable=False,
                details={"run_id": snapshot.run_id},
            )
        try:
            context = execution_context_from_value(thaw_data(self.context))
            kwargs = thaw_data(self.kwargs)
            actual_digest = runtime_request_digest(
                message=self.message,
                context=context,
                kwargs=kwargs,
                harness_spec_digest=harness_spec_digest,
            )
        except (TypeError, ValueError, HarnessError) as exc:
            raise HarnessError(
                code="RUN_RECOVERY_CHECKPOINT_INVALID",
                category="recovery",
                message="The request checkpoint failed schema validation.",
                retryable=False,
                details={"run_id": snapshot.run_id},
            ) from exc
        if actual_digest != self.request_digest:
            raise HarnessError(
                code="RUN_RECOVERY_REQUEST_MISMATCH",
                category="recovery",
                message="The checkpoint content does not match its frozen request digest.",
                retryable=False,
                details={"run_id": snapshot.run_id},
            )
        if (
            context.tenant_id != snapshot.tenant_id
            or context.user_id != snapshot.user_id
            or context.session_id != snapshot.session_id
        ):
            raise HarnessError(
                code="RUN_RECOVERY_CHECKPOINT_SCOPE_MISMATCH",
                category="authorization",
                message="The checkpoint authority differs from the durable run owner.",
                retryable=False,
                details={"run_id": snapshot.run_id},
            )
        return RecoveredRuntimeRequest(
            message=self.message,
            context=context,
            kwargs=kwargs,
        )


@dataclass(frozen=True, slots=True)
class RecoveredRuntimeRequest:
    message: str
    context: ExecutionContext
    kwargs: dict[str, Any]


def request_checkpoint_operation_id(run_id: str) -> str:
    return f"{run_id}:checkpoint:request:1"


def validate_recoverable_model_intent(
    operation: OperationRecord,
    *,
    run_id: str,
    model_target: str,
    request_digest: str,
    harness_spec_digest: str,
    timeout_seconds: float | None = None,
    effect_class: EffectClass = EffectClass.NON_REPEATABLE,
    orchestration_mode: str | None = None,
) -> None:
    """Require an exact recoverable model intent before continuation."""
    expected_metadata = {
        "harness_spec_digest": harness_spec_digest,
        "operation_ordinal": 1,
    }
    if orchestration_mode is not None:
        expected_metadata["orchestration_mode"] = orchestration_mode
    intent = operation.intent
    if (
        operation.state is not OperationState.PLANNED
        or intent.operation_id != f"{run_id}:model:1"
        or intent.run_id != run_id
        or intent.attempt_id != f"{run_id}:attempt:1"
        or intent.kind is not OperationKind.MODEL
        or intent.target != model_target
        or intent.request_digest != request_digest
        or intent.effect_class is not effect_class
        or intent.idempotency_key is not None
        or intent.timeout_seconds != timeout_seconds
        or thaw_data(intent.metadata) != expected_metadata
    ):
        raise HarnessError(
            code="RUN_RECOVERY_MODEL_INTENT_MISMATCH",
            category="recovery",
            message="The planned model operation does not match the certified request.",
            retryable=False,
            details={"run_id": run_id},
        )


async def persist_runtime_request_checkpoint(
    checkpoint: RuntimeRequestCheckpoint,
    *,
    store: RuntimeStore,
    artifact_store: ArtifactStore | None,
    worker_id: str,
) -> None:
    """Settle the exact request before model dispatch when durable bytes exist."""
    if artifact_store is None:
        return
    gateway = OperationGateway(
        store,
        worker_id=worker_id,
        artifact_store=artifact_store,
        artifact_purpose="run_request_checkpoint",
        result_cache_size=0,
    )
    await gateway.execute(
        OperationIntent(
            operation_id=request_checkpoint_operation_id(checkpoint.run_id),
            run_id=checkpoint.run_id,
            attempt_id=f"{checkpoint.run_id}:checkpoint:request:1",
            kind=OperationKind.CAPABILITY,
            target="agnoclaw.runtime.request_checkpoint",
            request_digest=checkpoint.request_digest,
            effect_class=EffectClass.READ_ONLY,
            metadata={
                "schema_version": checkpoint.schema_version,
                "harness_spec_digest": checkpoint.harness_spec_digest,
            },
        ),
        checkpoint.to_dict,
    )


async def load_runtime_request_checkpoint(
    *,
    store: RuntimeStore,
    artifact_store: ArtifactStore | None,
    snapshot: RunSnapshot,
    owner: RunOwner,
    harness_spec_digest: str,
) -> RecoveredRuntimeRequest:
    try:
        operation = store.get_operation(
            request_checkpoint_operation_id(snapshot.run_id),
            owner=owner,
        )
    except OperationNotFoundError as exc:
        raise HarnessError(
            code="RUN_RECOVERY_CHECKPOINT_UNAVAILABLE",
            category="recovery",
            message="This run has no settled pre-model request checkpoint.",
            retryable=False,
            details={"run_id": snapshot.run_id},
        ) from exc
    reference_id = (
        operation.settlement.result_reference
        if operation.state is OperationState.SUCCEEDED and operation.settlement
        else None
    )
    if artifact_store is None or reference_id is None:
        raise HarnessError(
            code="RUN_RECOVERY_CHECKPOINT_UNAVAILABLE",
            category="recovery",
            message="The pre-model request checkpoint is not durably readable.",
            retryable=False,
            details={"run_id": snapshot.run_id},
        )
    intent = operation.intent
    intent_metadata = thaw_data(intent.metadata)
    if (
        intent.operation_id != request_checkpoint_operation_id(snapshot.run_id)
        or intent.run_id != snapshot.run_id
        or intent.attempt_id != f"{snapshot.run_id}:checkpoint:request:1"
        or intent.kind is not OperationKind.CAPABILITY
        or intent.target != "agnoclaw.runtime.request_checkpoint"
        or intent.effect_class is not EffectClass.READ_ONLY
        or intent.idempotency_key is not None
        or intent.timeout_seconds is not None
        or not isinstance(intent_metadata, dict)
        or set(intent_metadata) != {"schema_version", "harness_spec_digest"}
        or intent_metadata.get("schema_version")
        != RUNTIME_REQUEST_CHECKPOINT_SCHEMA_VERSION
        or not isinstance(intent_metadata.get("harness_spec_digest"), str)
        or not _DIGEST_RE.fullmatch(intent_metadata["harness_spec_digest"])
    ):
        raise HarnessError(
            code="RUN_RECOVERY_CHECKPOINT_INVALID",
            category="recovery",
            message="The request checkpoint operation has invalid recovery evidence.",
            retryable=False,
            details={"run_id": snapshot.run_id},
        )
    reference = store.get_artifact(reference_id, owner=owner)
    expected_scope = ArtifactScope(
        run_id=snapshot.run_id,
        tenant_id=snapshot.tenant_id,
        user_id=snapshot.user_id,
    )
    metadata = thaw_data(reference.metadata)
    if (
        reference.scope != expected_scope
        or reference.purpose != "run_request_checkpoint"
        or not isinstance(metadata, dict)
        or metadata.get("operation_id") != operation.intent.operation_id
        or metadata.get("attempt_id") != operation.intent.attempt_id
        or metadata.get("kind") != operation.intent.kind.value
    ):
        raise HarnessError(
            code="RUN_RECOVERY_CHECKPOINT_SCOPE_MISMATCH",
            category="authorization",
            message="The request checkpoint artifact is not bound to this run.",
            retryable=False,
            details={"run_id": snapshot.run_id},
        )
    try:
        raw = await artifact_store.load_json(reference)
        checkpoint = RuntimeRequestCheckpoint.from_dict(raw)
    except (TypeError, ValueError) as exc:
        raise HarnessError(
            code="RUN_RECOVERY_CHECKPOINT_INVALID",
            category="recovery",
            message="The request checkpoint artifact has an invalid schema.",
            retryable=False,
            details={"run_id": snapshot.run_id},
        ) from exc
    if (
        checkpoint.request_digest != intent.request_digest
        or checkpoint.harness_spec_digest != intent_metadata["harness_spec_digest"]
    ):
        raise HarnessError(
            code="RUN_RECOVERY_REQUEST_MISMATCH",
            category="recovery",
            message="The request checkpoint differs from its operation intent.",
            retryable=False,
            details={"run_id": snapshot.run_id},
        )
    return checkpoint.restore(
        snapshot=snapshot,
        harness_spec_digest=harness_spec_digest,
    )


__all__ = [
    "RUNTIME_REQUEST_CHECKPOINT_SCHEMA_VERSION",
    "RecoveredRuntimeRequest",
    "RuntimeRequestCheckpoint",
    "canonical_request_value",
    "execution_context_from_value",
    "execution_context_value",
    "load_runtime_request_checkpoint",
    "persist_runtime_request_checkpoint",
    "persisted_result_value",
    "request_checkpoint_operation_id",
    "runtime_request_digest",
    "validate_recoverable_model_intent",
]
