"""Durable per-provider-call fencing for certified Agno lifecycle runs."""

from __future__ import annotations

import copy
import hashlib
import types
from collections.abc import AsyncIterator, Iterable
from typing import Any

from agno.models.response import ModelResponse

from .artifacts import ArtifactStore
from .checkpoints import canonical_request_value
from .errors import HarnessError
from .gateway import (
    OperationDispatchDeferredError,
    OperationGateway,
    OperationReconciliationRequiredError,
)
from .operations import EffectClass, OperationIntent, OperationKind
from .reconciliation import inspect_run_operation_safety
from .security import canonical_json_digest
from .store import RuntimeStore
from .usage import agno_model_response_settlement_evidence

PROVIDER_GATEWAY_SCHEMA_VERSION = "1.0"
PROVIDER_RESULT_SCHEMA_VERSION = "1.0"
MAX_PROVIDER_CALLS_PER_RUN = 256
_VOLATILE_MESSAGE_FIELDS = frozenset(
    {
        "checkpoint_created_at",
        "checkpoint_status",
        "created_at",
        "from_history",
        "id",
        "metrics",
    }
)
_NON_REQUEST_ARGUMENTS = frozenset({"assistant_message", "run_response"})


def _model_target(model: Any) -> str:
    provider = getattr(model, "provider", None) or model.__class__.__module__
    model_id = getattr(model, "id", None) or model.__class__.__qualname__
    value = f"{provider}:{model_id}"
    if len(value) <= 512:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"model:sha256:{digest}"


def _message_value(message: Any) -> Any:
    to_dict = getattr(message, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
    elif isinstance(message, dict):
        value = dict(message)
    else:
        return canonical_request_value(message, path="provider.messages")
    if isinstance(value, dict):
        for name in _VOLATILE_MESSAGE_FIELDS:
            value.pop(name, None)
    return canonical_request_value(value, path="provider.messages")


def _request_value(value: Any, *, path: str) -> Any:
    if isinstance(value, (list, tuple)) and value and all(
        isinstance(item, dict) or hasattr(item, "role") for item in value
    ):
        return [_message_value(item) for item in value]
    return canonical_request_value(value, path=path)


def _provider_request_value(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": PROVIDER_GATEWAY_SCHEMA_VERSION,
        "args": [
            _request_value(value, path=f"provider.args[{index}]")
            for index, value in enumerate(args)
        ],
        "kwargs": {
            key: _request_value(value, path=f"provider.{key}")
            for key, value in sorted(kwargs.items())
            if key not in _NON_REQUEST_ARGUMENTS
        },
    }


def _digest_value(value: Any) -> str:
    return canonical_json_digest(value)


def provider_request_digest(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Hash only stable provider-visible request fields, never live accumulators."""
    return _digest_value(_provider_request_value(args, kwargs))


def provider_request_component_digests(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Return content-free diagnostics for deterministic replay mismatches."""
    request = _provider_request_value(args, kwargs)
    request_kwargs = request["kwargs"]
    messages = request_kwargs.get("messages")
    tools = request_kwargs.get("tools")
    options = {
        key: value
        for key, value in request_kwargs.items()
        if key not in {"messages", "tools"}
    }
    return {
        "args": _digest_value(request["args"]),
        "messages": _digest_value(messages),
        "message_items": (
            [_digest_value(message) for message in messages]
            if isinstance(messages, list)
            else []
        ),
        "message_fields": (
            [
                {
                    str(key): _digest_value(value)
                    for key, value in sorted(message.items())
                }
                if isinstance(message, dict)
                else {}
                for message in messages
            ]
            if isinstance(messages, list)
            else []
        ),
        "options": _digest_value(options),
        "tools": _digest_value(tools),
    }


def _messages(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Iterable[Any]:
    value = kwargs.get("messages")
    if value is None and args:
        value = args[0]
    if not isinstance(value, (list, tuple)):
        raise HarnessError(
            code="PROVIDER_REQUEST_MESSAGES_REQUIRED",
            category="model",
            message="Durable provider dispatch requires an explicit bounded message list.",
            retryable=False,
        )
    return value


def provider_call_ordinal(args: tuple[Any, ...], kwargs: dict[str, Any]) -> int:
    """Derive a restart-stable call number from current-run assistant messages."""
    assistants = 0
    for message in _messages(args, kwargs):
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        from_history = (
            bool(message.get("from_history", False))
            if isinstance(message, dict)
            else bool(getattr(message, "from_history", False))
        )
        if role == "assistant" and not from_history:
            assistants += 1
    ordinal = assistants + 1
    if ordinal > MAX_PROVIDER_CALLS_PER_RUN:
        raise HarnessError(
            code="PROVIDER_CALL_LIMIT_EXCEEDED",
            category="model",
            message="The durable provider-call limit was exceeded.",
            retryable=False,
            details={"limit": MAX_PROVIDER_CALLS_PER_RUN},
        )
    return ordinal


def _provider_result_value(value: Any) -> dict[str, Any]:
    responses = value if isinstance(value, list) else [value]
    if not all(isinstance(item, ModelResponse) for item in responses):
        raise HarnessError(
            code="PROVIDER_RESPONSE_TYPE_INVALID",
            category="model",
            message="Agno provider dispatch returned an unsupported response type.",
            retryable=False,
        )
    return {
        "type": "agnoclaw.provider_result",
        "schema_version": PROVIDER_RESULT_SCHEMA_VERSION,
        "stream": isinstance(value, list),
        "responses": [
            canonical_request_value(item.to_dict(), path=f"provider.responses[{index}]")
            for index, item in enumerate(responses)
        ],
    }


def _restore_provider_result(value: Any, *, stream: bool) -> Any:
    if isinstance(value, ModelResponse):
        return [value] if stream else value
    if isinstance(value, list) and all(isinstance(item, ModelResponse) for item in value):
        return value if stream else value[0]
    if (
        not isinstance(value, dict)
        or value.get("type") != "agnoclaw.provider_result"
        or value.get("schema_version") != PROVIDER_RESULT_SCHEMA_VERSION
        or value.get("stream") is not stream
        or not isinstance(value.get("responses"), list)
        or not value["responses"]
    ):
        raise HarnessError(
            code="PROVIDER_RESULT_ARTIFACT_INVALID",
            category="model",
            message="The durable provider result artifact is invalid.",
            retryable=False,
        )
    restored = [
        ModelResponse.from_dict(copy.deepcopy(item))
        for item in value["responses"]
        if isinstance(item, dict)
    ]
    if len(restored) != len(value["responses"]):
        raise HarnessError(
            code="PROVIDER_RESULT_ARTIFACT_INVALID",
            category="model",
            message="The durable provider result artifact contains an invalid response.",
            retryable=False,
        )
    return restored if stream else restored[0]


def _assert_nested_dispatch_safe(store: RuntimeStore, run_id: str) -> None:
    inspection = inspect_run_operation_safety(store, run_id)
    if inspection.ambiguous is not None:
        raise OperationReconciliationRequiredError(inspection.ambiguous)
    if not inspection.complete:
        raise OperationDispatchDeferredError(
            run_id=run_id,
            reason_code="RUN_OPERATION_SAFETY_SCAN_INCOMPLETE",
        )


def install_agno_provider_gateway(
    model: Any,
    *,
    store: RuntimeStore,
    artifact_store: ArtifactStore,
    worker_id: str,
    run_id: str,
    harness_spec_digest: str,
    result_cache_size: int = 128,
) -> None:
    """Fence async Agno provider calls on one run-owned model instance."""
    if getattr(model, "_agnoclaw_provider_gateway", None) is not None:
        raise HarnessError(
            code="PROVIDER_GATEWAY_ALREADY_INSTALLED",
            category="model",
            message="The Agno model already has a durable provider gateway.",
            retryable=False,
        )
    original_ainvoke = getattr(model, "ainvoke", None)
    original_ainvoke_stream = getattr(model, "ainvoke_stream", None)
    if not callable(original_ainvoke) or not callable(original_ainvoke_stream):
        raise HarnessError(
            code="PROVIDER_GATEWAY_UNSUPPORTED",
            category="model",
            message="The Agno model lacks the certified async invocation contracts.",
            retryable=False,
        )
    gateway = OperationGateway(
        store,
        worker_id=worker_id,
        artifact_store=artifact_store,
        artifact_purpose="provider_model_response",
        result_serializer=_provider_result_value,
        result_cache_size=result_cache_size,
    )
    target = _model_target(model)

    def intent(args: tuple[Any, ...], kwargs: dict[str, Any]) -> OperationIntent:
        ordinal = provider_call_ordinal(args, kwargs)
        request_components = provider_request_component_digests(args, kwargs)
        return OperationIntent(
            operation_id=f"{run_id}:provider:{ordinal:06d}",
            run_id=run_id,
            attempt_id=f"{run_id}:attempt:1",
            kind=OperationKind.MODEL,
            target=target,
            request_digest=provider_request_digest(args, kwargs),
            effect_class=EffectClass.NON_REPEATABLE,
            metadata={
                "gateway_schema_version": PROVIDER_GATEWAY_SCHEMA_VERSION,
                "harness_spec_digest": harness_spec_digest,
                "operation_ordinal": ordinal,
                "request_components": request_components,
            },
        )

    async def wrapped_ainvoke(_model: Any, *args: Any, **kwargs: Any) -> ModelResponse:
        del _model
        _assert_nested_dispatch_safe(store, run_id)
        execution = await gateway.execute(
            intent(args, kwargs),
            lambda: original_ainvoke(*args, **kwargs),
            settlement_evidence=agno_model_response_settlement_evidence,
        )
        return _restore_provider_result(execution.value, stream=False)

    def wrapped_ainvoke_stream(
        _model: Any,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncIterator[ModelResponse]:
        del _model

        async def generate() -> AsyncIterator[ModelResponse]:
            _assert_nested_dispatch_safe(store, run_id)

            async def collect() -> list[ModelResponse]:
                responses: list[ModelResponse] = []
                async for response in original_ainvoke_stream(*args, **kwargs):
                    responses.append(response)
                if not responses:
                    raise HarnessError(
                        code="PROVIDER_STREAM_EMPTY",
                        category="model",
                        message="The provider stream ended without a response.",
                        retryable=False,
                    )
                return responses

            execution = await gateway.execute(
                intent(args, kwargs),
                collect,
                settlement_evidence=agno_model_response_settlement_evidence,
            )
            for response in _restore_provider_result(execution.value, stream=True):
                yield response

        return generate()

    model.ainvoke = types.MethodType(wrapped_ainvoke, model)
    model.ainvoke_stream = types.MethodType(wrapped_ainvoke_stream, model)
    model._agnoclaw_provider_gateway = {
        "schema_version": PROVIDER_GATEWAY_SCHEMA_VERSION,
        "run_id": run_id,
    }


def has_valid_tool_batch_checkpoint(run_output: Any) -> bool:
    """Recognize only a complete persisted Agno tool-result boundary."""
    if run_output is None:
        return False
    status = getattr(run_output, "status", None)
    status_value = getattr(status, "value", status)
    messages = getattr(run_output, "messages", None)
    checkpoint_index = getattr(run_output, "last_checkpoint_at_message_index", None)
    if (
        status_value != "RUNNING"
        or not isinstance(messages, list)
        or not messages
        or not isinstance(checkpoint_index, int)
        or isinstance(checkpoint_index, bool)
        or checkpoint_index != len(messages)
    ):
        return False
    last = messages[-1]
    role = last.get("role") if isinstance(last, dict) else getattr(last, "role", None)
    return role == "tool"


__all__ = [
    "MAX_PROVIDER_CALLS_PER_RUN",
    "PROVIDER_GATEWAY_SCHEMA_VERSION",
    "has_valid_tool_batch_checkpoint",
    "install_agno_provider_gateway",
    "provider_call_ordinal",
    "provider_request_component_digests",
    "provider_request_digest",
]
