"""Typed skill contracts and portable packs; the agent remains the task orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agnoclaw.capabilities import CapabilitySpec
from agnoclaw.runtime.security import canonical_json_digest

from ._contracts import fail, identifier
from .actions import ActionSpec
from .outputs import OutputSpec, WorkProduct
from .verification import VerificationReport


@dataclass(frozen=True)
class ProcedureSpec:
    name: str
    version: str
    skill: str
    output: OutputSpec
    required_checks: tuple[str, ...]
    actions: tuple[ActionSpec, ...] = ()

    def __post_init__(self) -> None:
        for name in ("name", "version", "skill"):
            identifier(getattr(self, name), name)
        object.__setattr__(self, "required_checks", tuple(self.required_checks))
        object.__setattr__(self, "actions", tuple(self.actions))
        if not self.required_checks or any("@" not in ref for ref in self.required_checks):
            raise ValueError("procedures require explicit versioned completion checks")
        if len(set(self.required_checks)) != len(self.required_checks):
            raise ValueError("procedure check requirements must be unique")

    @property
    def reference(self) -> str:
        return f"{self.name}@{self.version}"

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "skill": self.skill,
            "output": self.output.reference,
            "output_digest": self.output.digest,
            "required_checks": list(self.required_checks),
            "actions": [
                {
                    "capability": action.reference,
                    "digest": action.capability.digest,
                    "required_checks": list(action.required_checks),
                    "review_required": action.review_required,
                }
                for action in self.actions
            ],
        }

    @property
    def digest(self) -> str:
        return canonical_json_digest(self.manifest())

    def verify_completion(self, product: WorkProduct, report: VerificationReport) -> None:
        if (
            product.output_name != self.output.name
            or product.output_version != self.output.version
            or product.schema_digest != self.output.schema_digest
            or product.metadata != self.output.metadata
        ):
            fail("OUTPUT_CONTRACT_MISMATCH", "Work product does not match the procedure output.")
        report.require_pass(product.digest, self.required_checks)


@dataclass(frozen=True)
class DomainPack:
    name: str
    version: str
    procedures: tuple[ProcedureSpec, ...]

    def __post_init__(self) -> None:
        identifier(self.name, "name")
        identifier(self.version, "version")
        object.__setattr__(self, "procedures", tuple(self.procedures))
        if not self.procedures:
            raise ValueError("domain packs require at least one procedure")
        refs = [procedure.reference for procedure in self.procedures]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate procedure references")
        outputs: dict[str, str] = {}
        for procedure in self.procedures:
            ref, digest = procedure.output.reference, procedure.output.digest
            if ref in outputs and outputs[ref] != digest:
                raise ValueError("one output version cannot describe conflicting contracts")
            outputs[ref] = digest
        self.capabilities()

    def capabilities(self) -> tuple[CapabilitySpec, ...]:
        selected: dict[str, CapabilitySpec] = {}
        for procedure in self.procedures:
            for action in procedure.actions:
                existing = selected.get(action.reference)
                if existing is not None and existing.digest != action.capability.digest:
                    raise ValueError("conflicting capability implementations in domain pack")
                selected[action.reference] = action.capability
        return tuple(selected.values())

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "procedures": [procedure.manifest() for procedure in self.procedures],
        }
