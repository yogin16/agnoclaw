"""Contract tests for the storage-independent v0.12 run lifecycle domain."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from agnoclaw.commands import (
    Fork,
    Pause,
    Respond,
    Resume,
    Steer,
    command_from_dict,
    command_to_dict,
)
from agnoclaw.runtime.lifecycle import (
    InMemoryLifecycleStore,
    InvalidRunTransitionError,
    LifecycleIdempotencyConflictError,
    LifecycleTransition,
    RunRevisionConflictError,
    RunSnapshot,
    RunState,
    RunTerminalError,
    SteeringClosedError,
    TransitionKind,
    command_decision,
    reduce_lifecycle,
)


def _transition(
    kind: TransitionKind,
    *,
    state: RunState = RunState.CREATED,
    revision: int = 0,
    transition_id: str = "transition-1",
    pending_request_id: str | None = None,
) -> tuple[RunSnapshot, LifecycleTransition]:
    snapshot = RunSnapshot(
        run_id="run-1",
        state=state,
        revision=revision,
        pending_request_id=(
            "request-1"
            if state in {RunState.WAITING_FOR_INPUT, RunState.WAITING_FOR_APPROVAL}
            else None
        ),
    )
    transition = LifecycleTransition(
        run_id="run-1",
        kind=kind,
        transition_id=transition_id,
        pending_request_id=pending_request_id,
    )
    return snapshot, transition


@pytest.mark.parametrize(
    ("state", "kind", "target"),
    [
        (RunState.CREATED, TransitionKind.QUEUE, RunState.QUEUED),
        (RunState.QUEUED, TransitionKind.START, RunState.RUNNING),
        (RunState.RUNNING, TransitionKind.WAIT_FOR_INPUT, RunState.WAITING_FOR_INPUT),
        (
            RunState.RUNNING,
            TransitionKind.WAIT_FOR_APPROVAL,
            RunState.WAITING_FOR_APPROVAL,
        ),
        (
            RunState.RUNNING,
            TransitionKind.WAIT_FOR_RECONCILIATION,
            RunState.WAITING_FOR_RECONCILIATION,
        ),
        (RunState.RUNNING, TransitionKind.PAUSE, RunState.PAUSED),
        (RunState.PAUSED, TransitionKind.RESUME, RunState.RUNNING),
        (RunState.RUNNING, TransitionKind.REQUEST_CANCEL, RunState.CANCELLING),
        (RunState.CANCELLING, TransitionKind.CONFIRM_CANCEL, RunState.CANCELLED),
        (RunState.RUNNING, TransitionKind.COMPLETE, RunState.COMPLETED),
        (RunState.RUNNING, TransitionKind.FAIL, RunState.FAILED),
        (
            RunState.WAITING_FOR_RECONCILIATION,
            TransitionKind.FAIL_WITH_UNKNOWN_EFFECTS,
            RunState.FAILED_WITH_UNKNOWN_EFFECTS,
        ),
        (RunState.WAITING_FOR_INPUT, TransitionKind.EXPIRE, RunState.EXPIRED),
    ],
)
def test_reducer_accepts_declared_transitions(state, kind, target):
    pending_request = (
        "request-1"
        if kind in {TransitionKind.WAIT_FOR_INPUT, TransitionKind.WAIT_FOR_APPROVAL}
        else None
    )
    snapshot, transition = _transition(
        kind,
        state=state,
        pending_request_id=pending_request,
    )

    decision = reduce_lifecycle(snapshot, transition)

    assert decision.before is snapshot
    assert decision.after.state == target
    assert decision.after.revision == snapshot.revision + 1
    assert decision.after.last_transition_id == "transition-1"


def test_wait_and_response_are_bound_to_exact_request():
    running, wait = _transition(
        TransitionKind.WAIT_FOR_APPROVAL,
        state=RunState.RUNNING,
        pending_request_id="approval-1",
    )
    waiting = reduce_lifecycle(running, wait).after

    with pytest.raises(Exception) as mismatch:
        reduce_lifecycle(
            waiting,
            LifecycleTransition(
                run_id="run-1",
                kind=TransitionKind.RESPOND,
                transition_id="respond-wrong",
                pending_request_id="approval-2",
            ),
        )

    assert getattr(mismatch.value, "code", None) == "RUN_RESPONSE_REQUEST_MISMATCH"
    accepted = reduce_lifecycle(
        waiting,
        LifecycleTransition(
            run_id="run-1",
            kind=TransitionKind.RESPOND,
            transition_id="respond-right",
            pending_request_id="approval-1",
        ),
    )
    assert accepted.after.state == RunState.RUNNING
    assert accepted.after.pending_request_id is None


def test_terminal_state_is_immutable_but_repeat_cancel_is_observationally_idempotent():
    completed = RunSnapshot(run_id="run-1", state=RunState.COMPLETED)
    with pytest.raises(RunTerminalError):
        reduce_lifecycle(
            completed,
            LifecycleTransition(
                run_id="run-1",
                kind=TransitionKind.FAIL,
                transition_id="late-fail",
            ),
        )

    cancelled = RunSnapshot(run_id="run-1", state=RunState.CANCELLED, revision=4)
    decision = reduce_lifecycle(
        cancelled,
        LifecycleTransition(
            run_id="run-1",
            kind=TransitionKind.REQUEST_CANCEL,
            transition_id="cancel-again",
        ),
    )
    assert decision.idempotent
    assert not decision.applied
    assert decision.after.revision == 4


def test_invalid_transition_fails_closed():
    snapshot, transition = _transition(
        TransitionKind.COMPLETE,
        state=RunState.CREATED,
    )
    with pytest.raises(InvalidRunTransitionError):
        reduce_lifecycle(snapshot, transition)


def test_steering_closes_at_explicit_safe_point():
    running = RunSnapshot(run_id="run-1", state=RunState.RUNNING)
    closed = reduce_lifecycle(
        running,
        LifecycleTransition(
            run_id="run-1",
            kind=TransitionKind.CLOSE_STEERING,
            transition_id="safe-point",
        ),
    ).after

    with pytest.raises(SteeringClosedError):
        reduce_lifecycle(
            closed,
            LifecycleTransition(
                run_id="run-1",
                kind=TransitionKind.STEER,
                transition_id="late-steer",
            ),
        )


def test_command_payload_is_frozen_and_round_trips_strictly():
    payload = {"approved": True, "evidence": ["one"]}
    command = Respond("approval-1", payload, command_id="command-1")
    payload["evidence"].append("two")

    encoded = command_to_dict(command)
    decoded = command_from_dict(encoded)

    assert encoded["payload"] == {"approved": True, "evidence": ["one"]}
    assert decoded == command
    with pytest.raises(ValueError, match="unknown run command field"):
        command_from_dict({**encoded, "surprise": True})


@pytest.mark.parametrize(
    "command",
    [
        Pause("operator requested", command_id="pause-1"),
        Resume(command_id="resume-1"),
        Respond("request-1", {"value": "yes"}, command_id="respond-1"),
        Steer("Prioritize database evidence", command_id="steer-1"),
        Fork(17, command_id="fork-1"),
    ],
)
def test_all_public_commands_round_trip(command):
    assert command_from_dict(command_to_dict(command)) == command


def test_fork_is_a_new_run_intent_and_does_not_mutate_source():
    source = RunSnapshot(run_id="run-1", state=RunState.COMPLETED, revision=7)

    decision = command_decision(source, Fork(3, command_id="fork-1"))

    assert decision.snapshot is source
    assert decision.transition is None
    assert decision.fork_from_step == 3
    assert source.revision == 7


def test_in_memory_reference_enforces_cas_and_idempotency():
    store = InMemoryLifecycleStore()
    store.create(RunSnapshot(run_id="run-1"))
    transition = LifecycleTransition(
        run_id="run-1",
        kind=TransitionKind.QUEUE,
        transition_id="queue-1",
    )

    first = store.apply(transition, expected_revision=0)
    repeated = store.apply(transition, expected_revision=0)

    assert first.applied
    assert repeated.idempotent
    assert store.get("run-1").revision == 1
    with pytest.raises(LifecycleIdempotencyConflictError):
        store.apply(
            LifecycleTransition(
                run_id="run-1",
                kind=TransitionKind.START,
                transition_id="queue-1",
            ),
            expected_revision=1,
        )
    with pytest.raises(RunRevisionConflictError):
        store.apply(
            LifecycleTransition(
                run_id="run-1",
                kind=TransitionKind.START,
                transition_id="start-stale",
            ),
            expected_revision=0,
        )


def test_concurrent_compare_and_set_has_one_winner():
    store = InMemoryLifecycleStore()
    store.create(RunSnapshot(run_id="run-1", state=RunState.QUEUED, revision=1))

    def attempt(transition_id: str):
        try:
            return store.apply(
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
        outcomes = list(pool.map(attempt, ["start-1", "start-2"]))

    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, RunRevisionConflictError) for item in outcomes) == 1
    assert store.get("run-1").state == RunState.RUNNING
    assert store.get("run-1").revision == 2
