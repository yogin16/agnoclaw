"""Contracts for durable per-provider-call Agno fencing."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from agno.db.sqlite import SqliteDb
from agno.models.base import Model
from agno.models.message import Message
from agno.models.openai.responses import OpenAIResponses
from agno.models.response import ModelResponse

from agnoclaw import (
    AgentHarness,
    CapabilityConcurrency,
    CapabilityKind,
    CapabilityLifetime,
    CapabilityRecovery,
    CapabilitySpec,
    CapabilityTrust,
    HarnessConfig,
)
from agnoclaw.compat import AgnoFeature, inspect_agno_compatibility
from agnoclaw.runtime import (
    LocalArtifactStore,
    OperationIntent,
    OperationKind,
    OperationReconciliationRequiredError,
    OperationSettlement,
    OperationState,
    RunSnapshot,
    SQLiteRuntimeStore,
)
from agnoclaw.runtime.model_gateway import (
    has_valid_tool_batch_checkpoint,
    install_agno_provider_gateway,
    provider_call_ordinal,
    provider_request_digest,
)
from agnoclaw.runtime.operations import EffectClass


class CountingModel(Model):
    calls: int = 0

    def _response(self) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            content=f"response-{self.calls}",
            provider_data={"request_id": f"provider-{self.calls}"},
        )

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return self._response()

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return self._response()

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        yield self._response()

    async def ainvoke_stream(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[ModelResponse]:
        yield self._response()

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return ModelResponse(content=str(response))

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return ModelResponse(content=str(response))


def _store(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(RunSnapshot(run_id="run-1", user_id="user-1"))
    return store


def _kwargs(messages):
    return {
        "messages": messages,
        "assistant_message": Message(role="assistant"),
        "response_format": None,
        "tools": [],
        "tool_choice": None,
        "run_response": None,
        "compress_tool_results": False,
    }


def test_provider_identity_ignores_message_runtime_metadata() -> None:
    first = Message(role="user", content="hello")
    second = Message(role="user", content="hello")
    first.checkpoint_status = "RUNNING"
    first.checkpoint_created_at = 123
    first.from_history = True

    assert provider_request_digest((), _kwargs([first])) == provider_request_digest(
        (), _kwargs([second])
    )


def test_provider_ordinal_counts_only_current_run_assistant_messages() -> None:
    messages = [
        Message(role="assistant", content="old", from_history=True),
        Message(role="user", content="new"),
        Message(role="assistant", tool_calls=[{"id": "call-1"}]),
        Message(role="tool", tool_call_id="call-1", content="done"),
    ]

    assert provider_call_ordinal((), _kwargs(messages)) == 2


@pytest.mark.asyncio
async def test_succeeded_provider_response_replays_from_artifact(tmp_path) -> None:
    store = _store(tmp_path)
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    messages = [Message(role="user", content="hello")]

    first = CountingModel(id="gateway-model")
    install_agno_provider_gateway(
        first,
        store=store,
        artifact_store=artifacts,
        worker_id="worker-1",
        run_id="run-1",
        harness_spec_digest="sha256:" + "1" * 64,
    )
    first_result = await first.ainvoke(**_kwargs(messages))

    reopened = CountingModel(id="gateway-model")
    install_agno_provider_gateway(
        reopened,
        store=store,
        artifact_store=artifacts,
        worker_id="worker-2",
        run_id="run-1",
        harness_spec_digest="sha256:" + "1" * 64,
    )
    replayed = await reopened.ainvoke(**_kwargs([Message(role="user", content="hello")]))

    assert first_result.content == replayed.content == "response-1"
    assert first.calls == 1
    assert reopened.calls == 0
    operation = store.get_operation("run-1:provider:000001")
    assert operation.state is OperationState.SUCCEEDED
    assert operation.settlement is not None
    assert operation.settlement.provider_request_id == "provider-1"


@pytest.mark.asyncio
async def test_ambiguous_nested_effect_blocks_next_provider_dispatch(tmp_path) -> None:
    store = _store(tmp_path)
    operation_id = "run-1:capability:ambiguous"
    prepared = store.prepare_operation(
        OperationIntent(
            operation_id=operation_id,
            run_id="run-1",
            attempt_id="run-1:attempt:1",
            kind=OperationKind.CAPABILITY,
            target="effect@test",
            request_digest="sha256:" + "2" * 64,
            effect_class=EffectClass.NON_REPEATABLE,
        )
    ).record
    dispatching = store.begin_operation(
        operation_id,
        mutation_id="dispatch",
        expected_revision=prepared.revision,
        worker_id="worker-1",
        fence_token=1,
    ).record
    store.settle_operation(
        operation_id,
        mutation_id="settle",
        expected_revision=dispatching.revision,
        fence_token=dispatching.fence_token,
        settlement=OperationSettlement(
            state=OperationState.UNKNOWN,
            safe_error={"code": "AMBIGUOUS"},
        ),
    )
    model = CountingModel(id="gateway-model")
    install_agno_provider_gateway(
        model,
        store=store,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        worker_id="worker-2",
        run_id="run-1",
        harness_spec_digest="sha256:" + "1" * 64,
    )

    with pytest.raises(OperationReconciliationRequiredError):
        await model.ainvoke(**_kwargs([Message(role="user", content="hello")]))
    assert model.calls == 0


def test_tool_batch_checkpoint_requires_running_exact_tool_boundary() -> None:
    valid = type(
        "Checkpoint",
        (),
        {
            "status": "RUNNING",
            "messages": [Message(role="user", content="x"), Message(role="tool")],
            "last_checkpoint_at_message_index": 2,
        },
    )()
    invalid = type(
        "Checkpoint",
        (),
        {
            "status": "RUNNING",
            "messages": [Message(role="user", content="x")],
            "last_checkpoint_at_message_index": 1,
        },
    )()

    assert has_valid_tool_batch_checkpoint(valid)
    assert not has_valid_tool_batch_checkpoint(invalid)


@pytest.mark.asyncio
async def test_public_start_gates_real_agno_tool_loop_and_preserves_authority(
    tmp_path, monkeypatch
) -> None:
    if not inspect_agno_compatibility().has(AgnoFeature.TOOL_BATCH_CHECKPOINT):
        pytest.skip("installed Agno has no exact tool-batch checkpoint contract")
    provider_calls: list[int] = []
    effects: list[str] = []

    async def provider_call(_model, *args, **kwargs):
        ordinal = provider_call_ordinal(args, kwargs)
        provider_calls.append(ordinal)
        if ordinal == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "call-probe-1",
                        "type": "function",
                        "function": {
                            "name": "probe_effect",
                            "arguments": '{"value":"once"}',
                        },
                    }
                ],
                provider_data={"request_id": "provider-1"},
            )
        return ModelResponse(
            content="done",
            provider_data={"request_id": "provider-2"},
        )

    monkeypatch.setattr(OpenAIResponses, "ainvoke", provider_call)

    def effect(value: str) -> dict[str, str]:
        effects.append(value)
        return {"stored": value}

    capability = CapabilitySpec(
        name="probe_effect",
        version="1.0.0",
        kind=CapabilityKind.TOOL,
        effect_class=EffectClass.NON_REPEATABLE,
        trust=CapabilityTrust.VERIFIED,
        lifetime=CapabilityLifetime.RUN,
        concurrency=CapabilityConcurrency.ISOLATED,
        recovery=CapabilityRecovery.RECONCILABLE,
        implementation_digest="sha256:probe-effect-v1",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        factory=lambda: effect,
    )
    store = SQLiteRuntimeStore(tmp_path / "lifecycle.db")
    harness = AgentHarness(
        model="openai:provider-gateway-probe",
        capabilities=[capability],
        include_default_tools=False,
        db=SqliteDb(db_file=str(tmp_path / "agno.db")),
        runtime_store=store,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        config=HarnessConfig(
            enable_plugins=False,
            workspace_dir=str(tmp_path / "workspace"),
        ),
        workspace_dir=tmp_path / "workspace",
        user_id="user-1",
    )
    try:
        run = await harness.start("use the tool", session_id="session-1")
        result = await run.wait(timeout=10)
    finally:
        await harness.aclose()

    assert result.content == "done"
    assert provider_calls == [1, 2]
    assert effects == ["once"]
    operations = store.list_run_operations(run.run_id)
    assert store.get_operation(f"{run.run_id}:model:1").intent.effect_class is EffectClass.READ_ONLY
    assert [
        record.intent.operation_id
        for record in operations
        if ":provider:" in record.intent.operation_id
    ] == [
        f"{run.run_id}:provider:000001",
        f"{run.run_id}:provider:000002",
    ]
