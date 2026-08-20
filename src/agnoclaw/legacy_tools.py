"""Truthful normalization for opaque `tools=` compatibility inputs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

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
from .runtime.security import freeze_data, thaw_data
from .runtime.tool_ingress import toolkit_functions

_MAX_LEGACY_TOOLS = 1_000
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_]+")


def _digest(value: Any) -> str:
    try:
        canonical = json.dumps(
            thaw_data(freeze_data(value)),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        canonical = json.dumps(
            {"opaque_type": f"{type(value).__module__}.{type(value).__qualname__}"},
            separators=(",", ":"),
            sort_keys=True,
        )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _callable_identity(value: Any) -> dict[str, str | None]:
    target = value.entrypoint if isinstance(value, Function) else value
    target_type = target.__class__
    return {
        "object_type": f"{type(value).__module__}.{type(value).__qualname__}",
        "target_type": f"{target_type.__module__}.{target_type.__qualname__}",
        "module": getattr(target, "__module__", None),
        "qualname": getattr(target, "__qualname__", None),
    }


def _normalized_name(advertised_name: str, fingerprint: str) -> str:
    base = _SAFE_NAME.sub("_", advertised_name).strip("_") or "tool"
    if not base[0].isalpha():
        base = f"tool_{base}"
    suffix = fingerprint.split(":", 1)[1][:12]
    return f"legacy.{base[:95]}.{suffix}"


def _advertised_name(value: Any) -> str | None:
    if isinstance(value, Function):
        return str(value.name) if value.name else None
    name = getattr(value, "name", None) or getattr(value, "__name__", None)
    return str(name) if name else None


@dataclass(frozen=True)
class LegacyToolBinding:
    """Opaque tool identity plus its deliberately non-durable CapabilitySpec."""

    advertised_name: str
    source: str
    container_type: str
    precedence: int
    shadowed: bool
    spec: CapabilitySpec

    def manifest(self) -> dict[str, Any]:
        return {
            "advertised_name": self.advertised_name,
            "source": self.source,
            "container_type": self.container_type,
            "precedence": self.precedence,
            "shadowed": self.shadowed,
            "spec": self.spec.manifest(),
        }


def normalize_legacy_tools(
    tools: list[Any] | tuple[Any, ...],
    *,
    source: str = "tools",
    max_tools: int = _MAX_LEGACY_TOOLS,
) -> tuple[LegacyToolBinding, ...]:
    """Describe raw tools without inferring effect, trust, or recovery guarantees."""
    if not isinstance(source, str) or not source.strip() or len(source) > 64:
        raise ValueError("legacy tool source must be a non-empty string up to 64 characters")
    if max_tools < 1 or max_tools > _MAX_LEGACY_TOOLS:
        raise ValueError(f"max_tools must be between 1 and {_MAX_LEGACY_TOOLS}")

    flattened: list[tuple[str, Any, str]] = []
    for tool in tools:
        container_type = f"{type(tool).__module__}.{type(tool).__qualname__}"
        if isinstance(tool, Toolkit):
            functions = tuple(toolkit_functions(tool).items())
            if functions:
                flattened.extend(
                    (str(name), function, container_type) for name, function in functions
                )
            else:
                # Empty toolkits can discover executable functions after harness
                # construction (notably MCP); retain the mutable container in the
                # compatibility inventory so lifecycle start cannot miss it.
                name = _advertised_name(tool)
                if name is None:
                    raise HarnessError(
                        code="LEGACY_TOOL_NAME_REQUIRED",
                        category="configuration",
                        message="Every raw toolkit needs an advertised name.",
                        retryable=False,
                        details={"source": source},
                    )
                flattened.append((name, tool, container_type))
        else:
            if not isinstance(tool, Function) and not callable(tool):
                raise HarnessError(
                    code="LEGACY_TOOL_INVALID",
                    category="configuration",
                    message="Every raw tools= entry must be callable or an Agno tool.",
                    retryable=False,
                    details={"source": source},
                )
            name = _advertised_name(tool)
            if name is None:
                raise HarnessError(
                    code="LEGACY_TOOL_NAME_REQUIRED",
                    category="configuration",
                    message="Every raw tools= entry needs an advertised tool name.",
                    retryable=False,
                    details={"source": source},
                )
            flattened.append((name, tool, container_type))
        if len(flattened) > max_tools:
            raise HarnessError(
                code="LEGACY_TOOL_BUDGET_EXCEEDED",
                category="configuration",
                message="Raw tools= expands beyond the compatibility tool budget.",
                retryable=False,
                details={"limit": max_tools, "source": source},
            )

    invalid_name = next(
        (name for name, _tool, _container in flattened if not name or len(name) > 256),
        None,
    )
    if invalid_name is not None:
        raise HarnessError(
            code="LEGACY_TOOL_NAME_INVALID",
            category="configuration",
            message="Raw tool names must contain 1 to 256 characters.",
            retryable=False,
            details={"source": source},
        )
    bindings: list[LegacyToolBinding] = []
    precedence: dict[str, int] = {}
    for advertised_name, tool, container_type in flattened:
        parameters = tool.parameters if isinstance(tool, Function) else None
        description = getattr(tool, "description", None) or getattr(
            tool,
            "__doc__",
            None,
        )
        fingerprint = _digest(
            {
                "schema": "agnoclaw-legacy-tool-v1",
                "source": source,
                "advertised_name": advertised_name,
                "callable": _callable_identity(tool),
                "parameters_digest": _digest(parameters),
                "description_digest": _digest(description or ""),
            }
        )
        spec = CapabilitySpec(
            name=_normalized_name(advertised_name, fingerprint),
            version="0+opaque",
            kind=CapabilityKind.TOOL,
            description=(
                f"Opaque compatibility tool '{advertised_name}'; use an explicit "
                "CapabilitySpec for durable execution."
            )[:1024],
            tags=("legacy", "opaque", source),
            effect_class=EffectClass.NON_REPEATABLE,
            trust=CapabilityTrust.OPAQUE_LEGACY,
            lifetime=CapabilityLifetime.PROCESS_POOL,
            concurrency=CapabilityConcurrency.SERIALIZED,
            recovery=CapabilityRecovery.LIVE_ONLY,
            implementation_digest=fingerprint,
            input_schema={"type": "object", "additionalProperties": True},
        )
        bindings.append(
            LegacyToolBinding(
                advertised_name=advertised_name,
                source=source,
                container_type=container_type,
                precedence=precedence.get(advertised_name, 0),
                shadowed=advertised_name in precedence,
                spec=spec,
            )
        )
        precedence[advertised_name] = precedence.get(advertised_name, 0) + 1
    return tuple(bindings)


def require_no_legacy_tools_for_durable(
    bindings: tuple[LegacyToolBinding, ...],
) -> None:
    """Reject opaque caller tools before creating a lifecycle run or model intent."""
    if not bindings:
        return
    names = tuple(binding.advertised_name for binding in bindings[:20])
    raise HarnessError(
        code="LEGACY_TOOL_DURABLE_UNSUPPORTED",
        category="configuration",
        message=(
            "AgentHarness.start() cannot execute opaque tools= entries. Convert them "
            "to explicit CapabilitySpec values and pass capabilities= instead."
        ),
        retryable=False,
        details={"tool_count": len(bindings), "tool_names": names},
    )


def require_no_extension_tools_for_lifecycle(
    bindings: tuple[LegacyToolBinding, ...],
) -> None:
    """Reject opaque internal extension tools before lifecycle state is created."""
    if not bindings:
        return
    raise HarnessError(
        code="EXTENSION_TOOL_LIFECYCLE_UNSUPPORTED",
        category="configuration",
        message=(
            "AgentHarness.start() cannot execute opaque extension tools. Publish "
            "CapabilitySpec values from plugins/packs or pass capabilities= instead."
        ),
        retryable=False,
        details={
            "tool_count": len(bindings),
            "tool_names": tuple(binding.advertised_name for binding in bindings[:20]),
            "sources": tuple(sorted({binding.source for binding in bindings})),
        },
    )


__all__ = [
    "LegacyToolBinding",
    "normalize_legacy_tools",
    "require_no_extension_tools_for_lifecycle",
    "require_no_legacy_tools_for_durable",
]
