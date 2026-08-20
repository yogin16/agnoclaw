#!/usr/bin/env python3
"""Certify multi-step Agno tool-checkpoint recovery across real process death."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

from agno.db.sqlite import SqliteDb
from agno.models.openai.responses import OpenAIResponses
from agno.models.response import ModelResponse

from agnoclaw import (
    AgentHarness,
    AgnoModelFactory,
    CapabilityConcurrency,
    CapabilityKind,
    CapabilityLifetime,
    CapabilityRecovery,
    CapabilitySpec,
    CapabilityTrust,
    EffectClass,
    HarnessConfig,
)
from agnoclaw.runtime import (
    ArtifactReference,
    LocalArtifactStore,
    OperationIdempotencyConflictError,
    OperationIntent,
    OperationKind,
    OperationNotFoundError,
    OperationSettlement,
    OperationState,
    RunState,
    SQLiteRuntimeStore,
)
from agnoclaw.runtime.model_gateway import (
    has_valid_tool_batch_checkpoint,
    provider_call_ordinal,
)
from agnoclaw.runtime.store import StoredOperationDecision

_CRASH_EXIT_CODE = 89
_SCENARIOS = (
    "after_tool_checkpoint",
    "during_second_provider",
    "after_second_settlement",
)
_TOOL_NAME = "probe_effect"
_MODEL_FACTORY_DIGEST = "sha256:" + "c" * 64


class ProbeConfigurationError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Kill and reopen a two-provider-call Agno tool loop."
    )
    parser.add_argument("--allow-process-crash", action="store_true")
    parser.add_argument(
        "--scenario",
        choices=_SCENARIOS,
        help="Run one boundary instead of the complete certification matrix.",
    )
    parser.add_argument("--_child-scenario", choices=_SCENARIOS, help=argparse.SUPPRESS)
    parser.add_argument("--_root", type=Path, help=argparse.SUPPRESS)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args._child_scenario is not None:
        if args._root is None:
            raise ProbeConfigurationError("child mode requires --_root")
        return
    if args._root is not None:
        raise ProbeConfigurationError("--_root is internal")
    if not args.allow_process_crash:
        raise ProbeConfigurationError("--allow-process-crash is required")


def _path(root: Path, name: str) -> Path:
    return root / name


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, value.encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mode(root: Path) -> str:
    return _path(root, "mode").read_text(encoding="utf-8").strip()


def _initialize_evidence(root: Path) -> None:
    with sqlite3.connect(_path(root, "evidence.db")) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE provider_calls (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "scenario TEXT NOT NULL, phase TEXT NOT NULL, ordinal INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE tool_effects (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "scenario TEXT NOT NULL, phase TEXT NOT NULL, value TEXT NOT NULL)"
        )


def _record(root: Path, table: str, values: tuple[Any, ...]) -> None:
    columns = (
        "scenario, phase, ordinal" if table == "provider_calls" else "scenario, phase, value"
    )
    with sqlite3.connect(_path(root, "evidence.db"), timeout=10) as connection:
        connection.execute(
            f"INSERT INTO {table}({columns}) VALUES (?, ?, ?)",  # noqa: S608
            values,
        )
        connection.commit()


def _evidence(root: Path, table: str) -> list[tuple[Any, ...]]:
    columns = "scenario, phase, ordinal" if table == "provider_calls" else "scenario, phase, value"
    with sqlite3.connect(_path(root, "evidence.db")) as connection:
        return list(
            connection.execute(
                f"SELECT {columns} FROM {table} ORDER BY sequence"  # noqa: S608
            ).fetchall()
        )


async def _probe_ainvoke(model: Any, *args: Any, **kwargs: Any) -> ModelResponse:
    del model
    root = Path(os.environ["AGNOCLAW_TOOL_CHECKPOINT_PROBE_ROOT"])
    scenario = os.environ["AGNOCLAW_TOOL_CHECKPOINT_PROBE_SCENARIO"]
    ordinal = provider_call_ordinal(args, kwargs)
    phase = _mode(root)
    _record(root, "provider_calls", (scenario, phase, ordinal))
    if phase == "child" and scenario == "during_second_provider" and ordinal == 2:
        os._exit(_CRASH_EXIT_CODE)
    if ordinal == 1:
        return ModelResponse(
            tool_calls=[
                {
                    "id": "call-probe-effect-1",
                    "type": "function",
                    "function": {
                        "name": _TOOL_NAME,
                        "arguments": '{"value":"exactly-once"}',
                    },
                }
            ],
            provider_data={"request_id": f"{scenario}:provider:1"},
        )
    return ModelResponse(
        content=f"completed:{scenario}",
        provider_data={"request_id": f"{scenario}:provider:2"},
    )


class BoundaryRuntimeStore(SQLiteRuntimeStore):
    def __init__(self, path: Path, *, root: Path, scenario: str) -> None:
        self._probe_root = root
        self._probe_scenario = scenario
        super().__init__(path)

    def prepare_operation(self, intent: OperationIntent) -> StoredOperationDecision:
        if (
            _mode(self._probe_root) == "child"
            and self._probe_scenario == "after_tool_checkpoint"
            and intent.operation_id.endswith(":provider:000002")
        ):
            os._exit(_CRASH_EXIT_CODE)
        try:
            return super().prepare_operation(intent)
        except OperationIdempotencyConflictError:
            existing = self.get_operation(intent.operation_id)
            self.last_prepare_conflict = {
                "operation_id": intent.operation_id,
                "existing_request_digest": existing.intent.request_digest,
                "attempted_request_digest": intent.request_digest,
                "existing_intent_digest": existing.intent.digest,
                "attempted_intent_digest": intent.digest,
                "existing_request_components": dict(
                    existing.intent.metadata.get("request_components", {})
                ),
                "attempted_request_components": dict(
                    intent.metadata.get("request_components", {})
                ),
            }
            raise

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
            and self._probe_scenario == "after_second_settlement"
            and operation_id.endswith(":provider:000002")
            and settlement.state is OperationState.SUCCEEDED
        ):
            os._exit(_CRASH_EXIT_CODE)
        return decision


def _capability(root: Path, scenario: str) -> CapabilitySpec:
    def effect(value: str) -> dict[str, str]:
        _record(root, "tool_effects", (scenario, _mode(root), value))
        return {"stored": value}

    return CapabilitySpec(
        name=_TOOL_NAME,
        version="1.0.0",
        kind=CapabilityKind.TOOL,
        effect_class=EffectClass.NON_REPEATABLE,
        trust=CapabilityTrust.VERIFIED,
        lifetime=CapabilityLifetime.RUN,
        concurrency=CapabilityConcurrency.ISOLATED,
        recovery=CapabilityRecovery.RECONCILABLE,
        implementation_digest="sha256:tool-checkpoint-probe-v1",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        factory=lambda: effect,
    )


def _model_factory() -> AgnoModelFactory:
    return AgnoModelFactory(
        model_id="provider-checkpoint-probe",
        provider="OpenAI",
        implementation_digest=_MODEL_FACTORY_DIGEST,
        factory=lambda: OpenAIResponses(id="provider-checkpoint-probe"),
    )


def _assert_factory_manifest(harness: AgentHarness) -> None:
    model_resource = next(
        item
        for item in harness.runtime_manifest().to_dict()["resources"]
        if item["resource_id"] == "model"
    )
    if (
        model_resource["trust"],
        model_resource["lifetime"],
        model_resource["concurrency"],
        model_resource["recovery"],
    ) != ("factory", "run", "isolated", "recreatable"):
        raise AssertionError("the multi-step probe did not compile a run-owned factory model")


def _harness(root: Path, scenario: str) -> tuple[AgentHarness, BoundaryRuntimeStore]:
    os.environ["AGNOCLAW_TOOL_CHECKPOINT_PROBE_ROOT"] = str(root)
    os.environ["AGNOCLAW_TOOL_CHECKPOINT_PROBE_SCENARIO"] = scenario
    OpenAIResponses.ainvoke = _probe_ainvoke
    store = BoundaryRuntimeStore(_path(root, "runtime.db"), root=root, scenario=scenario)
    harness = AgentHarness(
        name="agno-tool-checkpoint-probe",
        agent_id="agno-tool-checkpoint-probe",
        model=_model_factory(),
        capabilities=[_capability(root, scenario)],
        include_default_tools=False,
        user_id="restart-user",
        workspace_dir=_path(root, "workspace"),
        config=HarnessConfig(
            enable_plugins=False,
            workspace_dir=str(_path(root, "workspace")),
            runtime_lease_seconds=3,
            runtime_lease_renew_interval_seconds=1,
        ),
        db=SqliteDb(db_file=str(_path(root, "agno.db"))),
        runtime_store=store,
        artifact_store=LocalArtifactStore(_path(root, "artifacts")),
    )
    _assert_factory_manifest(harness)
    return harness, store


async def _child_run(root: Path, scenario: str) -> NoReturn:
    harness, _store = _harness(root, scenario)
    run = await harness.start(
        "execute the exact probe effect and report completion",
        session_id=f"checkpoint-session-{scenario}",
        idempotency_key=f"tool-checkpoint:{scenario}",
    )
    _write_text(_path(root, "run-id"), run.run_id)
    await run.wait(timeout=30)
    raise RuntimeError("child unexpectedly completed")


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
            f"scenario {scenario} did not crash at its boundary: "
            f"returncode={completed.returncode}, stderr={completed.stderr[-1000:]!r}"
        )
    return _path(root, "run-id").read_text(encoding="utf-8").strip()


async def _recover(root: Path, scenario: str, run_id: str) -> dict[str, Any]:
    _write_text(_path(root, "mode"), "recovery")
    harness, store = _harness(root, scenario)
    try:
        checkpoint = await harness._base_agent.aget_run_output(
            run_id,
            session_id=f"checkpoint-session-{scenario}",
            user_id="restart-user",
        )
        if not has_valid_tool_batch_checkpoint(checkpoint):
            raise AssertionError("the child did not persist an exact tool-batch checkpoint")
        outer = store.get_operation(f"{run_id}:model:1")
        if outer.state is not OperationState.DISPATCHING:
            raise AssertionError("the replayable outer loop was not left dispatching")
        provider_one = store.get_operation(f"{run_id}:provider:000001")
        if provider_one.state is not OperationState.SUCCEEDED:
            raise AssertionError("the first provider response was not settled")
        capability = next(
            record
            for record in store.list_run_operations(run_id)
            if ":capability:" in record.intent.operation_id
        )
        if capability.state is not OperationState.SUCCEEDED:
            raise AssertionError("the tool effect was not settled before checkpointing")

        try:
            second_before = store.get_operation(f"{run_id}:provider:000002").state
        except OperationNotFoundError:
            second_before = None
        expected_before = {
            "after_tool_checkpoint": None,
            "during_second_provider": OperationState.DISPATCHING,
            "after_second_settlement": OperationState.SUCCEEDED,
        }[scenario]
        if second_before is not expected_before:
            raise AssertionError(
                f"unexpected second-provider boundary: {second_before!r} != {expected_before!r}"
            )

        recovered = await harness.recover_run(run_id)
        expected_state = (
            RunState.WAITING_FOR_RECONCILIATION
            if scenario == "during_second_provider"
            else RunState.COMPLETED
        )
        if expected_state is RunState.COMPLETED:
            result = await recovered.wait(timeout=15)
            if getattr(result, "content", None) != f"completed:{scenario}":
                raise AssertionError(
                    "checkpoint continuation did not restore exact output: "
                    f"content={getattr(result, 'content', None)!r}, "
                    f"conflict={getattr(store, 'last_prepare_conflict', None)!r}"
                )
        status = await recovered.status()
        if status.state is not expected_state:
            raise AssertionError(f"unexpected recovered state: {status.state.value}")

        provider_calls = _evidence(root, "provider_calls")
        effects = _evidence(root, "tool_effects")
        if len(effects) != 1 or effects[0][2] != "exactly-once":
            raise AssertionError("the external tool effect was missing or duplicated")
        return {
            "scenario": scenario,
            "second_provider_before_recovery": (
                second_before.value if second_before is not None else "absent"
            ),
            "recovered_run_state": status.state.value,
            "provider_calls": len(provider_calls),
            "post_restart_provider_calls": sum(row[1] == "recovery" for row in provider_calls),
            "tool_effects": len(effects),
            "checkpoint_message_index": checkpoint.last_checkpoint_at_message_index,
        }
    finally:
        await harness.aclose(policy="cancel")
        store.close()


def _integrity(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


async def _exercise(scenarios: tuple[str, ...] = _SCENARIOS) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="agnoclaw-tool-checkpoint-") as directory:
        base = Path(directory)
        run_ids: dict[str, str] = {}
        for scenario in scenarios:
            root = base / scenario
            root.mkdir()
            _initialize_evidence(root)
            _write_text(_path(root, "mode"), "child")
            run_ids[scenario] = await asyncio.to_thread(_spawn_child, root, scenario)
        await asyncio.sleep(3.5)

        outcomes = [
            await _recover(base / scenario, scenario, run_ids[scenario])
            for scenario in scenarios
        ]
        databases = [
            _path(base / scenario, name)
            for scenario in scenarios
            for name in ("runtime.db", "agno.db", "evidence.db")
        ]
        if any(_integrity(path) != "ok" for path in databases):
            raise AssertionError("a reopened runtime, Agno, or evidence database is corrupt")
        if any(item["provider_calls"] != 2 for item in outcomes):
            raise AssertionError("a scenario caused a missing or duplicate provider call")
        expected_restart_calls = {
            "after_tool_checkpoint": 1,
            "during_second_provider": 0,
            "after_second_settlement": 0,
        }
        if any(
            item["post_restart_provider_calls"] != expected_restart_calls[item["scenario"]]
            for item in outcomes
        ):
            raise AssertionError("only the pre-second-dispatch checkpoint may call after restart")
        return {
            "status": "passed",
            "scope": "real-process-agno-tool-checkpoint-restart",
            "operation_kind": OperationKind.MODEL.value,
            "model_construction": "public_agno_model_factory",
            "scenarios": len(outcomes),
            "real_process_crashes": len(outcomes),
            "checkpoint_continuations": sum(
                item["recovered_run_state"] == RunState.COMPLETED.value
                for item in outcomes
            ),
            "reconciliation_blocks": sum(
                item["recovered_run_state"]
                == RunState.WAITING_FOR_RECONCILIATION.value
                for item in outcomes
            ),
            "provider_calls": sum(item["provider_calls"] for item in outcomes),
            "post_restart_provider_calls": sum(
                item["post_restart_provider_calls"] for item in outcomes
            ),
            "tool_effects": sum(item["tool_effects"] for item in outcomes),
            "duplicate_provider_calls": 0,
            "duplicate_tool_effects": 0,
            "database_integrity": True,
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
        _child_root = args._root
        if _child_root is None:  # pragma: no cover - validated above
            raise AssertionError("child root missing")
        asyncio.run(_child_run(_child_root, args._child_scenario))
        return 1
    scenarios = (args.scenario,) if args.scenario is not None else _SCENARIOS
    print(json.dumps(asyncio.run(_exercise(scenarios)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
