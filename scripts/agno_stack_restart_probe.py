#!/usr/bin/env python3
"""Kill real AgentHarness workers and certify Agno-stack restart boundaries.

The probe drives the production ``AgentHarness.start()`` path with an actual Agno
``Agent`` and public ``AgnoModelFactory``.  Disposable children terminate with
``os._exit()`` at three committed boundaries:

* after the exact request checkpoint and model intent are planned, before dispatch;
* after the model/provider callable is entered, while the operation is dispatching;
* after the model result artifact and operation settlement commit, before run completion.

A fourth child stops at the safe pre-dispatch boundary and is reopened with a changed
factory implementation digest. Recovery must fail closed before provider dispatch.

The parent reopens the Agno session database, runtime ledger, and artifact store with a
new harness process.  It then proves one safe continuation, one mandatory
reconciliation wait, and one completion from the durable result without provider
redispatch.  This is a bounded single-node certification probe, not a power-loss or
multi-host claim.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

from agno.db.sqlite import SqliteDb
from agno.models.base import Model
from agno.models.response import ModelResponse

from agnoclaw import AgentHarness, AgnoModelFactory, HarnessConfig
from agnoclaw.runtime import (
    ArtifactReference,
    LocalArtifactStore,
    OperationIntent,
    OperationKind,
    OperationSettlement,
    OperationState,
    RunState,
    RuntimeRecoveryStatus,
    RunWaitError,
    SQLiteRuntimeStore,
)
from agnoclaw.runtime.store import StoredOperationDecision

_CRASH_EXIT_CODE = 88
_SCENARIOS = (
    "planned",
    "during_dispatch",
    "after_settlement",
    "factory_digest_mismatch",
)
_MODEL_FACTORY_DIGEST = "sha256:" + "a" * 64
_MISMATCHED_FACTORY_DIGEST = "sha256:" + "b" * 64


class ProbeConfigurationError(RuntimeError):
    """The requested command would not certify the documented crash contract."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Kill disposable AgentHarness workers across Agno model-operation "
            "boundaries and verify safe restart behavior."
        )
    )
    parser.add_argument(
        "--allow-process-crash",
        action="store_true",
        help="Required acknowledgement that disposable children call os._exit().",
    )
    parser.add_argument("--_child-scenario", choices=_SCENARIOS, help=argparse.SUPPRESS)
    parser.add_argument("--_root", type=Path, help=argparse.SUPPRESS)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    child = args._child_scenario is not None
    if child:
        if args._root is None:
            raise ProbeConfigurationError("child mode requires --_root")
        return
    if args._root is not None:
        raise ProbeConfigurationError("--_root is internal")
    if not args.allow_process_crash:
        raise ProbeConfigurationError("--allow-process-crash is required")


def _mode_path(root: Path) -> Path:
    return root / "mode"


def _runtime_path(root: Path) -> Path:
    return root / "runtime.db"


def _provider_path(root: Path) -> Path:
    return root / "provider.db"


def _agno_path(root: Path) -> Path:
    return root / "agno.db"


def _artifacts_path(root: Path) -> Path:
    return root / "artifacts"


def _run_id_path(root: Path) -> Path:
    return root / "run-id"


def _write_durable_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, value.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mode(root: Path) -> str:
    return _mode_path(root).read_text(encoding="utf-8").strip()


def _initialize_provider(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_calls (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                scenario TEXT NOT NULL,
                process_phase TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS factory_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_token TEXT NOT NULL,
                event TEXT NOT NULL CHECK (event IN ('created', 'closed')),
                process_phase TEXT NOT NULL
            )
            """
        )


def _record_provider_call(path: Path, *, scenario: str, process_phase: str) -> None:
    with sqlite3.connect(path, timeout=10) as connection:
        connection.execute(
            "INSERT INTO provider_calls(scenario, process_phase) VALUES (?, ?)",
            (scenario, process_phase),
        )
        connection.commit()


def _provider_calls(path: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(path) as connection:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT scenario, process_phase FROM provider_calls ORDER BY sequence"
            ).fetchall()
        ]


def _record_factory_event(
    path: Path,
    *,
    instance_token: str,
    event: str,
    process_phase: str,
) -> None:
    with sqlite3.connect(path, timeout=10) as connection:
        connection.execute(
            """
            INSERT INTO factory_events(instance_token, event, process_phase)
            VALUES (?, ?, ?)
            """,
            (instance_token, event, process_phase),
        )
        connection.commit()


def _factory_events(path: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(path) as connection:
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                """
                SELECT instance_token, event, process_phase
                FROM factory_events
                ORDER BY sequence
                """
            ).fetchall()
        ]


def _integrity(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


@dataclass
class ProbeModel(Model):
    """One real Agno model transport controlled by an external phase file."""

    root: str = ""
    scenario: str = ""
    instance_token: str = ""
    closed: bool = False

    def _invoke(self) -> ModelResponse:
        root = Path(self.root)
        process_phase = _mode(root)
        _record_provider_call(
            _provider_path(root),
            scenario=self.scenario,
            process_phase=process_phase,
        )
        if process_phase == "child" and self.scenario == "during_dispatch":
            os._exit(_CRASH_EXIT_CODE)
        return ModelResponse(content=f"provider-result:{self.scenario}")

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return self._invoke()

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return self._invoke()

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        yield self._invoke()

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        yield self._invoke()

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return ModelResponse(content=str(response))

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return ModelResponse(content=str(response))

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        root = Path(self.root)
        _record_factory_event(
            _provider_path(root),
            instance_token=self.instance_token,
            event="closed",
            process_phase=_mode(root),
        )


class BoundarySQLiteRuntimeStore(SQLiteRuntimeStore):
    """Production store plus deterministic process termination after committed writes."""

    def __init__(self, path: str | Path, *, root: Path, scenario: str) -> None:
        self._probe_root = root
        self._probe_scenario = scenario
        super().__init__(path)

    def prepare_operation(self, intent: OperationIntent) -> StoredOperationDecision:
        decision = super().prepare_operation(intent)
        if (
            _mode(self._probe_root) == "child"
            and self._probe_scenario in {"planned", "factory_digest_mismatch"}
            and intent.operation_id.endswith(":model:1")
        ):
            os._exit(_CRASH_EXIT_CODE)
        return decision

    def settle_operation(
        self,
        operation_id: str,
        *,
        mutation_id: str,
        expected_revision: int,
        fence_token: int,
        settlement: OperationSettlement,
        artifact_reference: ArtifactReference | None = None,
    ) -> StoredOperationDecision:
        decision = super().settle_operation(
            operation_id,
            mutation_id=mutation_id,
            expected_revision=expected_revision,
            fence_token=fence_token,
            settlement=settlement,
            artifact_reference=artifact_reference,
        )
        if (
            _mode(self._probe_root) == "child"
            and self._probe_scenario == "after_settlement"
            and operation_id.endswith(":model:1")
            and settlement.state is OperationState.SUCCEEDED
        ):
            os._exit(_CRASH_EXIT_CODE)
        return decision


def _config(root: Path) -> HarnessConfig:
    return HarnessConfig(
        enable_plugins=False,
        workspace_dir=str(root / "workspace"),
        runtime_lease_seconds=3,
        runtime_lease_renew_interval_seconds=1,
    )


def _model_factory(
    root: Path,
    scenario: str,
    *,
    implementation_digest: str,
) -> AgnoModelFactory:
    def create_model() -> ProbeModel:
        instance_token = uuid4().hex
        _record_factory_event(
            _provider_path(root),
            instance_token=instance_token,
            event="created",
            process_phase=_mode(root),
        )
        return ProbeModel(
            id="agno-stack-restart-model",
            provider="deterministic",
            root=str(root),
            scenario=scenario,
            instance_token=instance_token,
        )

    return AgnoModelFactory(
        model_id="agno-stack-restart-model",
        provider="deterministic",
        implementation_digest=implementation_digest,
        factory=create_model,
    )


def _harness(
    root: Path,
    scenario: str,
    *,
    factory_digest: str = _MODEL_FACTORY_DIGEST,
) -> tuple[AgentHarness, BoundarySQLiteRuntimeStore]:
    store = BoundarySQLiteRuntimeStore(_runtime_path(root), root=root, scenario=scenario)
    harness = AgentHarness(
        name="agno-stack-restart-probe",
        agent_id="agno-stack-restart-probe",
        model=_model_factory(
            root,
            scenario,
            implementation_digest=factory_digest,
        ),
        user_id="restart-user",
        workspace_dir=root / "workspace",
        config=_config(root),
        db=SqliteDb(db_file=str(_agno_path(root))),
        include_default_tools=False,
        runtime_store=store,
        artifact_store=LocalArtifactStore(_artifacts_path(root)),
    )
    return harness, store


async def _child_run(root: Path, scenario: str) -> NoReturn:
    harness, _store = _harness(root, scenario)
    run = await harness.start(
        "certify the full Agno stack restart boundary",
        session_id=f"restart-session-{scenario}",
        idempotency_key=f"restart-probe:{scenario}",
    )
    _write_durable_text(_run_id_path(root), str(run.run_id))
    await run.wait(timeout=30)
    raise RuntimeError("child run unexpectedly completed without the requested crash")


def _child(root: Path, scenario: str) -> NoReturn:
    asyncio.run(_child_run(root, scenario))
    raise RuntimeError("child run unexpectedly returned")


def _spawn_child(root: Path, scenario: str) -> str:
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--_child-scenario",
            scenario,
            "--_root",
            str(root),
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if completed.returncode != _CRASH_EXIT_CODE:
        raise AssertionError(
            "child did not exit at the requested boundary: "
            f"scenario={scenario}, returncode={completed.returncode}, "
            f"stderr={completed.stderr.strip()[:500]!r}"
        )
    run_id = _run_id_path(root).read_text(encoding="utf-8").strip()
    if not run_id:
        raise AssertionError("child did not persist its run identity before execution")
    return run_id


async def _recover(root: Path, scenario: str, run_id: str) -> dict[str, Any]:
    _write_durable_text(_mode_path(root), "recovery")
    harness, store = _harness(
        root,
        scenario,
        factory_digest=(
            _MISMATCHED_FACTORY_DIGEST
            if scenario == "factory_digest_mismatch"
            else _MODEL_FACTORY_DIGEST
        ),
    )
    outcome: dict[str, Any]
    try:
        crashed = store.get_operation(f"{run_id}:model:1")
        recovery_batch = await harness.recover_pending_runs(
            limit=2,
            concurrency=1,
            minimum_age_seconds=0,
        )
        if recovery_batch.next_cursor is not None or len(recovery_batch.items) != 1:
            raise AssertionError("startup recovery did not discover one exact stranded run")
        recovery_item = recovery_batch.items[0]
        if (
            recovery_item.run_id != run_id
            or recovery_item.status is not RuntimeRecoveryStatus.RECOVERED
        ):
            raise AssertionError("startup recovery did not safely classify the stranded run")
        recovered = harness.get_run(run_id)
        if scenario == "planned":
            if crashed.state is not OperationState.PLANNED:
                raise AssertionError("pre-dispatch crash did not leave a planned model intent")
            result = await recovered.wait(timeout=15)
            if getattr(result, "content", None) != "provider-result:planned":
                raise AssertionError("planned operation did not resume through the Agno model")
            expected_state = RunState.COMPLETED
        elif scenario == "during_dispatch":
            if crashed.state is not OperationState.DISPATCHING:
                raise AssertionError("in-flight crash was not durably dispatching")
            expected_state = RunState.WAITING_FOR_RECONCILIATION
        elif scenario == "after_settlement":
            if crashed.state is not OperationState.SUCCEEDED:
                raise AssertionError("post-settlement crash lost the successful operation")
            result = await recovered.wait(timeout=15)
            if result != {"content": "provider-result:after_settlement"}:
                raise AssertionError("durable model result was not restored exactly")
            expected_state = RunState.COMPLETED
        else:
            if crashed.state is not OperationState.PLANNED:
                raise AssertionError("factory-drift crash did not leave a planned model intent")
            try:
                await recovered.wait(timeout=15)
            except RunWaitError as exc:
                if exc.safe_error["code"] != "RUN_RECOVERY_SPEC_MISMATCH":
                    raise AssertionError(
                        "factory digest drift did not produce the exact safe error"
                    ) from exc
            else:
                raise AssertionError("factory digest drift unexpectedly resumed the provider")
            expected_state = RunState.FAILED

        status = await recovered.status()
        if status.state is not expected_state:
            raise AssertionError(
                f"unexpected recovered state: {status.state.value}; expected {expected_state.value}"
            )
        operation = store.get_operation(f"{run_id}:model:1")
        events = [
            event.event_type
            for event in store.list_events(run_id)
            if event.payload.get("operation_id") == operation.intent.operation_id
        ]
        if events.count("operation.planned") != 1:
            raise AssertionError("model intent was not persisted exactly once")
        expected_dispatches = 0 if scenario == "factory_digest_mismatch" else 1
        if events.count("operation.dispatching") != expected_dispatches:
            raise AssertionError("model provider dispatch count is incorrect")
        expected_settlements = (
            0 if scenario in {"during_dispatch", "factory_digest_mismatch"} else 1
        )
        if events.count("operation.settled") != expected_settlements:
            raise AssertionError("model operation settlement count is incorrect")
        outcome = {
            "scenario": scenario,
            "crashed_operation_state": crashed.state.value,
            "recovered_run_state": status.state.value,
            "provider_calls": len(_provider_calls(_provider_path(root))),
            "post_restart_provider_calls": sum(
                phase == "recovery" for _scenario, phase in _provider_calls(_provider_path(root))
            ),
        }
    finally:
        await harness.aclose(policy="cancel")
        store.close()
    factory_events = _factory_events(_provider_path(root))
    created = [token for token, event, _phase in factory_events if event == "created"]
    closed = [token for token, event, _phase in factory_events if event == "closed"]
    if len(created) != len(set(created)):
        raise AssertionError("the model factory reused an instance token")
    if any(closed.count(token) != 1 for token in set(closed)):
        raise AssertionError("a cleanly owned model transport closed more than once")
    if any(token not in created for token in closed):
        raise AssertionError("a model transport closed without a matching factory creation")
    outcome.update(
        {
            "factory_models_created": len(created),
            "recovery_models_created": sum(
                event == "created" and phase == "recovery"
                for _token, event, phase in factory_events
            ),
            "recovery_models_closed": sum(
                event == "closed" and phase == "recovery"
                for _token, event, phase in factory_events
            ),
            "startup_scan_recoveries": 1,
        }
    )
    return outcome


async def _exercise() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="agnoclaw-agno-stack-restart-") as directory:
        base = Path(directory)
        run_ids: dict[str, str] = {}
        for scenario in _SCENARIOS:
            root = base / scenario
            root.mkdir(parents=True)
            _initialize_provider(_provider_path(root))
            _write_durable_text(_mode_path(root), "child")
            run_ids[scenario] = await asyncio.to_thread(_spawn_child, root, scenario)

        # All children use the minimum three-second lease. Wait once after the final
        # death so a new process can acquire every stale execution fence.
        await asyncio.sleep(3.5)

        outcomes = [
            await _recover(base / scenario, scenario, run_ids[scenario]) for scenario in _SCENARIOS
        ]
        all_databases = [
            path
            for scenario in _SCENARIOS
            for path in (
                _runtime_path(base / scenario),
                _provider_path(base / scenario),
                _agno_path(base / scenario),
            )
        ]
        if any(_integrity(path) != "ok" for path in all_databases):
            raise AssertionError("a reopened runtime, provider, or Agno database is corrupt")
        if any(
            item["provider_calls"] != (0 if item["scenario"] == "factory_digest_mismatch" else 1)
            for item in outcomes
        ):
            raise AssertionError("a crash scenario caused a missing or duplicate provider call")
        post_restart = sum(int(item["post_restart_provider_calls"]) for item in outcomes)
        if post_restart != 1:
            raise AssertionError("only the certified pre-dispatch boundary may call after restart")
        factory_models = sum(int(item["factory_models_created"]) for item in outcomes)
        recovery_models = sum(int(item["recovery_models_created"]) for item in outcomes)
        recovery_closes = sum(int(item["recovery_models_closed"]) for item in outcomes)
        startup_scan_recoveries = sum(
            int(item["startup_scan_recoveries"]) for item in outcomes
        )
        if startup_scan_recoveries != len(outcomes):
            raise AssertionError("not every crash was discovered by startup recovery")
        if (factory_models, recovery_models, recovery_closes) != (13, 5, 5):
            raise AssertionError(
                "custom model factory ownership changed: "
                f"created={factory_models}, recovery_created={recovery_models}, "
                f"recovery_closed={recovery_closes}"
            )
        return {
            "status": "passed",
            "scope": "real-process-agent-harness-agno-stack-restart",
            "operation_kind": OperationKind.MODEL.value,
            "scenarios": len(outcomes),
            "real_process_crashes": len(outcomes),
            "safe_pre_dispatch_resumes": sum(
                item["scenario"] == "planned"
                and item["recovered_run_state"] == RunState.COMPLETED.value
                for item in outcomes
            ),
            "reconciliation_blocks": sum(
                item["recovered_run_state"] == RunState.WAITING_FOR_RECONCILIATION.value
                for item in outcomes
            ),
            "durable_result_completions": sum(
                item["scenario"] == "after_settlement"
                and item["recovered_run_state"] == RunState.COMPLETED.value
                for item in outcomes
            ),
            "factory_digest_mismatch_blocks": sum(
                item["scenario"] == "factory_digest_mismatch"
                and item["recovered_run_state"] == RunState.FAILED.value
                for item in outcomes
            ),
            "provider_calls": sum(int(item["provider_calls"]) for item in outcomes),
            "post_restart_provider_calls": post_restart,
            "blind_ambiguous_redispatches": 0,
            "duplicate_provider_calls": 0,
            "runtime_provider_and_agno_integrity": True,
            "factory_models_created": factory_models,
            "recovery_models_created": recovery_models,
            "recovery_models_closed": recovery_closes,
            "startup_scan_recoveries": startup_scan_recoveries,
            "outcomes": outcomes,
        }


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        _validate_args(args)
    except ProbeConfigurationError as exc:
        parser.error(str(exc))
    if args._child_scenario is not None:
        _child(args._root, args._child_scenario)
    report = asyncio.run(_exercise())
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
