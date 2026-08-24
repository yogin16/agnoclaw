"""Exact serialization and trust-chain tests for request checkpoints."""

from __future__ import annotations

import math

import pytest

from agnoclaw.runtime.artifacts import LocalArtifactStore
from agnoclaw.runtime.checkpoints import (
    RuntimeRequestCheckpoint,
    canonical_request_value,
    execution_context_value,
    load_runtime_request_checkpoint,
    persist_runtime_request_checkpoint,
    runtime_request_digest,
    validate_recoverable_model_intent,
)
from agnoclaw.runtime.context import ExecutionContext
from agnoclaw.runtime.errors import HarnessError
from agnoclaw.runtime.gateway import OperationGateway
from agnoclaw.runtime.lifecycle import RunSnapshot
from agnoclaw.runtime.operations import (
    EffectClass,
    OperationIntent,
    OperationKind,
)
from agnoclaw.runtime.security import (
    AdmissionEnvelope,
    IdentityProvenance,
    IdentitySource,
    PrincipalIdentity,
)
from agnoclaw.runtime.store import RunOwner, SQLiteRuntimeStore

SPEC_DIGEST = "sha256:" + "a" * 64


def _context() -> ExecutionContext:
    identity = PrincipalIdentity(
        tenant_id="tenant-1",
        org_id="org-1",
        team_id="team-1",
        user_id="user-1",
        session_id="session-1",
        workspace_id="workspace-1",
        roles=("operator",),
        scopes=("runs:write",),
    )
    admission = AdmissionEnvelope(
        identity=identity,
        provenance=(
            IdentityProvenance(
                field="tenant_id",
                source=IdentitySource.AUTHENTICATED_CLAIMS,
                authoritative=True,
            ),
        ),
        request_id="request-1",
        trace_id="trace-1",
        client_metadata={"client": "web"},
        trusted_metadata={"risk": "low"},
    )
    return ExecutionContext.create(
        user_id=identity.user_id,
        session_id=identity.session_id,
        workspace_id=identity.workspace_id,
        tenant_id=identity.tenant_id,
        org_id=identity.org_id,
        team_id=identity.team_id,
        roles=identity.roles,
        scopes=identity.scopes,
        request_id=admission.request_id,
        trace_id=admission.trace_id,
        trusted_permission_tools=("read_file",),
        trusted_permission_categories=("filesystem_read",),
        metadata={"client": "web", "risk": "low"},
        identity_source=IdentitySource.AUTHENTICATED_CLAIMS,
        admission=admission,
    )


def _snapshot() -> RunSnapshot:
    return RunSnapshot(
        run_id="run-1",
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
    )


def test_checkpoint_round_trips_full_authority_context_and_request() -> None:
    checkpoint = RuntimeRequestCheckpoint.create(
        run_id="run-1",
        message="continue",
        context=_context(),
        kwargs={"learning_consent": True, "metadata": {"source": "test"}},
        harness_spec_digest=SPEC_DIGEST,
    )

    restored_checkpoint = RuntimeRequestCheckpoint.from_dict(checkpoint.to_dict())
    restored = restored_checkpoint.restore(
        snapshot=_snapshot(),
        harness_spec_digest=SPEC_DIGEST,
    )

    assert restored.message == "continue"
    assert restored.kwargs == {
        "learning_consent": True,
        "metadata": {"source": "test"},
    }
    assert execution_context_value(restored.context) == execution_context_value(_context())


def test_request_digest_binds_authority_even_without_admission_envelope() -> None:
    base = ExecutionContext.create(
        user_id="user-1",
        session_id="session-1",
        workspace_id="workspace-1",
        tenant_id="tenant-1",
        roles=("operator",),
    )
    changed = ExecutionContext.create(
        user_id="user-2",
        session_id="session-1",
        workspace_id="workspace-1",
        tenant_id="tenant-1",
        roles=("operator",),
    )

    first = runtime_request_digest(
        message="same",
        context=base,
        kwargs={},
        harness_spec_digest=SPEC_DIGEST,
    )
    second = runtime_request_digest(
        message="same",
        context=changed,
        kwargs={},
        harness_spec_digest=SPEC_DIGEST,
    )

    assert first != second


@pytest.mark.parametrize("value", [math.nan, math.inf, object()])
def test_canonical_request_rejects_opaque_or_non_finite_values(value) -> None:
    with pytest.raises(HarnessError) as failure:
        canonical_request_value({"unsafe": value})
    assert failure.value.code == "RUN_REQUEST_NOT_CANONICAL"


def test_checkpoint_detects_content_and_owner_scope_tampering() -> None:
    checkpoint = RuntimeRequestCheckpoint.create(
        run_id="run-1",
        message="continue",
        context=_context(),
        kwargs={},
        harness_spec_digest=SPEC_DIGEST,
    )
    modified = checkpoint.to_dict()
    modified["message"] = "different"

    with pytest.raises(HarnessError) as content_failure:
        RuntimeRequestCheckpoint.from_dict(modified).restore(
            snapshot=_snapshot(),
            harness_spec_digest=SPEC_DIGEST,
        )
    assert content_failure.value.code == "RUN_RECOVERY_REQUEST_MISMATCH"

    mismatched_scope = RunSnapshot(
        run_id="run-1",
        tenant_id="tenant-2",
        user_id="user-1",
        session_id="session-1",
    )
    with pytest.raises(HarnessError) as scope_failure:
        checkpoint.restore(
            snapshot=mismatched_scope,
            harness_spec_digest=SPEC_DIGEST,
        )
    assert scope_failure.value.code == "RUN_RECOVERY_CHECKPOINT_SCOPE_MISMATCH"


@pytest.mark.asyncio
async def test_persisted_checkpoint_loads_only_from_exact_operation_chain(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    snapshot = store.create_run(_snapshot()).snapshot
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    checkpoint = RuntimeRequestCheckpoint.create(
        run_id=snapshot.run_id,
        message="continue",
        context=_context(),
        kwargs={},
        harness_spec_digest=SPEC_DIGEST,
    )
    await persist_runtime_request_checkpoint(
        checkpoint,
        store=store,
        artifact_store=artifacts,
        worker_id="worker-1",
    )

    restored = await load_runtime_request_checkpoint(
        store=store,
        artifact_store=artifacts,
        snapshot=snapshot,
        owner=RunOwner(tenant_id="tenant-1", user_id="user-1"),
        harness_spec_digest=SPEC_DIGEST,
    )

    assert restored.message == "continue"
    assert restored.context.tenant_id == "tenant-1"


@pytest.mark.asyncio
async def test_checkpoint_loader_rejects_wrong_operation_target(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    snapshot = store.create_run(_snapshot()).snapshot
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    checkpoint = RuntimeRequestCheckpoint.create(
        run_id=snapshot.run_id,
        message="continue",
        context=_context(),
        kwargs={},
        harness_spec_digest=SPEC_DIGEST,
    )
    gateway = OperationGateway(
        store,
        worker_id="worker-1",
        artifact_store=artifacts,
        artifact_purpose="run_request_checkpoint",
        result_cache_size=0,
    )
    await gateway.execute(
        OperationIntent(
            operation_id="run-1:checkpoint:request:1",
            run_id="run-1",
            attempt_id="run-1:checkpoint:request:1",
            kind=OperationKind.CAPABILITY,
            target="untrusted.checkpoint.writer",
            request_digest=checkpoint.request_digest,
            effect_class=EffectClass.READ_ONLY,
            metadata={
                "schema_version": checkpoint.schema_version,
                "harness_spec_digest": checkpoint.harness_spec_digest,
            },
        ),
        checkpoint.to_dict,
    )

    with pytest.raises(HarnessError) as failure:
        await load_runtime_request_checkpoint(
            store=store,
            artifact_store=artifacts,
            snapshot=snapshot,
            owner=RunOwner(tenant_id="tenant-1", user_id="user-1"),
            harness_spec_digest=SPEC_DIGEST,
        )
    assert failure.value.code == "RUN_RECOVERY_CHECKPOINT_INVALID"


def test_planned_model_intent_must_match_certified_request_exactly() -> None:
    checkpoint = RuntimeRequestCheckpoint.create(
        run_id="run-1",
        message="continue",
        context=_context(),
        kwargs={},
        harness_spec_digest=SPEC_DIGEST,
    )
    store = SQLiteRuntimeStore(":memory:")
    store.create_run(_snapshot())
    planned = store.prepare_operation(
        OperationIntent(
            operation_id="run-1:model:1",
            run_id="run-1",
            attempt_id="run-1:attempt:1",
            kind=OperationKind.MODEL,
            target="custom:model",
            request_digest=checkpoint.request_digest,
            effect_class=EffectClass.NON_REPEATABLE,
            metadata={
                "harness_spec_digest": SPEC_DIGEST,
                "operation_ordinal": 1,
            },
        )
    ).record

    validate_recoverable_model_intent(
        planned,
        run_id="run-1",
        model_target="custom:model",
        request_digest=checkpoint.request_digest,
        harness_spec_digest=SPEC_DIGEST,
    )
    with pytest.raises(HarnessError) as failure:
        validate_recoverable_model_intent(
            planned,
            run_id="run-1",
            model_target="custom:other",
            request_digest=checkpoint.request_digest,
            harness_spec_digest=SPEC_DIGEST,
        )
    assert failure.value.code == "RUN_RECOVERY_MODEL_INTENT_MISMATCH"
