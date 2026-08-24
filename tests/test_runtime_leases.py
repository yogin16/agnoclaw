"""Execution-lease contracts shared by durable/service coordinators."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agnoclaw.runtime import RunSnapshot, RunState
from agnoclaw.runtime.leases import (
    RuntimeLeaseClaimReleasedError,
    RuntimeLeaseLostError,
    RuntimeLeaseTerminalRunError,
    RuntimeLeaseUnavailableError,
)
from agnoclaw.runtime.lifecycle import LifecycleTransition, TransitionKind
from agnoclaw.runtime.store import RunOwner, SQLiteRuntimeStore, TerminalRecord


def _run(run_id: str, *, session_id: str = "session-1") -> RunSnapshot:
    return RunSnapshot(
        run_id=run_id,
        tenant_id="tenant-1",
        user_id="user-1",
        session_id=session_id,
    )


def test_claim_is_atomic_idempotent_renewable_and_releasable(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(_run("run-1"))

    first = store.acquire_run_lease(
        "run-1",
        worker_id="worker-1",
        claim_id="claim-1",
    )
    replay = store.acquire_run_lease(
        "run-1",
        worker_id="worker-1",
        claim_id="claim-1",
    )
    renewed = store.renew_run_lease(first.claim)
    released = store.release_run_lease(renewed)
    released_again = store.release_run_lease(renewed)

    assert first.acquired and not first.idempotent
    assert replay.idempotent and replay.event == first.event
    assert renewed.run.fence_token == first.claim.run.fence_token
    assert renewed.session.fence_token == first.claim.session.fence_token
    assert renewed.run.expires_at >= first.claim.run.expires_at
    assert released.released and not released.idempotent
    assert released_again.idempotent
    assert [event.event_type for event in store.list_events("run-1")] == [
        "run.created",
        "run.lease.acquired",
    ]
    with pytest.raises(RuntimeLeaseClaimReleasedError):
        store.acquire_run_lease(
            "run-1",
            worker_id="worker-1",
            claim_id="claim-1",
        )


def test_named_session_is_fenced_across_runs_and_release_allows_next(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(_run("run-1"))
    store.create_run(_run("run-2"))
    first = store.acquire_run_lease(
        "run-1",
        worker_id="worker-1",
        claim_id="claim-1",
    ).claim

    with pytest.raises(RuntimeLeaseUnavailableError) as unavailable:
        store.acquire_run_lease(
            "run-2",
            worker_id="worker-2",
            claim_id="claim-2",
        )
    assert unavailable.value.details["lease_kind"] == "session"

    store.release_run_lease(first)
    second = store.acquire_run_lease(
        "run-2",
        worker_id="worker-2",
        claim_id="claim-2",
    ).claim
    assert second.run.fence_token == 1
    assert second.session.fence_token == first.session.fence_token + 1


def test_exact_token_and_fence_are_required_for_renew_and_release(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(_run("run-1"))
    claim = store.acquire_run_lease(
        "run-1",
        worker_id="worker-1",
        claim_id="claim-1",
    ).claim
    forged = replace(
        claim,
        run=replace(claim.run, lease_token="lease_forged"),
    )

    with pytest.raises(RuntimeLeaseLostError):
        store.renew_run_lease(forged)
    with pytest.raises(RuntimeLeaseLostError):
        store.release_run_lease(forged)


def test_expired_crash_lease_is_reclaimed_under_a_new_session_fence(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(_run("run-1"))
    store.create_run(_run("run-2"))
    first = store.acquire_run_lease(
        "run-1",
        worker_id="dead-worker",
        claim_id="dead-claim",
    ).claim
    with store._transaction() as conn:
        conn.execute(
            """
            UPDATE runtime_execution_leases
            SET expires_at = '2000-01-01T00:00:00+00:00'
            WHERE claim_id = ?
            """,
            (first.claim_id,),
        )

    reclaimed = store.acquire_run_lease(
        "run-2",
        worker_id="new-worker",
        claim_id="new-claim",
    )

    assert reclaimed.reclaimed
    assert reclaimed.claim.session.fence_token == first.session.fence_token + 1
    with pytest.raises(RuntimeLeaseLostError):
        store.release_run_lease(first)


def test_terminal_or_wrong_owner_run_cannot_be_claimed(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    created = store.create_run(_run("run-1")).snapshot
    store.apply_transition(
        LifecycleTransition(
            run_id="run-1",
            kind=TransitionKind.FAIL,
            transition_id="terminal",
        ),
        expected_revision=created.revision,
        terminal=TerminalRecord(run_id="run-1", state=RunState.FAILED),
    )

    with pytest.raises(RuntimeLeaseTerminalRunError):
        store.acquire_run_lease(
            "run-1",
            worker_id="worker-1",
            claim_id="claim-1",
        )
    with pytest.raises(Exception) as hidden:
        store.acquire_run_lease(
            "run-1",
            worker_id="worker-1",
            claim_id="claim-2",
            owner=RunOwner(tenant_id="tenant-2", user_id="user-1"),
        )
    assert getattr(hidden.value, "code", None) == "RUN_NOT_FOUND"


def test_lease_fault_rolls_back_rows_and_event(tmp_path):
    enabled = True

    def fail(stage: str) -> None:
        if enabled and stage == "lease.acquire.after_rows":
            raise RuntimeError("injected")

    store = SQLiteRuntimeStore(tmp_path / "runtime.db", fault_injector=fail)
    store.create_run(_run("run-1"))
    with pytest.raises(RuntimeError, match="injected"):
        store.acquire_run_lease(
            "run-1",
            worker_id="worker-1",
            claim_id="claim-1",
        )

    enabled = False
    decision = store.acquire_run_lease(
        "run-1",
        worker_id="worker-1",
        claim_id="claim-2",
    )
    assert decision.claim.run.fence_token == 1
    assert [event.event_type for event in store.list_events("run-1")] == [
        "run.created",
        "run.lease.acquired",
    ]


def test_two_connections_cannot_claim_the_same_session(tmp_path):
    path = tmp_path / "runtime.db"
    first_store = SQLiteRuntimeStore(path)
    second_store = SQLiteRuntimeStore(path)
    first_store.create_run(_run("run-1"))
    first_store.create_run(_run("run-2"))

    def claim(args):
        store, run_id, worker_id = args
        try:
            return store.acquire_run_lease(
                run_id,
                worker_id=worker_id,
                claim_id=f"claim-{worker_id}",
            )
        except RuntimeLeaseUnavailableError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                claim,
                (
                    (first_store, "run-1", "worker-1"),
                    (second_store, "run-2", "worker-2"),
                ),
            )
        )
    assert sum(result is not None for result in results) == 1
