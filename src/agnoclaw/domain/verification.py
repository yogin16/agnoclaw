"""Subject-bound checks; unknown and errors never implicitly count as success."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ._contracts import Record, digest_string, fail, identifier
from .evidence import EvidenceRef, TextSource, quote_is_located


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    ERROR = "error"


@dataclass(frozen=True)
class CheckResult(Record):
    check_id: str
    check_version: str
    subject_digest: str
    status: CheckStatus
    reason: str
    evidence_digests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        identifier(self.check_id, "check_id")
        identifier(self.check_version, "check_version")
        identifier(self.reason, "reason")
        digest_string(self.subject_digest)
        object.__setattr__(self, "status", CheckStatus(self.status))
        object.__setattr__(self, "evidence_digests", tuple(self.evidence_digests))
        for digest in self.evidence_digests:
            digest_string(digest)

    @property
    def reference(self) -> str:
        return f"{self.check_id}@{self.check_version}"


@dataclass(frozen=True)
class VerificationReport(Record):
    subject_digest: str
    results: tuple[CheckResult, ...]

    def __post_init__(self) -> None:
        digest_string(self.subject_digest)
        object.__setattr__(self, "results", tuple(self.results))
        names = [result.reference for result in self.results]
        if len(names) != len(set(names)):
            raise ValueError("a report cannot contain duplicate check references")
        if any(result.subject_digest != self.subject_digest for result in self.results):
            fail("CHECK_SUBJECT_MISMATCH", "Checks must describe the exact report subject.")

    def passes(self, required: Sequence[str]) -> bool:
        """Explicit versioned requirements prevent vacuous or stale-check approval."""
        found = {result.reference: result.status for result in self.results}
        return bool(required) and all(found.get(ref) is CheckStatus.PASS for ref in required)

    def require_pass(self, subject_digest: str, required: Sequence[str]) -> None:
        if self.subject_digest != subject_digest:
            fail("CHECK_SUBJECT_MISMATCH", "Verification describes a different subject.")
        if not self.passes(required):
            fail("VERIFICATION_REQUIRED", "Every required versioned check must pass.")


def check_quote(evidence: EvidenceRef, source: TextSource) -> CheckResult:
    located = quote_is_located(evidence, source)
    return CheckResult(
        "quote.location",
        "1",
        evidence.digest,
        CheckStatus.PASS if located else CheckStatus.FAIL,
        "Quote located; semantic support not assessed." if located else "Source or quote mismatch.",
        (evidence.digest,),
    )


def run_check(
    check_id: str,
    version: str,
    subject_digest: str,
    check: Callable[[], CheckResult],
) -> CheckResult:
    """Contain checker failures without leaking source text from exception messages."""
    try:
        result = check()
        if (result.check_id, result.check_version, result.subject_digest) != (
            check_id,
            version,
            subject_digest,
        ):
            raise ValueError("checker returned the wrong identity")
        return result
    except Exception:
        return CheckResult(check_id, version, subject_digest, CheckStatus.ERROR, "Checker failed.")
