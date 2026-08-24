#!/usr/bin/env python3
"""Prove SQLite runtime atomicity across real, ungraceful worker exits."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

from agnoclaw.runtime import (
    LifecycleTransition,
    OperationIntent,
    OperationKind,
    OperationNotFoundError,
    OperationSettlement,
    OperationState,
    RunNotFoundError,
    RunSnapshot,
    RunState,
    SQLiteRuntimeStore,
    TransitionKind,
)
from agnoclaw.runtime.operations import EffectClass

_CRASH_EXIT_CODE = 86
_SCENARIOS = (
    "create",
    "transition",
    "operation_prepare",
    "operation_dispatch",
    "operation_settle",
)
_STAGES = {
    "create": "create.after_event",
    "transition": "transition.after_state",
    "operation_prepare": "operation.after_prepare",
    "operation_dispatch": "operation.after_dispatch",
    "operation_settle": "operation.after_settle",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run ungraceful child-process exits inside disposable SQLite transactions "
            "and verify rollback, retry idempotency, and database integrity."
        )
    )
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument(
        "--allow-process-crash",
        action="store_true",
        help="Required acknowledgement that disposable child processes call os._exit().",
    )
    parser.add_argument("--_child-scenario", choices=_SCENARIOS, help=argparse.SUPPRESS)
    parser.add_argument("--_database", type=Path, help=argparse.SUPPRESS)
    return parser


def _run_id(scenario: str) -> str:
    return f"crash-probe-{scenario}"


def _snapshot(scenario: str) -> RunSnapshot:
    return RunSnapshot(
        run_id=_run_id(scenario),
        tenant_id="tenant-crash-probe",
        user_id="user-crash-probe",
    )


def _transition(scenario: str) -> LifecycleTransition:
    return LifecycleTransition(
        run_id=_run_id(scenario),
        kind=TransitionKind.QUEUE,
        transition_id="queue-after-crash",
    )


def _intent(scenario: str) -> OperationIntent:
    return OperationIntent(
        operation_id=f"{_run_id(scenario)}-operation",
        run_id=_run_id(scenario),
        attempt_id=f"{_run_id(scenario)}-attempt",
        kind=OperationKind.CAPABILITY,
        target="crash_probe.effect",
        request_digest="sha256:" + "c" * 64,
        effect_class=EffectClass.NON_REPEATABLE,
    )


def _settlement(scenario: str) -> OperationSettlement:
    intent = _intent(scenario)
    return OperationSettlement(
        state=OperationState.SUCCEEDED,
        result_reference=f"result-{intent.operation_id}",
    )


def _exit_at(expected_stage: str, reached_stage: str) -> None:
    if reached_stage == expected_stage:
        os._exit(_CRASH_EXIT_CODE)


def _child(scenario: str, database: Path) -> NoReturn:
    stage = _STAGES[scenario]
    store = SQLiteRuntimeStore(
        database,
        fault_injector=lambda reached: _exit_at(stage, reached),
    )
    if scenario == "create":
        store.create_run(_snapshot(scenario))
    elif scenario == "transition":
        store.apply_transition(_transition(scenario), expected_revision=0)
    elif scenario == "operation_prepare":
        store.prepare_operation(_intent(scenario))
    elif scenario == "operation_dispatch":
        store.begin_operation(
            _intent(scenario).operation_id,
            mutation_id="dispatch-after-crash",
            expected_revision=0,
            worker_id="crash-probe-worker",
            fence_token=1,
        )
    else:
        store.settle_operation(
            _intent(scenario).operation_id,
            mutation_id="settle-after-crash",
            expected_revision=1,
            fence_token=1,
            settlement=_settlement(scenario),
        )
    store.close()
    raise RuntimeError(f"crash stage was not reached for {scenario}")


def _seed(database: Path, scenario: str) -> None:
    if scenario == "create":
        return
    with SQLiteRuntimeStore(database) as store:
        store.create_run(_snapshot(scenario))
        if scenario in {"operation_dispatch", "operation_settle"}:
            store.prepare_operation(_intent(scenario))
        if scenario == "operation_settle":
            store.begin_operation(
                _intent(scenario).operation_id,
                mutation_id="initial-dispatch",
                expected_revision=0,
                worker_id="crash-probe-worker",
                fence_token=1,
            )


def _event_types(store: SQLiteRuntimeStore, scenario: str) -> list[str]:
    return [event.event_type for event in store.list_events(_run_id(scenario))]


def _verify_and_retry(database: Path, scenario: str) -> dict[str, Any]:
    with SQLiteRuntimeStore(database) as store:
        if scenario == "create":
            try:
                store.get_run(_run_id(scenario))
            except RunNotFoundError:
                pass
            else:
                raise AssertionError("crashed create transaction became visible")
            created = store.create_run(_snapshot(scenario))
            if created.snapshot.state is not RunState.CREATED:
                raise AssertionError("create retry produced an unexpected run state")
            expected_event = "run.created"
            post_state = created.snapshot.state.value
        elif scenario == "transition":
            before = store.get_run(_run_id(scenario))
            if before.state is not RunState.CREATED or before.revision != 0:
                raise AssertionError("crashed transition transaction became visible")
            first = store.apply_transition(_transition(scenario), expected_revision=0)
            second = store.apply_transition(_transition(scenario), expected_revision=0)
            if (
                not second.lifecycle.idempotent
                or first.lifecycle.after.state is not RunState.QUEUED
            ):
                raise AssertionError("transition retry was not exactly idempotent")
            expected_event = "run.state.changed"
            post_state = first.lifecycle.after.state.value
        elif scenario == "operation_prepare":
            try:
                store.get_operation(_intent(scenario).operation_id)
            except OperationNotFoundError:
                pass
            else:
                raise AssertionError("crashed operation prepare became visible")
            first = store.prepare_operation(_intent(scenario))
            second = store.prepare_operation(_intent(scenario))
            if not second.idempotent or first.record.state is not OperationState.PLANNED:
                raise AssertionError("operation prepare retry was not exactly idempotent")
            expected_event = "operation.planned"
            post_state = first.record.state.value
        elif scenario == "operation_dispatch":
            before = store.get_operation(_intent(scenario).operation_id)
            if before.state is not OperationState.PLANNED or before.revision != 0:
                raise AssertionError("crashed operation dispatch became visible")
            first = store.begin_operation(
                before.intent.operation_id,
                mutation_id="dispatch-after-crash",
                expected_revision=0,
                worker_id="crash-probe-worker",
                fence_token=1,
            )
            second = store.begin_operation(
                before.intent.operation_id,
                mutation_id="dispatch-after-crash",
                expected_revision=0,
                worker_id="crash-probe-worker",
                fence_token=1,
            )
            if not second.idempotent or first.record.state is not OperationState.DISPATCHING:
                raise AssertionError("operation dispatch retry was not exactly idempotent")
            expected_event = "operation.dispatching"
            post_state = first.record.state.value
        else:
            before = store.get_operation(_intent(scenario).operation_id)
            if (
                before.state is not OperationState.DISPATCHING
                or before.revision != 1
                or before.settlement is not None
            ):
                raise AssertionError("crashed operation settlement became visible")
            settlement = _settlement(scenario)
            first = store.settle_operation(
                before.intent.operation_id,
                mutation_id="settle-after-crash",
                expected_revision=1,
                fence_token=1,
                settlement=settlement,
            )
            second = store.settle_operation(
                before.intent.operation_id,
                mutation_id="settle-after-crash",
                expected_revision=1,
                fence_token=1,
                settlement=settlement,
            )
            if not second.idempotent or first.record.state is not OperationState.SUCCEEDED:
                raise AssertionError("operation settlement retry was not exactly idempotent")
            expected_event = "operation.settled"
            post_state = first.record.state.value

        events = _event_types(store, scenario)
        if events.count(expected_event) != 1:
            raise AssertionError(f"{scenario} committed duplicate {expected_event} events")
        return {
            "scenario": scenario,
            "post_retry_state": post_state,
            "event_count": len(events),
        }


def _integrity(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise AssertionError("SQLite integrity_check failed after crash recovery")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()


def _run_crashing_child(database: Path, scenario: str) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--_child-scenario",
            scenario,
            "--_database",
            str(database),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    if completed.returncode != _CRASH_EXIT_CODE:
        raise RuntimeError(
            f"crash child for {scenario} exited with code {completed.returncode}"
        )


def _probe(iterations: int) -> dict[str, Any]:
    if not 1 <= iterations <= 100:
        raise ValueError("iterations must be between 1 and 100")
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="agnoclaw-runtime-crash-") as directory:
        root = Path(directory)
        for iteration in range(1, iterations + 1):
            for scenario in _SCENARIOS:
                database = root / f"{iteration}-{scenario}.db"
                _seed(database, scenario)
                _run_crashing_child(database, scenario)
                result = _verify_and_retry(database, scenario)
                _integrity(database)
                results.append({"iteration": iteration, **result})
    outcomes: list[dict[str, Any]] = []
    for scenario in _SCENARIOS:
        matches = [
            {key: value for key, value in result.items() if key != "iteration"}
            for result in results
            if result["scenario"] == scenario
        ]
        if len(matches) != iterations or any(item != matches[0] for item in matches[1:]):
            raise AssertionError(f"{scenario} recovery outcome drifted across iterations")
        outcomes.append(matches[0])
    return {
        "status": "passed",
        "iterations": iterations,
        "crash_count": len(results),
        "scenarios": list(_SCENARIOS),
        "outcomes": outcomes,
    }


def main() -> int:
    parser = _parser()
    arguments = parser.parse_args()
    if arguments._child_scenario is not None:
        if arguments._database is None:
            parser.error("--_database is required in child mode")
        _child(arguments._child_scenario, arguments._database)
    if arguments._database is not None:
        parser.error("--_database is internal")
    if not arguments.allow_process_crash:
        parser.error("--allow-process-crash is required")
    try:
        report = _probe(arguments.iterations)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
