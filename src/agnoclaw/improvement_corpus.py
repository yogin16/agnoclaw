"""Content-free corpus provenance and leakage controls for improvement evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .improvement import EvaluationSlice
from .runtime.artifacts import ArtifactReference
from .runtime.errors import HarnessError
from .runtime.security import freeze_data, thaw_data

EVALUATION_CORPUS_SCHEMA_VERSION = "1.0"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        thaw_data(freeze_data(value)),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def _require_text(value: str, *, field_name: str, maximum: int = 512) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{field_name} cannot exceed {maximum} characters")


def _require_digest(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical sha256 digest")


def _error(code: str, message: str, **details: Any) -> HarnessError:
    return HarnessError(
        code=code,
        category="evaluation",
        message=message,
        retryable=False,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """One immutable case in exactly one frozen evaluation slice."""

    case_id: str
    slice: EvaluationSlice
    task_class: str
    payload: Any

    def __post_init__(self) -> None:
        _require_text(self.case_id, field_name="case_id")
        object.__setattr__(self, "slice", EvaluationSlice(self.slice))
        _require_text(self.task_class, field_name="task_class")
        object.__setattr__(self, "payload", freeze_data(self.payload))

    @property
    def payload_digest(self) -> str:
        return _digest(thaw_data(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "slice": self.slice.value,
            "task_class": self.task_class,
            "payload": thaw_data(self.payload),
        }


class EvaluationCaseExposure(StrEnum):
    DEVELOPMENT = "development"
    SEALED = "sealed"


class EvaluationCorpusUsageBasis(StrEnum):
    INTERNAL_AUTHORIZED = "internal_authorized"
    LICENSE = "license"
    CONSENT = "consent"
    PUBLIC_DOMAIN = "public_domain"


def evaluation_corpus_case_set_digest(
    entries: Iterable[EvaluationCorpusEntry],
) -> str:
    """Digest an ordered, content-free corpus entry set before staging its audit."""
    values = tuple(entries)
    if not values or any(not isinstance(item, EvaluationCorpusEntry) for item in values):
        raise TypeError("entries must contain EvaluationCorpusEntry values")
    return _digest([item.to_dict() for item in values])


@dataclass(frozen=True, slots=True)
class EvaluationCorpusEntry:
    """Content-free case identity, provenance, split, and semantic lineage."""

    case_id: str
    slice: EvaluationSlice
    task_class: str
    payload_digest: str
    lineage_digest: str
    source_artifact_id: str
    exposure: EvaluationCaseExposure

    def __post_init__(self) -> None:
        _require_text(self.case_id, field_name="case_id")
        object.__setattr__(self, "slice", EvaluationSlice(self.slice))
        _require_text(self.task_class, field_name="task_class")
        _require_digest(self.payload_digest, field_name="payload_digest")
        _require_digest(self.lineage_digest, field_name="lineage_digest")
        _require_text(self.source_artifact_id, field_name="source_artifact_id")
        object.__setattr__(self, "exposure", EvaluationCaseExposure(self.exposure))
        expected = (
            EvaluationCaseExposure.DEVELOPMENT
            if self.slice is EvaluationSlice.HELD_IN
            else EvaluationCaseExposure.SEALED
        )
        if self.exposure is not expected:
            raise _error(
                "IMPROVEMENT_CORPUS_EXPOSURE_INVALID",
                "Held-in cases must be development-visible and evaluation cases sealed.",
                case_id=self.case_id,
                slice=self.slice.value,
            )

    @classmethod
    def from_case(
        cls,
        case: EvaluationCase,
        *,
        lineage_digest: str,
        source_artifact_id: str,
        exposure: EvaluationCaseExposure,
    ) -> EvaluationCorpusEntry:
        if not isinstance(case, EvaluationCase):
            raise TypeError("case must be an EvaluationCase")
        return cls(
            case_id=case.case_id,
            slice=case.slice,
            task_class=case.task_class,
            payload_digest=case.payload_digest,
            lineage_digest=lineage_digest,
            source_artifact_id=source_artifact_id,
            exposure=exposure,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "slice": self.slice.value,
            "task_class": self.task_class,
            "payload_digest": self.payload_digest,
            "lineage_digest": self.lineage_digest,
            "source_artifact_id": self.source_artifact_id,
            "exposure": self.exposure.value,
        }


@dataclass(frozen=True, slots=True)
class EvaluationCorpusManifest:
    """Frozen corpus membership plus independently retained contamination evidence."""

    corpus_id: str
    version: str
    entries: tuple[EvaluationCorpusEntry, ...]
    selection_policy_digest: str
    sampling_seed_digest: str
    sealed_access_policy_digest: str
    decontamination_method_digest: str
    decontamination_artifact_id: str
    curator_identity_digest: str
    schema_version: str = EVALUATION_CORPUS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        _require_text(self.corpus_id, field_name="corpus_id")
        _require_text(self.version, field_name="version", maximum=128)
        if self.schema_version != EVALUATION_CORPUS_SCHEMA_VERSION:
            raise ValueError("unsupported evaluation corpus schema")
        if not self.entries:
            raise ValueError("entries cannot be empty")
        if any(not isinstance(item, EvaluationCorpusEntry) for item in self.entries):
            raise TypeError("entries must contain EvaluationCorpusEntry values")
        for field_name in (
            "selection_policy_digest",
            "sampling_seed_digest",
            "sealed_access_policy_digest",
            "decontamination_method_digest",
            "curator_identity_digest",
        ):
            _require_digest(getattr(self, field_name), field_name=field_name)
        _require_text(
            self.decontamination_artifact_id,
            field_name="decontamination_artifact_id",
        )
        case_ids = [item.case_id for item in self.entries]
        if len(case_ids) != len(set(case_ids)):
            raise _error(
                "IMPROVEMENT_CORPUS_CASE_DUPLICATE",
                "Corpus case identifiers must be globally unique.",
            )
        payloads = [item.payload_digest for item in self.entries]
        if len(payloads) != len(set(payloads)):
            raise _error(
                "IMPROVEMENT_CORPUS_EXACT_DUPLICATE",
                "Exact duplicate payloads cannot enter an evaluation corpus.",
            )
        if {item.slice for item in self.entries} != set(EvaluationSlice):
            raise _error(
                "IMPROVEMENT_CORPUS_SLICE_REQUIRED",
                "Held-in, held-out, and transfer corpus entries are all required.",
            )
        lineage_slices: dict[str, set[EvaluationSlice]] = {}
        for entry in self.entries:
            lineage_slices.setdefault(entry.lineage_digest, set()).add(entry.slice)
        if any(len(slices) > 1 for slices in lineage_slices.values()):
            raise _error(
                "IMPROVEMENT_CORPUS_LINEAGE_LEAKAGE",
                "One semantic lineage cannot cross evaluation splits.",
            )

    @property
    def case_set_digest(self) -> str:
        return evaluation_corpus_case_set_digest(self.entries)

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    @property
    def evidence_artifact_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    self.decontamination_artifact_id,
                    *(item.source_artifact_id for item in self.entries),
                }
            )
        )

    def verify_cases(self, cases: Iterable[EvaluationCase]) -> tuple[EvaluationCase, ...]:
        values = tuple(cases)
        if any(not isinstance(item, EvaluationCase) for item in values):
            raise TypeError("cases must contain EvaluationCase values")
        if len(values) != len(self.entries):
            raise _error(
                "IMPROVEMENT_CORPUS_CASE_MISMATCH",
                "Runtime cases must exactly match the frozen corpus manifest.",
                expected=len(self.entries),
                supplied=len(values),
            )
        for entry, case in zip(self.entries, values, strict=True):
            if (
                entry.case_id != case.case_id
                or entry.slice is not case.slice
                or entry.task_class != case.task_class
                or entry.payload_digest != case.payload_digest
            ):
                raise _error(
                    "IMPROVEMENT_CORPUS_CASE_MISMATCH",
                    "Runtime cases must exactly match the frozen corpus manifest.",
                    case_id=case.case_id,
                )
        return values

    def verify_authority(self, *, proposer_identity_digest: str) -> None:
        _require_digest(proposer_identity_digest, field_name="proposer_identity_digest")
        if self.curator_identity_digest == proposer_identity_digest:
            raise _error(
                "IMPROVEMENT_CORPUS_AUTHORITY_CONFLICT",
                "The candidate proposer cannot curate its own evaluation corpus.",
            )

    def verify_evidence(
        self,
        references: Mapping[str, ArtifactReference],
        loaded: Mapping[str, Any],
    ) -> None:
        required = set(self.evidence_artifact_ids)
        if not required.issubset(references) or not required.issubset(loaded):
            raise _error(
                "IMPROVEMENT_CORPUS_EVIDENCE_INVALID",
                "Corpus provenance and contamination evidence must be present.",
                reason="missing",
            )
        source_ids = {item.source_artifact_id for item in self.entries}
        for artifact_id in source_ids:
            reference = references[artifact_id]
            value = loaded[artifact_id]
            if reference.purpose != "evaluation_corpus_source" or not isinstance(
                value,
                Mapping,
            ):
                raise _error(
                    "IMPROVEMENT_CORPUS_EVIDENCE_INVALID",
                    "Corpus source evidence has an invalid public contract.",
                    reason="source_contract",
                )
            if (
                value.get("type") != "agnoclaw.evaluation_corpus_source"
                or value.get("schema_version") != EVALUATION_CORPUS_SCHEMA_VERSION
                or not isinstance(value.get("source_id"), str)
                or not value.get("source_id", "").strip()
                or not isinstance(value.get("source_digest"), str)
                or _DIGEST_RE.fullmatch(value["source_digest"]) is None
                or value.get("usage_basis")
                not in {item.value for item in EvaluationCorpusUsageBasis}
                or not isinstance(value.get("retention_policy_digest"), str)
                or _DIGEST_RE.fullmatch(value["retention_policy_digest"]) is None
            ):
                raise _error(
                    "IMPROVEMENT_CORPUS_EVIDENCE_INVALID",
                    "Corpus source evidence has an invalid public contract.",
                    reason="source_schema",
                )
        reference = references[self.decontamination_artifact_id]
        value = loaded[self.decontamination_artifact_id]
        if reference.purpose != "evaluation_corpus_decontamination" or not isinstance(
            value,
            Mapping,
        ):
            raise _error(
                "IMPROVEMENT_CORPUS_EVIDENCE_INVALID",
                "Corpus contamination evidence has an invalid public contract.",
                reason="decontamination_contract",
            )
        comparison_digests = value.get("comparison_corpus_digests")
        known = value.get("known_overlap_case_ids")
        unresolved = value.get("unresolved_case_ids")
        valid_comparisons = (
            isinstance(comparison_digests, list)
            and bool(comparison_digests)
            and len(comparison_digests) == len(set(comparison_digests))
            and all(
                isinstance(item, str) and _DIGEST_RE.fullmatch(item)
                for item in comparison_digests
            )
        )
        if not (
            value.get("type") == "agnoclaw.evaluation_corpus_decontamination"
            and value.get("schema_version") == EVALUATION_CORPUS_SCHEMA_VERSION
            and value.get("case_set_digest") == self.case_set_digest
            and value.get("method_digest") == self.decontamination_method_digest
            and value.get("checked_case_count") == len(self.entries)
            and value.get("reviewer_identity_digest") == self.curator_identity_digest
            and valid_comparisons
            and isinstance(known, list)
            and isinstance(unresolved, list)
        ):
            raise _error(
                "IMPROVEMENT_CORPUS_EVIDENCE_INVALID",
                "Corpus contamination evidence does not match the frozen manifest.",
                reason="decontamination_schema",
            )
        case_ids = {item.case_id for item in self.entries}
        if (
            any(not isinstance(item, str) or item not in case_ids for item in known)
            or any(not isinstance(item, str) or item not in case_ids for item in unresolved)
            or known
            or unresolved
        ):
            raise _error(
                "IMPROVEMENT_CORPUS_CONTAMINATION_DETECTED",
                "Known or unresolved corpus overlap blocks qualification evidence.",
                known_count=len(known),
                unresolved_count=len(unresolved),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "corpus_id": self.corpus_id,
            "version": self.version,
            "entries": [item.to_dict() for item in self.entries],
            "selection_policy_digest": self.selection_policy_digest,
            "sampling_seed_digest": self.sampling_seed_digest,
            "sealed_access_policy_digest": self.sealed_access_policy_digest,
            "decontamination_method_digest": self.decontamination_method_digest,
            "decontamination_artifact_id": self.decontamination_artifact_id,
            "curator_identity_digest": self.curator_identity_digest,
        }


__all__ = [
    "EVALUATION_CORPUS_SCHEMA_VERSION",
    "EvaluationCase",
    "EvaluationCaseExposure",
    "EvaluationCorpusEntry",
    "EvaluationCorpusManifest",
    "EvaluationCorpusUsageBasis",
    "evaluation_corpus_case_set_digest",
]
