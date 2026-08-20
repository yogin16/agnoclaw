"""Artifact byte integrity, scope, encryption, and ledger-commit contracts."""

from __future__ import annotations

import base64
import hashlib

import pytest

from agnoclaw.runtime import (
    ArtifactCorruptError,
    ArtifactNotFoundError,
    ArtifactScope,
    ArtifactTenantRequiredError,
    ArtifactTooLargeError,
    KeyPurpose,
    KeyReference,
    LocalArtifactStore,
    SealedContent,
)
from agnoclaw.runtime.lifecycle import RunSnapshot
from agnoclaw.runtime.operations import (
    EffectClass,
    OperationIntent,
    OperationKind,
    OperationSettlement,
    OperationState,
)
from agnoclaw.runtime.store import RunOwner, SQLiteRuntimeStore


def _intent() -> OperationIntent:
    return OperationIntent(
        operation_id="operation-1",
        run_id="run-1",
        attempt_id="attempt-1",
        kind=OperationKind.MODEL,
        target="example:model",
        request_digest="sha256:request",
        effect_class=EffectClass.NON_REPEATABLE,
    )


@pytest.mark.asyncio
async def test_local_store_is_content_addressed_paged_and_deterministic(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts", max_page_bytes=8)
    scope = ArtifactScope(run_id="run-1", tenant_id="tenant-1", user_id="user-1")

    first = await store.stage_json(
        {"answer": 42, "values": [1, 2, 3]},
        scope=scope,
        purpose="operation_result",
    )
    repeated = await store.stage_json(
        {"values": [1, 2, 3], "answer": 42},
        scope=scope,
        purpose="operation_result",
    )

    assert repeated.artifact_id == first.artifact_id
    assert repeated.storage_key == first.storage_key
    assert repeated.storage_identity_digest == first.storage_identity_digest
    assert first.artifact_id.startswith(f"artifact:v1:{scope.digest}:")
    chunks = []
    offset = 0
    while True:
        chunk = await store.read(first, offset=offset, limit=8)
        chunks.append(chunk.data)
        if chunk.complete:
            break
        assert chunk.next_offset is not None
        offset = chunk.next_offset
    assert b"".join(chunks) == (b'{"answer":42,"values":[1,2,3]}')
    assert await store.load_json(first) == {"answer": 42, "values": [1, 2, 3]}


@pytest.mark.asyncio
async def test_artifact_size_and_page_bounds_fail_typed(tmp_path):
    store = LocalArtifactStore(
        tmp_path / "artifacts",
        max_artifact_bytes=16,
        max_page_bytes=8,
    )
    scope = ArtifactScope(run_id="run-1")
    with pytest.raises(ArtifactTooLargeError):
        await store.stage_json("x" * 32, scope=scope, purpose="result")
    reference = await store.stage_json("ok", scope=scope, purpose="result")
    with pytest.raises(Exception) as invalid:
        await store.read(reference, limit=9)
    assert getattr(invalid.value, "code", None) == "ARTIFACT_RANGE_INVALID"


@pytest.mark.asyncio
async def test_missing_and_tampered_committed_bytes_are_corruption(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    reference = await store.stage_json(
        {"secret": "evidence"},
        scope=ArtifactScope(run_id="run-1"),
        purpose="result",
    )
    path = store._objects / reference.storage_key
    path.write_bytes(b"changed")
    with pytest.raises(ArtifactCorruptError) as restage:
        await store.stage_json(
            {"secret": "evidence"},
            scope=ArtifactScope(run_id="run-1"),
            purpose="result",
        )
    assert restage.value.details["reason"] == "existing_object_mismatch"
    with pytest.raises(ArtifactCorruptError) as corrupt:
        await store.load_json(reference)
    assert corrupt.value.details["reason"] == "stored_size_mismatch"

    path.unlink()
    with pytest.raises(ArtifactCorruptError) as missing:
        await store.load_json(reference)
    assert missing.value.details["reason"] == "missing_bytes"


@pytest.mark.asyncio
async def test_key_provider_seals_bytes_and_ledger_metadata_has_no_ciphertext(tmp_path):
    class ReversingProvider:
        def seal(self, plaintext, *, tenant_id, purpose, aad):
            assert purpose is KeyPurpose.ARTIFACT
            return SealedContent(
                key=KeyReference("artifact-key", "7", tenant_id, purpose),
                algorithm="test-reverse",
                nonce_b64="bm9uY2U=",
                ciphertext_b64=base64.b64encode(plaintext[::-1]).decode("ascii"),
                aad_digest=f"sha256:{hashlib.sha256(aad).hexdigest()}",
            )

        def unseal(self, content, *, aad):
            assert content.aad_digest == f"sha256:{hashlib.sha256(aad).hexdigest()}"
            return base64.b64decode(content.ciphertext_b64)[::-1]

        def destroy(self, key):
            del key

    store = LocalArtifactStore(tmp_path / "artifacts", key_provider=ReversingProvider())
    scope = ArtifactScope(run_id="run-1", tenant_id="tenant-1", user_id="user-1")
    reference = await store.stage_json(
        {"value": "plaintext-never-in-reference"},
        scope=scope,
        purpose="result",
    )

    assert reference.protection is not None
    assert "plaintext-never-in-reference" not in str(reference.to_dict())
    assert await store.load_json(reference) == {"value": "plaintext-never-in-reference"}

    unscoped = ArtifactScope(run_id="run-2")
    with pytest.raises(ArtifactTenantRequiredError):
        await store.stage_json("value", scope=unscoped, purpose="result")


@pytest.mark.asyncio
async def test_gc_removes_only_old_unreferenced_objects(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    scope = ArtifactScope(run_id="run-1")
    live = await store.stage_json("live", scope=scope, purpose="result")
    orphan = await store.stage_json("orphan", scope=scope, purpose="result")
    interrupted_stage = store._staging / "interrupted.stage"
    interrupted_stage.write_bytes(b"uncommitted")

    decision = await store.garbage_collect(
        [live.storage_key],
        grace_seconds=0,
    )

    assert decision.deleted == 2
    assert await store.load_json(live) == "live"
    with pytest.raises(ArtifactCorruptError):
        await store.load_json(orphan)


@pytest.mark.asyncio
async def test_artifact_reference_commits_atomically_with_operation_settlement(tmp_path):
    bytes_store = LocalArtifactStore(tmp_path / "artifacts")
    runtime = SQLiteRuntimeStore(tmp_path / "runtime.db")
    runtime.create_run(
        RunSnapshot(
            run_id="run-1",
            tenant_id="tenant-1",
            user_id="user-1",
        )
    )
    prepared = runtime.prepare_operation(_intent())
    dispatching = runtime.begin_operation(
        "operation-1",
        mutation_id="dispatch",
        expected_revision=prepared.record.revision,
        worker_id="worker-1",
        fence_token=1,
    )
    reference = await bytes_store.stage_json(
        {"content": "durable"},
        scope=ArtifactScope(
            run_id="run-1",
            tenant_id="tenant-1",
            user_id="user-1",
        ),
        purpose="operation_result",
    )

    runtime.settle_operation(
        "operation-1",
        mutation_id="settle",
        expected_revision=dispatching.record.revision,
        fence_token=1,
        settlement=OperationSettlement(
            state=OperationState.SUCCEEDED,
            result_reference=reference.artifact_id,
        ),
        artifact_reference=reference,
    )

    assert (
        runtime.get_artifact(
            reference.artifact_id,
            owner=RunOwner(tenant_id="tenant-1", user_id="user-1"),
        )
        == reference
    )
    with pytest.raises(ArtifactNotFoundError):
        runtime.get_artifact(
            reference.artifact_id,
            owner=RunOwner(tenant_id="other", user_id="user-1"),
        )
    assert runtime.list_artifact_storage_keys() == [reference.storage_key]
    assert [event.event_type for event in runtime.list_events("run-1")] == [
        "run.created",
        "operation.planned",
        "operation.dispatching",
        "artifact.committed",
        "operation.settled",
    ]


@pytest.mark.asyncio
async def test_artifact_reference_fault_rolls_back_but_leaves_gc_safe_staging(tmp_path):
    def fail(stage: str) -> None:
        if stage == "artifact.after_reference":
            raise RuntimeError("injected artifact-reference crash")

    bytes_store = LocalArtifactStore(tmp_path / "artifacts")
    runtime = SQLiteRuntimeStore(tmp_path / "runtime.db", fault_injector=fail)
    runtime.create_run(RunSnapshot(run_id="run-1"))
    runtime.prepare_operation(_intent())
    dispatching = runtime.begin_operation(
        "operation-1",
        mutation_id="dispatch",
        expected_revision=0,
        worker_id="worker-1",
        fence_token=1,
    )
    reference = await bytes_store.stage_json(
        {"content": "staged"},
        scope=ArtifactScope(run_id="run-1"),
        purpose="operation_result",
    )

    with pytest.raises(RuntimeError, match="artifact-reference crash"):
        runtime.settle_operation(
            "operation-1",
            mutation_id="settle",
            expected_revision=dispatching.record.revision,
            fence_token=1,
            settlement=OperationSettlement(
                state=OperationState.SUCCEEDED,
                result_reference=reference.artifact_id,
            ),
            artifact_reference=reference,
        )

    assert runtime.get_operation("operation-1").state is OperationState.DISPATCHING
    with pytest.raises(ArtifactNotFoundError):
        runtime.get_artifact(reference.artifact_id)
    assert runtime.list_artifact_storage_keys() == []
    collected = await bytes_store.garbage_collect([], grace_seconds=0)
    assert collected.deleted == 1
