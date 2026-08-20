"""Real-service conformance tests for PostgresRuntimeStore."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from agnoclaw.runtime import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalState,
    ArtifactScope,
    AuthorizationGrant,
    ChildRunBudget,
    ChildRunContractError,
    ChildRunSpec,
    GrantScope,
    LocalArtifactStore,
    PostgresRuntimeStore,
    RuntimeStoreConnectionLostError,
    RuntimeStoreReadOnlyError,
)
from agnoclaw.runtime.leases import RuntimeLeaseUnavailableError
from agnoclaw.runtime.lifecycle import (
    LifecycleTransition,
    RunNotFoundError,
    RunRevisionConflictError,
    RunSnapshot,
    RunState,
    TransitionKind,
)
from agnoclaw.runtime.operations import (
    EffectClass,
    OperationIntent,
    OperationKind,
    OperationReconciliation,
    OperationReconciliationVerdict,
    OperationSettlement,
    OperationState,
)
from agnoclaw.runtime.recovery import inspect_child_recovery
from agnoclaw.runtime.scheduler import (
    RuntimeSchedulerBackend,
    SchedulerJob,
    SchedulerLeaseLostError,
    scheduler_jitter_seconds,
    scheduler_occurrence_id,
)
from agnoclaw.runtime.store import (
    RUNTIME_SCHEMA_VERSION,
    EventCursorExpiredError,
    OperationNotFoundError,
    OperationRevisionConflictError,
    OutboxDeadLetterConflictError,
    OutboxDeadLetterMutationConflictError,
    OutboxLeaseError,
    RunOwner,
    RuntimeEventIdempotencyConflictError,
    RuntimeEventInput,
    StartIdempotencyConflictError,
    TerminalRecord,
)

POSTGRES_URL = os.getenv("AGNOCLAW_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="AGNOCLAW_TEST_POSTGRES_URL is not configured",
)


def _snapshot(run_id: str) -> RunSnapshot:
    return RunSnapshot(
        run_id=run_id,
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
    )


@pytest.fixture
def store():
    assert POSTGRES_URL is not None
    value = PostgresRuntimeStore(
        POSTGRES_URL,
        min_pool_size=1,
        max_pool_size=4,
        max_waiting=16,
    )
    with value._transaction() as conn:
        conn.execute(
            """
            TRUNCATE runtime_scheduler_runs, runtime_scheduler_jobs,
                     runtime_dead_letter_audit, runtime_runs, runtime_schema_migrations
            RESTART IDENTITY CASCADE
            """
        )
    value.migrate()
    yield value
    value.close()


def test_postgres_read_only_store_skips_migration_and_rejects_mutation(store):
    assert POSTGRES_URL is not None
    store.create_run(_snapshot("pg-read-only"))
    reader = PostgresRuntimeStore(
        POSTGRES_URL,
        min_pool_size=1,
        max_pool_size=1,
        max_waiting=2,
        read_only=True,
    )
    try:
        assert reader.read_only
        assert reader.get_run("pg-read-only").user_id == "user-1"
        with reader._connection() as conn:
            setting = conn.execute("SHOW default_transaction_read_only").fetchone()
        assert setting["default_transaction_read_only"] == "on"
        with pytest.raises(RuntimeStoreReadOnlyError):
            reader.create_run(_snapshot("pg-read-only-forbidden"))
    finally:
        reader.close()


def test_postgres_schema_create_transition_terminal_and_outbox(store):
    assert store.schema_version == RUNTIME_SCHEMA_VERSION
    created = store.create_run(_snapshot("pg-lifecycle"))
    queued = store.apply_transition(
        LifecycleTransition(
            run_id="pg-lifecycle",
            kind=TransitionKind.QUEUE,
            transition_id="queue",
        ),
        expected_revision=0,
    )
    started = store.apply_transition(
        LifecycleTransition(
            run_id="pg-lifecycle",
            kind=TransitionKind.START,
            transition_id="start",
        ),
        expected_revision=queued.lifecycle.after.revision,
    )
    completed = store.apply_transition(
        LifecycleTransition(
            run_id="pg-lifecycle",
            kind=TransitionKind.COMPLETE,
            transition_id="complete",
        ),
        expected_revision=started.lifecycle.after.revision,
        terminal=TerminalRecord(
            run_id="pg-lifecycle",
            state=RunState.COMPLETED,
            value={"content": "ok"},
        ),
    )

    assert created.event.sequence == 1
    assert completed.lifecycle.after.state == RunState.COMPLETED
    assert store.get_terminal("pg-lifecycle").to_dict()["value"] == {"content": "ok"}
    assert [event.sequence for event in store.list_events("pg-lifecycle")] == [1, 2, 3, 4]
    leased = store.lease_outbox(owner="exporter")
    assert [item.sequence for item in leased] == [1, 2, 3, 4]
    assert leased[0].lease_token is not None
    store.defer_outbox(
        outbox_id=leased[0].outbox_id,
        lease_token=leased[0].lease_token,
    )
    retried = store.lease_outbox(owner="retry-exporter", limit=1)
    assert retried[0].outbox_id == leased[0].outbox_id
    assert retried[0].attempts == 2
    store.acknowledge_outbox(
        outbox_id=retried[0].outbox_id,
        lease_token=str(retried[0].lease_token),
    )
    with pytest.raises(OutboxLeaseError):
        store.acknowledge_outbox(
            outbox_id=retried[0].outbox_id,
            lease_token=str(retried[0].lease_token),
        )
    for item in leased[1:]:
        store.acknowledge_outbox(
            outbox_id=item.outbox_id,
            lease_token=str(item.lease_token),
        )

    retention = store.prune_run_events("pg-lifecycle", through_sequence=99)

    assert retention.pruned_through_sequence == 3
    with pytest.raises(EventCursorExpiredError):
        store.list_events("pg-lifecycle", after_sequence=0)
    assert [event.sequence for event in store.list_events("pg-lifecycle", after_sequence=3)] == [4]
    repeated = store.apply_transition(
        LifecycleTransition(
            run_id="pg-lifecycle",
            kind=TransitionKind.QUEUE,
            transition_id="queue",
        ),
        expected_revision=0,
    )
    assert repeated.lifecycle.idempotent
    assert repeated.event.sequence == 2


def test_postgres_child_creation_atomically_links_lineage_and_parent_event(store):
    owner = RunOwner("tenant-1", "user-1")
    parent = RunSnapshot(
        run_id="pg-parent",
        state=RunState.RUNNING,
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        session_id="parent-session",
    )
    store.create_run(parent)
    spec = ChildRunSpec.for_parent(
        parent,
        delegation_id="research-1",
        purpose_code="research",
        budget=ChildRunBudget(max_depth=3, max_fanout=2),
        capability_allowlist=("web.search@1.0.0",),
        result_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
    child = RunSnapshot(
        run_id=spec.child_run_id,
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        session_id=f"child:{spec.child_run_id}",
        parent_run_id=spec.parent_run_id,
        root_run_id=spec.root_run_id,
        child_depth=spec.depth,
    )

    created = store.create_run(
        child,
        idempotency_scope="tenant-1:user-1",
        idempotency_key="child:pg-parent:research-1",
        request_digest="sha256:" + "c" * 64,
        child_spec=spec,
    )
    repeated = store.create_run(
        child,
        idempotency_scope="tenant-1:user-1",
        idempotency_key="child:pg-parent:research-1",
        request_digest="sha256:" + "c" * 64,
        child_spec=spec,
    )

    assert created.created
    assert repeated.idempotent
    assert store.list_children(parent.run_id, owner=owner) == [child]
    assert store.get_child_spec(child.run_id, owner=owner) == spec
    assert store.get_child_spec(child.run_id, owner=owner).result_schema_digest is not None
    assert [event.event_type for event in store.list_events(parent.run_id)] == [
        "run.created",
        "run.child.created",
    ]
    assert store.list_events(child.run_id)[0].payload["delegation_digest"] == spec.digest


def test_postgres_child_join_settlement_and_completion_gate(store):
    owner = RunOwner("tenant-1", "user-1")
    parent = RunSnapshot(
        run_id="pg-join-parent",
        state=RunState.RUNNING,
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
    )
    store.create_run(parent)
    spec = ChildRunSpec.for_parent(
        parent,
        delegation_id="join-child",
        purpose_code="research",
    )
    child = RunSnapshot(
        run_id=spec.child_run_id,
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        parent_run_id=parent.run_id,
        root_run_id=parent.run_id,
        child_depth=1,
    )
    store.create_run(
        child,
        idempotency_scope="tenant-1:user-1",
        idempotency_key="child:pg-join-parent:join-child",
        request_digest="sha256:" + "d" * 64,
        child_spec=spec,
    )
    queued = store.apply_transition(
        LifecycleTransition(
            run_id=child.run_id,
            kind=TransitionKind.QUEUE,
            transition_id="pg-join-child:queue",
        ),
        expected_revision=0,
    ).lifecycle.after
    started = store.apply_transition(
        LifecycleTransition(
            run_id=child.run_id,
            kind=TransitionKind.START,
            transition_id="pg-join-child:start",
        ),
        expected_revision=queued.revision,
    ).lifecycle.after

    with pytest.raises(ChildRunContractError) as pending:
        store.apply_transition(
            LifecycleTransition(
                run_id=parent.run_id,
                kind=TransitionKind.COMPLETE,
                transition_id="pg-join-parent:complete",
            ),
            expected_revision=parent.revision,
            terminal=TerminalRecord(
                run_id=parent.run_id,
                state=RunState.COMPLETED,
                value={"content": "parent"},
            ),
        )
    assert pending.value.code == "CHILD_JOIN_PENDING"

    child_complete = LifecycleTransition(
        run_id=child.run_id,
        kind=TransitionKind.COMPLETE,
        transition_id="pg-join-child:complete",
    )
    child_terminal = TerminalRecord(
        run_id=child.run_id,
        state=RunState.COMPLETED,
        value={"content": "child"},
    )
    store.apply_transition(
        child_complete,
        expected_revision=started.revision,
        terminal=child_terminal,
    )
    repeated = store.apply_transition(
        child_complete,
        expected_revision=started.revision,
        terminal=child_terminal,
    )
    assert repeated.lifecycle.idempotent
    assert [event.event_type for event in store.list_events(parent.run_id)].count(
        "run.child.settled"
    ) == 1

    completed = store.apply_transition(
        LifecycleTransition(
            run_id=parent.run_id,
            kind=TransitionKind.COMPLETE,
            transition_id="pg-join-parent:complete",
        ),
        expected_revision=parent.revision,
        terminal=TerminalRecord(
            run_id=parent.run_id,
            state=RunState.COMPLETED,
            value={"content": "parent"},
        ),
    )
    assert completed.lifecycle.after.state is RunState.COMPLETED


def test_postgres_terminal_root_propagates_through_max_depth_tree(store):
    owner = RunOwner("tenant-1", "user-1")
    root = RunSnapshot(
        run_id="pg-cancel-root",
        state=RunState.RUNNING,
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
    )
    store.create_run(root)
    budget = ChildRunBudget(max_depth=16)

    parent = root
    descendants: list[RunSnapshot] = []
    for depth in range(1, 17):
        delegation_id = f"depth-{depth}"
        spec = ChildRunSpec.for_parent(
            parent,
            delegation_id=delegation_id,
            purpose_code="research",
            budget=budget,
        )
        child = RunSnapshot(
            run_id=spec.child_run_id,
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            parent_run_id=spec.parent_run_id,
            root_run_id=spec.root_run_id,
            child_depth=depth,
        )
        store.create_run(
            child,
            idempotency_scope="tenant-1:user-1",
            idempotency_key=f"child:{parent.run_id}:{delegation_id}",
            request_digest="sha256:" + f"{depth % 16:x}" * 64,
            child_spec=spec,
        )
        queued = store.apply_transition(
            LifecycleTransition(
                run_id=child.run_id,
                kind=TransitionKind.QUEUE,
                transition_id=f"{child.run_id}:queue",
            ),
            expected_revision=0,
        ).lifecycle.after
        parent = store.apply_transition(
            LifecycleTransition(
                run_id=child.run_id,
                kind=TransitionKind.START,
                transition_id=f"{child.run_id}:start",
            ),
            expected_revision=queued.revision,
        ).lifecycle.after
        descendants.append(parent)

    inspected = inspect_child_recovery(store, descendants[-1], owner=owner)
    assert inspected.error is None
    assert inspected.spec is not None and inspected.spec.depth == 16

    transition = LifecycleTransition(
        run_id=root.run_id,
        kind=TransitionKind.FAIL,
        transition_id="pg-fail-tree",
    )
    terminal = TerminalRecord(
        run_id=root.run_id,
        state=RunState.FAILED,
        error={"code": "ROOT_FAILED", "safe_message": "The root failed."},
    )
    applied = store.apply_transition(
        transition,
        expected_revision=root.revision,
        terminal=terminal,
    )
    repeated = store.apply_transition(
        transition,
        expected_revision=root.revision,
        terminal=terminal,
    )

    assert applied.lifecycle.after.state is RunState.FAILED
    assert repeated.lifecycle.idempotent
    assert all(store.get_run(item.run_id).state is RunState.CANCELLING for item in descendants)
    reinspection = inspect_child_recovery(store, descendants[-1], owner=owner)
    assert reinspection.error is None
    assert reinspection.terminal_ancestor == applied.lifecycle.after


def test_postgres_recoverable_run_scan_is_owner_scoped_and_keyset_paginated(store):
    for run_id, state in (
        ("pg-recovery-c", RunState.CANCELLING),
        ("pg-recovery-a", RunState.QUEUED),
        ("pg-recovery-b", RunState.RUNNING),
        ("pg-recovery-paused", RunState.PAUSED),
        ("pg-recovery-approval", RunState.WAITING_FOR_APPROVAL),
        ("pg-recovery-complete", RunState.COMPLETED),
    ):
        store.create_run(
            RunSnapshot(
                run_id=run_id,
                state=state,
                tenant_id="tenant-1",
                user_id="user-1",
            )
        )
    store.create_run(
        RunSnapshot(
            run_id="pg-recovery-other",
            state=RunState.RUNNING,
            tenant_id="tenant-2",
            user_id="user-1",
        )
    )
    store.create_run(RunSnapshot(run_id="pg-recovery-null", state=RunState.RUNNING))
    store.create_run(
        RunSnapshot(
            run_id="pg-recovery-skewed",
            state=RunState.QUEUED,
            tenant_id="tenant-1",
            user_id="user-1",
            updated_at="2000-01-01T00:00:00+00:00",
        )
    )

    assert store.list_recoverable_runs(owner=RunOwner("tenant-1", "user-1")) == []

    first = store.list_recoverable_runs(
        owner=RunOwner("tenant-1", "user-1"), minimum_age_seconds=0, limit=2
    )
    second = store.list_recoverable_runs(
        owner=RunOwner("tenant-1", "user-1"),
        after_run_id=first[-1].run_id,
        minimum_age_seconds=0,
        limit=2,
    )

    assert [item.run_id for item in first] == ["pg-recovery-a", "pg-recovery-b"]
    assert [item.run_id for item in second] == ["pg-recovery-c", "pg-recovery-skewed"]
    assert [
        item.run_id
        for item in store.list_recoverable_runs(owner=RunOwner(None, None), minimum_age_seconds=0)
    ] == ["pg-recovery-null"]


def test_postgres_v7_outbox_migrates_dead_letter_columns(store):
    store.create_run(_snapshot("pg-v7-migration"))
    with store._transaction() as conn:
        conn.execute("DROP INDEX IF EXISTS runtime_outbox_ready_v8_idx")
        conn.execute("ALTER TABLE runtime_outbox DROP COLUMN dead_lettered_at")
        conn.execute("ALTER TABLE runtime_outbox DROP COLUMN dead_letter_reason_code")
        conn.execute("DELETE FROM runtime_schema_migrations WHERE version >= 8")

    store.migrate()

    with store._connection() as conn:
        columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'runtime_outbox'
                """
            ).fetchall()
        }
    assert {"dead_lettered_at", "dead_letter_reason_code"} <= columns
    assert store.schema_version == RUNTIME_SCHEMA_VERSION
    assert store.lease_outbox(owner="exporter")[0].run_id == "pg-v7-migration"


def test_postgres_v8_migrates_database_clock_recovery_timestamp(store):
    store.create_run(_snapshot("pg-v8-recovery-migration"))
    with store._transaction() as conn:
        conn.execute("DROP INDEX IF EXISTS runtime_runs_recovery_idx")
        conn.execute("ALTER TABLE runtime_runs DROP COLUMN authority_updated_at")
        conn.execute("DELETE FROM runtime_schema_migrations WHERE version = 9")

    store.migrate()

    with store._connection() as conn:
        row = conn.execute(
            """
            SELECT authority_updated_at FROM runtime_runs
            WHERE run_id = %s
            """,
            ("pg-v8-recovery-migration",),
        ).fetchone()
    assert row["authority_updated_at"] is not None
    assert store.schema_version == RUNTIME_SCHEMA_VERSION


def test_postgres_recovery_queries_have_history_bounded_index_plans(store):
    with store._connection() as conn:
        conn.execute("SET enable_seqscan = off")
        try:
            operation_plan = "\n".join(
                row["QUERY PLAN"]
                for row in conn.execute(
                    """
                    EXPLAIN (COSTS OFF)
                    SELECT record_json FROM runtime_operations
                    WHERE state IN ('planned', 'dispatching')
                    ORDER BY updated_at ASC, operation_id ASC LIMIT %s
                    """,
                    (100,),
                ).fetchall()
            )
            run_plan = "\n".join(
                row["QUERY PLAN"]
                for row in conn.execute(
                    """
                    EXPLAIN (COSTS OFF)
                    SELECT snapshot_json FROM runtime_runs
                    WHERE tenant_id IS NOT DISTINCT FROM %s
                      AND user_id IS NOT DISTINCT FROM %s
                      AND state IN ('queued', 'running', 'cancelling')
                      AND run_id > %s
                      AND authority_updated_at <=
                          CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
                    ORDER BY run_id ASC LIMIT %s
                    """,
                    ("tenant-1", "user-1", "", 0, 100),
                ).fetchall()
            )
            reconciliation_plan = "\n".join(
                row["QUERY PLAN"]
                for row in conn.execute(
                    """
                    EXPLAIN (COSTS OFF)
                    SELECT operation.record_json
                    FROM runtime_operations AS operation
                    JOIN runtime_runs AS run ON run.run_id = operation.run_id
                    WHERE run.tenant_id IS NOT DISTINCT FROM %s
                      AND run.user_id IS NOT DISTINCT FROM %s
                      AND run.state = 'waiting_for_reconciliation'
                      AND operation.operation_id > %s
                      AND run.authority_updated_at <=
                          CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
                      AND (
                        operation.state IN ('unknown', 'succeeded', 'failed', 'cancelled')
                        OR (operation.state = 'dispatching' AND operation.effect_class
                            IN ('compensatable', 'non_repeatable'))
                      )
                    ORDER BY operation.operation_id ASC LIMIT %s
                    """,
                    ("tenant-1", "user-1", "", 0, 100),
                ).fetchall()
            )
        finally:
            conn.execute("RESET enable_seqscan")

    assert "runtime_operations_dispatch_queue_idx" in operation_plan
    assert "runtime_runs_executable_owner_idx" in run_plan
    assert "runtime_operations_run_reconcile_idx" in reconciliation_plan
    assert "runtime_runs_reconciliation_owner_idx" in reconciliation_plan


def test_postgres_recovery_plans_remain_indexed_over_ten_thousand_terminal_rows(store):
    with store._transaction() as conn:
        conn.execute(
            """
            INSERT INTO runtime_runs(
                run_id, tenant_id, user_id, session_id, state, revision,
                next_sequence, snapshot_json, created_at, updated_at,
                authority_updated_at
            )
            SELECT 'index-noise-run-' || LPAD(value::text, 6, '0'),
                   'tenant-1', 'user-1', NULL, 'completed', 1, 1, '{}',
                   CURRENT_TIMESTAMP - INTERVAL '1 hour',
                   CURRENT_TIMESTAMP - INTERVAL '1 hour',
                   CURRENT_TIMESTAMP - INTERVAL '1 hour'
            FROM generate_series(1, 10000) AS value
            """
        )
        conn.execute(
            """
            INSERT INTO runtime_runs(
                run_id, tenant_id, user_id, session_id, state, revision,
                next_sequence, snapshot_json, created_at, updated_at,
                authority_updated_at
            ) VALUES
                ('index-recoverable', 'tenant-1', 'user-1', NULL, 'queued', 1, 1,
                 '{}', CURRENT_TIMESTAMP - INTERVAL '1 hour',
                 CURRENT_TIMESTAMP - INTERVAL '1 hour',
                 CURRENT_TIMESTAMP - INTERVAL '1 hour'),
                ('index-reconciliation', 'tenant-1', 'user-1', NULL,
                 'waiting_for_reconciliation', 1, 1, '{}',
                 CURRENT_TIMESTAMP - INTERVAL '1 hour',
                 CURRENT_TIMESTAMP - INTERVAL '1 hour',
                 CURRENT_TIMESTAMP - INTERVAL '1 hour')
            """
        )
        conn.execute(
            """
            INSERT INTO runtime_operations(
                operation_id, run_id, attempt_id, state, revision, effect_class,
                fence_token, intent_digest, record_json, prepared_event_json,
                created_at, updated_at
            )
            SELECT 'index-noise-operation-' || LPAD(value::text, 6, '0'),
                   'index-noise-run-' || LPAD(value::text, 6, '0'),
                   'attempt-' || value::text, 'succeeded', 2, 'non_repeatable', 1,
                   'sha256:index-noise', '{}', '{}',
                   CURRENT_TIMESTAMP - INTERVAL '1 hour',
                   CURRENT_TIMESTAMP - INTERVAL '1 hour'
            FROM generate_series(1, 10000) AS value
            """
        )
        conn.execute(
            """
            INSERT INTO runtime_operations(
                operation_id, run_id, attempt_id, state, revision, effect_class,
                fence_token, intent_digest, record_json, prepared_event_json,
                created_at, updated_at
            ) VALUES
                ('index-recoverable-operation', 'index-recoverable', 'attempt-r',
                 'planned', 0, 'non_repeatable', 0, 'sha256:index-r', '{}', '{}',
                 CURRENT_TIMESTAMP - INTERVAL '1 hour',
                 CURRENT_TIMESTAMP - INTERVAL '1 hour'),
                ('index-reconciliation-operation', 'index-reconciliation', 'attempt-x',
                 'unknown', 2, 'non_repeatable', 1, 'sha256:index-x', '{}', '{}',
                 CURRENT_TIMESTAMP - INTERVAL '1 hour',
                 CURRENT_TIMESTAMP - INTERVAL '1 hour')
            """
        )
        conn.execute("ANALYZE runtime_runs")
        conn.execute("ANALYZE runtime_operations")
        operation_plan = "\n".join(
            row["QUERY PLAN"]
            for row in conn.execute(
                """
                EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
                SELECT record_json FROM runtime_operations
                WHERE state IN ('planned', 'dispatching')
                ORDER BY updated_at ASC, operation_id ASC LIMIT 100
                """
            ).fetchall()
        )
        run_plan = "\n".join(
            row["QUERY PLAN"]
            for row in conn.execute(
                """
                EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
                SELECT snapshot_json FROM runtime_runs
                WHERE tenant_id IS NOT DISTINCT FROM 'tenant-1'
                  AND user_id IS NOT DISTINCT FROM 'user-1'
                  AND state IN ('queued', 'running', 'cancelling')
                  AND run_id > '' AND authority_updated_at <= CURRENT_TIMESTAMP
                ORDER BY run_id ASC LIMIT 100
                """
            ).fetchall()
        )
        reconciliation_plan = "\n".join(
            row["QUERY PLAN"]
            for row in conn.execute(
                """
                EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
                SELECT operation.record_json
                FROM runtime_operations AS operation
                JOIN runtime_runs AS run ON run.run_id = operation.run_id
                WHERE run.tenant_id IS NOT DISTINCT FROM 'tenant-1'
                  AND run.user_id IS NOT DISTINCT FROM 'user-1'
                  AND run.state = 'waiting_for_reconciliation'
                  AND operation.operation_id > ''
                  AND run.authority_updated_at <= CURRENT_TIMESTAMP
                  AND (
                    operation.state IN ('unknown', 'succeeded', 'failed', 'cancelled')
                    OR (operation.state = 'dispatching' AND operation.effect_class
                        IN ('compensatable', 'non_repeatable'))
                  )
                ORDER BY operation.operation_id ASC LIMIT 100
                """
            ).fetchall()
        )

    assert "runtime_operations_dispatch_queue_idx" in operation_plan
    assert "runtime_runs_executable_owner_idx" in run_plan
    assert "runtime_operations_run_reconcile_idx" in reconciliation_plan
    assert "runtime_runs_reconciliation_owner_idx" in reconciliation_plan
    assert "Seq Scan on runtime_operations" not in operation_plan
    assert "Seq Scan on runtime_runs" not in run_plan


def test_postgres_v9_migrates_dead_letter_audit_without_losing_runtime_data(store):
    store.create_run(_snapshot("pg-v9-dead-letter-migration"))
    with store._transaction() as conn:
        conn.execute("DROP TABLE runtime_dead_letter_audit")
        conn.execute("DELETE FROM runtime_schema_migrations WHERE version = 10")

    store.migrate()

    with store._connection() as conn:
        table = conn.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name = 'runtime_dead_letter_audit'
            """
        ).fetchone()
    assert table["table_name"] == "runtime_dead_letter_audit"
    assert store.schema_version == RUNTIME_SCHEMA_VERSION
    assert store.get_run("pg-v9-dead-letter-migration").user_id == "user-1"
    assert store.lease_outbox(owner="migration-worker")[0].run_id == ("pg-v9-dead-letter-migration")


def test_postgres_observer_event_is_atomic_and_idempotent(store):
    store.create_run(_snapshot("pg-trajectory"))
    proposed = RuntimeEventInput(
        event_id="evt_pg_trajectory",
        run_id="pg-trajectory",
        event_type="model.request.started",
        occurred_at="2026-08-10T00:00:00+00:00",
        attempt_id="pg-trajectory:attempt:1",
        payload={"projection_schema_version": "1.0", "stream": False},
    )

    first = store.append_runtime_event(
        proposed,
        owner=RunOwner(tenant_id="tenant-1", user_id="user-1"),
    )
    repeated = store.append_runtime_event(
        proposed,
        owner=RunOwner(tenant_id="tenant-1", user_id="user-1"),
    )

    assert first.appended and repeated.idempotent
    assert [event.sequence for event in store.list_events("pg-trajectory")] == [1, 2]
    assert [item.sequence for item in store.lease_outbox(owner="trajectory-exporter")] == [
        1,
        2,
    ]
    with pytest.raises(RuntimeEventIdempotencyConflictError):
        store.append_runtime_event(
            RuntimeEventInput(**{**proposed.semantic_value(), "payload": {"stream": True}})
        )


def test_postgres_dead_letter_requeue_is_exact_cas(store):
    store.create_run(_snapshot("pg-dead-letter"))
    item = store.lease_outbox(owner="exporter")[0]
    assert item.lease_token is not None
    dead_letter = store.dead_letter_outbox(
        outbox_id=item.outbox_id,
        lease_token=item.lease_token,
        reason_code="export_timeout",
    )

    digest = "sha256:" + "a" * 64
    owner = RunOwner(tenant_id="tenant-1", user_id="user-1")
    inspected = store.inspect_dead_letters(
        owner=owner,
        operator_digest=digest,
        authority_digest=digest,
        reason_code="test_inspection",
    )
    assert inspected.items == (dead_letter,)
    assert store.lease_outbox(owner="other") == []
    first = store.requeue_dead_letter(
        owner=owner,
        operator_digest=digest,
        authority_digest=digest,
        reason_code="operator_retry",
        mutation_id="pg-requeue-1",
        outbox_id=dead_letter.outbox_id,
        expected_dead_lettered_at=dead_letter.dead_lettered_at,
    )
    repeated = store.requeue_dead_letter(
        owner=owner,
        operator_digest=digest,
        authority_digest=digest,
        reason_code="operator_retry",
        mutation_id="pg-requeue-1",
        outbox_id=dead_letter.outbox_id,
        expected_dead_lettered_at=dead_letter.dead_lettered_at,
    )
    assert not first.idempotent and repeated.idempotent
    assert repeated.audit == first.audit
    with pytest.raises(OutboxDeadLetterConflictError):
        store.requeue_dead_letter(
            owner=owner,
            operator_digest=digest,
            authority_digest=digest,
            reason_code="operator_retry",
            mutation_id="pg-requeue-2",
            outbox_id=dead_letter.outbox_id,
            expected_dead_lettered_at=dead_letter.dead_lettered_at,
        )
    with pytest.raises(OutboxDeadLetterMutationConflictError):
        store.requeue_dead_letter(
            owner=owner,
            operator_digest=digest,
            authority_digest=digest,
            reason_code="different_semantics",
            mutation_id="pg-requeue-1",
            outbox_id=dead_letter.outbox_id,
            expected_dead_lettered_at=dead_letter.dead_lettered_at,
        )
    assert [record.action.value for record in store.list_dead_letter_audit(owner=owner)] == [
        "inspected",
        "requeued",
    ]
    assert store.lease_outbox(owner="retry")[0].outbox_id == dead_letter.outbox_id


def test_postgres_dead_letter_same_mutation_converges_concurrently(store):
    store.create_run(_snapshot("pg-dead-letter-concurrent"))
    item = store.lease_outbox(owner="exporter")[0]
    assert item.lease_token is not None
    dead_letter = store.dead_letter_outbox(
        outbox_id=item.outbox_id,
        lease_token=item.lease_token,
        reason_code="export_timeout",
    )
    digest = "sha256:" + "a" * 64
    owner = RunOwner(tenant_id="tenant-1", user_id="user-1")

    def replay(_index: int):
        return store.requeue_dead_letter(
            owner=owner,
            operator_digest=digest,
            authority_digest=digest,
            reason_code="operator_retry",
            mutation_id="pg-concurrent-requeue",
            outbox_id=dead_letter.outbox_id,
            expected_dead_lettered_at=dead_letter.dead_lettered_at,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(replay, range(2)))

    assert sorted(decision.idempotent for decision in decisions) == [False, True]
    assert decisions[0].audit == decisions[1].audit
    assert [record.action.value for record in store.list_dead_letter_audit(owner=owner)] == [
        "requeued"
    ]


def test_postgres_dead_letter_fault_rolls_back_requeue_and_audit(store):
    store.create_run(_snapshot("pg-dead-letter-rollback"))
    item = store.lease_outbox(owner="exporter")[0]
    assert item.lease_token is not None
    dead_letter = store.dead_letter_outbox(
        outbox_id=item.outbox_id,
        lease_token=item.lease_token,
        reason_code="export_timeout",
    )
    digest = "sha256:" + "a" * 64
    owner = RunOwner(tenant_id="tenant-1", user_id="user-1")

    def fail(stage: str) -> None:
        if stage == "dead_letter_requeue_after_update":
            raise RuntimeError("injected dead-letter failure")

    store._fault_injector = fail
    with pytest.raises(RuntimeError, match="injected dead-letter failure"):
        store.requeue_dead_letter(
            owner=owner,
            operator_digest=digest,
            authority_digest=digest,
            reason_code="operator_retry",
            mutation_id="pg-rollback-requeue",
            outbox_id=dead_letter.outbox_id,
            expected_dead_lettered_at=dead_letter.dead_lettered_at,
        )
    assert store.list_dead_letter_audit(owner=owner) == []
    assert store.lease_outbox(owner="must-remain-quarantined") == []

    store._fault_injector = None
    recovered = store.requeue_dead_letter(
        owner=owner,
        operator_digest=digest,
        authority_digest=digest,
        reason_code="operator_retry",
        mutation_id="pg-rollback-requeue",
        outbox_id=dead_letter.outbox_id,
        expected_dead_lettered_at=dead_letter.dead_lettered_at,
    )
    assert not recovered.idempotent


def test_postgres_durable_approval_wait_and_exact_grant(store):
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=10)
    store.create_run(_snapshot("pg-approval"))
    started = store.apply_transition(
        LifecycleTransition(
            run_id="pg-approval",
            kind=TransitionKind.START,
            transition_id="start",
        ),
        expected_revision=0,
    )
    request = ApprovalRequest(
        request_id="pg-approval-request",
        run_id="pg-approval",
        call_id="call-1",
        capability_id="shell@1",
        capability_digest="sha256:capability",
        effect_category="execute",
        argument_digest="sha256:arguments",
        policy_version="sha256:policy",
        authority_digest="sha256:authority",
        tenant_id="tenant-1",
        principal_id="user-1",
        session_id="session-1",
        requested_at=now.isoformat(),
        expires_at=expires.isoformat(),
        nonce="pg-request-nonce",
    )
    store.apply_transition(
        LifecycleTransition(
            run_id="pg-approval",
            kind=TransitionKind.WAIT_FOR_APPROVAL,
            transition_id="wait",
            pending_request_id=request.request_id,
            occurred_at=request.requested_at,
        ),
        expected_revision=started.lifecycle.after.revision,
        approval_request=request,
    )
    decision = ApprovalDecision(
        decision_id="pg-decision",
        request_id=request.request_id,
        request_digest=request.digest,
        request_nonce=request.nonce,
        approved=True,
        issuer="operator-1",
        reason_code="APPROVED_BY_OPERATOR",
    )
    grant = AuthorizationGrant(
        grant_id="pg-grant",
        scope=GrantScope.RUN,
        tenant_id=request.tenant_id,
        principal_id=request.principal_id,
        session_id=request.session_id,
        run_id=request.run_id,
        capability_ids=(request.capability_id,),
        capability_digests=(request.capability_digest,),
        effect_categories=(request.effect_category,),
        argument_digest=request.argument_digest,
        policy_version=request.policy_version,
        authority_digest=request.authority_digest,
        issuer=decision.issuer,
        issued_at=decision.decided_at,
        expires_at=expires.isoformat(),
        nonce="pg-grant-nonce",
    )

    settled = store.settle_approval(decision, expected_revision=0, grant=grant)

    assert settled.record.state is ApprovalState.APPROVED
    assert store.get_approval(request.request_id).grant.digest == grant.digest
    assert store.list_approvals(
        request.run_id,
        states=(ApprovalState.APPROVED,),
    ) == [settled.record]


@pytest.mark.asyncio
async def test_postgres_artifact_reference_is_atomic_with_operation_settlement(
    store,
    tmp_path,
):
    store.create_run(_snapshot("pg-artifact"))
    intent = OperationIntent(
        operation_id="pg-artifact:model:1",
        run_id="pg-artifact",
        attempt_id="attempt-1",
        kind=OperationKind.MODEL,
        target="example:model",
        request_digest="sha256:request",
        effect_class=EffectClass.NON_REPEATABLE,
    )
    prepared = store.prepare_operation(intent)
    dispatching = store.begin_operation(
        intent.operation_id,
        mutation_id="dispatch",
        expected_revision=prepared.record.revision,
        worker_id="worker-1",
        fence_token=1,
    )
    bytes_store = LocalArtifactStore(tmp_path / "artifacts")
    reference = await bytes_store.stage_json(
        {"content": "postgres-durable"},
        scope=ArtifactScope(
            run_id="pg-artifact",
            tenant_id="tenant-1",
            user_id="user-1",
        ),
        purpose="operation_result",
    )

    settled = store.settle_operation(
        intent.operation_id,
        mutation_id="settle",
        expected_revision=dispatching.record.revision,
        fence_token=1,
        settlement=OperationSettlement(
            state=OperationState.SUCCEEDED,
            result_reference=reference.artifact_id,
            usage={"input_tokens": 19, "output_tokens": 23, "private": "secret"},
            cost={"microusd": 29, "account": "private"},
        ),
        artifact_reference=reference,
    )

    assert (
        store.get_artifact(
            reference.artifact_id,
            owner=RunOwner(tenant_id="tenant-1", user_id="user-1"),
        )
        == reference
    )
    assert store.list_artifacts(
        "pg-artifact",
        owner=RunOwner(tenant_id="tenant-1", user_id="user-1"),
    ) == [reference]
    assert store.list_artifact_storage_keys() == [reference.storage_key]
    assert settled.record.settlement is not None
    assert settled.record.settlement.result_slot_id == intent.result_slot_id
    assert store.list_run_operations(
        "pg-artifact",
        owner=RunOwner(tenant_id="tenant-1", user_id="user-1"),
    ) == [settled.record]
    with pytest.raises(RunNotFoundError):
        store.list_run_operations(
            "pg-artifact",
            owner=RunOwner(tenant_id="other", user_id="user-1"),
        )
    events = store.list_events("pg-artifact")
    assert events[1].payload["result_slot_id"] == intent.result_slot_id
    assert events[3].payload["result_slot_id"] == intent.result_slot_id
    assert events[4].payload["result_slot_id"] == intent.result_slot_id
    assert events[4].payload["usage"] == {"input_tokens": 19, "output_tokens": 23}
    assert events[4].payload["cost"] == {"microusd": 29}
    assert "secret" not in str(events[4].payload)
    assert "private" not in str(events[4].payload)
    assert [event.event_type for event in events] == [
        "run.created",
        "operation.planned",
        "operation.dispatching",
        "artifact.committed",
        "operation.settled",
    ]


@pytest.mark.asyncio
async def test_postgres_runtime_event_commits_output_artifact_atomically(store, tmp_path):
    store.create_run(_snapshot("pg-output"))
    bytes_store = LocalArtifactStore(tmp_path / "output-artifacts")
    reference = await bytes_store.stage_json(
        {"content": "provider output"},
        scope=ArtifactScope(
            run_id="pg-output",
            tenant_id="tenant-1",
            user_id="user-1",
        ),
        purpose="run.output.segment",
    )
    proposed = RuntimeEventInput(
        event_id="pg-output-segment-1",
        run_id="pg-output",
        event_type="run.output.segment",
        occurred_at=datetime.now(UTC).isoformat(),
        payload={"artifact_id": reference.artifact_id},
    )

    decision = store.append_runtime_event(
        proposed,
        owner=RunOwner(tenant_id="tenant-1", user_id="user-1"),
        artifact_reference=reference,
    )

    assert decision.appended is True
    assert store.get_artifact(reference.artifact_id) == reference
    assert [event.event_type for event in store.list_events("pg-output")] == [
        "run.created",
        "run.output.segment",
    ]


def test_postgres_start_idempotency_serializes_concurrent_callers(store):
    kwargs = {
        "idempotency_scope": "tenant-1:user-1",
        "idempotency_key": "same-request",
        "request_digest": "sha256:same",
    }

    def create(index: int):
        return store.create_run(_snapshot(f"pg-idem-{index}"), **kwargs)

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(create, range(2)))

    assert {item.snapshot.run_id for item in decisions} in (
        {"pg-idem-0"},
        {"pg-idem-1"},
    )
    assert sorted(item.idempotent for item in decisions) == [False, True]
    with pytest.raises(StartIdempotencyConflictError):
        store.create_run(
            _snapshot("pg-idem-conflict"),
            **{**kwargs, "request_digest": "sha256:different"},
        )


def test_postgres_row_lock_and_revision_cas_allow_one_transition_winner(store):
    store.create_run(_snapshot("pg-cas"))
    transitions = [
        LifecycleTransition(
            run_id="pg-cas",
            kind=TransitionKind.QUEUE,
            transition_id=f"queue-{index}",
        )
        for index in range(2)
    ]

    def apply(transition):
        try:
            return store.apply_transition(transition, expected_revision=0)
        except RunRevisionConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, transitions))

    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, RunRevisionConflictError) for item in results) == 1
    assert store.get_run("pg-cas").revision == 1


def test_postgres_fault_rolls_back_state_event_and_outbox():
    assert POSTGRES_URL is not None

    def fail(stage: str) -> None:
        if stage == "transition.after_state":
            raise RuntimeError("injected failure")

    store = PostgresRuntimeStore(POSTGRES_URL, fault_injector=fail)
    try:
        with store._transaction() as conn:
            conn.execute("TRUNCATE runtime_runs RESTART IDENTITY CASCADE")
        store.create_run(_snapshot("pg-rollback"))
        with pytest.raises(RuntimeError, match="injected failure"):
            store.apply_transition(
                LifecycleTransition(
                    run_id="pg-rollback",
                    kind=TransitionKind.QUEUE,
                    transition_id="queue",
                ),
                expected_revision=0,
            )
        assert store.get_run("pg-rollback").state == RunState.CREATED
        assert [event.sequence for event in store.list_events("pg-rollback")] == [1]
        assert [item.sequence for item in store.lease_outbox(owner="exporter")] == [1]
    finally:
        store.close()


def test_postgres_pool_reconnects_after_backend_loss_without_partial_commit(store):
    assert POSTGRES_URL is not None
    import psycopg

    victim_pid = 0
    injected = False

    def terminate_backend(stage: str) -> None:
        nonlocal injected
        if stage != "transition.after_state" or injected:
            return
        injected = True
        with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
            terminated = admin.execute(
                "SELECT pg_terminate_backend(%s)",
                (victim_pid,),
            ).fetchone()
        assert terminated == (True,)

    reconnecting = PostgresRuntimeStore(
        POSTGRES_URL,
        min_pool_size=1,
        max_pool_size=1,
        fault_injector=terminate_backend,
    )
    try:
        reconnecting.create_run(_snapshot("pg-connection-loss"))
        claim = reconnecting.acquire_run_lease(
            "pg-connection-loss",
            worker_id="worker-before-loss",
            claim_id="claim-before-loss",
            lease_seconds=60,
        ).claim
        with reconnecting._connection() as conn:
            victim_pid = int(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])

        with pytest.raises(RuntimeStoreConnectionLostError) as lost:
            reconnecting.apply_transition(
                LifecycleTransition(
                    run_id="pg-connection-loss",
                    kind=TransitionKind.QUEUE,
                    transition_id="queue-after-loss",
                ),
                expected_revision=0,
            )
        assert lost.value.code == "RUNTIME_STORE_CONNECTION_LOST"
        assert not lost.value.retryable
        assert lost.value.details == {
            "backend": "postgres",
            "reconciliation_required": True,
        }
        assert str(victim_pid) not in str(lost.value)

        unchanged = reconnecting.get_run("pg-connection-loss")
        assert unchanged.state is RunState.CREATED
        assert unchanged.revision == 0
        assert [event.event_type for event in reconnecting.list_events(unchanged.run_id)] == [
            "run.created",
            "run.lease.acquired",
        ]
        renewed = reconnecting.renew_run_lease(claim, lease_seconds=60)
        assert renewed.run.fence_token == claim.run.fence_token
        queued = reconnecting.apply_transition(
            LifecycleTransition(
                run_id=unchanged.run_id,
                kind=TransitionKind.QUEUE,
                transition_id="queue-after-loss",
            ),
            expected_revision=unchanged.revision,
        )
        assert queued.lifecycle.after.state is RunState.QUEUED
        assert reconnecting.release_run_lease(renewed).released
    finally:
        reconnecting.close()


def test_postgres_operation_intent_dispatch_and_settlement(store):
    store.create_run(_snapshot("pg-operation"))
    intent = OperationIntent(
        operation_id="pg-operation-1",
        run_id="pg-operation",
        attempt_id="attempt-1",
        kind=OperationKind.CAPABILITY,
        target="example.lookup",
        request_digest="sha256:request",
        effect_class=EffectClass.READ_ONLY,
    )
    prepared = store.prepare_operation(intent)
    dispatching = store.begin_operation(
        intent.operation_id,
        mutation_id="dispatch-1",
        expected_revision=prepared.record.revision,
        worker_id="worker-1",
        fence_token=1,
    )
    settled = store.settle_operation(
        intent.operation_id,
        mutation_id="settle-1",
        expected_revision=dispatching.record.revision,
        fence_token=1,
        settlement=OperationSettlement(
            state=OperationState.SUCCEEDED,
            result_reference="artifact:sha256:result",
        ),
    )
    repeated = store.settle_operation(
        intent.operation_id,
        mutation_id="settle-1",
        expected_revision=dispatching.record.revision,
        fence_token=1,
        settlement=OperationSettlement(
            state=OperationState.SUCCEEDED,
            result_reference="artifact:sha256:result",
            settled_at=settled.record.settlement.settled_at,
        ),
    )

    assert settled.record.state is OperationState.SUCCEEDED
    assert repeated.idempotent
    assert store.get_operation(intent.operation_id) == settled.record
    assert store.list_recoverable_operations() == []


@pytest.mark.asyncio
async def test_postgres_owner_scoped_reconciliation_is_atomic_and_idempotent(
    store,
    tmp_path,
):
    snapshot = RunSnapshot(
        run_id="pg-reconcile",
        state=RunState.WAITING_FOR_RECONCILIATION,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    store.create_run(snapshot)
    intent = OperationIntent(
        operation_id="pg-reconcile:model:1",
        run_id=snapshot.run_id,
        attempt_id="attempt-1",
        kind=OperationKind.MODEL,
        target="provider.model",
        request_digest="sha256:request",
        effect_class=EffectClass.NON_REPEATABLE,
    )
    prepared = store.prepare_operation(intent)
    dispatching = store.begin_operation(
        intent.operation_id,
        mutation_id="dispatch",
        expected_revision=prepared.record.revision,
        worker_id="lost-worker",
        fence_token=1,
    )
    unknown = store.settle_operation(
        intent.operation_id,
        mutation_id="unknown",
        expected_revision=dispatching.record.revision,
        fence_token=1,
        settlement=OperationSettlement(
            state=OperationState.UNKNOWN,
            safe_error={"code": "WIRE_LOST"},
        ),
    ).record
    artifacts = LocalArtifactStore(tmp_path / "pg-reconciliation-artifacts")
    reference = await artifacts.stage_json(
        {"effect": "absent"},
        scope=ArtifactScope(
            run_id=snapshot.run_id,
            tenant_id=snapshot.tenant_id,
            user_id=snapshot.user_id,
        ),
        purpose="operation.reconciliation.evidence",
    )
    reconciliation = OperationReconciliation(
        reconciliation_id="pg-reconciliation-1",
        operation_id=intent.operation_id,
        expected_revision=unknown.revision,
        operation_digest=unknown.digest,
        verdict=OperationReconciliationVerdict.EFFECT_ABSENT,
        observer_digest="sha256:" + "1" * 64,
        evidence_artifact_ids=(reference.artifact_id,),
    )

    assert [
        item.intent.operation_id
        for item in store.list_reconciliation_operations(
            owner=RunOwner("tenant-1", "user-1"), minimum_age_seconds=0
        )
    ] == [intent.operation_id]
    assert (
        store.list_reconciliation_operations(
            owner=RunOwner("tenant-2", "user-1"), minimum_age_seconds=0
        )
        == []
    )
    settled = store.reconcile_operation(
        intent.operation_id,
        mutation_id=reconciliation.reconciliation_id,
        reconciliation=reconciliation,
        evidence_artifacts=(reference,),
        owner=RunOwner("tenant-1", "user-1"),
    )
    repeated = store.reconcile_operation(
        intent.operation_id,
        mutation_id=reconciliation.reconciliation_id,
        reconciliation=reconciliation,
        evidence_artifacts=(reference,),
        owner=RunOwner("tenant-1", "user-1"),
    )

    assert settled.record.state is OperationState.FAILED
    assert settled.record.fence_token == 2
    assert repeated.idempotent
    with pytest.raises(OperationNotFoundError):
        store.reconcile_operation(
            intent.operation_id,
            mutation_id=reconciliation.reconciliation_id,
            reconciliation=reconciliation,
            evidence_artifacts=(reference,),
            owner=RunOwner("tenant-2", "user-1"),
        )
    assert store.get_artifact(reference.artifact_id) == reference
    assert [event.event_type for event in store.list_events(snapshot.run_id)].count(
        "operation.reconciled"
    ) == 1


def test_postgres_operation_row_lock_has_one_dispatch_winner(store):
    store.create_run(_snapshot("pg-operation-cas"))
    intent = OperationIntent(
        operation_id="pg-operation-cas-1",
        run_id="pg-operation-cas",
        attempt_id="attempt-1",
        kind=OperationKind.MODEL,
        target="provider.model",
        request_digest="sha256:request",
        effect_class=EffectClass.READ_ONLY,
    )
    store.prepare_operation(intent)

    def dispatch(worker: str):
        try:
            return store.begin_operation(
                intent.operation_id,
                mutation_id=f"dispatch-{worker}",
                expected_revision=0,
                worker_id=worker,
                fence_token=1,
            )
        except OperationRevisionConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(dispatch, ("one", "two")))

    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, OperationRevisionConflictError) for item in results) == 1


def test_postgres_execution_lease_serializes_session_and_advances_fence(store):
    store.create_run(_snapshot("pg-lease-one"))
    store.create_run(_snapshot("pg-lease-two"))
    first = store.acquire_run_lease(
        "pg-lease-one",
        worker_id="worker-one",
        claim_id="claim-one",
    )
    repeated = store.acquire_run_lease(
        "pg-lease-one",
        worker_id="worker-one",
        claim_id="claim-one",
    )

    assert repeated.idempotent
    with pytest.raises(RuntimeLeaseUnavailableError):
        store.acquire_run_lease(
            "pg-lease-two",
            worker_id="worker-two",
            claim_id="claim-two",
        )

    renewed = store.renew_run_lease(first.claim)
    assert renewed.session.fence_token == first.claim.session.fence_token
    assert store.release_run_lease(renewed).released
    second = store.acquire_run_lease(
        "pg-lease-two",
        worker_id="worker-two",
        claim_id="claim-two",
    )
    assert second.claim.run.fence_token == 1
    assert second.claim.session.fence_token == first.claim.session.fence_token + 1

    with store._transaction() as conn:
        conn.execute(
            """
            UPDATE runtime_execution_leases
            SET expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
            WHERE claim_id = %s
            """,
            (second.claim.claim_id,),
        )
    store.create_run(_snapshot("pg-lease-three"))
    reclaimed = store.acquire_run_lease(
        "pg-lease-three",
        worker_id="worker-three",
        claim_id="claim-three",
    )
    assert reclaimed.reclaimed
    assert reclaimed.claim.session.fence_token == second.claim.session.fence_token + 1


def test_postgres_scheduler_claim_reclaim_retry_and_stale_fence(store):
    backend = RuntimeSchedulerBackend(store)
    due = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    backend.upsert_job(
        SchedulerJob(
            name="pg-durable-job",
            schedule="1h",
            prompt="perform bounded work",
            next_run_at=due,
            max_retries=1,
            retry_delay_seconds=0,
            retry_max_delay_seconds=0,
        )
    )

    first = backend.claim_due_runs(worker_id="worker-one")[0]
    assert backend.claim_due_runs(worker_id="worker-two") == []
    first = backend.bind_runtime_run(first, runtime_run_id="runtime-one")
    assert backend.release_claim(first)

    reclaimed = backend.claim_due_runs(worker_id="worker-two")[0]
    assert reclaimed.run_id == first.run_id
    assert reclaimed.record.runtime_run_id == "runtime-one"
    assert reclaimed.fence_token == first.fence_token + 1
    with pytest.raises(SchedulerLeaseLostError):
        backend.bind_runtime_run(first, runtime_run_id="stale-runtime")

    backend.finish_claim(reclaimed, status="failed", error="KNOWN_FAILURE")
    retry = backend.claim_due_runs(worker_id="worker-three")[0]
    assert retry.record.attempt == 2
    assert retry.record.occurrence_id == first.record.occurrence_id
    assert retry.run_id != first.run_id


def test_postgres_scheduler_many_workers_claim_one_occurrence(store):
    backend = RuntimeSchedulerBackend(store)
    backend.upsert_job(
        SchedulerJob(
            name="pg-race-job",
            schedule="1h",
            prompt="race-safe work",
            next_run_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        )
    )

    def claim(index: int):
        assert POSTGRES_URL is not None
        worker_store = PostgresRuntimeStore(
            POSTGRES_URL,
            min_pool_size=0,
            max_pool_size=1,
            max_waiting=2,
        )
        try:
            return RuntimeSchedulerBackend(worker_store).claim_due_runs(
                worker_id=f"worker-{index}",
                limit=1,
            )
        finally:
            worker_store.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        batches = list(pool.map(claim, range(16)))

    claims = [claim for batch in batches for claim in batch]
    assert len(claims) == 1
    assert claims[0].fence_token == 1
    assert len(backend.list_runs(job_name="pg-race-job")) == 1


def test_postgres_scheduler_future_jitter_bounds_group_backlog(store):
    backend = RuntimeSchedulerBackend(store)
    due = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()

    def future_job(prefix: str) -> SchedulerJob:
        for index in range(1_000):
            name = f"{prefix}-{index}"
            job = SchedulerJob(
                name=name,
                schedule="1h",
                prompt="jittered work",
                next_run_at=due,
                jitter_seconds=86_400,
                concurrency_key="pg-jitter-group",
            )
            occurrence_id = scheduler_occurrence_id(
                job_name=name,
                job_revision=1,
                scheduled_at=due,
            )
            if scheduler_jitter_seconds(job, occurrence_id=occurrence_id) > 60:
                return job
        raise AssertionError("failed to select a deterministic future-jitter job")

    backend.upsert_job(future_job("pg-jitter-a"))
    backend.upsert_job(future_job("pg-jitter-b"))

    assert backend.claim_due_runs(worker_id="worker-one", limit=2) == []
    records = backend.list_runs()
    assert len(records) == 1
    assert records[0].status == "pending"
    still_due = [
        job.name
        for job in backend.list_jobs()
        if datetime.fromisoformat(job.next_run_at) <= datetime.now(UTC)
    ]
    assert len(still_due) == 1
