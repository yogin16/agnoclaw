"""Explicit startup-recovery classification without unsafe replay."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agnoclaw import AgentHarness, HarnessConfig
from agnoclaw.runtime import (
    ArtifactScope,
    ChildRunBudget,
    ChildRunSpec,
    EffectClass,
    ExecutionContext,
    LocalArtifactStore,
    OperationIntent,
    OperationKind,
    OperationSettlement,
    OperationState,
    RunOwner,
    RunSnapshot,
    RunState,
    RuntimeLeaseUnavailableError,
    RunWaitError,
    TerminalRecord,
)
from agnoclaw.runtime.checkpoints import (
    RuntimeRequestCheckpoint,
    persist_runtime_request_checkpoint,
)
from agnoclaw.runtime.lifecycle import LifecycleTransition, TransitionKind
from agnoclaw.runtime.recovery import inspect_child_recovery
from agnoclaw.runtime.store import RuntimeStoreOverloadedError, SQLiteRuntimeStore


class ResumedAgent:
    calls = 0
    messages: list[str] = []

    def __init__(self, **kwargs):
        self.system_message = kwargs["system_message"]
        self.session_id = kwargs.get("session_id")
        self.user_id = kwargs.get("user_id")
        self.tools = list(kwargs.get("tools") or [])
        self.db = kwargs.get("db")
        self.learning = kwargs.get("learning")

    async def arun(self, message, **_kwargs):
        self.__class__.calls += 1
        self.__class__.messages.append(message)
        return SimpleNamespace(content="resumed")


def _harness(tmp_path, store, *, artifact_store=None):
    with patch("agnoclaw.agent.Agent", return_value=MagicMock()):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            return AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path / "workspace",
                config=HarnessConfig(enable_plugins=False),
                include_default_tools=False,
                runtime_store=store,
                artifact_store=artifact_store,
            )


def _running(store: SQLiteRuntimeStore, run_id: str) -> RunSnapshot:
    created = store.create_run(RunSnapshot(run_id=run_id, session_id=f"session-{run_id}")).snapshot
    queued = store.apply_transition(
        LifecycleTransition(
            run_id=run_id,
            kind=TransitionKind.QUEUE,
            transition_id=f"{run_id}:queue",
        ),
        expected_revision=created.revision,
    ).lifecycle.after
    return store.apply_transition(
        LifecycleTransition(
            run_id=run_id,
            kind=TransitionKind.START,
            transition_id=f"{run_id}:start",
        ),
        expected_revision=queued.revision,
    ).lifecycle.after


def _model_intent(run_id: str) -> OperationIntent:
    return OperationIntent(
        operation_id=f"{run_id}:model:1",
        run_id=run_id,
        attempt_id=f"{run_id}:attempt:1",
        kind=OperationKind.MODEL,
        target="custom:model",
        request_digest="sha256:request",
        effect_class=EffectClass.NON_REPEATABLE,
    )


def test_child_lineage_inspection_does_not_permanentize_transient_store_failure():
    store = MagicMock()
    store.get_child_spec.side_effect = RuntimeStoreOverloadedError(
        backend="postgres",
        retry_after_seconds=1,
    )
    child = RunSnapshot(
        run_id="run-child",
        parent_run_id="run-parent",
        root_run_id="run-root",
        child_depth=2,
    )

    with pytest.raises(RuntimeStoreOverloadedError):
        inspect_child_recovery(store, child, owner=RunOwner(None, None))


def _child(
    store: SQLiteRuntimeStore,
    parent: RunSnapshot,
    *,
    delegation_id: str,
    budget: ChildRunBudget,
    result_schema=None,
) -> tuple[ChildRunSpec, RunSnapshot]:
    spec = ChildRunSpec.for_parent(
        parent,
        delegation_id=delegation_id,
        purpose_code="recovery",
        budget=budget,
        result_schema=result_schema,
    )
    created = store.create_run(
        RunSnapshot(
            run_id=spec.child_run_id,
            session_id=f"child:{spec.child_run_id}",
            parent_run_id=spec.parent_run_id,
            root_run_id=spec.root_run_id,
            child_depth=spec.depth,
        ),
        idempotency_scope="-:-",
        idempotency_key=f"child:{parent.run_id}:{delegation_id}",
        request_digest="sha256:" + f"{spec.depth % 16:x}" * 64,
        child_spec=spec,
    ).snapshot
    queued = store.apply_transition(
        LifecycleTransition(
            run_id=created.run_id,
            kind=TransitionKind.QUEUE,
            transition_id=f"{created.run_id}:queue",
        ),
        expected_revision=created.revision,
    ).lifecycle.after
    running = store.apply_transition(
        LifecycleTransition(
            run_id=created.run_id,
            kind=TransitionKind.START,
            transition_id=f"{created.run_id}:start",
        ),
        expected_revision=queued.revision,
    ).lifecycle.after
    return spec, running


@pytest.mark.asyncio
async def test_recovery_parks_interrupted_model_dispatch_for_reconciliation(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    _running(store, "run-1")
    prepared = store.prepare_operation(_model_intent("run-1"))
    store.begin_operation(
        "run-1:model:1",
        mutation_id="dispatch",
        expected_revision=prepared.record.revision,
        worker_id="dead-worker",
        fence_token=1,
    )
    harness = _harness(tmp_path, store)

    recovered = await harness.recover_run("run-1")

    assert (await recovered.status()).state is RunState.WAITING_FOR_RECONCILIATION
    assert store.get_terminal("run-1") is None
    event = store.list_events("run-1")[-1]
    assert event.event_type == "run.state.changed"
    assert event.payload["after"] == RunState.WAITING_FOR_RECONCILIATION.value


@pytest.mark.asyncio
async def test_recovery_without_checkpoint_fails_known_and_never_dispatches(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    created = store.create_run(RunSnapshot(run_id="run-1")).snapshot
    store.apply_transition(
        LifecycleTransition(
            run_id="run-1",
            kind=TransitionKind.QUEUE,
            transition_id="queue",
        ),
        expected_revision=created.revision,
    )
    harness = _harness(tmp_path, store)

    recovered = await harness.recover_run("run-1")

    with pytest.raises(RunWaitError) as failure:
        await recovered.wait()
    assert failure.value.code == "RUN_FAILED"
    terminal = store.get_terminal("run-1")
    assert terminal is not None
    assert terminal.error["code"] == "RUN_RECOVERY_CHECKPOINT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_known_operation_success_without_artifact_is_not_replayed(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    _running(store, "run-1")
    prepared = store.prepare_operation(_model_intent("run-1"))
    dispatching = store.begin_operation(
        "run-1:model:1",
        mutation_id="dispatch",
        expected_revision=prepared.record.revision,
        worker_id="dead-worker",
        fence_token=1,
    )
    store.settle_operation(
        "run-1:model:1",
        mutation_id="settle",
        expected_revision=dispatching.record.revision,
        fence_token=1,
        settlement=OperationSettlement(
            state=OperationState.SUCCEEDED,
            result_reference="result:sha256:known",
        ),
    )
    harness = _harness(tmp_path, store)

    recovered = await harness.recover_run("run-1")

    with pytest.raises(RunWaitError):
        await recovered.wait()
    terminal = store.get_terminal("run-1")
    assert terminal is not None
    assert terminal.error["code"] == "RUN_RECOVERY_RESULT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_known_success_result_artifact_completes_after_restart_without_replay(
    tmp_path,
):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    _running(store, "run-1")
    prepared = store.prepare_operation(_model_intent("run-1"))
    dispatching = store.begin_operation(
        "run-1:model:1",
        mutation_id="dispatch",
        expected_revision=prepared.record.revision,
        worker_id="dead-worker",
        fence_token=1,
    )
    reference = await artifacts.stage_json(
        {"content": "durable-result"},
        scope=ArtifactScope(run_id="run-1"),
        purpose="operation_result",
    )
    store.settle_operation(
        "run-1:model:1",
        mutation_id="settle",
        expected_revision=dispatching.record.revision,
        fence_token=1,
        settlement=OperationSettlement(
            state=OperationState.SUCCEEDED,
            result_reference=reference.artifact_id,
        ),
        artifact_reference=reference,
    )
    harness = _harness(tmp_path, store, artifact_store=artifacts)

    recovered = await harness.recover_run("run-1")

    assert await recovered.wait() == {"content": "durable-result"}
    assert (await recovered.status()).state is RunState.COMPLETED
    assert store.get_terminal("run-1").value == {"content": "durable-result"}
    assert [event.event_type for event in store.list_events("run-1")].count(
        "operation.dispatching"
    ) == 1


@pytest.mark.asyncio
async def test_child_recovery_reapplies_output_schema_before_completion(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    parent = _running(store, "run-parent")
    spec = ChildRunSpec.for_parent(
        parent,
        delegation_id="structured",
        purpose_code="research",
        result_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
    child = store.create_run(
        RunSnapshot(
            run_id=spec.child_run_id,
            session_id=f"child:{spec.child_run_id}",
            parent_run_id=parent.run_id,
            root_run_id=parent.run_id,
            child_depth=1,
        ),
        idempotency_scope="-:-",
        idempotency_key="child:run-parent:structured",
        request_digest="sha256:" + "1" * 64,
        child_spec=spec,
    ).snapshot
    queued = store.apply_transition(
        LifecycleTransition(
            run_id=child.run_id,
            kind=TransitionKind.QUEUE,
            transition_id=f"{child.run_id}:queue",
        ),
        expected_revision=child.revision,
    ).lifecycle.after
    store.apply_transition(
        LifecycleTransition(
            run_id=child.run_id,
            kind=TransitionKind.START,
            transition_id=f"{child.run_id}:start",
        ),
        expected_revision=queued.revision,
    )
    prepared = store.prepare_operation(_model_intent(child.run_id))
    dispatching = store.begin_operation(
        f"{child.run_id}:model:1",
        mutation_id="dispatch",
        expected_revision=prepared.record.revision,
        worker_id="dead-worker",
        fence_token=1,
    )
    reference = await artifacts.stage_json(
        {"content": {"answer": 42}},
        scope=ArtifactScope(run_id=child.run_id),
        purpose="operation_result",
    )
    store.settle_operation(
        f"{child.run_id}:model:1",
        mutation_id="settle",
        expected_revision=dispatching.record.revision,
        fence_token=1,
        settlement=OperationSettlement(
            state=OperationState.SUCCEEDED,
            result_reference=reference.artifact_id,
            usage={"reported": True, "total_tokens": 10},
            cost={"reported": True, "microusd": 1},
        ),
        artifact_reference=reference,
    )
    harness = _harness(tmp_path, store, artifact_store=artifacts)

    recovered = await harness.recover_run(child.run_id)

    with pytest.raises(RunWaitError) as failed:
        await recovered.wait()
    assert failed.value.safe_error["code"] == "CHILD_OUTPUT_SCHEMA_MISMATCH"
    assert store.get_operation(f"{child.run_id}:model:1").state is OperationState.SUCCEEDED
    assert [
        event.payload["valid"]
        for event in store.list_events(child.run_id)
        if event.event_type == "run.child.output.validated"
    ] == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize("model_planned", [False, True])
async def test_settled_pre_model_checkpoint_continues_once_after_restart(
    tmp_path,
    model_planned,
):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    snapshot = _running(store, "run-1")
    harness = _harness(tmp_path, store, artifact_store=artifacts)
    context = ExecutionContext.create(
        user_id=None,
        session_id=snapshot.session_id,
        workspace_id=str(tmp_path / "workspace"),
    )
    checkpoint = RuntimeRequestCheckpoint.create(
        run_id="run-1",
        message="continue durable work",
        context=context,
        kwargs={"learning_consent": False},
        harness_spec_digest=harness._spec.settings_digest,
    )
    await persist_runtime_request_checkpoint(
        checkpoint,
        store=store,
        artifact_store=artifacts,
        worker_id="dead-worker",
    )
    if model_planned:
        store.prepare_operation(
            OperationIntent(
                operation_id="run-1:model:1",
                run_id="run-1",
                attempt_id="run-1:attempt:1",
                kind=OperationKind.MODEL,
                target=harness.model_name,
                request_digest=checkpoint.request_digest,
                effect_class=EffectClass.NON_REPEATABLE,
                metadata={
                    "harness_spec_digest": harness._spec.settings_digest,
                    "operation_ordinal": 1,
                },
            )
        )
    ResumedAgent.calls = 0
    ResumedAgent.messages = []
    harness._agent_constructor = ResumedAgent

    recovered = await harness.recover_run("run-1")
    result = await recovered.wait(timeout=2)

    assert result.content == "resumed"
    assert ResumedAgent.calls == 1
    assert ResumedAgent.messages == ["continue durable work"]
    assert (await recovered.status()).state is RunState.COMPLETED
    assert store.get_operation("run-1:model:1").state is OperationState.SUCCEEDED
    assert [
        event.event_type
        for event in store.list_events("run-1")
        if event.payload.get("operation_id") == "run-1:model:1"
    ].count("operation.dispatching") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("model_planned", [False, True])
async def test_planned_child_restart_restores_timeout_and_output_contract(
    tmp_path,
    model_planned,
):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    parent = _running(store, "run-parent")
    budget = ChildRunBudget(timeout_seconds=37)
    spec, child = _child(
        store,
        parent,
        delegation_id="restart-child",
        budget=budget,
        result_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
    harness = _harness(tmp_path, store, artifact_store=artifacts)
    context = ExecutionContext.create(
        user_id=None,
        session_id=child.session_id,
        workspace_id=str(tmp_path / "workspace"),
    )
    checkpoint = RuntimeRequestCheckpoint.create(
        run_id=child.run_id,
        message="continue bounded child",
        context=context,
        kwargs={"learning_consent": False, "persist_output": False},
        harness_spec_digest=harness._spec.settings_digest,
    )
    await persist_runtime_request_checkpoint(
        checkpoint,
        store=store,
        artifact_store=artifacts,
        worker_id="dead-worker",
    )
    if model_planned:
        store.prepare_operation(
            OperationIntent(
                operation_id=f"{child.run_id}:model:1",
                run_id=child.run_id,
                attempt_id=f"{child.run_id}:attempt:1",
                kind=OperationKind.MODEL,
                target=harness.model_name,
                request_digest=checkpoint.request_digest,
                effect_class=EffectClass.NON_REPEATABLE,
                timeout_seconds=budget.timeout_seconds + 1,
                metadata={
                    "harness_spec_digest": harness._spec.settings_digest,
                    "operation_ordinal": 1,
                },
            )
        )
    ResumedAgent.calls = 0
    harness._agent_constructor = ResumedAgent

    recovered = await harness.recover_run(child.run_id)

    with pytest.raises(RunWaitError) as failed:
        await recovered.wait(timeout=2)
    assert failed.value.safe_error["code"] == "CHILD_OUTPUT_SCHEMA_MISMATCH"
    assert ResumedAgent.calls == 1
    operation = store.get_operation(f"{child.run_id}:model:1")
    assert operation.intent.timeout_seconds == budget.timeout_seconds + 1
    assert [
        event.payload["valid"]
        for event in store.list_events(child.run_id)
        if event.event_type == "run.child.output.validated"
    ] == [False]
    assert store.get_child_spec(child.run_id).digest == spec.digest


@pytest.mark.asyncio
async def test_max_depth_tree_is_certified_and_terminal_root_orphans_are_reaped(tmp_path):
    ResumedAgent.calls = 0
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    root = _running(store, "run-root")
    budget = ChildRunBudget(max_depth=16)
    parent = root
    descendants = []
    for depth in range(1, 17):
        _spec, parent = _child(
            store,
            parent,
            delegation_id=f"depth-{depth}",
            budget=budget,
        )
        descendants.append(parent)

    inspected = inspect_child_recovery(
        store,
        descendants[-1],
        owner=RunOwner(None, None),
    )
    assert inspected.error is None
    assert inspected.spec is not None and inspected.spec.depth == 16
    assert inspected.terminal_ancestor is None

    store.apply_transition(
        LifecycleTransition(
            run_id=root.run_id,
            kind=TransitionKind.FAIL,
            transition_id="root:failed",
        ),
        expected_revision=root.revision,
        terminal=TerminalRecord(
            run_id=root.run_id,
            state=RunState.FAILED,
            error={"code": "ROOT_FAILED", "safe_message": "The root failed."},
        ),
    )
    assert all(store.get_run(item.run_id).state is RunState.CANCELLING for item in descendants)
    harness = _harness(tmp_path, store)
    harness._agent_constructor = ResumedAgent

    recovered = await harness.recover_run(descendants[-1].run_id)

    with pytest.raises(RunWaitError) as cancelled:
        await recovered.wait(timeout=2)
    assert cancelled.value.safe_error["code"] == "CHILD_RECOVERY_ANCESTOR_TERMINAL"
    assert ResumedAgent.calls == 0
    assert store.get_run(descendants[-1].run_id).state is RunState.CANCELLED


@pytest.mark.asyncio
async def test_checkpoint_harness_spec_drift_fails_without_dispatch(tmp_path):
    ResumedAgent.calls = 0
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    snapshot = _running(store, "run-1")
    harness = _harness(tmp_path, store, artifact_store=artifacts)
    checkpoint = RuntimeRequestCheckpoint.create(
        run_id="run-1",
        message="must not dispatch",
        context=ExecutionContext.create(
            user_id=None,
            session_id=snapshot.session_id,
            workspace_id=str(tmp_path / "workspace"),
        ),
        kwargs={"learning_consent": False},
        harness_spec_digest="sha256:" + "0" * 64,
    )
    await persist_runtime_request_checkpoint(
        checkpoint,
        store=store,
        artifact_store=artifacts,
        worker_id="dead-worker",
    )

    recovered = await harness.recover_run("run-1")

    with pytest.raises(RunWaitError):
        await recovered.wait()
    terminal = store.get_terminal("run-1")
    assert terminal is not None
    assert terminal.error["code"] == "RUN_RECOVERY_SPEC_MISMATCH"
    assert ResumedAgent.calls == 0


@pytest.mark.asyncio
async def test_recovery_cannot_steal_an_unexpired_worker_lease(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    _running(store, "run-1")
    store.acquire_run_lease(
        "run-1",
        worker_id="live-worker",
        claim_id="live-claim",
    )
    harness = _harness(tmp_path, store)

    with pytest.raises(RuntimeLeaseUnavailableError):
        await harness.recover_run("run-1")
    assert store.get_run("run-1").state is RunState.RUNNING
