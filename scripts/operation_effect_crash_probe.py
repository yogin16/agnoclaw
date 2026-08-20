#!/usr/bin/env python3
"""Kill real workers around a capability effect and prove restart truth.

The probe uses agnoclaw's real ``OperationGateway`` and ``SQLiteRuntimeStore`` plus a
separate disposable SQLite database that represents an external tool provider. Each
child exits with ``os._exit`` either immediately before provider dispatch or after the
provider transaction commits but before operation settlement. The parent reopens both
databases and proves safe retry or mandatory reconciliation from durable evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any, NoReturn

from agnoclaw.runtime import (
    EffectClass,
    OperationGateway,
    OperationInFlightError,
    OperationIntent,
    OperationKind,
    OperationReconciliationRequiredError,
    OperationState,
    RecoveryAction,
    RunSnapshot,
    SQLiteRuntimeStore,
    recovery_action,
)

_CRASH_EXIT_CODE = 87
_BOUNDARIES = ("before_effect", "after_effect")
_EFFECTS = tuple(effect.value for effect in EffectClass)


class ProbeConfigurationError(RuntimeError):
    """The requested run would not certify the documented crash contract."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Kill disposable OperationGateway workers before/after a capability "
            "effect and verify effect-safe restart behavior."
        )
    )
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument(
        "--allow-process-crash",
        action="store_true",
        help="Required acknowledgement that disposable children call os._exit().",
    )
    parser.add_argument("--_child-effect", choices=_EFFECTS, help=argparse.SUPPRESS)
    parser.add_argument("--_child-boundary", choices=_BOUNDARIES, help=argparse.SUPPRESS)
    parser.add_argument("--_runtime-db", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_provider-db", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_run-id", help=argparse.SUPPRESS)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    child = args._child_effect is not None
    child_fields = (
        args._child_effect,
        args._child_boundary,
        args._runtime_db,
        args._provider_db,
        args._run_id,
    )
    if child:
        if any(value is None for value in child_fields):
            raise ProbeConfigurationError("child mode requires every private argument")
        return
    if any(value is not None for value in child_fields):
        raise ProbeConfigurationError("private child arguments cannot be used in parent mode")
    if not args.allow_process_crash:
        raise ProbeConfigurationError("--allow-process-crash is required")
    if not 1 <= args.iterations <= 100:
        raise ProbeConfigurationError("--iterations must be between 1 and 100")


def _operation_id(run_id: str) -> str:
    return f"{run_id}:capability:external-probe:1"


def _intent(run_id: str, effect: EffectClass) -> OperationIntent:
    operation_id = _operation_id(run_id)
    request = json.dumps(
        {"operation_id": operation_id, "effect_class": effect.value},
        separators=(",", ":"),
        sort_keys=True,
    )
    return OperationIntent(
        operation_id=operation_id,
        run_id=run_id,
        attempt_id=f"{run_id}:attempt:1",
        kind=OperationKind.CAPABILITY,
        target="probe.external_tool",
        request_digest=f"sha256:{hashlib.sha256(request.encode()).hexdigest()}",
        effect_class=effect,
        idempotency_key=(
            f"provider-key:{operation_id}" if effect is EffectClass.IDEMPOTENT else None
        ),
    )


def _initialize_provider(database: Path) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS delivery_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL,
                effect_class TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS committed_effects (
                effect_id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL,
                idempotency_key TEXT UNIQUE,
                effect_class TEXT NOT NULL
            );
            """
        )


def _provider_dispatch(
    database: Path,
    *,
    intent: OperationIntent,
) -> dict[str, str]:
    """Commit one synthetic provider delivery and its externally visible effect."""
    with closing(sqlite3.connect(database, timeout=10)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "INSERT INTO delivery_attempts(operation_id, effect_class) VALUES (?, ?)",
            (intent.operation_id, intent.effect_class.value),
        )
        if cursor.lastrowid is None:  # pragma: no cover - SQLite INSERT contract
            raise RuntimeError("provider attempt insert did not return an identity")
        attempt_id = int(cursor.lastrowid)
        if intent.effect_class is EffectClass.IDEMPOTENT:
            connection.execute(
                """
                INSERT OR IGNORE INTO committed_effects(
                    operation_id, idempotency_key, effect_class
                ) VALUES (?, ?, ?)
                """,
                (
                    intent.operation_id,
                    intent.idempotency_key,
                    intent.effect_class.value,
                ),
            )
        elif intent.effect_class in {
            EffectClass.COMPENSATABLE,
            EffectClass.NON_REPEATABLE,
        }:
            # NULL is deliberately not unique in SQLite. A forbidden replay would
            # create another physical effect row and make this probe fail visibly.
            connection.execute(
                """
                INSERT INTO committed_effects(operation_id, idempotency_key, effect_class)
                VALUES (?, NULL, ?)
                """,
                (intent.operation_id, intent.effect_class.value),
            )
        connection.commit()
    return {
        "status": "provider-committed",
        "attempt": str(attempt_id),
    }


def _provider_counts(database: Path, operation_id: str) -> tuple[int, int]:
    with closing(sqlite3.connect(database)) as connection:
        attempts = int(
            connection.execute(
                "SELECT COUNT(*) FROM delivery_attempts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()[0]
        )
        effects = int(
            connection.execute(
                "SELECT COUNT(*) FROM committed_effects WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()[0]
        )
    return attempts, effects


def _integrity(database: Path) -> str:
    with closing(sqlite3.connect(database)) as connection:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


async def _child_execute(args: argparse.Namespace) -> NoReturn:
    effect = EffectClass(args._child_effect)
    intent = _intent(args._run_id, effect)
    store = SQLiteRuntimeStore(args._runtime_db)
    gateway = OperationGateway(store, worker_id=f"crash-child:{args._run_id}")

    async def dispatch() -> dict[str, str]:
        if args._child_boundary == "before_effect":
            os._exit(_CRASH_EXIT_CODE)
        value = await asyncio.to_thread(
            _provider_dispatch,
            args._provider_db,
            intent=intent,
        )
        os._exit(_CRASH_EXIT_CODE)
        return value  # pragma: no cover - os._exit does not return

    await gateway.execute(intent, dispatch)
    raise RuntimeError("child operation unexpectedly reached settlement")


def _child(args: argparse.Namespace) -> NoReturn:
    asyncio.run(_child_execute(args))
    raise RuntimeError("child operation unexpectedly returned")


def _spawn_child(
    *,
    runtime_db: Path,
    provider_db: Path,
    run_id: str,
    effect: EffectClass,
    boundary: str,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--_child-effect",
            effect.value,
            "--_child-boundary",
            boundary,
            "--_runtime-db",
            str(runtime_db),
            "--_provider-db",
            str(provider_db),
            "--_run-id",
            run_id,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != _CRASH_EXIT_CODE:
        error = completed.stderr.strip()[:500]
        raise AssertionError(
            f"child did not exit at the requested crash boundary: "
            f"returncode={completed.returncode}, stderr={error!r}"
        )


async def _verify_scenario(
    root: Path,
    *,
    iteration: int,
    effect: EffectClass,
    boundary: str,
) -> dict[str, Any]:
    label = f"i{iteration}-{effect.value}-{boundary}"
    runtime_db = root / f"runtime-{label}.db"
    provider_db = root / f"provider-{label}.db"
    run_id = f"effect-crash-{label}"
    intent = _intent(run_id, effect)
    _initialize_provider(provider_db)
    with SQLiteRuntimeStore(runtime_db) as store:
        store.create_run(
            RunSnapshot(
                run_id=run_id,
                tenant_id="tenant-effect-crash",
                user_id="user-effect-crash",
            )
        )

    await asyncio.to_thread(
        _spawn_child,
        runtime_db=runtime_db,
        provider_db=provider_db,
        run_id=run_id,
        effect=effect,
        boundary=boundary,
    )

    with SQLiteRuntimeStore(runtime_db) as store:
        crashed = store.get_operation(intent.operation_id)
        if crashed.state is not OperationState.DISPATCHING:
            raise AssertionError("crashed capability was not durably dispatching")
        action = recovery_action(crashed)
        gateway = OperationGateway(
            store,
            worker_id=f"recovery-parent:{label}",
            result_loader=lambda _reference: {"status": "provider-committed"},
        )
        dispatches_after_reopen = 0

        async def retry_dispatch() -> dict[str, str]:
            nonlocal dispatches_after_reopen
            dispatches_after_reopen += 1
            return await asyncio.to_thread(
                _provider_dispatch,
                provider_db,
                intent=intent,
            )

        if effect in {EffectClass.READ_ONLY, EffectClass.IDEMPOTENT}:
            if action is not RecoveryAction.RETRY:
                raise AssertionError("safely replayable operation was not classified retry")
            recovered = await gateway.recover_interrupted(
                intent.operation_id,
                recovery_id=f"{intent.operation_id}:recover-after-process-death",
            )
            if recovered.state is not OperationState.PLANNED:
                raise AssertionError("safe recovery did not return operation to planned")
            execution = await gateway.execute(intent, retry_dispatch)
            if execution.record.state is not OperationState.SUCCEEDED:
                raise AssertionError("safe retry did not settle success")
            replay_dispatches = 0

            async def forbidden_replay() -> dict[str, str]:
                nonlocal replay_dispatches
                replay_dispatches += 1
                return {"status": "forbidden"}

            replayed = await gateway.execute(intent, forbidden_replay)
            if not replayed.replayed or replay_dispatches:
                raise AssertionError("terminal operation was dispatched instead of replayed")
            expected_state = OperationState.SUCCEEDED
            safe_retry = True
            reconciliation_blocked = False
        else:
            if action is not RecoveryAction.RECONCILE:
                raise AssertionError("ambiguous operation was not classified reconcile")
            try:
                await gateway.recover_interrupted(
                    intent.operation_id,
                    recovery_id=f"{intent.operation_id}:forbidden-recovery",
                )
            except OperationReconciliationRequiredError:
                pass
            else:
                raise AssertionError("ambiguous operation was recovered without evidence")
            try:
                await gateway.execute(intent, retry_dispatch)
            except OperationInFlightError:
                pass
            else:
                raise AssertionError("ambiguous operation was blindly redispatched")
            expected_state = OperationState.DISPATCHING
            safe_retry = False
            reconciliation_blocked = True

        final = store.get_operation(intent.operation_id)
        if final.state is not expected_state:
            raise AssertionError("post-restart operation state is incorrect")
        events = [event.event_type for event in store.list_events(run_id)]
        if events.count("operation.planned") != 1:
            raise AssertionError("operation intent was not persisted exactly once")
        if events.count("operation.settled") != (1 if safe_retry else 0):
            raise AssertionError("operation settlement count is incorrect")

    attempts, effects = _provider_counts(provider_db, intent.operation_id)
    crashed_provider_attempts = 1 if boundary == "after_effect" else 0
    expected_attempts = crashed_provider_attempts + (1 if safe_retry else 0)
    if attempts != expected_attempts:
        raise AssertionError("provider delivery count contradicts recovery policy")
    expected_effects = (
        1
        if effect is EffectClass.IDEMPOTENT and safe_retry
        else crashed_provider_attempts
        if effect in {EffectClass.COMPENSATABLE, EffectClass.NON_REPEATABLE}
        else 0
    )
    if effects != expected_effects:
        raise AssertionError("external effect count contradicts exactly-once boundary")
    if dispatches_after_reopen != (1 if safe_retry else 0):
        raise AssertionError("post-reopen dispatch count is incorrect")
    if _integrity(runtime_db) != "ok" or _integrity(provider_db) != "ok":
        raise AssertionError("SQLite integrity check failed after process death")
    return {
        "safe_retry": safe_retry,
        "reconciliation_blocked": reconciliation_blocked,
        "provider_attempts": attempts,
        "external_effects": effects,
    }


async def _exercise(iterations: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="agnoclaw-effect-crash-") as directory:
        root = Path(directory)
        outcomes = []
        for iteration in range(1, iterations + 1):
            for effect in EffectClass:
                for boundary in _BOUNDARIES:
                    outcomes.append(
                        await _verify_scenario(
                            root,
                            iteration=iteration,
                            effect=effect,
                            boundary=boundary,
                        )
                    )
    return {
        "status": "passed",
        "scope": "real-process-operation-gateway-capability-effect-crash",
        "operation_kind": OperationKind.CAPABILITY.value,
        "iterations": iterations,
        "scenarios": len(outcomes),
        "real_process_crashes": len(outcomes),
        "safe_retries": sum(bool(item["safe_retry"]) for item in outcomes),
        "reconciliation_blocks": sum(
            bool(item["reconciliation_blocked"]) for item in outcomes
        ),
        "provider_delivery_attempts": sum(int(item["provider_attempts"]) for item in outcomes),
        "external_effect_commits": sum(int(item["external_effects"]) for item in outcomes),
        "blind_ambiguous_redispatches": 0,
        "duplicate_external_effects": 0,
        "runtime_and_provider_integrity": True,
    }


def main() -> int:
    args = _parser().parse_args()
    try:
        _validate_args(args)
        if args._child_effect is not None:
            _child(args)
        report = asyncio.run(_exercise(args.iterations))
    except (ProbeConfigurationError, AssertionError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
