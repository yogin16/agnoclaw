"""Typed work products and public capability binding for host-owned persistence."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import BaseModel

from agnoclaw.capabilities import CapabilityLifetime, CapabilitySpec
from agnoclaw.runtime.context import ExecutionContext
from agnoclaw.runtime.security import canonical_json_digest, thaw_data

from ._contracts import Record, digest_string, fail, frozen_json, identifier
from .evidence import EvidenceRef
from .objects import ObjectRef


@dataclass(frozen=True)
class WorkProduct(Record):
    target: ObjectRef
    run_id: str
    output_name: str
    output_version: str
    schema_digest: str
    content: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        for name in ("run_id", "output_name", "output_version"):
            identifier(getattr(self, name), name)
        digest_string(self.schema_digest)
        object.__setattr__(self, "content", frozen_json(self.content))
        object.__setattr__(self, "metadata", frozen_json(self.metadata))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if any(item.source.tenant_id != self.target.tenant_id for item in self.evidence):
            fail("SCOPE_MISMATCH", "Work product evidence belongs to another tenant.")


@dataclass(frozen=True)
class OutputSpec:
    name: str
    version: str
    content_model: type[BaseModel]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    _schema: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        identifier(self.name, "name")
        identifier(self.version, "version")
        if not isinstance(self.content_model, type) or not issubclass(
            self.content_model, BaseModel
        ):
            raise TypeError("content_model must be a Pydantic model class")
        if self.content_model.model_config.get("extra") != "forbid":
            raise ValueError("output models must declare ConfigDict(extra='forbid')")
        object.__setattr__(self, "metadata", frozen_json(self.metadata))
        schema = frozen_json(self.content_model.model_json_schema())
        if canonical_json_digest(schema) != canonical_json_digest(
            self.content_model.model_json_schema(mode="serialization")
        ):
            raise ValueError("output models must use matching input and serialization schemas")
        object.__setattr__(self, "_schema", schema)

    @property
    def reference(self) -> str:
        return f"{self.name}@{self.version}"

    @property
    def schema_digest(self) -> str:
        return canonical_json_digest(self._schema)

    @property
    def digest(self) -> str:
        return canonical_json_digest([self.reference, self.schema_digest, self.metadata])

    def json_schema(self) -> dict[str, Any]:
        return thaw_data(self._schema)

    def validate_content(self, content: Mapping[str, Any]) -> dict[str, Any]:
        """Validate detached JSON for hosts that already own output identity/storage."""
        schemas = (
            self.content_model.model_json_schema(),
            self.content_model.model_json_schema(mode="serialization"),
        )
        if any(canonical_json_digest(schema) != self.schema_digest for schema in schemas):
            fail("SCHEMA_DRIFT", "Output schema changed after registration.")
        payload = thaw_data(frozen_json(content))
        validated = self.content_model.model_validate_json(json.dumps(payload), strict=True)
        return thaw_data(frozen_json(validated.model_dump(mode="json", by_alias=True)))

    def build(
        self,
        content: Mapping[str, Any],
        *,
        target: ObjectRef,
        run_id: str,
        context: ExecutionContext,
        evidence: Sequence[EvidenceRef] = (),
    ) -> WorkProduct:
        target.require_scope(context)
        validated = self.validate_content(content)
        return WorkProduct(
            target,
            run_id,
            self.name,
            self.version,
            self.schema_digest,
            validated,
            self.metadata,
            tuple(evidence),
        )


def _nested_schema(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "#/properties/content" + item[1:]
                if key == "$ref" and isinstance(item, str) and item.startswith("#")
                else _nested_schema(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_nested_schema(item) for item in value]
    return value


def bind_output_schema(base_schema: Mapping[str, Any], output: OutputSpec) -> dict[str, Any]:
    """Narrow an existing tool's content argument without inspecting a live agent.

    Returns a detached schema, preserving other arguments and root definitions.
    Nested output references remain under content, avoiding definition collisions.
    This only advertises a contract: hosts must also validate at their save boundary.
    """
    base = thaw_data(frozen_json(base_schema))
    if base.get("type") != "object" or not isinstance(base.get("properties"), dict):
        raise ValueError("output tool schema must be an object with properties")
    if "content" not in base["properties"]:
        raise ValueError("output tool schema must already declare a content argument")
    base["properties"]["content"] = _nested_schema(output.json_schema())
    required = base.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise ValueError("output tool schema required must be a list of argument names")
    base["required"] = list(dict.fromkeys([*required, "content"]))
    return base


class _OutputWriter:
    def __init__(self, writer: Any, output: OutputSpec, bound: dict[str, Any]):
        if not callable(writer):
            raise TypeError("output capability factory must return a callable work-product writer")
        self.writer, self.output, self.bound = writer, output, bound

    def __call__(self, content: dict[str, Any]) -> Any:
        # Public tool context supplies the lifecycle ID that start() allocates.
        from agnoclaw.agent import get_current_tool_runtime

        bound = dict(self.bound)
        runtime = get_current_tool_runtime()
        if runtime is not None:
            active_context = runtime.get("context")
            active_run_id = runtime.get("parent_run_id")
            if not isinstance(active_context, ExecutionContext) or not isinstance(
                active_run_id, str
            ):
                fail(
                    "RUN_CONTEXT_REQUIRED", "Output binding requires an authoritative tool context."
                )
            expected = bound["context"]
            if bound["run_id"] is not None and bound["run_id"] != active_run_id:
                fail("RUN_CONTEXT_MISMATCH", "Output binding belongs to another run.")
            if expected is not None and any(
                getattr(expected, key) != getattr(active_context, key)
                for key in ("tenant_id", "user_id", "session_id")
            ):
                fail(
                    "RUN_CONTEXT_MISMATCH", "Output binding belongs to another execution identity."
                )
            bound.update(context=active_context, run_id=active_run_id)
        if bound["context"] is None or bound["run_id"] is None:
            fail("RUN_CONTEXT_REQUIRED", "Output binding requires an active governed tool call.")
        product = self.output.build(content, **bound)
        # Preserve executor-owned sync offloading, cancellation tracking and async awaiting.
        return self.writer(product)

    def close(self) -> Any:
        close = getattr(self.writer, "aclose", None) or getattr(self.writer, "close", None)
        if close is not None:
            return close()
        return None


def bind_output_capability(
    capability: CapabilitySpec,
    output: OutputSpec,
    *,
    target: ObjectRef,
    run_id: str | None = None,
    context: ExecutionContext | None = None,
    evidence: Sequence[EvidenceRef] = (),
) -> CapabilitySpec:
    """Expose only content to the model; bind identity/schema/provenance in host code.

    The supplied capability keeps its explicit effect, concurrency and recovery contract.
    Its run-owned factory must return a writer accepting one WorkProduct, committing
    before returning, with idempotency/reconciliation as declared by the host.
    """
    if (run_id is None) != (context is None):
        raise ValueError("explicit output binding requires both run_id and context")
    if context is not None:
        target.require_scope(context)
    if run_id is not None:
        identifier(run_id, "run_id")
    if capability.lifetime is not CapabilityLifetime.RUN or capability.factory is None:
        raise ValueError("output writers require an explicit run-owned capability factory")
    bound = dict(target=target, run_id=run_id, context=context, evidence=tuple(evidence))
    schema = bind_output_schema(
        {
            "type": "object",
            "properties": {"content": {}},
            "required": ["content"],
            "additionalProperties": False,
        },
        output,
    )
    return replace(
        capability,
        input_schema=schema,
        implementation_digest=canonical_json_digest(
            [
                capability.digest,
                output.digest,
                target.to_dict(),
                run_id,
                [context.tenant_id, context.user_id, context.session_id]
                if context
                else "active-tool",
                [item.digest for item in evidence],
            ]
        ),
        factory=lambda: _OutputWriter(capability.materialize(), output, bound),
    )
