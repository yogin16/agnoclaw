"""Version-pinned Agno tool adapters for governed capability specifications."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agno.tools.function import Function

from .capabilities import (
    CapabilityConcurrency,
    CapabilityKind,
    CapabilityLifetime,
    CapabilityRecovery,
    CapabilitySpec,
    CapabilityTrust,
)
from .runtime.errors import HarnessError
from .runtime.operations import EffectClass
from .runtime.security import thaw_data

CapabilityToolInvoker = Callable[[str, Mapping[str, Any]], Awaitable[Any]]

_AGNO_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_MODEL_CALLABLE_KINDS = frozenset(
    {
        CapabilityKind.TOOL,
        CapabilityKind.CONTEXT_PROVIDER,
        CapabilityKind.CHILD_RUN,
        CapabilityKind.MCP_TOOL,
        CapabilityKind.SKILL_COMMAND,
    }
)


def _validate_declared_child_binding(spec: CapabilitySpec, schema: dict[str, Any]) -> None:
    if spec.kind is not CapabilityKind.CHILD_RUN:
        return
    properties = schema.get("properties")
    required = schema.get("required")
    task = properties.get("task") if isinstance(properties, dict) else None
    delegation = properties.get("delegation_id") if isinstance(properties, dict) else None
    semantics_valid = (
        spec.effect_class is EffectClass.IDEMPOTENT
        and spec.trust is CapabilityTrust.HOST_MANAGED
        and spec.lifetime is CapabilityLifetime.RUN
        and spec.concurrency is CapabilityConcurrency.ISOLATED
        and spec.recovery is CapabilityRecovery.RECONCILABLE
        and spec.supports_idempotency_key
    )
    schema_valid = (
        isinstance(required, list)
        and {"task", "delegation_id"}.issubset(required)
        and schema.get("additionalProperties") is False
        and isinstance(task, dict)
        and task.get("type") == "string"
        and isinstance(task.get("maxLength"), int)
        and 1 <= task["maxLength"] <= 60_000
        and isinstance(delegation, dict)
        and delegation.get("type") == "string"
        and isinstance(delegation.get("maxLength"), int)
        and 1 <= delegation["maxLength"] <= 256
    )
    if not semantics_valid or not schema_valid:
        raise HarnessError(
            code="CHILD_CAPABILITY_DECLARATION_INVALID",
            category="configuration",
            message=(
                "Model-visible child capabilities must use the bounded, idempotent, "
                "host-managed declared-child contract."
            ),
            retryable=False,
            details={"capability": spec.name},
        )


def _tool_name(spec: CapabilitySpec) -> str:
    """Return a stable provider-safe name without losing collision evidence."""
    if _AGNO_TOOL_NAME.fullmatch(spec.name):
        return spec.name
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", spec.name).strip("_-") or "capability"
    if not (base[0].isalpha() or base[0] == "_"):
        base = f"capability_{base}"
    suffix = hashlib.sha256(spec.name.encode("utf-8")).hexdigest()[:10]
    return f"{base[:52]}_{suffix}"


@dataclass(frozen=True)
class AgnoCapabilityBinding:
    """One generated Agno function pinned to an immutable capability version."""

    tool_name: str
    reference: str
    spec: CapabilitySpec
    function: Function


def build_agno_capability_bindings(
    specs: Sequence[CapabilitySpec],
    *,
    invoke: CapabilityToolInvoker,
) -> tuple[AgnoCapabilityBinding, ...]:
    """Generate model-callable functions without copying execution semantics."""
    if not callable(invoke):
        raise TypeError("capability tool invoker must be callable")
    bindings: list[AgnoCapabilityBinding] = []
    names: dict[str, str] = {}
    exposed_capability_names: set[str] = set()
    for spec in specs:
        if spec.kind not in _MODEL_CALLABLE_KINDS:
            continue
        reference = f"{spec.name}@{spec.version}"
        if spec.name in exposed_capability_names:
            raise HarnessError(
                code="CAPABILITY_MODEL_VERSION_AMBIGUOUS",
                category="configuration",
                message=(
                    f"Model exposure for '{spec.name}' has multiple versions. "
                    "Expose one pinned version per harness."
                ),
                retryable=False,
                details={"capability": spec.name},
            )
        exposed_capability_names.add(spec.name)
        tool_name = _tool_name(spec)
        existing = names.get(tool_name)
        if existing is not None:
            raise HarnessError(
                code="CAPABILITY_TOOL_NAME_CONFLICT",
                category="configuration",
                message="Two capabilities map to the same model tool name.",
                retryable=False,
                details={"capability": spec.name, "tool_name": tool_name},
            )
        names[tool_name] = reference

        schema = thaw_data(spec.input_schema)
        if not isinstance(schema, dict) or schema.get("type", "object") != "object":
            raise HarnessError(
                code="CAPABILITY_INPUT_SCHEMA_INVALID",
                category="configuration",
                message=f"Capability '{spec.name}' requires an object input schema.",
                retryable=False,
                details={"capability": spec.name},
            )
        _validate_declared_child_binding(spec, schema)

        async def entrypoint(
            _reference: str = reference,
            **arguments: Any,
        ) -> Any:
            return await invoke(_reference, arguments)

        entrypoint.__name__ = tool_name
        entrypoint.__qualname__ = tool_name
        function = Function(
            name=tool_name,
            description=spec.description or f"Governed capability {reference}",
            parameters=schema,
            entrypoint=entrypoint,
            skip_entrypoint_processing=True,
        )
        bindings.append(
            AgnoCapabilityBinding(
                tool_name=tool_name,
                reference=reference,
                spec=spec,
                function=function,
            )
        )
    return tuple(bindings)


__all__ = [
    "AgnoCapabilityBinding",
    "CapabilityToolInvoker",
    "build_agno_capability_bindings",
]
