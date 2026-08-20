"""Governed, owner-exact dead-letter inspection and replay contracts."""

from __future__ import annotations

import asyncio
import dataclasses
import threading

import pytest

from agnoclaw import (
    DEAD_LETTER_AUDIT_SCOPE,
    DEAD_LETTER_INSPECT_SCOPE,
    DEAD_LETTER_REQUEUE_SCOPE,
    DeadLetterAdminAuthorizationError,
    DeadLetterAdminCursorError,
    ExecutionContext,
    OutboxDeadLetterConflictError,
    OutboxDeadLetterMutationConflictError,
    RunOwner,
    RuntimeDeadLetterAdmin,
    SQLiteRuntimeStore,
)
from agnoclaw.runtime.lifecycle import RunSnapshot
from agnoclaw.runtime.security import IdentitySource
from agnoclaw.runtime.store import RUNTIME_SCHEMA_VERSION

ALL_SCOPES = (
    DEAD_LETTER_INSPECT_SCOPE,
    DEAD_LETTER_REQUEUE_SCOPE,
    DEAD_LETTER_AUDIT_SCOPE,
)


def _context(
    *,
    tenant_id: str | None = "tenant-a",
    user_id: str | None = "operator",
    scopes: tuple[str, ...] = ALL_SCOPES,
    identity_source: IdentitySource = IdentitySource.TRUSTED_HOST,
) -> ExecutionContext:
    return ExecutionContext.create(
        user_id=user_id,
        session_id="admin-session",
        workspace_id="admin-workspace",
        tenant_id=tenant_id,
        roles=("runtime-operator",),
        scopes=scopes,
        identity_source=identity_source,
    )


def _quarantine(
    store: SQLiteRuntimeStore,
    *,
    run_id: str,
    tenant_id: str | None = "tenant-a",
    user_id: str | None = "alice",
):
    store.create_run(
        RunSnapshot(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=f"session-{run_id}",
            metadata={"private_marker": f"secret-{run_id}"},
        )
    )
    leased = store.lease_outbox(owner=f"worker-{run_id}", limit=1)
    assert len(leased) == 1 and leased[0].lease_token is not None
    return store.dead_letter_outbox(
        outbox_id=leased[0].outbox_id,
        lease_token=leased[0].lease_token,
        reason_code="export_failed",
    )


@pytest.mark.asyncio
async def test_admin_fails_closed_before_store_access(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    admin = RuntimeDeadLetterAdmin(store)
    target = RunOwner("tenant-a", "alice")
    denied = (
        _context(scopes=()),
        _context(identity_source=IdentitySource.REQUEST_PATH_BODY),
        _context(user_id=None),
        _context(tenant_id="tenant-b"),
    )

    for context in denied:
        with pytest.raises(DeadLetterAdminAuthorizationError) as error:
            await admin.inspect(
                context=context,
                owner=target,
                reason_code="incident_review",
            )
        assert error.value.details == {"required_scope": DEAD_LETTER_INSPECT_SCOPE}

    with pytest.raises(DeadLetterAdminAuthorizationError):
        await admin.inspect(
            context=_context(tenant_id=None),
            owner=RunOwner(None, "someone-else"),
            reason_code="incident_review",
        )
    assert store.list_dead_letter_audit(owner=target) == []


@pytest.mark.asyncio
async def test_inspection_is_exactly_owner_bound_audited_and_cursor_scoped(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    alice_first = _quarantine(store, run_id="alice-1")
    alice_second = _quarantine(store, run_id="alice-2")
    _quarantine(store, run_id="bob-1", user_id="bob")
    admin = RuntimeDeadLetterAdmin(store)
    context = _context()
    alice = RunOwner("tenant-a", "alice")
    bob = RunOwner("tenant-a", "bob")

    first = await admin.inspect(
        context=context,
        owner=alice,
        reason_code="incident_review",
        limit=1,
    )
    assert first.items == (alice_first,)
    assert first.next_cursor is not None
    assert first.audit.result_count == 1
    assert first.audit.requested_after_outbox_id == 0
    assert first.audit.requested_limit == 1
    assert first.audit.first_outbox_id == alice_first.outbox_id
    assert first.audit.owner == alice

    second = await admin.inspect(
        context=context,
        owner=alice,
        reason_code="incident_review",
        cursor=first.next_cursor,
        limit=1,
    )
    assert second.items == (alice_second,)
    assert second.next_cursor is not None

    with pytest.raises(DeadLetterAdminCursorError):
        await admin.inspect(
            context=context,
            owner=bob,
            reason_code="incident_review",
            cursor=first.next_cursor,
        )

    empty = await admin.inspect(
        context=context,
        owner=alice,
        reason_code="incident_review_complete",
        cursor=second.next_cursor,
        limit=1,
    )
    assert empty.items == () and empty.next_cursor is None
    assert empty.audit.result_count == 0
    assert empty.audit.requested_after_outbox_id == alice_second.outbox_id
    assert empty.audit.requested_limit == 1
    assert [record.result_count for record in store.list_dead_letter_audit(owner=alice)] == [
        1,
        1,
        0,
    ]


@pytest.mark.asyncio
async def test_audit_is_content_minimized_and_history_reads_do_not_self_audit(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    _quarantine(store, run_id="private-run")
    admin = RuntimeDeadLetterAdmin(store)
    context = _context(user_id="private-operator")
    owner = RunOwner("tenant-a", "alice")

    page = await admin.inspect(
        context=context,
        owner=owner,
        reason_code="privacy_safe_review",
    )
    audit_value = dataclasses.asdict(page.audit)
    row = store._connection.execute(
        "SELECT * FROM runtime_dead_letter_audit WHERE audit_id = ?",
        (page.audit.audit_id,),
    ).fetchone()

    assert page.audit.operator_digest.startswith("sha256:")
    assert page.audit.authority_digest.startswith("sha256:")
    assert "private-operator" not in repr(audit_value)
    assert "secret-private-run" not in repr(audit_value)
    assert "event_json" not in row.keys()
    assert set(audit_value) == {
        "audit_sequence",
        "audit_id",
        "action",
        "owner",
        "operator_digest",
        "authority_digest",
        "reason_code",
        "requested_after_outbox_id",
        "requested_limit",
        "result_count",
        "first_outbox_id",
        "last_outbox_id",
        "outbox_id",
        "run_id",
        "expected_dead_lettered_at",
        "delay_seconds",
        "mutation_id",
        "mutation_digest",
        "created_at",
    }

    history = await admin.audit_history(context=context, owner=owner, limit=1)
    repeated = await admin.audit_history(context=context, owner=owner, limit=1)
    assert history.items == repeated.items == (page.audit,)
    assert len(store.list_dead_letter_audit(owner=owner)) == 1


@pytest.mark.asyncio
async def test_requeue_is_exact_idempotent_and_records_one_mutation(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    dead_letter = _quarantine(store, run_id="retry-run")
    admin = RuntimeDeadLetterAdmin(store)
    context = _context()
    owner = RunOwner("tenant-a", "alice")
    await admin.inspect(context=context, owner=owner, reason_code="incident_review")

    first = await admin.requeue(
        dead_letter,
        context=context,
        owner=owner,
        reason_code="operator_retry",
        mutation_id="requeue-retry-run-1",
    )
    repeated = await admin.requeue(
        dead_letter,
        context=context,
        owner=owner,
        reason_code="operator_retry",
        mutation_id="requeue-retry-run-1",
    )

    assert not first.idempotent and repeated.idempotent
    assert first.audit == repeated.audit
    assert first.audit.outbox_id == dead_letter.outbox_id
    assert first.audit.run_id == "retry-run"
    assert first.audit.expected_dead_lettered_at == dead_letter.dead_lettered_at
    assert store.lease_outbox(owner="recovered")[0].outbox_id == dead_letter.outbox_id

    with pytest.raises(OutboxDeadLetterMutationConflictError):
        await admin.requeue(
            dead_letter,
            context=context,
            owner=owner,
            reason_code="changed_reason",
            mutation_id="requeue-retry-run-1",
        )
    with pytest.raises(OutboxDeadLetterMutationConflictError):
        await admin.requeue(
            dead_letter,
            context=_context(user_id="different-operator"),
            owner=owner,
            reason_code="operator_retry",
            mutation_id="requeue-retry-run-1",
        )
    with pytest.raises(OutboxDeadLetterConflictError):
        await admin.requeue(
            dead_letter,
            context=context,
            owner=owner,
            reason_code="operator_retry",
            mutation_id="requeue-retry-run-2",
        )
    assert [record.action.value for record in store.list_dead_letter_audit(owner=owner)] == [
        "inspected",
        "requeued",
    ]


@pytest.mark.asyncio
async def test_requeue_rejects_cross_owner_item_and_global_mutation_collision(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    alice_item = _quarantine(store, run_id="alice-run")
    bob_item = _quarantine(store, run_id="bob-run", user_id="bob")
    admin = RuntimeDeadLetterAdmin(store)
    context = _context()
    alice = RunOwner("tenant-a", "alice")
    bob = RunOwner("tenant-a", "bob")

    with pytest.raises(OutboxDeadLetterConflictError):
        await admin.requeue(
            alice_item,
            context=context,
            owner=bob,
            reason_code="operator_retry",
            mutation_id="wrong-owner",
        )
    await admin.requeue(
        alice_item,
        context=context,
        owner=alice,
        reason_code="operator_retry",
        mutation_id="globally-unique-mutation",
    )
    with pytest.raises(OutboxDeadLetterMutationConflictError):
        await admin.requeue(
            bob_item,
            context=context,
            owner=bob,
            reason_code="operator_retry",
            mutation_id="globally-unique-mutation",
        )
    assert store.list_dead_letter_audit(owner=bob) == []


@pytest.mark.asyncio
async def test_requeue_fault_rolls_back_outbox_and_audit_together(tmp_path) -> None:
    fail_requeue = False

    def inject(stage: str) -> None:
        if fail_requeue and stage == "dead_letter_requeue_after_update":
            raise RuntimeError("injected requeue fault")

    store = SQLiteRuntimeStore(tmp_path / "runtime.db", fault_injector=inject)
    dead_letter = _quarantine(store, run_id="rollback-run")
    admin = RuntimeDeadLetterAdmin(store)
    context = _context()
    owner = RunOwner("tenant-a", "alice")
    await admin.inspect(context=context, owner=owner, reason_code="incident_review")

    fail_requeue = True
    with pytest.raises(RuntimeError, match="injected requeue fault"):
        await admin.requeue(
            dead_letter,
            context=context,
            owner=owner,
            reason_code="operator_retry",
            mutation_id="rollback-mutation",
        )
    assert [record.action.value for record in store.list_dead_letter_audit(owner=owner)] == [
        "inspected"
    ]
    assert store.lease_outbox(owner="must-remain-quarantined") == []

    fail_requeue = False
    recovered = await admin.requeue(
        dead_letter,
        context=context,
        owner=owner,
        reason_code="operator_retry",
        mutation_id="rollback-mutation",
    )
    assert not recovered.idempotent


@pytest.mark.asyncio
async def test_concurrent_same_mutation_converges_on_one_audit(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    dead_letter = _quarantine(store, run_id="concurrent-run")
    admin = RuntimeDeadLetterAdmin(store)
    context = _context()
    owner = RunOwner("tenant-a", "alice")

    decisions = await asyncio.gather(
        *(
            admin.requeue(
                dead_letter,
                context=context,
                owner=owner,
                reason_code="operator_retry",
                mutation_id="concurrent-mutation",
            )
            for _ in range(2)
        )
    )

    assert sorted(decision.idempotent for decision in decisions) == [False, True]
    assert decisions[0].audit == decisions[1].audit
    assert [record.action.value for record in store.list_dead_letter_audit(owner=owner)] == [
        "requeued"
    ]


@pytest.mark.asyncio
async def test_cancellation_waits_for_authoritative_commit_then_retry_is_idempotent(
    tmp_path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    should_block = False

    def inject(stage: str) -> None:
        if should_block and stage == "dead_letter_requeue_after_update":
            entered.set()
            assert release.wait(timeout=5)

    store = SQLiteRuntimeStore(tmp_path / "runtime.db", fault_injector=inject)
    dead_letter = _quarantine(store, run_id="cancel-run")
    admin = RuntimeDeadLetterAdmin(store)
    context = _context()
    owner = RunOwner("tenant-a", "alice")
    should_block = True
    task = asyncio.create_task(
        admin.requeue(
            dead_letter,
            context=context,
            owner=owner,
            reason_code="operator_retry",
            mutation_id="cancel-mutation",
        )
    )
    assert await asyncio.to_thread(entered.wait, 2)

    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    repeated = await admin.requeue(
        dead_letter,
        context=context,
        owner=owner,
        reason_code="operator_retry",
        mutation_id="cancel-mutation",
    )
    assert repeated.idempotent
    assert [record.action.value for record in store.list_dead_letter_audit(owner=owner)] == [
        "requeued"
    ]


@pytest.mark.asyncio
async def test_cancellation_remains_visible_when_authoritative_commit_rolls_back(
    tmp_path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    should_fail = False

    def inject(stage: str) -> None:
        if should_fail and stage == "dead_letter_requeue_after_update":
            entered.set()
            assert release.wait(timeout=5)
            raise RuntimeError("rollback after cancellation")

    store = SQLiteRuntimeStore(tmp_path / "runtime.db", fault_injector=inject)
    dead_letter = _quarantine(store, run_id="cancel-rollback-run")
    admin = RuntimeDeadLetterAdmin(store)
    context = _context()
    owner = RunOwner("tenant-a", "alice")
    should_fail = True
    task = asyncio.create_task(
        admin.requeue(
            dead_letter,
            context=context,
            owner=owner,
            reason_code="operator_retry",
            mutation_id="cancel-rollback-mutation",
        )
    )
    assert await asyncio.to_thread(entered.wait, 2)

    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.list_dead_letter_audit(owner=owner) == []
    assert store.lease_outbox(owner="must-remain-quarantined") == []

    should_fail = False
    recovered = await admin.requeue(
        dead_letter,
        context=context,
        owner=owner,
        reason_code="operator_retry",
        mutation_id="cancel-rollback-mutation",
    )
    assert not recovered.idempotent


@pytest.mark.asyncio
async def test_admin_rejects_invalid_bounds_cursors_and_safe_identifiers(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    dead_letter = _quarantine(store, run_id="validation-run")
    admin = RuntimeDeadLetterAdmin(store)
    context = _context()
    owner = RunOwner("tenant-a", "alice")

    for limit in (0, 101):
        with pytest.raises(ValueError, match="between 1 and 100"):
            await admin.inspect(
                context=context,
                owner=owner,
                reason_code="incident_review",
                limit=limit,
            )
        with pytest.raises(ValueError, match="between 1 and 100"):
            await admin.audit_history(context=context, owner=owner, limit=limit)
    for cursor in ("wrong-prefix", "dead_letter_v1_not-valid-base64"):
        with pytest.raises(DeadLetterAdminCursorError):
            await admin.inspect(
                context=context,
                owner=owner,
                reason_code="incident_review",
                cursor=cursor,
            )
    with pytest.raises(ValueError, match="safe lowercase"):
        await admin.inspect(
            context=context,
            owner=owner,
            reason_code="raw secret: do not persist",
        )
    with pytest.raises(ValueError, match="mutation_id"):
        await admin.requeue(
            dead_letter,
            context=context,
            owner=owner,
            reason_code="operator_retry",
            mutation_id="contains spaces",
        )
    with pytest.raises(ValueError, match="between 0 and 86400"):
        await admin.requeue(
            dead_letter,
            context=context,
            owner=owner,
            reason_code="operator_retry",
            mutation_id="valid-mutation",
            delay_seconds=-1,
        )


def test_schema_v9_migrates_audit_domain_without_losing_runtime_data(tmp_path) -> None:
    path = tmp_path / "runtime-v9.db"
    store = SQLiteRuntimeStore(path)
    store.create_run(
        RunSnapshot(
            run_id="preserved-run",
            tenant_id="tenant-a",
            user_id="alice",
            session_id="preserved-session",
        )
    )
    store._connection.execute("DROP INDEX runtime_dead_letter_audit_owner_idx")
    store._connection.execute("DROP TABLE runtime_dead_letter_audit")
    store._connection.execute("DELETE FROM runtime_schema_migrations WHERE version >= 10")
    store.close()

    migrated = SQLiteRuntimeStore(path)

    assert migrated.schema_version == RUNTIME_SCHEMA_VERSION
    assert migrated.get_run("preserved-run").user_id == "alice"
    table = migrated._connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("runtime_dead_letter_audit",),
    ).fetchone()
    assert table["name"] == "runtime_dead_letter_audit"
    assert [item.run_id for item in migrated.lease_outbox(owner="preserved-worker")] == [
        "preserved-run"
    ]


def test_admin_requires_the_complete_runtime_store_contract() -> None:
    with pytest.raises(TypeError, match="RuntimeStore"):
        RuntimeDeadLetterAdmin(object())  # type: ignore[arg-type]
