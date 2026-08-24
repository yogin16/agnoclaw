"""Transactional conformance tests for the canonical SQLite runtime ledger."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agnoclaw.runtime.lifecycle import (
    LifecycleIdempotencyConflictError,
    LifecycleTransition,
    RunRevisionConflictError,
    RunSnapshot,
    RunState,
    TransitionKind,
)
from agnoclaw.runtime.store import (
    RUNTIME_SCHEMA_VERSION,
    EventCursorError,
    EventCursorExpiredError,
    OutboxLeaseError,
    RunOwner,
    RuntimeEventIdempotencyConflictError,
    RuntimeEventInput,
    RuntimeEventTerminalRunError,
    RuntimeRetentionError,
    RuntimeStoreReadOnlyError,
    SQLiteRuntimeStore,
    StartIdempotencyConflictError,
    TerminalRecord,
    decode_event_cursor,
    encode_event_cursor,
)


def _snapshot(run_id: str = "run-1") -> RunSnapshot:
    return RunSnapshot(
        run_id=run_id,
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        metadata={"request": {"kind": "test"}},
    )


def test_schema_migration_is_idempotent_and_reopenable(tmp_path):
    path = tmp_path / "runtime.db"
    first = SQLiteRuntimeStore(path)
    assert first.schema_version == RUNTIME_SCHEMA_VERSION
    first.close()

    second = SQLiteRuntimeStore(path)
    assert second.schema_version == RUNTIME_SCHEMA_VERSION
    migrations = second._connection.execute(
        "SELECT COUNT(*) AS count FROM runtime_schema_migrations"
    ).fetchone()
    assert migrations["count"] == RUNTIME_SCHEMA_VERSION
    second.close()


def test_read_only_store_requires_current_schema_and_cannot_mutate(tmp_path):
    path = tmp_path / "runtime-read-only.db"
    writer = SQLiteRuntimeStore(path)
    writer.create_run(_snapshot("read-only-run"))
    writer.close()
    before = path.read_bytes()

    reader = SQLiteRuntimeStore(path, read_only=True)

    assert reader.read_only
    assert reader.get_run("read-only-run").user_id == "user-1"
    with pytest.raises(RuntimeStoreReadOnlyError):
        reader.create_run(_snapshot("forbidden-run"))
    reader.close()
    assert path.read_bytes() == before
    with pytest.raises((FileNotFoundError, ValueError)):
        SQLiteRuntimeStore(tmp_path / "missing.db", read_only=True)


def test_v1_database_migrates_transition_lookup_without_json1(tmp_path):
    path = tmp_path / "runtime-v1.db"
    store = SQLiteRuntimeStore(path)
    store._connection.execute("DELETE FROM runtime_schema_migrations WHERE version > 1")
    store._connection.execute("DROP INDEX runtime_events_transition_idx")
    # SQLite cannot drop a column safely across all supported versions, so create
    # the exact legacy event table around the otherwise-real v1 database.
    store._connection.execute("ALTER TABLE runtime_events RENAME TO runtime_events_v2")
    store._connection.execute(
        """
        CREATE TABLE runtime_events (
            run_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            event_json TEXT NOT NULL,
            PRIMARY KEY(run_id, sequence),
            FOREIGN KEY(run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
        )
        """
    )
    store._connection.execute("DROP TABLE runtime_events_v2")
    store.close()

    migrated = SQLiteRuntimeStore(path)

    columns = {
        row["name"] for row in migrated._connection.execute("PRAGMA table_info(runtime_events)")
    }
    outbox_columns = {
        row["name"] for row in migrated._connection.execute("PRAGMA table_info(runtime_outbox)")
    }
    assert "transition_id" in columns
    assert {"dead_lettered_at", "dead_letter_reason_code"} <= outbox_columns
    assert migrated.schema_version == RUNTIME_SCHEMA_VERSION


def test_v7_outbox_migrates_dead_letter_columns_without_losing_events(tmp_path):
    path = tmp_path / "runtime-v7.db"
    store = SQLiteRuntimeStore(path)
    store.create_run(_snapshot())
    store._connection.execute("DROP INDEX runtime_outbox_ready_v8_idx")
    store._connection.execute("ALTER TABLE runtime_outbox RENAME TO runtime_outbox_v8")
    store._connection.execute(
        """
        CREATE TABLE runtime_outbox (
            outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            lease_owner TEXT,
            lease_token TEXT,
            lease_expires_at TEXT,
            delivered_at TEXT,
            UNIQUE(run_id, sequence),
            FOREIGN KEY(run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
        )
        """
    )
    store._connection.execute(
        """
        INSERT INTO runtime_outbox
        SELECT outbox_id, run_id, sequence, event_json, status, attempts,
               available_at, lease_owner, lease_token, lease_expires_at, delivered_at
        FROM runtime_outbox_v8
        """
    )
    store._connection.execute("DROP TABLE runtime_outbox_v8")
    store._connection.execute("DELETE FROM runtime_schema_migrations WHERE version >= 8")
    store.close()

    migrated = SQLiteRuntimeStore(path)

    columns = {
        row["name"] for row in migrated._connection.execute("PRAGMA table_info(runtime_outbox)")
    }
    assert {"dead_lettered_at", "dead_letter_reason_code"} <= columns
    assert migrated.schema_version == RUNTIME_SCHEMA_VERSION
    assert [item.sequence for item in migrated.lease_outbox(owner="exporter")] == [1]


def test_v8_database_migrates_store_authority_recovery_timestamp(tmp_path):
    path = tmp_path / "runtime-v8.db"
    snapshot = _snapshot("v8-run")
    payload = {
        "schema_version": snapshot.schema_version,
        "run_id": snapshot.run_id,
        "state": snapshot.state.value,
        "revision": snapshot.revision,
        "tenant_id": snapshot.tenant_id,
        "user_id": snapshot.user_id,
        "session_id": snapshot.session_id,
        "created_at": snapshot.created_at,
        "updated_at": snapshot.updated_at,
        "steering_open": snapshot.steering_open,
        "pending_request_id": snapshot.pending_request_id,
        "last_transition_id": snapshot.last_transition_id,
        "last_reason_code": snapshot.last_reason_code,
        "metadata": {},
    }
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE runtime_schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO runtime_schema_migrations(version, applied_at) VALUES (?, ?)",
        [(version, snapshot.created_at) for version in range(1, 9)],
    )
    connection.execute(
        """
        CREATE TABLE runtime_runs (
            run_id TEXT PRIMARY KEY, tenant_id TEXT, user_id TEXT, session_id TEXT,
            state TEXT NOT NULL, revision INTEGER NOT NULL, next_sequence INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO runtime_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot.run_id,
            snapshot.tenant_id,
            snapshot.user_id,
            snapshot.session_id,
            snapshot.state.value,
            snapshot.revision,
            1,
            json.dumps(payload),
            snapshot.created_at,
            snapshot.updated_at,
        ),
    )
    connection.commit()
    connection.close()

    migrated = SQLiteRuntimeStore(path)

    columns = {
        row["name"] for row in migrated._connection.execute("PRAGMA table_info(runtime_runs)")
    }
    authority = migrated._connection.execute(
        "SELECT authority_updated_at FROM runtime_runs WHERE run_id = ?", (snapshot.run_id,)
    ).fetchone()
    assert "authority_updated_at" in columns
    assert authority["authority_updated_at"] is not None
    assert migrated.schema_version == RUNTIME_SCHEMA_VERSION


def test_v10_database_adds_child_relations_without_losing_runtime_data(tmp_path):
    path = tmp_path / "runtime-v10.db"
    store = SQLiteRuntimeStore(path)
    original = _snapshot("preserved-v10-run")
    store.create_run(original)
    store._connection.execute("DROP INDEX runtime_children_parent_idx")
    store._connection.execute("DROP TABLE runtime_children")
    store._connection.execute("DELETE FROM runtime_schema_migrations WHERE version >= 11")
    store.close()

    migrated = SQLiteRuntimeStore(path)

    table = migrated._connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("runtime_children",),
    ).fetchone()
    index = migrated._connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
        ("runtime_children_parent_idx",),
    ).fetchone()
    assert table["name"] == "runtime_children"
    assert index["name"] == "runtime_children_parent_idx"
    assert migrated.schema_version == RUNTIME_SCHEMA_VERSION
    assert migrated.get_run(original.run_id) == original
    assert [event.event_type for event in migrated.list_events(original.run_id)] == ["run.created"]
    assert [item.run_id for item in migrated.lease_outbox(owner="migration-check")] == [
        original.run_id
    ]


def test_create_commits_snapshot_event_and_outbox_atomically(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")

    decision = store.create_run(_snapshot())

    assert decision.created
    assert decision.event.sequence == 1
    assert decision.event.event_type == "run.created"
    assert store.get_run("run-1").state == RunState.CREATED
    assert [event.sequence for event in store.list_events("run-1")] == [1]
    leased = store.lease_outbox(owner="exporter-1")
    assert [(item.run_id, item.sequence) for item in leased] == [("run-1", 1)]
    assert leased[0].event == decision.event


def test_observer_event_is_owner_bound_atomic_and_idempotent(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(_snapshot())
    proposed = RuntimeEventInput(
        event_id="evt_observer_1",
        run_id="run-1",
        event_type="model.request.started",
        occurred_at="2026-08-10T00:00:00+00:00",
        attempt_id="run-1:attempt:1",
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

    assert first.appended and not first.idempotent
    assert not repeated.appended and repeated.idempotent
    assert repeated.event == first.event
    assert [event.sequence for event in store.list_events("run-1")] == [1, 2]
    assert [item.sequence for item in store.lease_outbox(owner="exporter-1")] == [1, 2]

    with pytest.raises(RuntimeEventIdempotencyConflictError):
        store.append_runtime_event(
            RuntimeEventInput(**{**proposed.semantic_value(), "payload": {"stream": True}}),
            owner=RunOwner(tenant_id="tenant-1", user_id="user-1"),
        )


def test_observer_event_input_requires_safe_timestamp_and_bounded_payload() -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        RuntimeEventInput(
            event_id="evt_naive",
            run_id="run-1",
            event_type="prompt.built",
            occurred_at="2026-08-10T00:00:00",
        )
    with pytest.raises(ValueError, match="65536 encoded bytes"):
        RuntimeEventInput(
            event_id="evt_oversized",
            run_id="run-1",
            event_type="prompt.built",
            occurred_at="2026-08-10T00:00:00+00:00",
            payload={"content": "x" * 70_000},
        )


def test_outbox_deferral_requires_live_lease_and_retries_immediately(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(_snapshot())
    leased = store.lease_outbox(owner="exporter", lease_seconds=30)
    item = leased[0]
    assert item.lease_token is not None

    with pytest.raises(OutboxLeaseError):
        store.defer_outbox(
            outbox_id=item.outbox_id,
            lease_token="stale-token",
        )
    with pytest.raises(ValueError, match="between 0 and 86400"):
        store.defer_outbox(
            outbox_id=item.outbox_id,
            lease_token=item.lease_token,
            delay_seconds=86_401,
        )
    store.defer_outbox(
        outbox_id=item.outbox_id,
        lease_token=item.lease_token,
        delay_seconds=0,
    )
    repeated = store.lease_outbox(owner="retry-exporter")

    assert repeated[0].outbox_id == item.outbox_id
    assert repeated[0].attempts == 2


def test_dead_letter_requires_safe_reason_and_live_lease(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(_snapshot())
    item = store.lease_outbox(owner="exporter")[0]
    assert item.lease_token is not None

    with pytest.raises(ValueError, match="safe lowercase"):
        store.dead_letter_outbox(
            outbox_id=item.outbox_id,
            lease_token=item.lease_token,
            reason_code="raw error: secret",
        )
    dead_letter = store.dead_letter_outbox(
        outbox_id=item.outbox_id,
        lease_token=item.lease_token,
        reason_code="export_failed",
    )

    assert store.lease_outbox(owner="other-exporter") == []
    audit_digest = "sha256:" + "a" * 64
    owner = RunOwner(tenant_id="tenant-1", user_id="user-1")
    assert not store.inspect_dead_letters(
        owner=owner,
        operator_digest=audit_digest,
        authority_digest=audit_digest,
        reason_code="test_inspection",
        after_outbox_id=dead_letter.outbox_id,
    ).items
    assert (
        store.inspect_dead_letters(
            owner=owner,
            operator_digest=audit_digest,
            authority_digest=audit_digest,
            reason_code="test_inspection",
        ).items[0]
        == dead_letter
    )


def test_observer_event_rejects_terminal_run_and_rolls_back_fault(tmp_path):
    should_fail = False

    def fail(stage: str) -> None:
        if should_fail and stage == "runtime_event.after_event":
            raise RuntimeError("injected observer-event failure")

    store = SQLiteRuntimeStore(tmp_path / "runtime.db", fault_injector=fail)
    store.create_run(_snapshot())
    proposed = RuntimeEventInput(
        event_id="evt_observer_1",
        run_id="run-1",
        event_type="prompt.built",
        occurred_at="2026-08-10T00:00:00+00:00",
        payload={"system_chars": 100},
    )
    should_fail = True
    with pytest.raises(RuntimeError, match="injected observer-event failure"):
        store.append_runtime_event(proposed)
    assert [event.sequence for event in store.list_events("run-1")] == [1]
    should_fail = False
    appended = store.append_runtime_event(proposed)
    current = store.get_run("run-1")
    queued = store.apply_transition(
        LifecycleTransition(
            run_id="run-1",
            kind=TransitionKind.QUEUE,
            transition_id="queue",
        ),
        expected_revision=current.revision,
    ).lifecycle.after
    cancelling = store.apply_transition(
        LifecycleTransition(
            run_id="run-1",
            kind=TransitionKind.REQUEST_CANCEL,
            transition_id="request-cancel",
        ),
        expected_revision=queued.revision,
    ).lifecycle.after
    store.apply_transition(
        LifecycleTransition(
            run_id="run-1",
            kind=TransitionKind.CONFIRM_CANCEL,
            transition_id="confirm-cancel",
        ),
        expected_revision=cancelling.revision,
        terminal=TerminalRecord(run_id="run-1", state=RunState.CANCELLED),
    )

    with pytest.raises(RuntimeEventTerminalRunError):
        store.append_runtime_event(
            RuntimeEventInput(
                event_id="evt_late",
                run_id="run-1",
                event_type="run.late",
                occurred_at="2026-08-10T00:00:01+00:00",
            )
        )
    assert store.list_events("run-1")[1] == appended.event


def test_start_idempotency_returns_original_and_rejects_digest_change(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    kwargs = {
        "idempotency_scope": "tenant-1:user-1",
        "idempotency_key": "request-42",
        "request_digest": "sha256:same",
    }

    first = store.create_run(_snapshot("run-original"), **kwargs)
    repeated = store.create_run(_snapshot("run-ignored"), **kwargs)

    assert first.created
    assert repeated.idempotent
    assert repeated.snapshot.run_id == "run-original"
    assert repeated.event.event_id == first.event.event_id
    with pytest.raises(StartIdempotencyConflictError):
        store.create_run(
            _snapshot("run-conflict"),
            **{**kwargs, "request_digest": "sha256:different"},
        )


def test_transition_commits_state_event_and_outbox_in_one_transaction(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(_snapshot())
    transition = LifecycleTransition(
        run_id="run-1",
        kind=TransitionKind.QUEUE,
        transition_id="queue-1",
    )

    decision = store.apply_transition(transition, expected_revision=0)

    assert decision.lifecycle.after.state == RunState.QUEUED
    assert decision.lifecycle.after.revision == 1
    assert decision.event.sequence == 2
    assert store.get_run("run-1").revision == 1
    assert [event.sequence for event in store.list_events("run-1")] == [1, 2]
    assert [item.sequence for item in store.lease_outbox(owner="exporter-1")] == [1, 2]


def test_fault_after_state_update_rolls_back_state_event_and_outbox(tmp_path):
    def fail(stage: str) -> None:
        if stage == "transition.after_state":
            raise RuntimeError("injected process failure")

    store = SQLiteRuntimeStore(tmp_path / "runtime.db", fault_injector=fail)
    store.create_run(_snapshot())

    with pytest.raises(RuntimeError, match="injected process failure"):
        store.apply_transition(
            LifecycleTransition(
                run_id="run-1",
                kind=TransitionKind.QUEUE,
                transition_id="queue-1",
            ),
            expected_revision=0,
        )

    assert store.get_run("run-1").state == RunState.CREATED
    assert store.get_run("run-1").revision == 0
    assert [event.sequence for event in store.list_events("run-1")] == [1]
    assert [item.sequence for item in store.lease_outbox(owner="exporter-1")] == [1]


def test_transition_idempotency_survives_reopen(tmp_path):
    path = tmp_path / "runtime.db"
    transition = LifecycleTransition(
        run_id="run-1",
        kind=TransitionKind.QUEUE,
        transition_id="queue-1",
    )
    first_store = SQLiteRuntimeStore(path)
    first_store.create_run(_snapshot())
    first = first_store.apply_transition(transition, expected_revision=0)
    first_store.close()

    second_store = SQLiteRuntimeStore(path)
    repeated = second_store.apply_transition(transition, expected_revision=0)

    assert repeated.lifecycle.idempotent
    assert not repeated.lifecycle.applied
    assert repeated.event.event_id == first.event.event_id
    assert second_store.get_run("run-1").revision == 1
    with pytest.raises(LifecycleIdempotencyConflictError):
        second_store.apply_transition(
            LifecycleTransition(
                run_id="run-1",
                kind=TransitionKind.START,
                transition_id="queue-1",
            ),
            expected_revision=1,
        )


def test_two_store_instances_have_one_compare_and_set_winner(tmp_path):
    path = tmp_path / "runtime.db"
    first_store = SQLiteRuntimeStore(path)
    second_store = SQLiteRuntimeStore(path)
    first_store.create_run(RunSnapshot(run_id="run-1", state=RunState.QUEUED, revision=1))

    def attempt(args):
        store, transition_id = args
        try:
            return store.apply_transition(
                LifecycleTransition(
                    run_id="run-1",
                    kind=TransitionKind.START,
                    transition_id=transition_id,
                ),
                expected_revision=1,
            )
        except RunRevisionConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                attempt,
                [(first_store, "start-1"), (second_store, "start-2")],
            )
        )

    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, RunRevisionConflictError) for item in outcomes) == 1
    assert first_store.get_run("run-1").state == RunState.RUNNING


def test_owner_and_cursor_are_bound_to_exact_run(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(_snapshot())
    owner = RunOwner(tenant_id="tenant-1", user_id="user-1")

    assert store.get_run("run-1", owner=owner).run_id == "run-1"
    with pytest.raises(Exception) as hidden:
        store.get_run(
            "run-1",
            owner=RunOwner(tenant_id="tenant-2", user_id="user-1"),
        )
    assert getattr(hidden.value, "code", None) == "RUN_NOT_FOUND"

    cursor = encode_event_cursor(run_id="run-1", sequence=1)
    assert decode_event_cursor(cursor, run_id="run-1") == 1
    with pytest.raises(EventCursorError):
        decode_event_cursor(cursor, run_id="run-2")
    with pytest.raises(EventCursorError):
        decode_event_cursor("cursor_v1_not-base64", run_id="run-1")


def test_outbox_lease_requires_exact_token_and_ack_is_final(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(_snapshot())
    item = store.lease_outbox(owner="exporter-1")[0]

    with pytest.raises(OutboxLeaseError):
        store.acknowledge_outbox(outbox_id=item.outbox_id, lease_token="wrong")
    store.acknowledge_outbox(
        outbox_id=item.outbox_id,
        lease_token=str(item.lease_token),
    )

    assert store.lease_outbox(owner="exporter-2") == []
    with pytest.raises(OutboxLeaseError):
        store.acknowledge_outbox(
            outbox_id=item.outbox_id,
            lease_token=str(item.lease_token),
        )


def test_event_payload_is_snapshotted_before_persistence(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(_snapshot())
    payload = {"evidence": ["one"]}
    transition = LifecycleTransition(
        run_id="run-1",
        kind=TransitionKind.QUEUE,
        transition_id="queue-1",
        payload=payload,
    )
    payload["evidence"].append("two")

    store.apply_transition(transition, expected_revision=0)

    with pytest.raises(LifecycleIdempotencyConflictError):
        store.apply_transition(
            LifecycleTransition(
                run_id="run-1",
                kind=TransitionKind.QUEUE,
                transition_id="queue-1",
                payload={"evidence": ["two"]},
            ),
            expected_revision=1,
        )


def test_terminal_result_is_committed_with_terminal_state(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(RunSnapshot(run_id="run-1", state=RunState.RUNNING, revision=1))
    transition = LifecycleTransition(
        run_id="run-1",
        kind=TransitionKind.COMPLETE,
        transition_id="complete-1",
    )
    terminal = TerminalRecord(
        run_id="run-1",
        state=RunState.COMPLETED,
        value={"content": "done"},
    )

    store.apply_transition(
        transition,
        expected_revision=1,
        terminal=terminal,
    )

    assert store.get_run("run-1").state == RunState.COMPLETED
    assert store.get_terminal("run-1") == terminal
    with pytest.raises(LifecycleIdempotencyConflictError):
        store.apply_transition(
            transition,
            expected_revision=1,
            terminal=TerminalRecord(
                run_id="run-1",
                state=RunState.COMPLETED,
                value={"content": "changed"},
            ),
        )


def _complete_for_retention(store, *, run_id: str, idempotent: bool = False):
    kwargs = (
        {
            "idempotency_scope": "tenant-1:user-1",
            "idempotency_key": f"key-{run_id}",
            "request_digest": "sha256:same",
        }
        if idempotent
        else {}
    )
    created = store.create_run(_snapshot(run_id), **kwargs)
    queue = LifecycleTransition(
        run_id=run_id,
        kind=TransitionKind.QUEUE,
        transition_id=f"{run_id}:queue",
    )
    queued = store.apply_transition(queue, expected_revision=0)
    started = store.apply_transition(
        LifecycleTransition(
            run_id=run_id,
            kind=TransitionKind.START,
            transition_id=f"{run_id}:start",
        ),
        expected_revision=queued.lifecycle.after.revision,
    )
    store.apply_transition(
        LifecycleTransition(
            run_id=run_id,
            kind=TransitionKind.COMPLETE,
            transition_id=f"{run_id}:complete",
        ),
        expected_revision=started.lifecycle.after.revision,
        terminal=TerminalRecord(
            run_id=run_id,
            state=RunState.COMPLETED,
            value={"content": "done"},
        ),
    )
    return created, queue, kwargs


def test_retention_has_explicit_watermark_and_preserves_idempotency(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    created, queue, kwargs = _complete_for_retention(
        store,
        run_id="retained",
        idempotent=True,
    )
    for item in store.lease_outbox(owner="exporter"):
        store.acknowledge_outbox(
            outbox_id=item.outbox_id,
            lease_token=str(item.lease_token),
        )

    decision = store.prune_run_events("retained", through_sequence=99)

    assert decision.pruned_through_sequence == 3
    assert decision.deleted_events == 3
    with pytest.raises(EventCursorExpiredError) as expired:
        store.list_events("retained", after_sequence=0)
    assert expired.value.details["resume_after_sequence"] == 3
    assert [event.sequence for event in store.list_events("retained", after_sequence=3)] == [4]
    repeated_start = store.create_run(_snapshot("ignored"), **kwargs)
    repeated_queue = store.apply_transition(queue, expected_revision=0)
    assert repeated_start.event == created.event
    assert repeated_start.idempotent
    assert repeated_queue.event.sequence == 2
    assert repeated_queue.lifecycle.idempotent


def test_retention_requires_terminal_run_and_settled_outbox(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(_snapshot("active"))
    with pytest.raises(RuntimeRetentionError) as active:
        store.prune_run_events("active", through_sequence=1)
    assert active.value.code == "RUNTIME_RETENTION_RUN_ACTIVE"

    _complete_for_retention(store, run_id="pending")
    with pytest.raises(RuntimeRetentionError) as pending:
        store.prune_run_events("pending", through_sequence=3)
    assert pending.value.code == "RUNTIME_RETENTION_EXPORT_PENDING"
