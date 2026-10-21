"""Hook/listener ownership, cancellation, replay and protocol boundary contracts."""

import asyncio
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agnoclaw.domain import (
    ChannelCapabilities,
    ChannelReceipt,
    DeduplicationStatus,
    Domain,
    EventType,
    Extensions,
    Finding,
    Hook,
    Interaction,
    Listener,
    RuntimeListenerExporter,
    Schema,
    Trigger,
    Watcher,
    WatcherResult,
)
from agnoclaw.runtime import HarnessError
from tests.test_domain_core import context, target
from tests.test_domain_packages import Value
from tests.test_runtime_outbox import _worker


def event_definition():
    return EventType("metric.changed", Schema("value", Value))


def event(value=1):
    return event_definition().event(
        {"value": value}, source="host", event_id="evt-1", tenant_id="tenant-1"
    )


async def test_hooks_are_ordered_typed_fail_closed_and_scoped():
    schema = Schema("value", Value)
    seen = []

    def transform(values, _):
        seen.append("transform")
        return {"value": values["value"] + 1}

    def policy(values, _):
        seen.append("policy")
        return values["value"] == 2

    async with Extensions(tenant_id="tenant-1") as scope:
        scope.register(Hook("policy", schema, policy, implementation_id="p1", order=2))
        registration = scope.register(
            Hook("transform", schema, transform, implementation_id="t1", mode="transform")
        )
        assert await scope.apply(schema, {"value": 1}, context=context()) == {"value": 2}
        assert seen == ["transform", "policy"]
        registration.close()
        registration.close()
        with pytest.raises(HarnessError, match="Hook"):
            await scope.apply(schema, {"value": 1}, context=context())
        with pytest.raises(HarnessError, match="tenant"):
            await scope.apply(schema, {"value": 1}, context=context("other"))
    with pytest.raises(RuntimeError, match="closed"):
        await scope.apply(schema, {"value": 1}, context=context())


async def test_listener_receipts_and_observer_failures():
    receipts, applied = {}, []

    async def handle(delivery):
        status = delivery.identity.compare(receipts.get(delivery.identity.key))
        if status is DeduplicationStatus.CONFLICT:
            raise ValueError("changed intent")
        if status is DeduplicationStatus.NEW:
            # In a production host this pair belongs to one business transaction.
            applied.append(delivery.event.payload["value"])
            receipts[delivery.identity.key] = delivery.identity

    def broken(_):
        raise ValueError("private business payload")

    listener = Listener("projection", event_definition(), handle, implementation_id="v1")
    observer = Listener(
        "display", event_definition(), broken, implementation_id="v1", mode="observer"
    )
    compiled = Domain("pack", components=(listener, observer)).compile()
    async with compiled.extensions(tenant_id="tenant-1") as scope:
        result = await scope.publish(event())
        assert result.observer_failures == ("display@1",)
        await scope.publish(event())
        assert applied == [1]
        with pytest.raises(HarnessError, match="delivery must retry") as error:
            await scope.publish(event(2))
        assert "private" not in str(error.value)
        assert "changed intent" not in "".join(traceback.format_exception(error.value))
        with pytest.raises(HarnessError, match="tenant"):
            await scope.publish(replace(event(), tenant_id="other"))


async def test_close_cancels_owned_callbacks_without_waiting_on_callers_unrelated_work():
    started, stopped, caller_after = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handle(_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    scope = Extensions(tenant_id="tenant-1")
    scope.register(Listener("wait", event_definition(), handle, implementation_id="v1"))

    async def caller():
        try:
            await scope.publish(event())
        except asyncio.CancelledError:
            caller_after.set()
            await asyncio.sleep(0.05)

    task = asyncio.create_task(caller())
    await started.wait()
    await asyncio.wait_for(scope.aclose(cancel=True), timeout=1)
    assert stopped.is_set()
    await caller_after.wait()
    assert not task.done()
    await task


async def test_listener_timeout_propagates_cancellation_does_not_acknowledge():
    async def slow(_):
        await asyncio.Event().wait()

    async with Extensions(tenant_id="tenant-1") as scope:
        scope.register(
            Listener("slow", event_definition(), slow, implementation_id="v1", timeout_seconds=0.01)
        )
        with pytest.raises(HarnessError, match="delivery must retry"):
            await scope.publish(event())


async def test_runtime_outbox_adapter_retries_then_acknowledges_after_listener_success(tmp_path):
    seen, receipts = [], {}
    broken = True

    async def handle(delivery):
        if (
            delivery.identity.compare(receipts.get(delivery.identity.key))
            is DeduplicationStatus.NEW
        ):
            seen.append(delivery.event.event_id)
            receipts[delivery.identity.key] = delivery.identity
        if broken:
            raise RuntimeError("simulate commit before failed acknowledgement")

    async with Extensions(tenant_id="tenant") as scope:
        definition = event_definition()
        scope.register(Listener("project", definition, handle, implementation_id="v1"))
        exporter = RuntimeListenerExporter(
            scope,
            lambda value: definition.event(
                {"value": 1}, source="runtime", event_id=value.event_id, tenant_id="tenant"
            ),
        )
        store, worker = _worker(tmp_path, exporter)
        try:
            assert (await worker.run_once()).deferred == 2
            broken = False
            assert (await worker.run_once()).succeeded
            assert len(seen) == 2
            assert (await worker.run_once()).leased == 0
        finally:
            store.close()


async def test_watcher_trigger_chain_bounds_and_accounting():
    def detect(value, _):
        return WatcherResult(
            1, (Finding(value.event_id, target(), {"value": value.payload["value"]}),)
        )

    watcher = Watcher("monitor", event_definition(), detect, implementation_id="v1")
    trigger = Trigger("watch-on-change", event_definition(), watcher, max_depth=2)
    compiled = Domain("pack", components=(trigger,)).compile()
    assert compiled.use(watcher) is watcher
    request = trigger.plan(event(), context=context())
    assert request is not None
    assert request.causation == ("watch-on-change@1",)
    assert trigger.plan(replace(event(), causation=request.causation), context=context()) is None
    assert (await watcher.run(event(), context=context())).assessed == 1
    with pytest.raises(ValueError, match="accounted outcome"):
        WatcherResult(3, suppressed=1)
    with pytest.raises(HarnessError, match="tenant"):
        await watcher.run(event(), context=context("other"))


def test_interactions_require_supported_channel_and_exact_unexpired_subject():
    response = Schema("answer", Value)
    interaction = Interaction("compare", response, affordance="comparison")
    channel = ChannelCapabilities("web", "1", ("comparison",))
    channel.require(interaction)
    with pytest.raises(HarnessError, match="presentation adapter"):
        ChannelCapabilities("text", "1", ("question",)).require(interaction)
    now = datetime.now(UTC)
    request = interaction.request(
        request_id="q1",
        target=target(),
        subject_digest=response.digest,
        expires_at=now + timedelta(minutes=1),
        payload={"prompt": "Select value"},
    )
    assert interaction.accept(
        request, {"value": 1}, target=target(), subject_digest=response.digest, now=now
    ) == {"value": 1}
    with pytest.raises(HarnessError, match="stale"):
        interaction.accept(
            request, {"value": 1}, target=target("2"), subject_digest=response.digest, now=now
        )
    with pytest.raises(HarnessError, match="expired"):
        interaction.accept(
            request,
            {"value": 1},
            target=target(),
            subject_digest=response.digest,
            now=now + timedelta(minutes=2),
        )


async def test_channel_adapter_negotiates_before_delivery_and_checks_receipt_identity():
    schema = Schema("response", Value)
    interaction = Interaction("question", schema)
    now = datetime.now(UTC)
    request = interaction.request(
        request_id="request",
        target=target(),
        subject_digest=schema.digest,
        expires_at=now + timedelta(minutes=1),
        payload={"question": "Value?"},
    )

    class Adapter:
        capabilities = ChannelCapabilities("test", "1", ("question",))
        calls = 0
        wrong = False

        async def present(self, request):
            self.calls += 1
            return ChannelReceipt(
                "wrong" if self.wrong else request.request_id, "test", "delivered"
            )

    adapter = Adapter()
    assert (await interaction.present(adapter, request, now=now)).status == "delivered"
    adapter.wrong = True
    with pytest.raises(HarnessError, match="different delivery"):
        await interaction.present(adapter, request, now=now)
    adapter.capabilities = ChannelCapabilities("test", "2", ("question",))
    with pytest.raises(HarnessError, match="protocol"):
        await interaction.present(adapter, request, now=now)
    assert adapter.calls == 2
