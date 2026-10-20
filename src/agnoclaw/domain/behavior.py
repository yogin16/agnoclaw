"""Skill contracts, pinned callable interfaces, workflow plans and pack evaluations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from agnoclaw.capabilities import CapabilitySpec
from agnoclaw.capability_execution import (
    CapabilityExecution,
    CapabilityExecutor,
    CapabilityPreDispatch,
)
from agnoclaw.runtime.operations import EffectClass
from agnoclaw.runtime.security import AdmissionEnvelope, canonical_json_digest
from agnoclaw.skills.loader import Skill

from ._contracts import Record, digest_string, fail, frozen_json, identifier
from .components import DomainComponent, timeout
from .evaluation import EvaluationCase, EvaluationSummary, evaluate_cases
from .identity import ContractRef, OperationIdentity
from .outputs import OutputSpec


@dataclass(frozen=True)
class SkillBinding(DomainComponent):
    """Canonical output metadata and capabilities for an existing loaded skill."""

    name: str
    output: OutputSpec
    version: str = field(default="1", kw_only=True)
    capabilities: tuple[CapabilitySpec, ...] = field(default=(), kw_only=True)
    resource: str | None = field(default=None, kw_only=True)
    kind: ClassVar[str] = "skill"

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        if self.resource is not None:
            from .resources import resource_path

            resource_path(self.resource)

    def dependencies(self) -> tuple[object, ...]:
        return (self.output, *self.capabilities)

    def manifest(self) -> dict[str, Any]:
        return {
            "output": self.output.reference,
            "resource": self.resource,
            "capabilities": [f"{c.name}@{c.version}" for c in self.capabilities],
        }

    def validate_skill(self, skill: Skill) -> None:
        if skill.name != self.name:
            fail("SKILL_NAME_MISMATCH", "Loaded skill has a different canonical name.")
        declared = set(skill.meta.allowed_tools)
        allowed = {c.name for c in self.capabilities}
        if not declared <= allowed:
            fail("SKILL_CAPABILITY_MISMATCH", "Skill requests tools outside its domain binding.")

    def validate_output(self, content: Mapping[str, Any]) -> dict[str, Any]:
        return self.output.validate_content(content)


@dataclass(frozen=True)
class Interface(DomainComponent):
    name: str
    inputs: OutputSpec
    outputs: OutputSpec
    version: str = field(default="1", kw_only=True)
    kind: ClassVar[str] = "interface"

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)

    def dependencies(self) -> tuple[object, ...]:
        return (self.inputs, self.outputs)

    def manifest(self) -> dict[str, Any]:
        return {"inputs": self.inputs.reference, "outputs": self.outputs.reference}

    def bind(self, capability: CapabilitySpec, *, name: str | None = None) -> InterfaceBinding:
        return InterfaceBinding(name or self.name, self, capability)


@dataclass(frozen=True)
class InterfaceBinding(DomainComponent):
    name: str
    interface: Interface
    capability: CapabilitySpec
    version: str = field(default="1", kw_only=True)
    kind: ClassVar[str] = "binding"

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        if (
            canonical_json_digest(self.capability.input_schema)
            != self.interface.inputs.schema_digest
        ):
            raise ValueError("capability input_schema must equal the interface input JSON schema")

    def dependencies(self) -> tuple[object, ...]:
        return (self.interface, self.capability)

    def manifest(self) -> dict[str, Any]:
        return {
            "interface": str(self.interface.ref),
            "capability": f"{self.capability.name}@{self.capability.version}",
        }

    async def execute(
        self,
        executor: CapabilityExecutor,
        inputs: Mapping[str, Any],
        *,
        operation_id: str,
        run_id: str,
        attempt_id: str,
        admission: AdmissionEnvelope,
        idempotency_key: str | None = None,
        pre_dispatch: CapabilityPreDispatch | None = None,
        execution_digest: str | None = None,
    ) -> CapabilityExecution:
        """Use the existing effect ledger; output validation occurs before settlement."""
        if execution_digest is not None:
            digest_string(execution_digest)
        if self.capability.effect_class is not EffectClass.READ_ONLY and pre_dispatch is None:
            fail(
                "EFFECT_GUARD_REQUIRED",
                "Effectful interface calls require a host pre-dispatch guard.",
            )
        arguments = self.interface.inputs.validate_content(inputs)
        return await executor.execute(
            f"{self.capability.name}@{self.capability.version}",
            arguments=arguments,
            operation_id=operation_id,
            run_id=run_id,
            attempt_id=attempt_id,
            admission=admission,
            idempotency_key=idempotency_key,
            pre_dispatch=pre_dispatch,
            expected_capability_digest=self.capability.digest,
            execution_digest=canonical_json_digest([self.digest, execution_digest]),
            result_transformer=self.interface.outputs.validate_content,
        )


@dataclass(frozen=True)
class WorkflowStep:
    name: str
    call: InterfaceBinding

    def __post_init__(self) -> None:
        identifier(self.name, "step name")


@dataclass(frozen=True)
class WorkflowPlan(Record):
    workflow_reference: str
    workflow_digest: str
    tenant_id: str
    request_id: str
    inputs: Mapping[str, Any]

    def __post_init__(self) -> None:
        ContractRef.parse(self.workflow_reference)
        digest_string(self.workflow_digest)
        identifier(self.tenant_id, "tenant_id")
        identifier(self.request_id, "request_id")
        object.__setattr__(self, "inputs", frozen_json(self.inputs))


@dataclass(frozen=True)
class Workflow(DomainComponent):
    """A typed sequential capability adapter, using the existing operation ledger.

    Hosts persist the plan before execution and own run recovery and review queues.
    This adapter creates no workers and never automatically retries failed effects.
    Branching/scheduling can be performed by host orchestration around these plans.
    """

    name: str
    steps: tuple[WorkflowStep, ...]
    version: str = field(default="1", kw_only=True)
    kind: ClassVar[str] = "workflow"

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        object.__setattr__(self, "steps", tuple(self.steps))
        if not self.steps or len({s.name for s in self.steps}) != len(self.steps):
            raise ValueError("workflow needs non-empty uniquely named steps")
        for before, after in zip(self.steps, self.steps[1:], strict=False):
            if (
                before.call.interface.outputs.schema_digest
                != after.call.interface.inputs.schema_digest
            ):
                raise ValueError(
                    f"step {after.name}: input schema differs from {before.name} output"
                )

    def dependencies(self) -> tuple[object, ...]:
        return tuple(s.call for s in self.steps)

    def manifest(self) -> dict[str, Any]:
        return {"steps": [{"name": s.name, "binding": str(s.call.ref)} for s in self.steps]}

    def plan(self, inputs: Mapping[str, Any], *, tenant_id: str, request_id: str) -> WorkflowPlan:
        return WorkflowPlan(
            str(self.ref),
            self.digest,
            tenant_id,
            request_id,
            self.steps[0].call.interface.inputs.validate_content(inputs),
        )

    async def execute(
        self,
        executor: CapabilityExecutor,
        plan: WorkflowPlan,
        *,
        run_id: str,
        attempt_id: str,
        admission: AdmissionEnvelope,
        pre_dispatch: Mapping[str, CapabilityPreDispatch] | None = None,
    ) -> Mapping[str, Any]:
        if plan.workflow_reference != str(self.ref) or plan.workflow_digest != self.digest:
            fail("WORKFLOW_CHANGED", "Resume using the plan's pinned workflow definition.")
        if plan.tenant_id != admission.identity.tenant_id:
            fail("SCOPE_MISMATCH", "Workflow plan belongs to another tenant.")
        guards = pre_dispatch or {}
        if set(guards) - {s.name for s in self.steps}:
            raise ValueError("pre_dispatch names an unknown workflow step")
        values = plan.inputs
        for step in self.steps:
            identity = OperationIdentity(
                plan.tenant_id,
                "workflow-step:" + canonical_json_digest([self.name, step.name]),
                plan.request_id,
                canonical_json_digest([plan.digest, step.call.digest, values]),
            )
            execution = await step.call.execute(
                executor,
                values,
                operation_id=identity.key,
                run_id=run_id,
                attempt_id=attempt_id,
                admission=admission,
                idempotency_key=identity.key
                if step.call.capability.supports_idempotency_key
                else None,
                pre_dispatch=guards.get(step.name),
                execution_digest=plan.digest,
            )
            # Validate replayed values as well as newly transformed results.
            values = step.call.interface.outputs.validate_content(execution.value)
        return values


@dataclass(frozen=True)
class EvaluationSuite(DomainComponent):
    """Pack-bound facade over evaluate_cases; no additional runner or case type."""

    name: str
    cases: tuple[EvaluationCase, ...]
    run: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]] = field(repr=False)
    version: str = field(default="1", kw_only=True)
    implementation_id: str = field(kw_only=True)
    subjects: tuple[object, ...] = field(default=(), kw_only=True, repr=False)
    timeout_seconds: float = field(default=30, kw_only=True)
    kind: ClassVar[str] = "evaluation"

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        identifier(self.implementation_id, "implementation_id")
        object.__setattr__(self, "cases", tuple(self.cases))
        object.__setattr__(self, "subjects", tuple(self.subjects))
        if not self.cases or len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("suite requires non-empty unique cases")
        if not callable(self.run):
            raise TypeError("suite run must be an async callable")
        timeout(self.timeout_seconds)

    def dependencies(self) -> tuple[object, ...]:
        return self.subjects

    def manifest(self) -> dict[str, Any]:
        return {
            "cases": [c.digest for c in self.cases],
            "implementation_id": self.implementation_id,
            "timeout_seconds": self.timeout_seconds,
        }

    async def evaluate(self) -> EvaluationSummary:
        return await evaluate_cases(self.cases, self.run, timeout_seconds=self.timeout_seconds)
