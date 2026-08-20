"""Query-plan gates for history-size-independent SQLite recovery discovery."""

from __future__ import annotations

from agnoclaw.runtime.store import SQLiteRuntimeStore


def _plan(store: SQLiteRuntimeStore, sql: str, parameters: tuple[object, ...]) -> str:
    rows = store._connection.execute(  # noqa: SLF001 - release query-plan certification
        f"EXPLAIN QUERY PLAN {sql}",
        parameters,
    ).fetchall()
    return "\n".join(str(row["detail"]) for row in rows)


def test_recoverable_operation_plan_ignores_terminal_history(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    plan = _plan(
        store,
        """
        SELECT record_json FROM runtime_operations
        WHERE state IN ('planned', 'dispatching')
        ORDER BY updated_at ASC, operation_id ASC LIMIT ?
        """,
        (100,),
    )

    assert "runtime_operations_dispatch_queue_idx" in plan
    assert "USE TEMP B-TREE" not in plan


def test_owner_recovery_plan_ignores_terminal_runs(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    plan = _plan(
        store,
        """
        SELECT snapshot_json FROM runtime_runs
        WHERE tenant_id IS ? AND user_id IS ?
          AND state IN ('queued', 'running', 'cancelling') AND run_id > ?
          AND authority_updated_at <= ?
        ORDER BY run_id ASC LIMIT ?
        """,
        (
            "tenant-1",
            "user-1",
            "",
            "9999-12-31T23:59:59+00:00",
            100,
        ),
    )

    assert "runtime_runs_executable_owner_idx" in plan
    assert "USE TEMP B-TREE" not in plan


def test_owner_reconciliation_plan_uses_run_scoped_operation_index(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    plan = _plan(
        store,
        """
        SELECT operation.record_json FROM runtime_operations AS operation
        JOIN runtime_runs AS run ON run.run_id = operation.run_id
        WHERE run.tenant_id IS ? AND run.user_id IS ?
          AND run.state = 'waiting_for_reconciliation'
          AND operation.operation_id > ?
          AND run.authority_updated_at <= ?
          AND (
            operation.state IN ('unknown', 'succeeded', 'failed', 'cancelled') OR (
              operation.state = 'dispatching'
              AND operation.effect_class IN ('compensatable', 'non_repeatable')
            )
          )
        ORDER BY operation.operation_id ASC LIMIT ?
        """,
        (
            "tenant-1",
            "user-1",
            "",
            "9999-12-31T23:59:59+00:00",
            100,
        ),
    )

    assert "runtime_operations_run_reconcile_idx" in plan


def test_child_listing_plan_is_parent_bounded_and_ordered(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    plan = _plan(
        store,
        """
        SELECT run.snapshot_json
        FROM runtime_children AS child
        JOIN runtime_runs AS run ON run.run_id = child.child_run_id
        WHERE child.parent_run_id = ?
        ORDER BY child.created_at, child.child_run_id
        LIMIT ?
        """,
        ("parent-1", 64),
    )

    assert "runtime_children_parent_idx" in plan
    assert "USE TEMP B-TREE" not in plan
