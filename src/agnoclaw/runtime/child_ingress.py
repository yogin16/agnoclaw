"""Host-declared child templates shared by model and remote ingress."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..capabilities import (
    CapabilityConcurrency,
    CapabilityKind,
    CapabilityLifetime,
    CapabilityRecovery,
    CapabilitySpec,
    CapabilityTrust,
)
from ..capability_schema import validate_capability_input_schema
from .child_results import (
    MAX_SYNTHESIS_INLINE_CHARS,
    MIN_SYNTHESIS_INLINE_CHARS,
    ChildResultSet,
)
from .children import ChildJoinPolicy, ChildRunBudget, ChildRunContractError
from .context import ExecutionContext
from .errors import HarnessError
from .operations import EffectClass
from .run_handle import HarnessRun, RunWaitError
from .security import freeze_data, thaw_data

if TYPE_CHECKING:
    from ..agent import AgentHarness

MAX_DECLARED_CHILD_MESSAGE_CHARS = 60_000


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True)
class DeclaredChildTemplate:
    """One host-owned delegation policy usable as a model tool or remote route.

    The template fixes every authority-bearing choice. A caller may supply only the
    task and a stable delegation identity; it cannot select a model, tools, budget,
    learning policy, result contract, or child harness at invocation time.
    """

    name: str
    child_harness: Any = field(compare=False, repr=False)
    purpose_code: str
    description: str = "Delegate one bounded task to a declared child agent."
    version: str = "1.0.0"
    budget: ChildRunBudget = field(default_factory=ChildRunBudget)
    capability_allowlist: tuple[str, ...] = ()
    join_policy: ChildJoinPolicy = ChildJoinPolicy.ALL_SUCCESS
    learning_allowed: bool = False
    result_schema: Any = None
    persist_output: bool = False
    required_scopes: tuple[str, ...] = ()
    max_message_chars: int = MAX_DECLARED_CHILD_MESSAGE_CHARS
    max_inline_result_chars: int = 8_000

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("declared child template name must be non-empty")
        if not isinstance(self.purpose_code, str) or not self.purpose_code.strip():
            raise ValueError("declared child purpose_code must be non-empty")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("declared child description must be non-empty")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("declared child template version must be non-empty")
        if not isinstance(self.budget, ChildRunBudget):
            raise TypeError("declared child budget must be a ChildRunBudget")
        if self.child_harness is None or not callable(getattr(self.child_harness, "start", None)):
            raise TypeError("declared child template requires an AgentHarness-like child_harness")
        if not isinstance(self.learning_allowed, bool) or not isinstance(
            self.persist_output, bool
        ):
            raise TypeError("declared child learning and persistence flags must be booleans")
        if (
            isinstance(self.max_message_chars, bool)
            or not 1 <= self.max_message_chars <= MAX_DECLARED_CHILD_MESSAGE_CHARS
        ):
            raise ValueError(
                f"max_message_chars must be between 1 and {MAX_DECLARED_CHILD_MESSAGE_CHARS}"
            )
        if (
            isinstance(self.max_inline_result_chars, bool)
            or not MIN_SYNTHESIS_INLINE_CHARS
            <= self.max_inline_result_chars
            <= MAX_SYNTHESIS_INLINE_CHARS
        ):
            raise ValueError(
                "max_inline_result_chars must be between "
                f"{MIN_SYNTHESIS_INLINE_CHARS} and {MAX_SYNTHESIS_INLINE_CHARS}"
            )
        capabilities = tuple(self.capability_allowlist)
        scopes = tuple(sorted(set(self.required_scopes)))
        if any(not isinstance(item, str) or not item.strip() for item in capabilities):
            raise ValueError("capability_allowlist must contain non-empty strings")
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("capability_allowlist must not contain duplicates")
        if any(not isinstance(item, str) or not item.strip() for item in scopes):
            raise ValueError("required_scopes must contain non-empty strings")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "purpose_code", self.purpose_code.strip())
        object.__setattr__(self, "description", self.description.strip())
        object.__setattr__(self, "version", self.version.strip())
        object.__setattr__(self, "capability_allowlist", capabilities)
        object.__setattr__(self, "required_scopes", scopes)
        object.__setattr__(self, "join_policy", ChildJoinPolicy(self.join_policy))
        if self.result_schema is not None:
            schema = thaw_data(freeze_data(self.result_schema))
            validate_capability_input_schema(
                schema,
                capability=f"child-output:{self.purpose_code}",
            )
            object.__setattr__(self, "result_schema", freeze_data(schema))
        _ = self.manifest

    @property
    def manifest(self) -> dict[str, Any]:
        harness_spec = getattr(getattr(self.child_harness, "_spec", None), "settings_digest", None)
        if not isinstance(harness_spec, str) or not harness_spec.startswith("sha256:"):
            raise ChildRunContractError(
                code="CHILD_HARNESS_SPEC_REQUIRED",
                message="A declared child template requires an immutable child harness spec.",
            )
        return {
            "schema_version": "1.0",
            "name": self.name,
            "version": self.version,
            "purpose_code": self.purpose_code,
            "child_harness_spec_digest": harness_spec,
            "budget": self.budget.to_dict(),
            "capability_allowlist": list(self.capability_allowlist),
            "join_policy": self.join_policy.value,
            "learning_allowed": self.learning_allowed,
            "result_schema": thaw_data(self.result_schema),
            "persist_output": self.persist_output,
            "required_scopes": list(self.required_scopes),
            "max_message_chars": self.max_message_chars,
            "max_inline_result_chars": self.max_inline_result_chars,
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self.manifest)

    def capability(self, parent_harness: AgentHarness) -> CapabilitySpec:
        """Bind this declaration to one parent as a governed model capability."""
        if parent_harness is self.child_harness:
            raise ChildRunContractError(
                code="CHILD_HARNESS_REENTRANT",
                message=(
                    "Model-visible delegation requires a distinct child harness instance "
                    "so the parent cannot deadlock its own execution lane."
                ),
            )
        resource = _ModelDeclaredChildEntrypoint(parent_harness, self)
        return CapabilitySpec(
            name=self.name,
            version=self.version,
            kind=CapabilityKind.CHILD_RUN,
            effect_class=EffectClass.IDEMPOTENT,
            trust=CapabilityTrust.HOST_MANAGED,
            lifetime=CapabilityLifetime.RUN,
            concurrency=CapabilityConcurrency.ISOLATED,
            recovery=CapabilityRecovery.RECONCILABLE,
            implementation_digest=self.digest,
            description=self.description,
            tags=("child-run", "delegation", "host-declared"),
            input_schema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": self.max_message_chars,
                        "description": "The bounded task for the declared child agent.",
                    },
                    "delegation_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 256,
                        "description": (
                            "Stable safe identifier reused when retrying the same delegation."
                        ),
                    },
                },
                "required": ["task", "delegation_id"],
                "additionalProperties": False,
            },
            required_scopes=self.required_scopes,
            supports_idempotency_key=True,
            factory=lambda: resource,
        )

    async def start(
        self,
        parent_harness: AgentHarness,
        parent: HarnessRun,
        task: str,
        *,
        context: ExecutionContext,
        delegation_id: str,
        parent_step_id: str | None = None,
        parent_tool_call_id: str | None = None,
    ) -> HarnessRun:
        """Start the fixed child declaration without waiting for completion."""
        if parent_harness is self.child_harness:
            raise ChildRunContractError(
                code="CHILD_HARNESS_REENTRANT",
                message="A declared child must use a distinct harness instance.",
            )
        if not isinstance(task, str) or not task.strip():
            raise ValueError("declared child task must be a non-empty string")
        if len(task) > self.max_message_chars:
            raise ValueError("declared child task exceeds its template message bound")
        return await parent.child(
            self.child_harness,
            task.strip(),
            context=context,
            delegation_id=delegation_id,
            purpose_code=self.purpose_code,
            budget=self.budget,
            capability_allowlist=self.capability_allowlist,
            join_policy=self.join_policy,
            learning_allowed=self.learning_allowed,
            result_schema=thaw_data(self.result_schema),
            parent_step_id=parent_step_id,
            parent_tool_call_id=parent_tool_call_id,
            persist_output=self.persist_output,
        )


class _ModelDeclaredChildEntrypoint:
    def __init__(self, parent_harness: AgentHarness, template: DeclaredChildTemplate) -> None:
        self._parent_harness = parent_harness
        self._template = template

    async def entrypoint(self, *, task: str, delegation_id: str) -> dict[str, Any]:
        # Imported lazily to avoid an agent -> runtime -> agent module cycle.
        from ..agent import get_current_tool_runtime

        runtime = get_current_tool_runtime()
        context = runtime.get("context") if runtime is not None else None
        parent_run_id = runtime.get("parent_run_id") if runtime is not None else None
        if not isinstance(context, ExecutionContext) or not isinstance(parent_run_id, str):
            raise HarnessError(
                code="CHILD_GOVERNANCE_CONTEXT_REQUIRED",
                category="authorization",
                message="Declared child capabilities require a trusted active parent run.",
                retryable=False,
            )
        assert runtime is not None
        parent = self._parent_harness.get_run(parent_run_id, context=context)
        child = await self._template.start(
            self._parent_harness,
            parent,
            task,
            context=context,
            delegation_id=delegation_id,
            parent_step_id=runtime.get("parent_step_id"),
            parent_tool_call_id=runtime.get("parent_tool_call_id"),
        )
        try:
            await child.wait()
        except RunWaitError:
            if self._template.join_policy is not ChildJoinPolicy.COLLECT:
                raise
        results = await parent.child_results(require_terminal=True)
        selected = tuple(
            item for item in results.outcomes if item.delegation_id == delegation_id
        )
        if len(selected) != 1:
            raise ChildRunContractError(
                code="CHILD_RESULT_IDENTITY_MISSING",
                message="The declared child result could not be matched to its delegation.",
            )
        projected = ChildResultSet(parent_run_id=results.parent_run_id, outcomes=selected)
        payload = projected.synthesis_payload(
            max_inline_result_chars=self._template.max_inline_result_chars
        )
        return payload["outcomes"][0]


__all__ = [
    "MAX_DECLARED_CHILD_MESSAGE_CHARS",
    "DeclaredChildTemplate",
]
