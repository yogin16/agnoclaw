"""End-to-end contracts for AgentHarness.start/get_run and HarnessRun."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agnoclaw import AgentHarness, HarnessConfig
from agnoclaw.commands import Pause, Resume, Steer
from agnoclaw.runtime import (
    ExecutionContext,
    HarnessError,
    LifecycleTransition,
    LocalArtifactStore,
    RunReconciliationRequiredError,
    RunSnapshot,
    RunState,
    RuntimeLeaseLostError,
    RunWaitError,
    TransitionKind,
)
from agnoclaw.runtime.store import (
    SQLiteRuntimeStore,
    StartIdempotencyConflictError,
    encode_event_cursor,
)


class ControlledAgent:
    instances: list[ControlledAgent] = []
    calls = 0
    started: asyncio.Event
    dispatch_started: threading.Event
    release: asyncio.Event
    failure: BaseException | None = None
    messages: list[str] = []
    call_kwargs: list[dict] = []

    def __init__(self, **kwargs):
        self.system_message = kwargs["system_message"]
        self.session_id = kwargs.get("session_id")
        self.user_id = kwargs.get("user_id")
        self.tools = list(kwargs.get("tools") or [])
        self.db = kwargs.get("db")
        self.learning = kwargs.get("learning")
        # Capture each test's controls on the instance. A late task from another
        # test must never observe replacement class-level events.
        self._started = self.__class__.started
        self._dispatch_started = self.__class__.dispatch_started
        self._release = self.__class__.release
        self.__class__.instances.append(self)

    async def arun(self, message, **_kwargs):
        self.__class__.calls += 1
        self.__class__.messages.append(message)
        self.__class__.call_kwargs.append(dict(_kwargs))
        self._started.set()
        self._dispatch_started.set()
        await self._release.wait()
        if self.__class__.failure is not None:
            raise self.__class__.failure
        return SimpleNamespace(content="ok")


class StreamingControlledAgent(ControlledAgent):
    async def arun(self, message, **kwargs):
        if not kwargs.get("stream"):
            return await super().arun(message, **kwargs)

        self.__class__.calls += 1
        self.__class__.messages.append(message)
        self._started.set()
        self._dispatch_started.set()

        async def stream():
            await self._release.wait()
            if self.__class__.failure is not None:
                raise self.__class__.failure
            yield SimpleNamespace(event="RunContent", content="hello ")
            yield SimpleNamespace(event="RunContent", content="world")

        return stream()


class PartialStreamingControlledAgent(ControlledAgent):
    chunk_consumed: asyncio.Event

    async def arun(self, message, **kwargs):
        if not kwargs.get("stream"):
            return await super().arun(message, **kwargs)

        self.__class__.calls += 1
        self._started.set()
        self._dispatch_started.set()

        async def stream():
            yield SimpleNamespace(event="RunContent", content="persist before cancel")
            self.__class__.chunk_consumed.set()
            await self._release.wait()

        return stream()


def _harness(
    tmp_path,
    *,
    store=None,
    max_concurrency: int = 16,
    max_waiting: int = 1024,
    admission_timeout_seconds: float | None = 30.0,
    lease_seconds: int = 30,
    lease_interval: float = 10.0,
    artifact_store=None,
    event_sink=None,
    agent_type=ControlledAgent,
    profile=None,
):
    agent_type.instances = []
    agent_type.calls = 0
    agent_type.started = asyncio.Event()
    agent_type.dispatch_started = threading.Event()
    agent_type.release = asyncio.Event()
    agent_type.failure = None
    agent_type.messages = []
    agent_type.call_kwargs = []
    if issubclass(agent_type, PartialStreamingControlledAgent):
        agent_type.chunk_consumed = asyncio.Event()
    runtime_store = store or SQLiteRuntimeStore(tmp_path / "runtime.db")
    with patch("agnoclaw.agent.Agent", agent_type):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path / "workspace",
                config=HarnessConfig(
                    enable_plugins=False,
                    runtime_max_concurrency=max_concurrency,
                    runtime_max_waiting=max_waiting,
                    runtime_max_waiting_per_tenant=min(256, max_waiting),
                    runtime_max_waiting_per_session=min(32, max_waiting),
                    runtime_admission_timeout_seconds=admission_timeout_seconds,
                    runtime_lease_seconds=lease_seconds,
                    runtime_lease_renew_interval_seconds=lease_interval,
                ),
                include_default_tools=False,
                runtime_store=runtime_store,
                artifact_store=artifact_store,
                event_sink=event_sink,
                event_sink_mode="fail_closed" if event_sink is not None else None,
                profile=profile,
            )
    return harness, runtime_store


@pytest.mark.asyncio
async def test_lifecycle_live_presentation_streams_inside_settled_model_operation(tmp_path):
    from agnoclaw.runtime.presentation import LiveRunPresentation

    presentation = LiveRunPresentation(capacity=8)
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    harness, store = _harness(
        tmp_path,
        artifact_store=artifacts,
        agent_type=StreamingControlledAgent,
    )
    run = await harness.start(
        "work",
        session_id="session-1",
        _presentation=presentation,
    )
    presentation.bind(run.run_id)
    StreamingControlledAgent.release.set()

    raw_events = [event async for event in presentation.events()]
    result = await run.wait()

    assert [event.content for event in raw_events] == ["hello ", "world"]
    assert result.content == "hello world"
    assert (await run.status()).state is RunState.COMPLETED
    operation = store.get_operation(f"{run.run_id}:model:1")
    assert operation.state.value == "succeeded"
    assert operation.settlement is not None
    assert store.get_terminal(str(run.run_id)).value == {"content": "hello world"}
    output = [segment async for segment in run.output(follow=False)]
    assert [segment.content for segment in output] == ["hello world"]
    assert output[0].delta_count == 2
    assert [segment async for segment in run.output(after=output[0].cursor, follow=False)] == []
    events = store.list_events(str(run.run_id))
    segment_event = next(event for event in events if event.event_type == "run.output.segment")
    assert "hello world" not in str(segment_event.to_dict())

    reopened = SQLiteRuntimeStore(tmp_path / "runtime.db")
    reattached = _harness(
        tmp_path,
        store=reopened,
        artifact_store=artifacts,
        agent_type=StreamingControlledAgent,
    )[0].get_run(str(run.run_id))
    assert [segment.content async for segment in reattached.output(follow=False)] == [
        "hello world"
    ]


@pytest.mark.asyncio
async def test_lifecycle_persist_output_streams_without_a_live_consumer(tmp_path):
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    harness, _ = _harness(
        tmp_path,
        artifact_store=artifacts,
        agent_type=StreamingControlledAgent,
    )
    run = await harness.start("work", persist_output=True)
    StreamingControlledAgent.release.set()

    result = await run.wait()
    output = [segment async for segment in run.output(follow=False)]

    assert result.content == "hello world"
    assert [segment.content for segment in output] == ["hello world"]


@pytest.mark.asyncio
async def test_lifecycle_persist_output_requires_artifacts_before_dispatch(tmp_path):
    harness, _ = _harness(tmp_path, agent_type=StreamingControlledAgent)

    with pytest.raises(HarnessError) as missing:
        await harness.start("work", persist_output=True)

    assert missing.value.code == "RUN_OUTPUT_ARTIFACT_STORE_REQUIRED"
    assert StreamingControlledAgent.calls == 0

    with_artifacts, _ = _harness(
        tmp_path / "structured",
        artifact_store=LocalArtifactStore(tmp_path / "structured-artifacts"),
        agent_type=StreamingControlledAgent,
    )
    with pytest.raises(HarnessError) as structured:
        await with_artifacts.start(
            "work",
            persist_output=True,
            output_schema={"type": "object"},
        )
    assert structured.value.code == "RUN_OUTPUT_TEXT_REQUIRED"
    assert StreamingControlledAgent.calls == 0


@pytest.mark.asyncio
async def test_slow_lifecycle_presentation_detaches_without_stalling_terminal_result(tmp_path):
    from agnoclaw.runtime.presentation import LiveRunPresentation, RunPresentationDetached

    presentation = LiveRunPresentation(capacity=1)
    harness, _ = _harness(tmp_path, agent_type=StreamingControlledAgent)
    run = await harness.start("work", _presentation=presentation)
    presentation.bind(run.run_id)
    StreamingControlledAgent.release.set()

    result = await asyncio.wait_for(run.wait(), timeout=2)
    display_events = [event async for event in presentation.events()]

    assert result.content == "hello world"
    assert len(display_events) == 1
    assert isinstance(display_events[0], RunPresentationDetached)
    assert display_events[0].reason_code == "PRESENTATION_SLOW_CONSUMER"
    assert (await run.status()).state is RunState.COMPLETED


@pytest.mark.asyncio
async def test_lifecycle_presentation_closes_when_streaming_worker_is_cancelled(tmp_path):
    from agnoclaw.runtime.presentation import LiveRunPresentation

    presentation = LiveRunPresentation(capacity=8)
    harness, _ = _harness(tmp_path, agent_type=StreamingControlledAgent)
    run = await harness.start("work", _presentation=presentation)
    presentation.bind(run.run_id)
    await asyncio.wait_for(StreamingControlledAgent.started.wait(), timeout=2)

    await run.cancel()

    assert await asyncio.wait_for(
        _collect_presentation(presentation),
        timeout=2,
    ) == []
    assert (await run.status()).state in {
        RunState.CANCELLED,
        RunState.WAITING_FOR_RECONCILIATION,
    }


@pytest.mark.asyncio
async def test_lifecycle_cancellation_flushes_consumed_output_segment(tmp_path):
    from agnoclaw.runtime.presentation import LiveRunPresentation

    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    presentation = LiveRunPresentation(capacity=8)
    harness, _ = _harness(
        tmp_path,
        artifact_store=artifacts,
        agent_type=PartialStreamingControlledAgent,
    )
    run = await harness.start("work", _presentation=presentation)
    presentation.bind(run.run_id)
    await asyncio.wait_for(PartialStreamingControlledAgent.chunk_consumed.wait(), timeout=2)

    await run.cancel()

    output = [segment async for segment in run.output(follow=False)]
    assert [segment.content for segment in output] == ["persist before cancel"]
    assert (await run.status()).state is RunState.WAITING_FOR_RECONCILIATION


async def _collect_presentation(presentation):
    return [event async for event in presentation.events()]


@pytest.mark.asyncio
async def test_start_returns_live_handle_and_wait_settles_terminal_result(tmp_path):
    harness, _ = _harness(tmp_path)
    ControlledAgent.release.set()

    run = await harness.start("work", session_id="session-1", user_id="user-1")
    result = await run.wait()

    assert result.content == "ok"
    assert run.result is result
    snapshot = await run.status()
    assert snapshot.state == RunState.COMPLETED
    assert snapshot.session_id == "session-1"
    assert snapshot.user_id == "user-1"
    events = [event async for event in run.events()]
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[0].event_type == "run.created"
    assert events[-1].payload["after"] == "completed"
    event_types = {event.event_type for event in events}
    assert {
        "trajectory.run.started",
        "trajectory.prompt.built",
        "trajectory.model.request.started",
        "trajectory.model.request.completed",
        "trajectory.run.completed",
    } <= event_types
    projected = [
        event
        for event in events
        if event.event_type
        in {
            "trajectory.run.started",
            "trajectory.model.request.started",
            "trajectory.run.completed",
        }
    ]
    assert all(event.attempt_id == f"{run.run_id}:attempt:1" for event in projected)
    assert all(event.payload["projection_schema_version"] == "1.0" for event in projected)
    assert "work" not in str([event.to_dict() for event in projected])
    assert [event.event_type for event in events if event.event_type.startswith("operation.")] == [
        "operation.planned",
        "operation.dispatching",
        "operation.settled",
    ]


@pytest.mark.asyncio
async def test_explicit_profile_arun_is_start_wait_convenience_adapter(tmp_path):
    harness, _ = _harness(tmp_path, profile="quick")
    ControlledAgent.release.set()
    started = []
    original_start = harness.start

    async def capture_start(*args, **kwargs):
        run = await original_start(*args, **kwargs)
        started.append(run)
        return run

    with patch.object(harness, "start", side_effect=capture_start) as start_mock:
        result = await harness.arun(
            "work",
            session_id="session-1",
            user_id="user-1",
            max_turns=7,
            stream_events=True,
            idempotency_key="work:v1",
        )

    assert result.content == "ok"
    assert len(started) == 1
    assert started[0].run_id is not None
    assert (await started[0].status()).state is RunState.COMPLETED
    assert ControlledAgent.calls == 1
    start_mock.assert_awaited_once()
    start_kwargs = start_mock.call_args.kwargs
    assert start_kwargs["session_id"] == "session-1"
    assert start_kwargs["user_id"] == "user-1"
    assert start_kwargs["max_turns"] == 7
    assert start_kwargs["stream_events"] is True
    assert start_kwargs["idempotency_key"] == "work:v1"


def test_explicit_profile_run_is_blocking_start_wait_convenience_adapter(tmp_path):
    harness, _ = _harness(tmp_path, profile="quick")
    ControlledAgent.release.set()
    started = []
    original_start = harness.start

    async def capture_start(*args, **kwargs):
        run = await original_start(*args, **kwargs)
        started.append(run)
        return run

    with patch.object(harness, "start", side_effect=capture_start) as start_mock:
        result = harness.run("work", session_id="session-1", user_id="user-1")

    assert result.content == "ok"
    assert ControlledAgent.calls == 1
    assert len(started) == 1
    assert asyncio.run(started[0].status()).state is RunState.COMPLETED
    start_mock.assert_awaited_once()
    harness.close()


@pytest.mark.asyncio
async def test_explicit_profile_run_uses_owned_loop_inside_an_active_event_loop(tmp_path):
    harness, _ = _harness(tmp_path, profile="quick")
    ControlledAgent.release.set()

    result = harness.run("work")

    assert result.content == "ok"
    assert ControlledAgent.calls == 1
    await harness.aclose()


@pytest.mark.asyncio
async def test_explicit_profile_rejects_mixed_sync_and_async_loop_ownership(tmp_path):
    harness, _ = _harness(tmp_path, profile="quick")
    ControlledAgent.release.set()
    assert harness.run("sync work").content == "ok"

    with pytest.raises(HarnessError) as conflict:
        await harness.arun("async work")

    assert conflict.value.code == "HARNESS_EVENT_LOOP_OWNERSHIP_CONFLICT"
    assert ControlledAgent.calls == 1
    await harness.aclose()


@pytest.mark.asyncio
async def test_explicit_profile_arun_raw_stream_uses_lifecycle_presentation(tmp_path):
    harness, _ = _harness(
        tmp_path,
        profile="quick",
        agent_type=StreamingControlledAgent,
    )
    started = []
    original_start = harness.start

    async def capture_start(*args, **kwargs):
        run = await original_start(*args, **kwargs)
        started.append(run)
        return run

    with patch.object(harness, "start", side_effect=capture_start) as start_mock:
        stream = await harness.arun("work", stream=True, stream_events=False)
        StreamingControlledAgent.release.set()
        events = [event async for event in stream]

    assert [event.content for event in events] == ["hello ", "world"]
    assert len(started) == 1
    assert (await started[0].wait()).content == "hello world"
    assert (await started[0].status()).state is RunState.COMPLETED
    assert StreamingControlledAgent.calls == 1
    start_mock.assert_awaited_once()
    assert start_mock.call_args.kwargs.get("_presentation") is not None
    assert "stream" not in start_mock.call_args.kwargs
    assert "stream_events" not in start_mock.call_args.kwargs


def test_explicit_profile_sync_raw_stream_uses_owned_lifecycle_bridge(tmp_path):
    harness, _ = _harness(
        tmp_path,
        profile="quick",
        agent_type=StreamingControlledAgent,
    )

    stream = harness.run("work", stream=True)
    StreamingControlledAgent.release.set()
    events = list(stream)

    assert [event.content for event in events] == ["hello ", "world"]
    assert StreamingControlledAgent.calls == 1
    harness.close()


def test_closing_sync_raw_stream_detaches_observer_without_cancelling_run(tmp_path):
    harness, _ = _harness(
        tmp_path,
        profile="quick",
        agent_type=PartialStreamingControlledAgent,
    )

    stream = harness.run("work", stream=True)
    first = next(stream)
    stream.close()
    PartialStreamingControlledAgent.release.set()
    harness.close(policy="drain")

    assert first.content == "persist before cancel"
    assert PartialStreamingControlledAgent.calls == 1


def test_sync_raw_stream_surfaces_authoritative_terminal_failure(tmp_path):
    harness, _ = _harness(
        tmp_path,
        profile="quick",
        agent_type=StreamingControlledAgent,
    )
    StreamingControlledAgent.failure = RuntimeError("provider outcome is ambiguous")

    stream = harness.run("work", stream=True)
    StreamingControlledAgent.release.set()

    with pytest.raises(RunReconciliationRequiredError) as failure:
        list(stream)

    assert failure.value.code == "RUN_RECONCILIATION_REQUIRED"
    assert StreamingControlledAgent.calls == 1
    harness.close()


@pytest.mark.asyncio
async def test_lifecycle_observer_failure_cannot_override_committed_trajectory(tmp_path):
    class FailingSink:
        def emit(self, _event):
            raise RuntimeError("observer offline with private detail")

    harness, store = _harness(tmp_path, event_sink=FailingSink())
    ControlledAgent.release.set()

    run = await harness.start("work", session_id="session-1", user_id="user-1")
    result = await run.wait()

    assert result.content == "ok"
    assert (await run.status()).state is RunState.COMPLETED
    events = store.list_events(str(run.run_id))
    assert any(event.event_type == "trajectory.run.completed" for event in events)
    assert "observer offline" not in str([event.to_dict() for event in events])
    outbox = store.lease_outbox(owner="event-exporter", limit=100)
    assert [item.event.event_id for item in outbox] == [event.event_id for event in events]


@pytest.mark.asyncio
async def test_start_commits_result_artifact_before_terminal_completion(tmp_path):
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    harness, store = _harness(tmp_path, artifact_store=artifacts)
    ControlledAgent.release.set()
    assert {item.resource_id for item in harness._spec.resources} >= {
        "runtime_store",
        "artifact_store",
    }

    run = await harness.start("work", session_id="session-1", user_id="user-1")
    result = await run.wait()

    operation = store.get_operation(f"{run.run_id}:model:1")
    assert operation.settlement is not None
    reference = store.get_artifact(operation.settlement.result_reference)
    assert await artifacts.load_json(reference) == {"content": "ok"}
    assert result.content == "ok"
    owner_context = ExecutionContext.create(
        user_id="user-1",
        session_id="session-1",
        workspace_id=str(tmp_path / "workspace"),
    )
    listed = await harness.list_artifacts(str(run.run_id), context=owner_context)
    assert listed[-1] == reference
    assert [item.purpose for item in listed] == [
        "run_request_checkpoint",
        "operation_result",
    ]
    checkpoint = await artifacts.load_json(listed[0])
    assert checkpoint["type"] == "agnoclaw.runtime_request_checkpoint"
    assert checkpoint["message"] == "work"
    chunk = await harness.read_artifact(
        reference.artifact_id,
        limit=1024,
        context=owner_context,
    )
    assert chunk.complete and chunk.data == b'{"content":"ok"}'
    with pytest.raises(HarnessError) as hidden:
        await harness.read_artifact(
            reference.artifact_id,
            context=ExecutionContext.create(
                user_id="other",
                session_id="session-1",
                workspace_id=str(tmp_path / "workspace"),
            ),
        )
    assert hidden.value.code == "ARTIFACT_NOT_FOUND"
    events = [event async for event in run.events(follow=False)]
    event_types = [event.event_type for event in events]
    checkpoint_committed = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "artifact.committed"
        and event.payload["artifact_id"] == listed[0].artifact_id
    )
    model_planned = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "operation.planned"
        and event.payload["operation_id"] == f"{run.run_id}:model:1"
    )
    assert checkpoint_committed < model_planned
    assert event_types.index("artifact.committed") < event_types.index("operation.settled")
    assert event_types.index("operation.settled") < len(events) - 1
    assert events[-1].payload["after"] == "completed"


@pytest.mark.asyncio
async def test_start_idempotency_reuses_live_run_and_rejects_changed_request(tmp_path):
    harness, _ = _harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )

    first = await harness.start(
        "work",
        idempotency_key="request-1",
        session_id="session-1",
    )
    repeated = await harness.start(
        "work",
        idempotency_key="request-1",
        session_id="session-1",
    )

    assert repeated.run_id == first.run_id
    with pytest.raises(StartIdempotencyConflictError):
        await harness.start(
            "different",
            idempotency_key="request-1",
            session_id="session-1",
        )
    with pytest.raises(StartIdempotencyConflictError):
        await harness.start(
            "work",
            idempotency_key="request-1",
            session_id="session-2",
        )
    with pytest.raises(StartIdempotencyConflictError):
        await harness.start(
            "work",
            idempotency_key="request-1",
            session_id="session-1",
            persist_output=True,
        )
    ControlledAgent.release.set()
    await first.wait()
    assert ControlledAgent.calls == 1


@pytest.mark.asyncio
async def test_waiter_cancellation_and_timeout_do_not_cancel_run(tmp_path):
    harness, _ = _harness(tmp_path)
    run = await harness.start("work")
    await asyncio.wait_for(ControlledAgent.started.wait(), timeout=2)

    with pytest.raises(TimeoutError):
        await run.wait(timeout=0.01)
    waiter = asyncio.create_task(run.wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert (await run.status()).state == RunState.RUNNING
    ControlledAgent.release.set()
    assert (await run.wait()).content == "ok"


@pytest.mark.asyncio
async def test_cancel_during_model_dispatch_is_unknown_and_idempotent(tmp_path):
    harness, _ = _harness(tmp_path)
    run = await harness.start("work")
    await asyncio.wait_for(ControlledAgent.started.wait(), timeout=2)

    first = await run.cancel()
    repeated = await run.cancel()

    assert first.state == RunState.WAITING_FOR_RECONCILIATION
    assert repeated.state == RunState.WAITING_FOR_RECONCILIATION
    with pytest.raises(RunReconciliationRequiredError) as exc:
        await run.wait()
    assert exc.value.code == "RUN_RECONCILIATION_REQUIRED"
    assert exc.value.snapshot.state == RunState.WAITING_FOR_RECONCILIATION


@pytest.mark.asyncio
async def test_cancel_before_model_dispatch_is_truthfully_cancelled(tmp_path):
    harness, store = _harness(tmp_path)
    run = await harness.start("work")
    await run.command(Pause("before dispatch", command_id="pause-before-cancel"))

    cancelled = await run.cancel()

    assert cancelled.state == RunState.CANCELLED
    assert store.list_recoverable_operations() == []
    assert ControlledAgent.calls == 0


@pytest.mark.asyncio
async def test_cancellation_during_lease_commit_does_not_dispatch_or_strand_lane(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    original_acquire = store.acquire_run_lease
    entered = threading.Event()
    release = threading.Event()
    blocked_run_id: str | None = None

    def blocked_acquire(run_id, **kwargs):
        if run_id == blocked_run_id:
            entered.set()
            release.wait(timeout=2)
        return original_acquire(run_id, **kwargs)

    store.acquire_run_lease = blocked_acquire  # type: ignore[method-assign]
    harness, _ = _harness(tmp_path, store=store)
    first = await harness.start("first", session_id="same")
    blocked_run_id = str(first.run_id)
    assert await asyncio.to_thread(entered.wait, 1)

    cancelling = asyncio.create_task(first.cancel())
    await asyncio.sleep(0)
    release.set()
    assert (await cancelling).state is RunState.CANCELLED
    assert ControlledAgent.calls == 0

    ControlledAgent.release.set()
    second = await harness.start("second", session_id="same")
    assert (await second.wait()).content == "ok"


@pytest.mark.asyncio
async def test_lease_loss_cancels_worker_and_preserves_unknown_model_effect(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")

    def lose_lease(claim, *, lease_seconds=30):
        del lease_seconds
        if not ControlledAgent.dispatch_started.wait(timeout=2):
            raise AssertionError("model dispatch did not begin before lease-loss injection")
        raise RuntimeLeaseLostError(run_id=claim.run_id, kind=claim.run.kind)

    store.renew_run_lease = lose_lease  # type: ignore[method-assign]
    harness, _ = _harness(
        tmp_path,
        store=store,
        lease_seconds=3,
        lease_interval=0.01,
    )
    run = await harness.start("work")
    await asyncio.wait_for(ControlledAgent.started.wait(), timeout=2)

    with pytest.raises(RunReconciliationRequiredError) as failure:
        await asyncio.wait_for(run.wait(), timeout=1)
    assert failure.value.code == "RUN_RECONCILIATION_REQUIRED"
    assert (await run.status()).state is RunState.WAITING_FOR_RECONCILIATION


@pytest.mark.asyncio
async def test_failure_persists_only_safe_diagnostic(tmp_path):
    harness, store = _harness(tmp_path)
    ControlledAgent.failure = RuntimeError("secret-token-must-not-persist")
    ControlledAgent.release.set()
    run = await harness.start("work")

    with pytest.raises(RunReconciliationRequiredError) as exc:
        await run.wait()

    assert exc.value.code == "RUN_RECONCILIATION_REQUIRED"
    assert store.get_terminal(str(run.run_id)) is None
    operation = store.get_operation(f"{run.run_id}:model:1")
    serialized = str(operation.settlement.safe_error)
    assert "secret-token-must-not-persist" not in serialized
    assert "MODEL_RUN_FAILED" in serialized


@pytest.mark.asyncio
async def test_reattached_completed_handle_reads_persisted_result(tmp_path):
    harness, store = _harness(tmp_path)
    ControlledAgent.release.set()
    original = await harness.start("work")
    await original.wait()

    second_harness, _ = _harness(tmp_path / "second", store=store)
    reattached = second_harness.get_run(str(original.run_id))
    restored = await reattached.wait()

    assert restored == {"content": "ok"}
    assert (await reattached.status()).state == RunState.COMPLETED


@pytest.mark.asyncio
async def test_get_run_hides_cross_owner_run(tmp_path):
    harness, _ = _harness(tmp_path)
    ControlledAgent.release.set()
    context = ExecutionContext.create(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        workspace_id=str(tmp_path / "workspace"),
    )
    run = await harness.start("work", context=context)
    await run.wait()

    with pytest.raises(HarnessError) as hidden:
        harness.get_run(
            str(run.run_id),
            context=ExecutionContext.create(
                tenant_id="tenant-2",
                user_id="user-1",
                session_id="session-1",
                workspace_id=str(tmp_path / "workspace"),
            ),
        )
    assert hidden.value.code == "RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_pause_resume_before_dispatch_and_pre_dispatch_steering(tmp_path):
    harness, _ = _harness(tmp_path)
    run = await harness.start("work")

    paused = await run.command(Pause("inspect", command_id="pause-1"))
    assert paused.state == RunState.PAUSED
    resumed = await run.command(Resume(command_id="resume-1"))
    assert resumed.state == RunState.RUNNING
    # The worker is now running; steering correctly fails after the safe point.
    await asyncio.wait_for(ControlledAgent.started.wait(), timeout=2)
    with pytest.raises(HarnessError) as closed:
        await run.command(Steer("too late", command_id="steer-late"))
    assert closed.value.code == "RUN_STEERING_CLOSED"
    ControlledAgent.release.set()
    await run.wait()


@pytest.mark.asyncio
async def test_steering_before_worker_dispatch_is_applied_once(tmp_path):
    harness, _ = _harness(tmp_path)
    run = await harness.start("work")

    await run.command(Steer("prioritize evidence", command_id="steer-1"))
    ControlledAgent.release.set()
    await run.wait()

    assert len(ControlledAgent.messages) == 1
    assert "Operator steering" in ControlledAgent.messages[0]
    assert "prioritize evidence" in ControlledAgent.messages[0]


@pytest.mark.asyncio
async def test_cancel_retries_when_an_external_start_wins_the_revision(tmp_path, monkeypatch):
    harness, store = _harness(tmp_path)
    run_id = "run-cancel-interleave"
    store.create_run(RunSnapshot(run_id=run_id, session_id="session-1"))
    queued = store.apply_transition(
        LifecycleTransition(
            run_id=run_id,
            kind=TransitionKind.QUEUE,
            transition_id=f"{run_id}:queue",
        ),
        expected_revision=0,
    ).lifecycle.after
    original_apply = store.apply_transition
    interleaved = False

    def apply_with_external_start(transition, *, expected_revision, **kwargs):
        nonlocal interleaved
        if transition.kind is TransitionKind.REQUEST_CANCEL and not interleaved:
            interleaved = True
            original_apply(
                LifecycleTransition(
                    run_id=run_id,
                    kind=TransitionKind.START,
                    transition_id=f"{run_id}:external-start",
                ),
                expected_revision=queued.revision,
            )
        return original_apply(
            transition,
            expected_revision=expected_revision,
            **kwargs,
        )

    monkeypatch.setattr(store, "apply_transition", apply_with_external_start)

    cancelled = await harness._cancel_runtime_run(run_id)

    assert interleaved is True
    assert cancelled.state is RunState.CANCELLED


@pytest.mark.asyncio
async def test_accepted_steering_is_visible_before_worker_closes_window(tmp_path, monkeypatch):
    harness, store = _harness(tmp_path)
    run_id = "run-steer-interleave"
    context = ExecutionContext.create(
        user_id="user-1",
        session_id="session-1",
        workspace_id=str(tmp_path / "workspace"),
    )
    store.create_run(
        RunSnapshot(
            run_id=run_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=context.session_id,
        )
    )
    store.apply_transition(
        LifecycleTransition(
            run_id=run_id,
            kind=TransitionKind.QUEUE,
            transition_id=f"{run_id}:queue",
        ),
        expected_revision=0,
    )
    request = {
        "message": "work",
        "context": context,
        "kwargs": {},
        "steering": [],
        "child_spec": None,
    }
    harness._run_requests[run_id] = request
    harness._run_control_locks = {run_id: asyncio.Lock()}
    original_apply = store.apply_transition
    steer_committed = threading.Event()
    release_steer_return = threading.Event()
    target_attempts = 0

    def apply_with_delayed_steer_return(transition, *, expected_revision, **kwargs):
        nonlocal target_attempts
        if transition.transition_id == "steer-target":
            target_attempts += 1
            if target_attempts == 1:
                current = store.get_run(run_id)
                original_apply(
                    LifecycleTransition(
                        run_id=run_id,
                        kind=TransitionKind.STEER,
                        transition_id="steer-interleaving-revision",
                        payload={"instruction_digest": "interleaving"},
                    ),
                    expected_revision=current.revision,
                )
            result = original_apply(
                transition,
                expected_revision=expected_revision,
                **kwargs,
            )
            if target_attempts == 2:
                steer_committed.set()
                assert release_steer_return.wait(timeout=2)
            return result
        return original_apply(
            transition,
            expected_revision=expected_revision,
            **kwargs,
        )

    monkeypatch.setattr(store, "apply_transition", apply_with_delayed_steer_return)
    steer_task = asyncio.create_task(
        harness._command_runtime_run(
            run_id,
            Steer("prioritize evidence", command_id="steer-target"),
        )
    )
    assert await asyncio.to_thread(steer_committed.wait, 2)
    harness._launch_runtime_worker(run_id, request)

    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(ControlledAgent.started.wait(), timeout=0.05)
    finally:
        release_steer_return.set()

    await steer_task
    ControlledAgent.release.set()
    await asyncio.wait_for(harness._live_runs[run_id], timeout=2)

    assert target_attempts == 2
    assert "prioritize evidence" in ControlledAgent.messages[0]


@pytest.mark.asyncio
async def test_reattached_owner_local_controls_fail_before_acknowledgement(tmp_path):
    harness, store = _harness(tmp_path)
    run_id = "run-remote-steer"
    store.create_run(
        RunSnapshot(
            run_id=run_id,
            tenant_id=harness._tenant_id,
            user_id=harness.user_id,
            session_id="session-1",
        )
    )
    queued = store.apply_transition(
        LifecycleTransition(
            run_id=run_id,
            kind=TransitionKind.QUEUE,
            transition_id=f"{run_id}:queue",
        ),
        expected_revision=0,
    ).lifecycle.after
    reattached = harness.get_run(run_id)

    for command in (
        Pause("cannot reach the owner-local worker", command_id="pause-remote"),
        Steer("cannot reach an owner-local buffer", command_id="steer-remote"),
    ):
        with pytest.raises(HarnessError) as unavailable:
            await reattached.command(command)

        assert unavailable.value.code == "RUN_CONTROL_OWNER_UNAVAILABLE"
        assert unavailable.value.details["command_type"] == command.command_type
        assert store.get_run(run_id).revision == queued.revision
        assert store.get_run(run_id).last_transition_id == queued.last_transition_id

    paused = store.apply_transition(
        LifecycleTransition(
            run_id=run_id,
            kind=TransitionKind.PAUSE,
            transition_id=f"{run_id}:owner-pause",
        ),
        expected_revision=queued.revision,
    ).lifecycle.after
    with pytest.raises(HarnessError) as unavailable:
        await reattached.command(Resume(command_id="resume-remote"))

    assert unavailable.value.code == "RUN_CONTROL_OWNER_UNAVAILABLE"
    assert unavailable.value.details["command_type"] == "resume"
    assert store.get_run(run_id).state is RunState.PAUSED
    assert store.get_run(run_id).revision == paused.revision
    assert store.get_run(run_id).last_transition_id == paused.last_transition_id


@pytest.mark.asyncio
async def test_event_cursor_resumes_after_exact_sequence(tmp_path):
    harness, _ = _harness(tmp_path)
    ControlledAgent.release.set()
    run = await harness.start("work")
    await run.wait()
    all_events = [event async for event in run.events(follow=False)]
    cursor = encode_event_cursor(run_id=str(run.run_id), sequence=2)

    resumed = [event async for event in run.events(after=cursor, follow=False)]

    assert resumed == all_events[2:]


@pytest.mark.asyncio
async def test_lifecycle_runs_serialize_same_session_and_overlap_other_sessions(tmp_path):
    harness, _ = _harness(tmp_path)
    same_first = await harness.start("same-first", session_id="same")
    same_second = await harness.start("same-second", session_id="same")
    for _ in range(50):
        if ControlledAgent.calls == 1:
            break
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.01)
    assert ControlledAgent.calls == 1
    ControlledAgent.release.set()
    await asyncio.gather(same_first.wait(), same_second.wait())
    assert ControlledAgent.calls == 2

    other_harness, _ = _harness(tmp_path / "other")
    first = await other_harness.start("one", session_id="one")
    second = await other_harness.start("two", session_id="two")
    for _ in range(50):
        if ControlledAgent.calls == 2:
            break
        await asyncio.sleep(0.005)
    assert ControlledAgent.calls == 2
    ControlledAgent.release.set()
    await asyncio.gather(first.wait(), second.wait())


@pytest.mark.asyncio
async def test_lifecycle_global_concurrency_bound_is_enforced(tmp_path):
    harness, _ = _harness(tmp_path, max_concurrency=1)
    initial = harness.runtime_admission_stats()
    assert initial["max_concurrency"] == 1
    assert initial["active"] == initial["waiting"] == 0
    first = await harness.start("one", session_id="one")
    second = await harness.start("two", session_id="two")
    for _ in range(50):
        if ControlledAgent.calls == 1:
            break
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.01)
    assert ControlledAgent.calls == 1
    ControlledAgent.release.set()
    await asyncio.gather(first.wait(), second.wait())
    assert ControlledAgent.calls == 2
    settled = harness.runtime_admission_stats()
    assert settled["admitted"] == 2
    assert settled["peak_active"] == 1


@pytest.mark.asyncio
async def test_lifecycle_waiting_bound_settles_typed_retryable_overload(tmp_path):
    harness, _ = _harness(
        tmp_path,
        max_concurrency=1,
        max_waiting=1,
        admission_timeout_seconds=None,
    )
    first = await harness.start("first", session_id="first")
    await asyncio.wait_for(ControlledAgent.started.wait(), timeout=2)
    second = await harness.start("second", session_id="second")
    for _ in range(100):
        if harness.runtime_admission_stats()["waiting"] == 1:
            break
        await asyncio.sleep(0)
    third = await harness.start("third", session_id="third")

    with pytest.raises(RunWaitError) as overloaded:
        await asyncio.wait_for(third.wait(), timeout=2)
    assert overloaded.value.retryable is True
    assert overloaded.value.safe_error["code"] == "RUNTIME_ADMISSION_OVERLOADED"
    assert overloaded.value.safe_error["details"]["reason_code"] == (
        "RUNTIME_ADMISSION_OVERLOADED"
    )
    assert (await third.status()).last_reason_code == "RUN_EXECUTION_FAILED"

    ControlledAgent.release.set()
    await asyncio.gather(first.wait(), second.wait())
    stats = harness.runtime_admission_stats()
    assert stats["rejected"] == 1
    assert stats["active"] == stats["waiting"] == 0


@pytest.mark.asyncio
async def test_lifecycle_admission_timeout_is_terminal_and_cleans_waiter(tmp_path):
    harness, _ = _harness(
        tmp_path,
        max_concurrency=1,
        max_waiting=2,
        admission_timeout_seconds=0.02,
    )
    first = await harness.start("first", session_id="first")
    await asyncio.wait_for(ControlledAgent.started.wait(), timeout=2)
    timed_out = await harness.start("second", session_id="second")

    with pytest.raises(RunWaitError) as overloaded:
        await asyncio.wait_for(timed_out.wait(), timeout=2)
    assert overloaded.value.retryable is True
    assert overloaded.value.safe_error["code"] == "RUNTIME_ADMISSION_OVERLOADED"
    assert harness.runtime_admission_stats()["timed_out"] == 1
    assert harness.runtime_admission_stats()["waiting"] == 0

    ControlledAgent.release.set()
    await first.wait()


@pytest.mark.asyncio
async def test_aclose_drain_stops_admission_waits_and_cleans_ephemeral_state(tmp_path):
    harness, _ = _harness(tmp_path)
    run = await harness.start("work", session_id="session")
    await asyncio.wait_for(ControlledAgent.started.wait(), timeout=2)

    closing = asyncio.create_task(harness.aclose(policy="drain"))
    await asyncio.sleep(0)
    assert not closing.done()
    with pytest.raises(HarnessError) as closed:
        await harness.start("too-late")
    assert closed.value.code == "HARNESS_CLOSED"

    ControlledAgent.release.set()
    await closing
    assert (await run.status()).state == RunState.COMPLETED
    assert harness._live_runs == {}
    assert harness._run_requests == {}
    assert harness._run_resume_events == {}
    assert harness._resources_closed is True


@pytest.mark.asyncio
async def test_aclose_detach_returns_while_supervisor_finishes_run(tmp_path):
    harness, _ = _harness(tmp_path)
    run = await harness.start("work")
    await asyncio.wait_for(ControlledAgent.started.wait(), timeout=2)

    await harness.aclose(policy="detach")
    assert (await run.status()).state == RunState.RUNNING
    assert harness._resources_closed is False

    ControlledAgent.release.set()
    assert harness._shutdown_task is not None
    await harness._shutdown_task
    assert (await run.status()).state == RunState.COMPLETED
    assert harness._resources_closed is True


@pytest.mark.asyncio
async def test_aclose_cancel_preserves_unknown_model_effect_and_is_repeatable(tmp_path):
    harness, _ = _harness(tmp_path)
    run = await harness.start("work")
    await asyncio.wait_for(ControlledAgent.started.wait(), timeout=2)

    await harness.aclose(policy="cancel")
    await harness.aclose(policy="cancel")

    assert (await run.status()).state == RunState.WAITING_FOR_RECONCILIATION
    assert harness._resources_closed is True


@pytest.mark.asyncio
async def test_aclose_timeout_does_not_abandon_shutdown_supervisor(tmp_path):
    harness, _ = _harness(tmp_path)
    run = await harness.start("work")
    await asyncio.wait_for(ControlledAgent.started.wait(), timeout=2)

    with pytest.raises(HarnessError) as timeout:
        await harness.aclose(policy="drain", timeout=0.01)
    assert timeout.value.code == "HARNESS_CLOSE_TIMEOUT"
    assert harness._resources_closed is False

    ControlledAgent.release.set()
    assert harness._shutdown_task is not None
    await harness._shutdown_task
    assert (await run.status()).state == RunState.COMPLETED
    assert harness._resources_closed is True


@pytest.mark.asyncio
async def test_sync_close_rejects_running_loop_without_closing_harness(tmp_path):
    harness, _ = _harness(tmp_path)

    with pytest.raises(HarnessError) as required:
        harness.close()

    assert required.value.code == "HARNESS_ASYNC_CLOSE_REQUIRED"
    assert harness._closed is False
    await harness.aclose()


@pytest.mark.asyncio
async def test_owned_runtime_store_is_closed_but_injected_store_is_not(tmp_path):
    owned_harness, _ = _harness(tmp_path / "owned")
    owned_store = MagicMock()
    owned_harness._runtime_store = owned_store
    owned_harness._owns_runtime_store = True
    await owned_harness.aclose()
    owned_store.close.assert_called_once_with()

    injected_harness, injected_store = _harness(tmp_path / "injected")
    injected_close = MagicMock(wraps=injected_store.close)
    injected_store.close = injected_close
    await injected_harness.aclose()
    injected_close.assert_not_called()
