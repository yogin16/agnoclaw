"""Frozen corpus membership, provenance, and leakage contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agnoclaw import (
    EvaluationCase,
    EvaluationCaseExposure,
    EvaluationCorpusEntry,
    EvaluationCorpusManifest,
    EvaluationSlice,
    HarnessError,
    evaluation_corpus_case_set_digest,
)


def _sha(character: str) -> str:
    return "sha256:" + character * 64


def _cases() -> tuple[EvaluationCase, ...]:
    return tuple(
        EvaluationCase(
            case_id=f"{slice_name.value}-case",
            slice=slice_name,
            task_class=f"{slice_name.value}-tasks",
            payload={"private_prompt": f"prompt-{slice_name.value}"},
        )
        for slice_name in EvaluationSlice
    )


def _entry(
    case: EvaluationCase,
    *,
    lineage: str,
    exposure: EvaluationCaseExposure | None = None,
) -> EvaluationCorpusEntry:
    return EvaluationCorpusEntry.from_case(
        case,
        lineage_digest=_sha(lineage),
        source_artifact_id="artifact-source",
        exposure=exposure
        or (
            EvaluationCaseExposure.DEVELOPMENT
            if case.slice is EvaluationSlice.HELD_IN
            else EvaluationCaseExposure.SEALED
        ),
    )


def _manifest(
    entries: tuple[EvaluationCorpusEntry, ...],
) -> EvaluationCorpusManifest:
    return EvaluationCorpusManifest(
        corpus_id="safety-corpus",
        version="1",
        entries=entries,
        selection_policy_digest=_sha("4"),
        sampling_seed_digest=_sha("5"),
        sealed_access_policy_digest=_sha("8"),
        decontamination_method_digest=_sha("6"),
        decontamination_artifact_id="artifact-decontamination",
        curator_identity_digest=_sha("7"),
    )


def test_manifest_is_content_free_immutable_and_verifies_exact_ordered_cases() -> None:
    cases = _cases()
    entries = tuple(
        _entry(case, lineage=str(index + 1)) for index, case in enumerate(cases)
    )
    manifest = _manifest(entries)

    assert manifest.verify_cases(cases) == cases
    assert manifest.case_set_digest == evaluation_corpus_case_set_digest(entries)
    assert "private_prompt" not in str(manifest.to_dict())
    assert set(manifest.evidence_artifact_ids) == {
        "artifact-source",
        "artifact-decontamination",
    }
    with pytest.raises(FrozenInstanceError):
        manifest.version = "2"  # type: ignore[misc]
    with pytest.raises(HarnessError) as mismatch:
        manifest.verify_cases(reversed(cases))
    assert mismatch.value.code == "IMPROVEMENT_CORPUS_CASE_MISMATCH"


def test_manifest_rejects_exact_duplicates_and_cross_split_lineage() -> None:
    cases = list(_cases())
    cases[1] = EvaluationCase(
        case_id=cases[1].case_id,
        slice=cases[1].slice,
        task_class=cases[1].task_class,
        payload=cases[0].payload,
    )
    duplicate_entries = tuple(
        _entry(case, lineage=str(index + 1)) for index, case in enumerate(cases)
    )
    with pytest.raises(HarnessError) as duplicate:
        _manifest(duplicate_entries)
    assert duplicate.value.code == "IMPROVEMENT_CORPUS_EXACT_DUPLICATE"

    distinct = _cases()
    leaking = (
        _entry(distinct[0], lineage="a"),
        _entry(distinct[1], lineage="a"),
        _entry(distinct[2], lineage="c"),
    )
    with pytest.raises(HarnessError) as lineage:
        _manifest(leaking)
    assert lineage.value.code == "IMPROVEMENT_CORPUS_LINEAGE_LEAKAGE"


def test_manifest_enforces_sealed_evaluation_slices_and_curator_independence() -> None:
    cases = _cases()
    with pytest.raises(HarnessError) as exposure:
        _entry(
            cases[1],
            lineage="b",
            exposure=EvaluationCaseExposure.DEVELOPMENT,
        )
    assert exposure.value.code == "IMPROVEMENT_CORPUS_EXPOSURE_INVALID"

    manifest = _manifest(
        tuple(_entry(case, lineage=str(index + 1)) for index, case in enumerate(cases))
    )
    with pytest.raises(HarnessError) as authority:
        manifest.verify_authority(proposer_identity_digest=manifest.curator_identity_digest)
    assert authority.value.code == "IMPROVEMENT_CORPUS_AUTHORITY_CONFLICT"
