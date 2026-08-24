"""Bounded declared child-run lineage and authority contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .context import ExecutionContext
from .errors import HarnessError
from .security import IdentitySource, freeze_data, thaw_data

if TYPE_CHECKING:
    from ..agent import AgentHarness
    from .lifecycle import RunSnapshot
    from .run_handle import HarnessRun

CHILD_RUN_SCHEMA_VERSION = "1.1"
_SUPPORTED_CHILD_RUN_SCHEMA_VERSIONS = frozenset({"1.0", CHILD_RUN_SCHEMA_VERSION})
MAX_CHILD_DEPTH = 16
MAX_CHILD_FANOUT = 64
MAX_CHILD_TIMEOUT_SECONDS = 86_400
MAX_CHILD_TOKENS = 10_000_000
MAX_CHILD_COST_MICROUSD = 10_000_000_000
MAX_CHILD_CAPABILITIES = 128
_TRACE_METADATA_KEY = "_agnoclaw_trace"
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}")


class ChildJoinPolicy(StrEnum):
    """How a parent interprets one or more terminal child outcomes."""

    ALL_SUCCESS = "all_success"
    COLLECT = "collect"


class ChildCancellationPolicy(StrEnum):
    """Cancellation relationship supported by the first child contract."""

    PROPAGATE = "propagate"


class ChildRunContractError(HarnessError):
    """A child declaration tried to violate lineage or bounded authority."""

    def __init__(self, *, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            code=code,
            category="child_run",
            message=message,
            retryable=False,
            details=details,
        )


def _bounded_id(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value.strip()) is None:
        raise ValueError(f"{field_name} must be a safe non-empty identifier of at most 256 chars")
    return value.strip()


def _positive_int(value: int, *, field_name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{field_name} must be between 1 and {maximum}")
    return value


@dataclass(frozen=True)
class ChildRunBudget:
    """Finite tree and resource grant inherited monotonically by descendants."""

    max_depth: int = 4
    max_fanout: int = 8
    timeout_seconds: int = 600
    max_tokens: int = 100_000
    max_cost_microusd: int = 5_000_000

    def __post_init__(self) -> None:
        _positive_int(self.max_depth, field_name="max_depth", maximum=MAX_CHILD_DEPTH)
        _positive_int(self.max_fanout, field_name="max_fanout", maximum=MAX_CHILD_FANOUT)
        _positive_int(
            self.timeout_seconds,
            field_name="timeout_seconds",
            maximum=MAX_CHILD_TIMEOUT_SECONDS,
        )
        _positive_int(self.max_tokens, field_name="max_tokens", maximum=MAX_CHILD_TOKENS)
        _positive_int(
            self.max_cost_microusd,
            field_name="max_cost_microusd",
            maximum=MAX_CHILD_COST_MICROUSD,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_depth": self.max_depth,
            "max_fanout": self.max_fanout,
            "timeout_seconds": self.timeout_seconds,
            "max_tokens": self.max_tokens,
            "max_cost_microusd": self.max_cost_microusd,
        }


@dataclass(frozen=True)
class ChildRunSpec:
    """Immutable, owner-inherited declaration for one logical child run."""

    child_run_id: str
    parent_run_id: str
    root_run_id: str
    depth: int
    delegation_id: str
    purpose_code: str
    budget: ChildRunBudget = field(default_factory=ChildRunBudget)
    capability_allowlist: tuple[str, ...] = ()
    join_policy: ChildJoinPolicy = ChildJoinPolicy.ALL_SUCCESS
    cancellation_policy: ChildCancellationPolicy = ChildCancellationPolicy.PROPAGATE
    learning_allowed: bool = False
    result_schema: Any = None
    parent_step_id: str | None = None
    parent_tool_call_id: str | None = None
    schema_version: str = CHILD_RUN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in _SUPPORTED_CHILD_RUN_SCHEMA_VERSIONS:
            raise ValueError("unsupported child-run schema version")
        if self.schema_version == "1.0" and self.result_schema is not None:
            raise ValueError("child-run schema 1.0 cannot contain a result schema")
        for name in ("child_run_id", "parent_run_id", "root_run_id", "delegation_id"):
            object.__setattr__(self, name, _bounded_id(getattr(self, name), field_name=name))
        object.__setattr__(
            self,
            "purpose_code",
            _bounded_id(self.purpose_code, field_name="purpose_code"),
        )
        _positive_int(self.depth, field_name="depth", maximum=MAX_CHILD_DEPTH)
        if self.child_run_id in {self.parent_run_id, self.root_run_id}:
            raise ValueError("a child run must have a distinct run identity")
        if self.parent_step_id is not None:
            object.__setattr__(
                self,
                "parent_step_id",
                _bounded_id(self.parent_step_id, field_name="parent_step_id"),
            )
        if self.parent_tool_call_id is not None:
            object.__setattr__(
                self,
                "parent_tool_call_id",
                _bounded_id(self.parent_tool_call_id, field_name="parent_tool_call_id"),
            )
        capabilities = tuple(
            _bounded_id(item, field_name="capability_allowlist")
            for item in self.capability_allowlist
        )
        if len(capabilities) > MAX_CHILD_CAPABILITIES:
            raise ValueError(f"capability_allowlist cannot exceed {MAX_CHILD_CAPABILITIES}")
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("capability_allowlist must not contain duplicates")
        object.__setattr__(self, "capability_allowlist", capabilities)
        object.__setattr__(self, "join_policy", ChildJoinPolicy(self.join_policy))
        object.__setattr__(
            self,
            "cancellation_policy",
            ChildCancellationPolicy(self.cancellation_policy),
        )
        if not isinstance(self.learning_allowed, bool):
            raise ValueError("learning_allowed must be a boolean")
        if self.result_schema is not None:
            schema = thaw_data(freeze_data(self.result_schema))
            try:
                from ..capability_schema import validate_capability_input_schema

                validate_capability_input_schema(
                    schema,
                    capability=f"child-output:{self.purpose_code}",
                )
            except HarnessError as exc:
                details = {
                    key: value
                    for key, value in (exc.details or {}).items()
                    if key in {"keyword", "schema_path"}
                }
                raise ChildRunContractError(
                    code="CHILD_OUTPUT_SCHEMA_INVALID",
                    message="The declared child output schema is invalid or unbounded.",
                    details=details,
                ) from None
            object.__setattr__(self, "result_schema", freeze_data(schema))

    @classmethod
    def for_parent(
        cls,
        parent: RunSnapshot,
        *,
        delegation_id: str,
        purpose_code: str,
        budget: ChildRunBudget | None = None,
        capability_allowlist: tuple[str, ...] = (),
        join_policy: ChildJoinPolicy = ChildJoinPolicy.ALL_SUCCESS,
        learning_allowed: bool = False,
        result_schema: Any = None,
        parent_step_id: str | None = None,
        parent_tool_call_id: str | None = None,
    ) -> ChildRunSpec:
        if parent.terminal:
            raise ChildRunContractError(
                code="CHILD_PARENT_TERMINAL",
                message="A terminal run cannot dispatch a new child.",
                details={"parent_run_id": parent.run_id, "state": parent.state.value},
            )
        if parent.state.value != "running":
            raise ChildRunContractError(
                code="CHILD_PARENT_NOT_RUNNING",
                message="A child can only dispatch from an actively running parent.",
                details={"parent_run_id": parent.run_id, "state": parent.state.value},
            )
        root_run_id = parent.root_run_id or parent.run_id
        bounded_delegation = _bounded_id(delegation_id, field_name="delegation_id")
        identity = hashlib.sha256(
            f"{parent.run_id}\x00{bounded_delegation}".encode()
        ).hexdigest()[:32]
        return cls(
            child_run_id=f"run_child_{identity}",
            parent_run_id=parent.run_id,
            root_run_id=root_run_id,
            depth=parent.child_depth + 1,
            delegation_id=bounded_delegation,
            purpose_code=purpose_code,
            budget=budget or ChildRunBudget(),
            capability_allowlist=capability_allowlist,
            join_policy=join_policy,
            learning_allowed=learning_allowed,
            result_schema=result_schema,
            parent_step_id=parent_step_id,
            parent_tool_call_id=parent_tool_call_id,
        )

    @property
    def digest(self) -> str:
        canonical = json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)
        return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"

    @property
    def result_schema_digest(self) -> str | None:
        if self.result_schema is None:
            return None
        canonical = json.dumps(
            thaw_data(self.result_schema),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "child_run_id": self.child_run_id,
            "parent_run_id": self.parent_run_id,
            "root_run_id": self.root_run_id,
            "depth": self.depth,
            "delegation_id": self.delegation_id,
            "purpose_code": self.purpose_code,
            "budget": self.budget.to_dict(),
            "capability_allowlist": list(self.capability_allowlist),
            "join_policy": self.join_policy.value,
            "cancellation_policy": self.cancellation_policy.value,
            "learning_allowed": self.learning_allowed,
            "parent_step_id": self.parent_step_id,
            "parent_tool_call_id": self.parent_tool_call_id,
        }
        if self.schema_version != "1.0":
            value["result_schema"] = thaw_data(self.result_schema)
        return value

    @classmethod
    def from_dict(cls, value: Any) -> ChildRunSpec:
        data = thaw_data(freeze_data(value))
        if not isinstance(data, dict):
            raise ValueError("child-run specification must be an object")
        schema_version = data.get("schema_version")
        allowed = {
            "schema_version",
            "child_run_id",
            "parent_run_id",
            "root_run_id",
            "depth",
            "delegation_id",
            "purpose_code",
            "budget",
            "capability_allowlist",
            "join_policy",
            "cancellation_policy",
            "learning_allowed",
            "parent_step_id",
            "parent_tool_call_id",
        }
        if schema_version == CHILD_RUN_SCHEMA_VERSION:
            allowed.add("result_schema")
        if set(data) != allowed:
            raise ValueError("child-run specification fields are invalid")
        budget = data["budget"]
        if not isinstance(budget, dict):
            raise ValueError("child-run budget must be an object")
        capabilities = data["capability_allowlist"]
        if not isinstance(capabilities, list):
            raise ValueError("child-run capability_allowlist must be an array")
        scalar = {
            key: item
            for key, item in data.items()
            if key not in {"budget", "capability_allowlist"}
        }
        return cls(
            **scalar,
            budget=ChildRunBudget(**budget),
            capability_allowlist=tuple(capabilities),
        )

    def validate_parent(
        self,
        parent: RunSnapshot,
        *,
        child_owner: tuple[str | None, str | None],
        direct_children: int,
        parent_spec: ChildRunSpec | None,
    ) -> None:
        if parent.terminal:
            raise ChildRunContractError(
                code="CHILD_PARENT_TERMINAL",
                message="A terminal run cannot dispatch a new child.",
                details={"parent_run_id": parent.run_id, "state": parent.state.value},
            )
        if parent.state.value != "running":
            raise ChildRunContractError(
                code="CHILD_PARENT_NOT_RUNNING",
                message="A child can only dispatch from an actively running parent.",
                details={"parent_run_id": parent.run_id, "state": parent.state.value},
            )
        if parent.run_id != self.parent_run_id:
            raise ChildRunContractError(
                code="CHILD_PARENT_MISMATCH",
                message="The declared parent does not match the authoritative parent run.",
            )
        expected_root = parent.root_run_id or parent.run_id
        if self.root_run_id != expected_root or self.depth != parent.child_depth + 1:
            raise ChildRunContractError(
                code="CHILD_LINEAGE_INVALID",
                message="Child root/depth lineage does not extend the parent by one level.",
            )
        if child_owner != (parent.tenant_id, parent.user_id):
            raise ChildRunContractError(
                code="CHILD_OWNER_ESCALATION",
                message="A child must inherit the parent's exact tenant and user owner.",
            )
        governing = parent_spec.budget if parent_spec is not None else self.budget
        if self.depth > governing.max_depth:
            raise ChildRunContractError(
                code="CHILD_DEPTH_LIMIT",
                message="The child would exceed its inherited delegation depth.",
                details={"depth": self.depth, "max_depth": governing.max_depth},
            )
        if direct_children >= governing.max_fanout:
            raise ChildRunContractError(
                code="CHILD_FANOUT_LIMIT",
                message="The parent has exhausted its bounded child fan-out.",
                details={"max_fanout": governing.max_fanout},
            )
        if parent_spec is None:
            return
        parent_budget = parent_spec.budget
        if any(
            child > parent_value
            for child, parent_value in (
                (self.budget.max_depth, parent_budget.max_depth),
                (self.budget.max_fanout, parent_budget.max_fanout),
                (self.budget.timeout_seconds, parent_budget.timeout_seconds),
                (self.budget.max_tokens, parent_budget.max_tokens),
                (self.budget.max_cost_microusd, parent_budget.max_cost_microusd),
            )
        ):
            raise ChildRunContractError(
                code="CHILD_BUDGET_ESCALATION",
                message="A descendant cannot increase an inherited child budget.",
            )
        if not set(self.capability_allowlist).issubset(parent_spec.capability_allowlist):
            raise ChildRunContractError(
                code="CHILD_CAPABILITY_ESCALATION",
                message="A descendant capability grant must be a parent-grant subset.",
            )
        if self.learning_allowed and not parent_spec.learning_allowed:
            raise ChildRunContractError(
                code="CHILD_LEARNING_ESCALATION",
                message="A descendant cannot enable learning denied by its parent grant.",
            )


def trace_payload_from_context(context: ExecutionContext) -> dict[str, Any]:
    metadata = getattr(context, "metadata", None) or {}
    trace = metadata.get(_TRACE_METADATA_KEY)
    if not isinstance(trace, dict):
        return {}
    return {
        key: trace[key]
        for key in (
            "parent_run_id",
            "parent_tool_call_id",
            "parent_step_id",
            "parent_tool_name",
            "subagent_depth",
            "subagent_root_run_id",
        )
        if trace.get(key) is not None
    }


def build_subagent_execution_context(
    runtime: dict[str, Any] | None,
    *,
    workspace_id: str | None,
) -> ExecutionContext | None:
    """Compatibility trace builder until the legacy tool becomes a declared child."""
    if not runtime or not isinstance(runtime.get("context"), ExecutionContext):
        return None
    parent_context = runtime["context"]
    metadata = dict(parent_context.metadata)
    existing = metadata.get(_TRACE_METADATA_KEY)
    existing = existing if isinstance(existing, dict) else {}
    root = (
        existing.get("subagent_root_run_id")
        or existing.get("parent_run_id")
        or runtime.get("parent_run_id")
    )
    trace = {
        "parent_run_id": runtime.get("parent_run_id"),
        "parent_tool_call_id": runtime.get("parent_tool_call_id"),
        "parent_step_id": runtime.get("parent_step_id"),
        "parent_tool_name": runtime.get("parent_tool_name"),
        "subagent_depth": int(existing.get("subagent_depth") or 0) + 1,
        "subagent_root_run_id": root,
    }
    metadata[_TRACE_METADATA_KEY] = {key: item for key, item in trace.items() if item is not None}
    return ExecutionContext.create(
        user_id=parent_context.user_id,
        session_id=None,
        workspace_id=workspace_id or parent_context.workspace_id,
        tenant_id=parent_context.tenant_id,
        org_id=parent_context.org_id,
        team_id=parent_context.team_id,
        roles=parent_context.roles,
        scopes=parent_context.scopes,
        request_id=parent_context.request_id,
        trace_id=parent_context.trace_id,
        trusted_permission_tools=parent_context.trusted_permission_tools,
        trusted_permission_categories=parent_context.trusted_permission_categories,
        metadata=metadata,
        identity_source=IdentitySource.INTERNAL_PARENT,
    )


def child_execution_context(
    parent_context: ExecutionContext,
    spec: ChildRunSpec,
    *,
    workspace_id: str | None = None,
) -> ExecutionContext:
    """Derive a non-escalating internal context with an isolated child session."""
    metadata = dict(parent_context.metadata)
    metadata[_TRACE_METADATA_KEY] = {
        "parent_run_id": spec.parent_run_id,
        "parent_tool_call_id": spec.parent_tool_call_id,
        "parent_step_id": spec.parent_step_id,
        "subagent_depth": spec.depth,
        "subagent_root_run_id": spec.root_run_id,
    }
    return ExecutionContext.create(
        user_id=parent_context.user_id,
        session_id=f"child:{spec.child_run_id}",
        workspace_id=workspace_id or parent_context.workspace_id,
        tenant_id=parent_context.tenant_id,
        org_id=parent_context.org_id,
        team_id=parent_context.team_id,
        roles=parent_context.roles,
        scopes=parent_context.scopes,
        request_id=parent_context.request_id,
        trace_id=parent_context.trace_id,
        trusted_permission_tools=parent_context.trusted_permission_tools,
        trusted_permission_categories=parent_context.trusted_permission_categories,
        metadata=metadata,
        identity_source=IdentitySource.INTERNAL_PARENT,
    )


def _validate_child_harness(spec: ChildRunSpec, child_harness: AgentHarness) -> None:
    if bool(getattr(child_harness, "_has_non_capability_tools", True)):
        raise ChildRunContractError(
            code="CHILD_UNDECLARED_TOOLS",
            message=(
                "Declared children currently require capability-only tool surfaces; "
                "raw/default tools remain compatibility-only."
            ),
        )
    registry = getattr(child_harness, "_capability_registry", None)
    snapshot = getattr(registry, "snapshot", None)
    if not callable(snapshot):
        raise ChildRunContractError(
            code="CHILD_CAPABILITY_MANIFEST_REQUIRED",
            message="A declared child harness must expose its immutable capability manifest.",
        )
    configured = {f"{item.name}@{item.version}" for item in snapshot()}
    granted = set(spec.capability_allowlist)
    if not configured.issubset(granted):
        raise ChildRunContractError(
            code="CHILD_CAPABILITY_GRANT_MISMATCH",
            message="The child harness contains a capability outside its declared grant.",
            details={"ungranted_count": len(configured - granted)},
        )


async def start_declared_child(
    parent: HarnessRun,
    child_harness: AgentHarness,
    message: str,
    *,
    context: ExecutionContext,
    delegation_id: str,
    purpose_code: str,
    budget: ChildRunBudget | None = None,
    capability_allowlist: tuple[str, ...] = (),
    join_policy: ChildJoinPolicy = ChildJoinPolicy.ALL_SUCCESS,
    learning_allowed: bool = False,
    result_schema: Any = None,
    parent_step_id: str | None = None,
    parent_tool_call_id: str | None = None,
    persist_output: bool = False,
) -> HarnessRun:
    """Create one declared child through the child's ordinary lifecycle worker."""
    parent_snapshot = await parent.status()
    if (context.tenant_id, context.user_id) != (
        parent_snapshot.tenant_id,
        parent_snapshot.user_id,
    ):
        raise ChildRunContractError(
            code="CHILD_CONTEXT_OWNER_MISMATCH",
            message="Child dispatch context must match the authorized parent owner.",
        )
    spec = ChildRunSpec.for_parent(
        parent_snapshot,
        delegation_id=delegation_id,
        purpose_code=purpose_code,
        budget=budget,
        capability_allowlist=capability_allowlist,
        join_policy=join_policy,
        learning_allowed=learning_allowed,
        result_schema=result_schema,
        parent_step_id=parent_step_id,
        parent_tool_call_id=parent_tool_call_id,
    )
    _validate_child_harness(spec, child_harness)
    return await child_harness.start(
        message,
        idempotency_key=f"child:{spec.parent_run_id}:{spec.delegation_id}",
        context=child_execution_context(context, spec),
        learning_consent=learning_allowed,
        persist_output=persist_output,
        _child_spec=spec,
    )


__all__ = [
    "CHILD_RUN_SCHEMA_VERSION",
    "ChildCancellationPolicy",
    "ChildJoinPolicy",
    "ChildRunBudget",
    "ChildRunContractError",
    "ChildRunSpec",
    "build_subagent_execution_context",
    "child_execution_context",
    "start_declared_child",
    "trace_payload_from_context",
]
