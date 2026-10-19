"""Executable domain definitions over the existing 0.14 contract types."""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from agnoclaw.runtime.context import ExecutionContext
from agnoclaw.runtime.security import canonical_json_digest

from ._contracts import Record, digest_string, fail, identifier
from .actions import ActionProposal
from .evidence import EvidenceRef
from .identity import ContractRef
from .objects import ObjectRef
from .outputs import OutputSpec, WorkProduct
from .procedures import DomainPack, ProcedureSpec
from .verification import CheckResult, CheckStatus, VerificationReport

Subject = TypeVar("Subject", WorkProduct, ActionProposal)


class OutputSchema(OutputSpec):
    """Name the output shape first; the initial contract version defaults to '1'."""

    def __init__(
        self,
        name: str,
        content_model: type[BaseModel],
        *,
        version: str = "1",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        ContractRef(name, version)
        super().__init__(name, version, content_model, metadata if metadata is not None else {})

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.name, self.version)


@dataclass(frozen=True)
class CheckOutcome:
    status: CheckStatus
    reason: str
    evidence_digests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", CheckStatus(self.status))
        identifier(self.reason, "reason")
        object.__setattr__(self, "evidence_digests", tuple(self.evidence_digests))


@dataclass(frozen=True)
class Check(Generic[Subject]):
    """Bind an implementation to its reference and bind results to the actual subject.

    Callbacks return bool or CheckOutcome; arbitrary truthy values are errors.
    Synchronous callbacks must be short and pure. Async callbacks are cooperatively
    timed out; exceptions become sanitized ERROR results and cancellation propagates.
    Bump version when semantics change. implementation_id is optional build provenance,
    not a substitute for the compatibility version recorded in verification reports.
    """

    name: str
    evaluate: Callable[[Subject], bool | CheckOutcome | Awaitable[bool | CheckOutcome]]
    version: str = field(default="1", kw_only=True)
    implementation_id: str | None = field(default=None, kw_only=True)
    timeout_seconds: float = field(default=30, kw_only=True)

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        if not callable(self.evaluate):
            raise TypeError("check implementation must be callable")
        if self.implementation_id is not None:
            identifier(self.implementation_id, "implementation_id")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("check timeout must be positive and finite")

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.name, self.version)

    @property
    def digest(self) -> str:
        return canonical_json_digest([str(self.ref), self.implementation_id, self.timeout_seconds])

    async def run(self, subject: Subject) -> CheckResult:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                outcome = self.evaluate(subject)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                if isinstance(outcome, bool):
                    outcome = CheckOutcome(
                        CheckStatus.PASS if outcome else CheckStatus.FAIL,
                        "Check passed." if outcome else "Check failed.",
                    )
                if not isinstance(outcome, CheckOutcome):
                    raise TypeError("check must return bool or CheckOutcome")
                return CheckResult(
                    self.name,
                    self.version,
                    subject.digest,
                    outcome.status,
                    outcome.reason,
                    outcome.evidence_digests,
                )
        except Exception:
            return CheckResult(
                self.name,
                self.version,
                subject.digest,
                CheckStatus.ERROR,
                "Checker failed or timed out.",
            )


def _checks(checks: Sequence[Check[Subject]]) -> tuple[Check[Subject], ...]:
    selected = tuple(checks)
    if not selected or any(not isinstance(check, Check) for check in selected):
        raise ValueError("supply at least one executable Check object")
    if len({check.ref for check in selected}) != len(selected):
        raise ValueError("check references must be unique")
    return selected


async def _report(subject: Subject, checks: Sequence[Check[Subject]]) -> VerificationReport:
    # Sequential execution also supports host callbacks sharing one async DB session.
    return VerificationReport(subject.digest, tuple([await check.run(subject) for check in checks]))


@dataclass(frozen=True)
class PreparedWork(Record):
    product: WorkProduct
    report: VerificationReport
    procedure_reference: str
    procedure_digest: str
    required_checks: tuple[str, ...]

    def __post_init__(self) -> None:
        ContractRef.parse(self.procedure_reference)
        digest_string(self.procedure_digest)
        if self.product.digest != self.report.subject_digest:
            fail("CHECK_SUBJECT_MISMATCH", "Prepared work contains a report for different content.")
        object.__setattr__(self, "required_checks", tuple(self.required_checks))

    @property
    def ready(self) -> bool:
        """Checks passed; this is not authorization to commit a business action."""
        return self.report.passes(self.required_checks)

    def require_ready(self) -> WorkProduct:
        self.report.require_pass(self.product.digest, self.required_checks)
        return self.product


@dataclass(frozen=True)
class Procedure:
    """Validate a work product and execute its checks in one explicit call."""

    name: str
    output: OutputSpec
    checks: tuple[Check[WorkProduct], ...] = field(kw_only=True)
    version: str = field(default="1", kw_only=True)
    skill: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        object.__setattr__(self, "checks", _checks(self.checks))
        if not isinstance(self.output, OutputSpec):
            raise TypeError("output must be OutputSchema or OutputSpec")
        if self.skill is not None:
            identifier(self.skill, "skill")

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.name, self.version)

    @property
    def spec(self) -> ProcedureSpec:
        return ProcedureSpec(
            self.name,
            self.version,
            self.skill or self.name,
            self.output,
            tuple(str(check.ref) for check in self.checks),
        )

    @property
    def digest(self) -> str:
        return canonical_json_digest([self.spec.digest, [check.digest for check in self.checks]])

    async def prepare(
        self,
        content: Mapping[str, Any],
        *,
        target: ObjectRef,
        run_id: str,
        context: ExecutionContext,
        evidence: Sequence[EvidenceRef] = (),
    ) -> PreparedWork:
        product = self.output.build(
            content, target=target, run_id=run_id, context=context, evidence=evidence
        )
        report = await _report(product, self.checks)
        return PreparedWork(product, report, str(self.ref), self.digest, self.spec.required_checks)


@dataclass(frozen=True)
class Domain:
    """Compose executable procedures with explicit duplicate/conflict semantics."""

    name: str
    procedures: tuple[Procedure, ...]
    version: str = field(default="1", kw_only=True)

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        selected: dict[ContractRef, Procedure] = {}
        checks: dict[ContractRef, Check[WorkProduct]] = {}
        outputs: dict[str, OutputSpec] = {}
        for procedure in self.procedures:
            if not isinstance(procedure, Procedure):
                raise TypeError("Domain expects executable Procedure definitions")
            output = outputs.get(procedure.output.reference)
            if output is not None and (
                output.digest != procedure.output.digest
                or output.content_model is not procedure.output.content_model
            ):
                fail("DEFINITION_CONFLICT", f"Conflicting output {procedure.output.reference}.")
            outputs[procedure.output.reference] = procedure.output
            for check in procedure.checks:
                previous = checks.get(check.ref)
                if previous is not None and (
                    previous.digest != check.digest or previous.evaluate is not check.evaluate
                ):
                    fail(
                        "DEFINITION_CONFLICT", f"Conflicting implementations for check {check.ref}."
                    )
                checks[check.ref] = check
            existing = selected.get(procedure.ref)
            if existing is not None and existing.digest != procedure.digest:
                fail(
                    "DEFINITION_CONFLICT", f"Conflicting definitions for procedure {procedure.ref}."
                )
            selected[procedure.ref] = procedure
        object.__setattr__(self, "procedures", tuple(selected.values()))
        _ = self.pack  # Validate using the existing pack contract.

    @property
    def pack(self) -> DomainPack:
        """Declaration export for the trusted pack loader; does not install execution."""
        return DomainPack(self.name, self.version, tuple(item.spec for item in self.procedures))

    def procedure(self, reference: str | ContractRef) -> Procedure:
        exact = (
            reference
            if isinstance(reference, ContractRef)
            else (ContractRef.parse(reference) if "@" in reference else None)
        )
        matches = [
            item
            for item in self.procedures
            if item.ref == exact or (exact is None and item.name == reference)
        ]
        if not matches:
            fail("UNKNOWN_PROCEDURE", f"No procedure matches {reference!s}.")
        if len(matches) > 1:
            fail(
                "AMBIGUOUS_REFERENCE",
                f"Multiple versions of {reference!s}; select an explicit ref.",
            )
        return matches[0]

    def describe(self) -> dict[str, Any]:
        """Detached, JSON-compatible inspection without calling models or checkers."""
        return {
            **self.pack.manifest(),
            "checks": [
                {
                    "reference": str(check.ref),
                    "implementation_id": check.implementation_id,
                    "timeout_seconds": check.timeout_seconds,
                }
                for check in {
                    check.ref: check for item in self.procedures for check in item.checks
                }.values()
            ],
        }
