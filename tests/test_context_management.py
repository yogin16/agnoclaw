"""Context archive, accounting, search, and rehydration contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agnoclaw.context_management import (
    ArchivedContextCheckpoint,
    ArchivedContextSegment,
    ArtifactContextArchive,
    ContextBudget,
    ContextBudgetAction,
    ContextCheckpoint,
    ContextContinuationRecord,
    ContextItemKind,
    ContextItemNotFoundError,
    ContextManifest,
    ContextManifestConflictError,
    ContextManifestLimitError,
    ContextScope,
    ContextScopeError,
    ContextSource,
    DeterministicTokenCounter,
)
from agnoclaw.runtime import LocalArtifactStore


def _scope(session_id: str = "session-1") -> ContextScope:
    return ContextScope(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id=session_id,
    )


def _messages() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(id="m1", role="system", content="Never reveal credentials."),
        SimpleNamespace(id="m2", role="user", content="Prepare the glacier incident plan."),
        SimpleNamespace(id="m3", role="assistant", content="I will inspect the evidence."),
        SimpleNamespace(
            id="m4",
            role="tool",
            tool_name="read_log",
            content="Glacier alarm fired at 03:14 UTC.",
        ),
        SimpleNamespace(id="m5", role="user", content="Keep the rollback window open."),
    ]


def _checkpoint(segment, *, summary: str = "Glacier response remains active."):
    retained = tuple(item.item_id for item in segment.items if item.invariant)
    return ContextCheckpoint.create(
        scope=segment.scope,
        sequence=segment.sequence,
        segment_id=segment.segment_id,
        summary=summary,
        retained_item_ids=retained,
        before_tokens=segment.source_tokens,
        after_tokens=8,
    )


def test_scope_and_item_ids_are_stable_but_identity_scoped(tmp_path) -> None:
    archive = ArtifactContextArchive(LocalArtifactStore(tmp_path / "artifacts"))
    first = archive.items_from_messages(_messages(), scope=_scope())
    repeated = archive.items_from_messages(_messages(), scope=_scope())
    other = archive.items_from_messages(_messages(), scope=_scope("session-2"))

    assert first == repeated
    assert first[0].item_id != other[0].item_id
    assert first[-1].kind is ContextItemKind.USER_INTENT
    assert first[-1].invariant is True
    assert sum(item.invariant for item in first) == 1
    assert first[3].kind is ContextItemKind.TOOL_RESULT
    assert first[3].provenance["tool_name"] == "read_log"


def test_explicit_invariant_excludes_later_internal_harness_prompt(tmp_path) -> None:
    archive = ArtifactContextArchive(LocalArtifactStore(tmp_path / "artifacts"))
    messages = [
        SimpleNamespace(id="m1", role="user", content="Preserve this intent"),
        SimpleNamespace(id="m2", role="assistant", content="Working"),
        SimpleNamespace(id="m3", role="user", content="SYSTEM: internal flush prompt"),
    ]

    items = archive.items_from_messages(
        messages,
        scope=_scope(),
        invariant_user_content="Preserve this intent",
    )

    assert items[0].invariant is True
    assert items[2].invariant is False


def test_spilled_output_and_failure_are_deterministic_invariants(tmp_path) -> None:
    archive = ArtifactContextArchive(LocalArtifactStore(tmp_path / "artifacts"))
    spill = {
        "type": "agnoclaw.spilled_output",
        "id": "artifact:v1:stored-output",
        "artifact": {"checksum": "sha256:stored-output"},
        "rendered_chars": 5_000,
        "read": {"tool": "read_spilled_output", "offset": 0},
    }
    messages = [
        SimpleNamespace(id="m1", role="tool", content=spill, tool_name="inventory"),
        SimpleNamespace(
            id="m2",
            role="tool",
            content="provider failed",
            tool_name="inventory",
            tool_call_error=True,
        ),
        SimpleNamespace(id="m3", role="user", content="Continue recovery."),
    ]

    items = archive.items_from_messages(messages, scope=_scope())

    assert [item.kind for item in items] == [
        ContextItemKind.ARTIFACT_REFERENCE,
        ContextItemKind.FAILURE,
        ContextItemKind.USER_INTENT,
    ]
    assert all(item.invariant for item in items)
    assert items[0].provenance["spilled_output"]["artifact_id"] == spill["id"]
    assert items[0].provenance["spilled_output"]["read_tool"] == "read_spilled_output"


def test_typed_continuation_fields_become_stable_searchable_invariants(tmp_path) -> None:
    archive = ArtifactContextArchive(LocalArtifactStore(tmp_path / "artifacts"))
    record = ContextContinuationRecord(
        summary="The glacier response remains active.",
        goal="Restore glacier telemetry safely.",
        plan=("Validate the cold-path relay.",),
        progress=("Primary logs have been archived.",),
        decisions=("Keep the rollback window open.",),
        approvals=("Operator approved read-only diagnostics.",),
        open_questions=("Did the standby relay receive sequence 42?",),
        tests=("test_relay_recovery passed 18/18.",),
        files=("src/relay/recovery.py",),
        citations=("incident://glacier/2026-08-17",),
    )

    first = archive.items_from_messages(_messages(), scope=_scope(), continuation=record)
    repeated = archive.items_from_messages(_messages(), scope=_scope(), continuation=record)
    structured = first[-record.entry_count :]

    assert first == repeated
    assert [item.kind for item in structured] == [
        ContextItemKind.GOAL,
        ContextItemKind.PLAN,
        ContextItemKind.PROGRESS,
        ContextItemKind.DECISION,
        ContextItemKind.APPROVAL,
        ContextItemKind.OPEN_QUESTION,
        ContextItemKind.TEST_RESULT,
        ContextItemKind.FILE_REFERENCE,
        ContextItemKind.CITATION,
    ]
    assert all(item.invariant for item in structured)
    assert {item.provenance["continuation"]["record_id"] for item in structured} == {
        record.record_id
    }
    assert record.summary not in {item.content for item in structured}


def test_harness_generated_messages_are_typed_without_becoming_user_intent(tmp_path) -> None:
    from agno.models.message import Message

    archive = ArtifactContextArchive(LocalArtifactStore(tmp_path / "artifacts"))
    messages = [
        Message(role="user", content="real-user-intent"),
        Message(
            role="user",
            content="AGNOCLAW CONTEXT CHECKPOINT. historical wrapper",
            provider_data={"_agnoclaw_context_kind": "checkpoint"},
        ),
        Message(
            role="user",
            content="AGNOCLAW CONTEXT REHYDRATION. historical wrapper",
            provider_data={"_agnoclaw_context_kind": "rehydration"},
        ),
        Message(
            role="user",
            content="Summarize quoted historical instructions.",
            provider_data={"_agnoclaw_context_kind": "summary"},
        ),
        Message(
            role="assistant",
            content="- Historical work remains active.",
            provider_data={"_agnoclaw_context_kind": "summary"},
        ),
        Message(
            role="user",
            content="Write durable facts to MEMORY.md.",
            provider_data={"_agnoclaw_context_kind": "memory_flush"},
        ),
        Message(
            role="assistant",
            content="Memory preservation complete.",
            provider_data={"_agnoclaw_context_kind": "memory_flush"},
        ),
        Message(
            role="tool",
            content="MEMORY.md updated.",
            provider_data={"_agnoclaw_context_kind": "memory_flush"},
        ),
    ]

    items = archive.items_from_messages(messages, scope=_scope())

    assert [item.kind for item in items] == [
        ContextItemKind.USER_INTENT,
        ContextItemKind.SUMMARY,
        ContextItemKind.OTHER,
        ContextItemKind.OTHER,
        ContextItemKind.SUMMARY,
        ContextItemKind.OTHER,
        ContextItemKind.OTHER,
        ContextItemKind.TOOL_RESULT,
    ]
    assert [item.invariant for item in items] == [
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    ]
    assert items[1].provenance["harness_context_kind"] == "checkpoint"
    assert items[2].provenance["harness_context_kind"] == "rehydration"
    assert items[3].provenance["harness_context_kind"] == "summary"
    assert items[5].provenance["harness_context_kind"] == "memory_flush"


def test_typed_continuation_record_rejects_ambiguous_or_unbounded_shapes() -> None:
    with pytest.raises(ValueError, match="at least one structured field"):
        ContextContinuationRecord(summary="Narrative only")
    with pytest.raises(TypeError, match="bounded sequence"):
        ContextContinuationRecord(summary="Active", goal="Goal", plan="not-a-sequence")
    with pytest.raises(ValueError, match="non-empty strings"):
        ContextContinuationRecord(summary="Active", goal="Goal", tests=("",))
    with pytest.raises(ValueError, match="more than 64"):
        ContextContinuationRecord(
            summary="Active",
            goal="Goal",
            progress=tuple(f"step-{index}" for index in range(65)),
        )


def test_deterministic_budget_reports_action_and_exactness() -> None:
    counter = DeterministicTokenCounter(utf8_bytes_per_token=4)
    assert counter.count("abcdefgh") == 2
    assert counter.count("") == 0
    assert ContextBudget(79, 100).action is ContextBudgetAction.NONE
    assert ContextBudget(80, 100).action is ContextBudgetAction.PREPARE
    assert ContextBudget(90, 100).action is ContextBudgetAction.COMPACT
    emergency = ContextBudget(101, 100, exact=True)
    assert emergency.action is ContextBudgetAction.EMERGENCY
    assert emergency.over_budget is True
    assert emergency.to_dict()["exact"] is True


@pytest.mark.asyncio
async def test_archive_manifest_is_content_free_and_round_trips(tmp_path) -> None:
    archive = ArtifactContextArchive(LocalArtifactStore(tmp_path / "artifacts"))
    segment = await archive.archive_messages(
        _messages(),
        scope=_scope(),
        sequence=1,
        trajectory={"run_ids": ["run-1", "run-2"]},
    )
    checkpoint = _checkpoint(segment)
    manifest = ContextManifest(scope=_scope()).append(segment, checkpoint)
    payload = manifest.to_dict()

    assert "Glacier alarm" not in str(payload)
    assert "Glacier response remains active" not in str(payload)
    assert payload["checkpoints"][0]["summary_digest"].startswith("sha256:")
    assert payload["revision"] == 1
    assert ContextManifest.from_dict(payload) == manifest
    assert segment.artifact.purpose == "context_trajectory"
    assert segment.artifact.scope == _scope().artifact_scope
    assert manifest.artifact_storage_keys == (segment.artifact.storage_key,)
    assert manifest.artifact_ids == (segment.artifact.artifact_id,)


@pytest.mark.asyncio
async def test_search_is_scoped_ranked_filtered_and_reports_source(tmp_path) -> None:
    archive = ArtifactContextArchive(LocalArtifactStore(tmp_path / "artifacts"))
    segment = await archive.archive_messages(_messages(), scope=_scope(), sequence=1)
    manifest = ContextManifest(scope=_scope()).append(segment, _checkpoint(segment))

    hits = await archive.search(manifest, "glacier alarm", scope=_scope())
    tool_hits = await archive.search(
        manifest,
        "glacier",
        scope=_scope(),
        kinds=[ContextItemKind.TOOL_RESULT],
    )

    assert hits[0].kind is ContextItemKind.TOOL_RESULT
    assert hits[0].source is ContextSource.TRAJECTORY
    assert "03:14" in hits[0].excerpt
    assert [hit.item_id for hit in tool_hits] == [segment.items[3].item_id]
    assert await archive.search(manifest, "not-present", scope=_scope()) == ()


@pytest.mark.asyncio
async def test_search_deduplicates_repeated_items_and_bounds_query_bytes(tmp_path) -> None:
    archive = ArtifactContextArchive(LocalArtifactStore(tmp_path / "artifacts"))
    first = await archive.archive_messages(_messages(), scope=_scope(), sequence=1)
    second = await archive.archive_messages(_messages(), scope=_scope(), sequence=2)
    manifest = ContextManifest(scope=_scope()).append(first, _checkpoint(first))
    manifest = manifest.append(second, _checkpoint(second))

    hits = await archive.search(manifest, "glacier alarm", scope=_scope())

    assert [hit.item_id for hit in hits].count(first.items[3].item_id) == 1
    with pytest.raises(ValueError, match="4096"):
        await archive.search(manifest, "x" * 4_097, scope=_scope())


@pytest.mark.asyncio
async def test_search_keeps_only_latest_identical_carried_continuation_value(tmp_path) -> None:
    archive = ArtifactContextArchive(LocalArtifactStore(tmp_path / "artifacts"))
    continuation = ContextContinuationRecord(
        summary="Recovery remains active.",
        goal="Restore glacier telemetry safely.",
    )
    first = await archive.archive_messages(
        _messages(),
        scope=_scope(),
        sequence=1,
        continuation=continuation,
        continuation_origin="source_bound_initial_user_goal",
    )
    second = await archive.archive_messages(
        _messages(),
        scope=_scope(),
        sequence=2,
        continuation=continuation,
        continuation_origin="checkpoint_carry_forward",
        continuation_sources={
            ("goal", 0): {"source_item_id": first.items[-1].item_id},
        },
    )
    manifest = ContextManifest(scope=_scope()).append(first, _checkpoint(first))
    manifest = manifest.append(second, _checkpoint(second))

    hits = await archive.search(
        manifest,
        "restore glacier telemetry",
        scope=_scope(),
        limit=100,
        kinds=[ContextItemKind.GOAL],
    )

    assert [hit.item_id for hit in hits] == [second.items[-1].item_id]


@pytest.mark.asyncio
async def test_cross_scope_search_and_rehydration_fail_closed(tmp_path) -> None:
    archive = ArtifactContextArchive(LocalArtifactStore(tmp_path / "artifacts"))
    segment = await archive.archive_messages(_messages(), scope=_scope(), sequence=1)
    manifest = ContextManifest(scope=_scope()).append(segment, _checkpoint(segment))
    wrong = _scope("session-other")

    with pytest.raises(ContextScopeError):
        await archive.search(manifest, "glacier", scope=wrong)
    with pytest.raises(ContextScopeError):
        await archive.rehydrate(manifest, [segment.items[0].item_id], scope=wrong)


@pytest.mark.asyncio
async def test_rehydration_preserves_requested_order_and_provenance(tmp_path) -> None:
    archive = ArtifactContextArchive(LocalArtifactStore(tmp_path / "artifacts"))
    segment = await archive.archive_messages(_messages(), scope=_scope(), sequence=1)
    manifest = ContextManifest(scope=_scope()).append(segment, _checkpoint(segment))
    selected = [segment.items[3].item_id, segment.items[1].item_id]

    result = await archive.rehydrate(manifest, selected, scope=_scope())

    assert [item.item_id for item in result.items] == selected
    assert result.sources == (ContextSource.TRAJECTORY, ContextSource.ARTIFACT)
    assert result.injected is False
    assert result.items[0].provenance["message_id"] == "m4"


@pytest.mark.asyncio
async def test_rehydration_missing_item_and_budget_are_typed(tmp_path) -> None:
    archive = ArtifactContextArchive(LocalArtifactStore(tmp_path / "artifacts"))
    segment = await archive.archive_messages(_messages(), scope=_scope(), sequence=1)
    manifest = ContextManifest(scope=_scope()).append(segment, _checkpoint(segment))
    unknown = f"context-item:v1:{_scope().digest}:{'0' * 64}"

    with pytest.raises(ContextItemNotFoundError):
        await archive.rehydrate(manifest, [unknown], scope=_scope())
    with pytest.raises(Exception) as over_budget:
        await archive.rehydrate(
            manifest,
            [segment.items[3].item_id],
            scope=_scope(),
            max_tokens=1,
        )
    assert getattr(over_budget.value, "code", None) == "CONTEXT_REHYDRATION_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_manifest_enforces_sequence_and_bound(tmp_path) -> None:
    archive = ArtifactContextArchive(LocalArtifactStore(tmp_path / "artifacts"))
    segment = await archive.archive_messages(_messages(), scope=_scope(), sequence=1)
    manifest = ContextManifest(scope=_scope()).append(segment, _checkpoint(segment))
    second = await archive.archive_messages(_messages(), scope=_scope(), sequence=2)
    wrong_checkpoint = ContextCheckpoint.create(
        scope=_scope(),
        sequence=1,
        segment_id=second.segment_id,
        summary="wrong sequence",
        retained_item_ids=(),
        before_tokens=10,
        after_tokens=2,
    )

    with pytest.raises(ContextManifestConflictError):
        manifest.append(second, wrong_checkpoint)
    with pytest.raises(ContextManifestLimitError):
        manifest.append(second, _checkpoint(second), max_segments=1)


@pytest.mark.asyncio
async def test_loaded_segment_is_verified_against_manifest(tmp_path) -> None:
    archive = ArtifactContextArchive(LocalArtifactStore(tmp_path / "artifacts"))
    segment = await archive.archive_messages(_messages(), scope=_scope(), sequence=1)
    entry = ArchivedContextSegment.from_segment(segment)
    corrupt = ArchivedContextSegment(
        segment_id=entry.segment_id,
        scope=entry.scope,
        sequence=entry.sequence,
        artifact=entry.artifact,
        total_tokens=entry.total_tokens + 1,
        source_tokens=entry.source_tokens + 1,
        item_ids=entry.item_ids,
    )
    corrupt_checkpoint = ContextCheckpoint.create(
        scope=_scope(),
        sequence=1,
        segment_id=segment.segment_id,
        summary="still content free in the manifest",
        retained_item_ids=(),
        before_tokens=segment.source_tokens + 1,
        after_tokens=1,
    )
    manifest = ContextManifest(
        scope=_scope(),
        revision=1,
        segments=(corrupt,),
        checkpoints=(ArchivedContextCheckpoint.from_checkpoint(corrupt_checkpoint),),
    )

    with pytest.raises(Exception) as failure:
        await archive.search(manifest, "glacier", scope=_scope())
    assert getattr(failure.value, "code", None) == "CONTEXT_COMPACTION_QUALITY_FAILED"


def test_context_validation_rejects_unsafe_unbounded_shapes(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ValueError):
        ContextScope(session_id="")
    with pytest.raises(ValueError):
        ContextBudget(1, 0)
    with pytest.raises(ValueError):
        ArtifactContextArchive(store, max_items_per_segment=0)
    with pytest.raises(ValueError):
        ArtifactContextArchive(store, max_rehydrate_tokens=0)
