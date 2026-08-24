#!/usr/bin/env python3
"""Certify durable Agno capability approval across real process death."""

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
    ApprovalRequest,
    ApprovalState,
    LifecycleTransition,
    LocalArtifactStore,
    OperationIdempotencyConflictError,
    OperationIntent,
    OperationKind,
    OperationNotFoundError,
    OperationState,
    RunOwner,
    RunState,
    SQLiteRuntimeStore,
    TerminalRecord,
    TransitionKind,
)
from agnoclaw.runtime.checkpoints import load_runtime_request_checkpoint
from agnoclaw.runtime.model_gateway import (
    has_valid_tool_batch_checkpoint,
    provider_call_ordinal,
)
from agnoclaw.runtime.store import StoredOperationDecision, StoredTransitionDecision

_CRASH_EXIT_CODE = 89
_SCENARIOS = ("during_approval_wait",)
_TOOL_NAME = "approval_probe_effect"
_TENANT_ID = "approval-probe-tenant"
_USER_ID = "approval-probe-user"
_SESSION_ID = "approval-probe-session"
_MODEL_FACTORY_DIGEST = "sha256:" + "d" * 64


class ProbeConfigurationError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Kill an Agno tool loop after committing its durable approval wait."
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
            "phase TEXT NOT NULL, ordinal INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE tool_effects (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "phase TEXT NOT NULL, value TEXT NOT NULL)"
        )


def _record(root: Path, table: str, values: tuple[Any, ...]) -> None:
    columns = "phase, ordinal" if table == "provider_calls" else "phase, value"
    with sqlite3.connect(_path(root, "evidence.db"), timeout=10) as connection:
        connection.execute(
            f"INSERT INTO {table}({columns}) VALUES (?, ?)",  # noqa: S608
            values,
        )
        connection.commit()


def _evidence(root: Path, table: str) -> list[tuple[Any, ...]]:
    columns = "phase, ordinal" if table == "provider_calls" else "phase, value"
    with sqlite3.connect(_path(root, "evidence.db")) as connection:
        return list(
            connection.execute(
                f"SELECT {columns} FROM {table} ORDER BY sequence"  # noqa: S608
            ).fetchall()
        )


async def _probe_ainvoke(model: Any, *args: Any, **kwargs: Any) -> ModelResponse:
    del model
    root = Path(os.environ["AGNOCLAW_APPROVAL_RESTART_PROBE_ROOT"])
    ordinal = provider_call_ordinal(args, kwargs)
    phase = _mode(root)
    _record(root, "provider_calls", (phase, ordinal))
    if ordinal == 1:
        return ModelResponse(
            tool_calls=[
                {
                    "id": "call-durable-approval-1",
                    "type": "function",
                    "function": {
                        "name": _TOOL_NAME,
                        "arguments": '{"value":"exactly-once-after-approval"}',
                    },
                }
            ],
            provider_data={"request_id": "approval-probe:provider:1"},
        )
    return ModelResponse(
        content="completed:durable-approval-restart",
        provider_data={"request_id": "approval-probe:provider:2"},
    )


class BoundaryRuntimeStore(SQLiteRuntimeStore):
    """Crash only after the approval wait transaction has returned committed."""

    def __init__(self, path: Path, *, root: Path) -> None:
        self._probe_root = root
        super().__init__(path)

    def prepare_operation(self, intent: OperationIntent) -> StoredOperationDecision:
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

    def apply_transition(
        self,
        transition: LifecycleTransition,
        *,
        expected_revision: int,
        terminal: TerminalRecord | None = None,
        approval_request: ApprovalRequest | None = None,
    ) -> StoredTransitionDecision:
        decision = super().apply_transition(
            transition,
            expected_revision=expected_revision,
            terminal=terminal,
            approval_request=approval_request,
        )
        if (
            _mode(self._probe_root) == "child"
            and transition.kind is TransitionKind.WAIT_FOR_APPROVAL
            and approval_request is not None
        ):
            os._exit(_CRASH_EXIT_CODE)
        return decision


def _capability(root: Path) -> CapabilitySpec:
    def effect(value: str) -> dict[str, str]:
        _record(root, "tool_effects", (_mode(root), value))
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
        implementation_digest="sha256:durable-approval-restart-probe-v1",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        factory=lambda: effect,
    )


def _model_factory() -> AgnoModelFactory:
    return AgnoModelFactory(
        model_id="durable-approval-restart-probe",
        provider="OpenAI",
        implementation_digest=_MODEL_FACTORY_DIGEST,
        factory=lambda: OpenAIResponses(id="durable-approval-restart-probe"),
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
        raise AssertionError("the approval probe did not compile a run-owned factory model")


def _harness(
    root: Path,
) -> tuple[AgentHarness, BoundaryRuntimeStore, LocalArtifactStore]:
    os.environ["AGNOCLAW_APPROVAL_RESTART_PROBE_ROOT"] = str(root)
    OpenAIResponses.ainvoke = _probe_ainvoke
    store = BoundaryRuntimeStore(_path(root, "runtime.db"), root=root)
    artifacts = LocalArtifactStore(_path(root, "artifacts"))
    harness = AgentHarness(
        name="agno-durable-approval-restart-probe",
        agent_id="agno-durable-approval-restart-probe",
        model=_model_factory(),
        capabilities=[_capability(root)],
        include_default_tools=False,
        tenant_id=_TENANT_ID,
        user_id=_USER_ID,
        session_id=_SESSION_ID,
        permission_mode="default",
        permission_require_approver=True,
        workspace_dir=_path(root, "workspace"),
        config=HarnessConfig(
            enable_plugins=False,
            workspace_dir=str(_path(root, "workspace")),
            permission_approval_poll_interval_seconds=0.05,
            runtime_lease_seconds=3,
            runtime_lease_renew_interval_seconds=1,
        ),
        db=SqliteDb(db_file=str(_path(root, "agno.db"))),
        runtime_store=store,
        artifact_store=artifacts,
    )
    _assert_factory_manifest(harness)
    return harness, store, artifacts


async def _child_run(root: Path, scenario: str) -> NoReturn:
    if scenario not in _SCENARIOS:  # pragma: no cover - argparse validates this
        raise AssertionError(f"unknown scenario: {scenario}")
    harness, _store, _artifacts = _harness(root)
    run = await harness.start(
        "execute the approval probe effect and report completion",
        idempotency_key="durable-approval-restart",
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


async def _recover(root: Path, run_id: str) -> dict[str, Any]:
    _write_text(_path(root, "mode"), "recovery")
    harness, store, artifacts = _harness(root)
    owner = RunOwner(tenant_id=_TENANT_ID, user_id=_USER_ID)
    try:
        waiting = store.get_run(run_id, owner=owner)
        if (
            waiting.state is not RunState.WAITING_FOR_APPROVAL
            or waiting.pending_request_id is None
        ):
            raise AssertionError("the child did not commit an exact approval wait")
        approvals = store.list_approvals(run_id, owner=owner)
        if (
            len(approvals) != 1
            or approvals[0].state is not ApprovalState.PENDING
            or approvals[0].request.request_id != waiting.pending_request_id
        ):
            raise AssertionError("the child did not commit exactly one pending approval")

        outer = store.get_operation(f"{run_id}:model:1", owner=owner)
        provider_one = store.get_operation(f"{run_id}:provider:000001", owner=owner)
        if outer.state is not OperationState.DISPATCHING:
            raise AssertionError("the durable outer model loop was not left dispatching")
        if provider_one.state is not OperationState.SUCCEEDED:
            raise AssertionError("the first provider response was not durably settled")
        try:
            store.get_operation(f"{run_id}:provider:000002", owner=owner)
        except OperationNotFoundError:
            pass
        else:
            raise AssertionError("the second provider call started before approval")
        if _evidence(root, "tool_effects"):
            raise AssertionError("the tool effect ran before durable approval")

        agno_run = await harness._base_agent.aget_run_output(
            run_id,
            session_id=_SESSION_ID,
            user_id=_USER_ID,
        )
        if has_valid_tool_batch_checkpoint(agno_run):
            raise AssertionError("approval wait unexpectedly persisted a tool-result checkpoint")

        recovered_request = await load_runtime_request_checkpoint(
            store=store,
            artifact_store=artifacts,
            snapshot=waiting,
            owner=owner,
            harness_spec_digest=harness._spec.settings_digest,
        )
        settled = harness.decide_capability_approval(
            approvals[0].request.request_id,
            run_id=run_id,
            approved=True,
            issuer="approval-probe-operator",
            reason_code="APPROVED_BY_OPERATOR",
            context=recovered_request.context,
        )
        replayed_decision = harness.decide_capability_approval(
            approvals[0].request.request_id,
            run_id=run_id,
            approved=True,
            issuer="approval-probe-operator",
            reason_code="APPROVED_BY_OPERATOR",
            context=recovered_request.context,
        )
        resumed = store.get_run(run_id, owner=owner)
        if (
            settled.state is not ApprovalState.APPROVED
            or replayed_decision != settled
            or resumed.state is not RunState.RUNNING
            or resumed.pending_request_id is not None
        ):
            raise AssertionError("the host decision did not atomically resume the waiting run")

        recovered = await harness.recover_run(run_id, context=recovered_request.context)
        result = await recovered.wait(timeout=20)
        status = await recovered.status()
        if status.state is not RunState.COMPLETED:
            raise AssertionError(f"unexpected recovered state: {status.state.value}")
        if getattr(result, "content", None) != "completed:durable-approval-restart":
            raise AssertionError(
                "approval recovery did not restore exact output: "
                f"{getattr(result, 'content', None)!r}; "
                f"conflict={getattr(store, 'last_prepare_conflict', None)!r}"
            )

        provider_calls = _evidence(root, "provider_calls")
        effects = _evidence(root, "tool_effects")
        final_approvals = store.list_approvals(run_id, owner=owner)
        if provider_calls != [("child", 1), ("recovery", 2)]:
            raise AssertionError(f"provider replay was missing or duplicated: {provider_calls!r}")
        if effects != [("recovery", "exactly-once-after-approval")]:
            raise AssertionError(f"tool effect was missing or duplicated: {effects!r}")
        if len(final_approvals) != 1 or final_approvals[0].state is not ApprovalState.APPROVED:
            raise AssertionError("approval replay created or lost approval evidence")
        capability_operations = [
            record
            for record in store.list_run_operations(run_id, owner=owner)
            if ":capability:" in record.intent.operation_id
        ]
        if (
            len(capability_operations) != 1
            or capability_operations[0].state is not OperationState.SUCCEEDED
        ):
            raise AssertionError("capability recovery did not settle exactly one effect operation")
        approval_events = [
            event.event_type
            for event in store.list_events(run_id, owner=owner)
            if event.event_type.startswith("approval.")
        ]
        if approval_events.count("approval.requested") != 1:
            raise AssertionError("approval request was duplicated during replay")
        return {
            "scenario": _SCENARIOS[0],
            "state_before_decision": waiting.state.value,
            "state_after_decision": resumed.state.value,
            "recovered_run_state": status.state.value,
            "provider_calls": len(provider_calls),
            "post_restart_provider_calls": sum(
                phase == "recovery" for phase, _ordinal in provider_calls
            ),
            "approval_requests": approval_events.count("approval.requested"),
            "approved_records": sum(
                record.state is ApprovalState.APPROVED for record in final_approvals
            ),
            "tool_effects": len(effects),
            "tool_checkpoint_before_decision": False,
        }
    finally:
        await harness.aclose(policy="cancel")
        store.close()


def _integrity(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


async def _exercise(scenarios: tuple[str, ...] = _SCENARIOS) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="agnoclaw-approval-restart-") as directory:
        base = Path(directory)
        outcomes = []
        for scenario in scenarios:
            root = base / scenario
            root.mkdir()
            _initialize_evidence(root)
            _write_text(_path(root, "mode"), "child")
            run_id = await asyncio.to_thread(_spawn_child, root, scenario)
            await asyncio.sleep(3.5)
            outcomes.append(await _recover(root, run_id))
            databases = tuple(
                _path(root, name) for name in ("runtime.db", "agno.db", "evidence.db")
            )
            if any(_integrity(path) != "ok" for path in databases):
                raise AssertionError("a reopened runtime, Agno, or evidence database is corrupt")
        return {
            "status": "passed",
            "scope": "real-process-agno-durable-approval-restart",
            "operation_kind": OperationKind.MODEL.value,
            "model_construction": "public_agno_model_factory",
            "scenarios": len(outcomes),
            "real_process_crashes": len(outcomes),
            "approval_wait_recoveries": sum(
                item["recovered_run_state"] == RunState.COMPLETED.value
                for item in outcomes
            ),
            "provider_calls": sum(item["provider_calls"] for item in outcomes),
            "post_restart_provider_calls": sum(
                item["post_restart_provider_calls"] for item in outcomes
            ),
            "approval_requests": sum(item["approval_requests"] for item in outcomes),
            "approved_records": sum(item["approved_records"] for item in outcomes),
            "tool_effects": sum(item["tool_effects"] for item in outcomes),
            "duplicate_provider_calls": 0,
            "duplicate_approval_requests": 0,
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
        child_root = args._root
        if child_root is None:  # pragma: no cover - validated above
            raise AssertionError("child root missing")
        asyncio.run(_child_run(child_root, args._child_scenario))
        return 1
    scenarios = (args.scenario,) if args.scenario is not None else _SCENARIOS
    print(json.dumps(asyncio.run(_exercise(scenarios)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
