"""Durable provider-output segmentation, authorization, and replay contracts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agnoclaw.runtime import (
    ArtifactScope,
    HarnessError,
    HarnessRun,
    LocalArtifactStore,
    RunOutputSegmentError,
)
from agnoclaw.runtime.lifecycle import RunSnapshot
from agnoclaw.runtime.output_segments import (
    OUTPUT_SEGMENT_EVENT_TYPE,
    DurableOutputSegmentWriter,
)
from agnoclaw.runtime.store import RunOwner, RuntimeEventInput, SQLiteRuntimeStore


def _now() -> str:
    return datetime.now(UTC).isoformat()


@pytest.mark.asyncio
async def test_output_writer_commits_bounded_gap_free_segments_and_replays_after_reopen(tmp_path):
    database = tmp_path / "runtime.db"
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    owner = RunOwner(tenant_id="tenant-1", user_id="user-1")
    store = SQLiteRuntimeStore(database)
    store.create_run(
        RunSnapshot(run_id="run-output", tenant_id=owner.tenant_id, user_id=owner.user_id)
    )
    writer = DurableOutputSegmentWriter(
        run_id="run-output",
        attempt_id="attempt-1",
        owner=owner,
        store=store,
        artifact_store=artifacts,
        max_chars=5,
        max_deltas=2,
    )

    await writer.add("ab")
    await writer.add("cd")
    await writer.add("123456")
    await writer.finish()

    reopened = SQLiteRuntimeStore(database)
    run = HarnessRun(
        run_id="run-output",
        store=reopened,
        artifact_store=artifacts,
        owner=owner,
    )
    segments = [item async for item in run.output(follow=False)]

    assert [item.segment_sequence for item in segments] == [1, 2, 3]
    assert [item.content for item in segments] == ["abcd", "12345", "6"]
    assert [item.delta_count for item in segments] == [2, 1, 1]
    assert "".join(item.content for item in segments) == "abcd123456"
    resumed = [item async for item in run.output(after=segments[0].cursor, follow=False)]
    assert [item.segment_sequence for item in resumed] == [2, 3]
    ledger_text = str([event.to_dict() for event in reopened.list_events("run-output")])
    assert "abcd123456" not in ledger_text
    assert len(reopened.list_artifacts("run-output", owner=owner)) == 3


@pytest.mark.asyncio
async def test_runtime_event_artifact_commit_is_atomic_bound_and_idempotent(tmp_path):
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    owner = RunOwner(tenant_id="tenant-1", user_id="user-1")
    store.create_run(RunSnapshot(run_id="run-1", tenant_id="tenant-1", user_id="user-1"))
    reference = await artifacts.stage_json(
        {"content": "secret"},
        scope=ArtifactScope(run_id="run-1", tenant_id="tenant-1", user_id="user-1"),
        purpose="run.output.segment",
    )
    proposed = RuntimeEventInput(
        event_id="evt-output-1",
        run_id="run-1",
        event_type=OUTPUT_SEGMENT_EVENT_TYPE,
        occurred_at=_now(),
        payload={"artifact_id": reference.artifact_id},
    )

    first = store.append_runtime_event(
        proposed,
        owner=owner,
        artifact_reference=reference,
    )
    repeated = store.append_runtime_event(
        proposed,
        owner=owner,
        artifact_reference=reference,
    )

    assert first.appended is True
    assert repeated.idempotent is True
    assert store.get_artifact(reference.artifact_id, owner=owner) == reference
    with pytest.raises(HarnessError) as mismatch:
        store.append_runtime_event(
            RuntimeEventInput(
                event_id="evt-output-wrong",
                run_id="run-1",
                event_type=OUTPUT_SEGMENT_EVENT_TYPE,
                occurred_at=_now(),
                payload={"artifact_id": "different"},
            ),
            owner=owner,
            artifact_reference=reference,
        )
    assert mismatch.value.code == "ARTIFACT_EVENT_MISMATCH"


@pytest.mark.asyncio
async def test_runtime_event_artifact_reference_rolls_back_on_injected_fault(tmp_path):
    def fail(stage: str) -> None:
        if stage == "runtime_event.after_artifact":
            raise RuntimeError("injected")

    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    store = SQLiteRuntimeStore(tmp_path / "runtime.db", fault_injector=fail)
    store.create_run(RunSnapshot(run_id="run-fault"))
    reference = await artifacts.stage_json(
        {"content": "staged-only"},
        scope=ArtifactScope(run_id="run-fault"),
        purpose="run.output.segment",
    )
    proposed = RuntimeEventInput(
        event_id="evt-output-fault",
        run_id="run-fault",
        event_type=OUTPUT_SEGMENT_EVENT_TYPE,
        occurred_at=_now(),
        payload={"artifact_id": reference.artifact_id},
    )

    with pytest.raises(RuntimeError, match="injected"):
        store.append_runtime_event(proposed, artifact_reference=reference)

    assert store.list_artifacts("run-fault") == []
    assert [event.event_type for event in store.list_events("run-fault")] == ["run.created"]


@pytest.mark.asyncio
async def test_output_replay_rejects_artifact_event_binding_drift(tmp_path):
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(RunSnapshot(run_id="run-drift"))
    reference = await artifacts.stage_json(
        {
            "schema_version": "1.0",
            "run_id": "run-drift",
            "segment_sequence": 9,
            "content": "wrong-sequence",
            "delta_count": 1,
        },
        scope=ArtifactScope(run_id="run-drift"),
        purpose="run.output.segment",
    )
    store.append_runtime_event(
        RuntimeEventInput(
            event_id="evt-output-drift",
            run_id="run-drift",
            event_type=OUTPUT_SEGMENT_EVENT_TYPE,
            occurred_at=_now(),
            payload={
                "schema_version": "1.0",
                "segment_sequence": 1,
                "artifact_id": reference.artifact_id,
                "content_chars": len("wrong-sequence"),
                "delta_count": 1,
            },
        ),
        artifact_reference=reference,
    )
    run = HarnessRun(run_id="run-drift", store=store, artifact_store=artifacts)

    with pytest.raises(RunOutputSegmentError) as invalid:
        _ = [item async for item in run.output(follow=False)]
    assert invalid.value.details["reason"] == "artifact_binding"
