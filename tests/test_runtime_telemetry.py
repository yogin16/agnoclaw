"""Privacy, cardinality, durability, and OpenTelemetry bridge contracts."""

from __future__ import annotations

import json
import logging
from dataclasses import replace

import pytest

from agnoclaw.runtime.lifecycle import RunSnapshot
from agnoclaw.runtime.outbox import RuntimeOutboxConfig, RuntimeOutboxWorker
from agnoclaw.runtime.store import RuntimeEvent, RuntimeEventInput, SQLiteRuntimeStore
from agnoclaw.runtime.telemetry import (
    OpenTelemetryRuntimeSink,
    RuntimeTelemetryBatch,
    RuntimeTelemetryBatchExporter,
    RuntimeTelemetryPolicy,
    RuntimeTelemetryRecord,
    project_runtime_event,
)


def _policy(key: bytes = b"a" * 32) -> RuntimeTelemetryPolicy:
    return RuntimeTelemetryPolicy(
        identifier_key=key,
        key_id="rotation-7",
        service_name="agnoclaw-test",
    )


def _event(**overrides) -> RuntimeEvent:
    values = {
        "event_id": "event-private-1",
        "run_id": "run-private-1",
        "sequence": 7,
        "event_type": "operation.planned",
        "attempt_id": "attempt-private-1",
        "occurred_at": "2026-08-14T08:00:00+00:00",
        "payload": {
            "kind": "capability",
            "effect_class": "non_repeatable",
            "revision": 2,
            "target": "bank.transfer.secret-target",
            "prompt": "PROMPT_SECRET_SENTINEL",
            "arguments": {"password": "ARGUMENT_SECRET_SENTINEL"},
            "reason_code": "SECRET_REASON_SENTINEL",
        },
    }
    values.update(overrides)
    return RuntimeEvent(**values)


def test_projection_is_deterministic_content_free_and_domain_separated() -> None:
    policy = _policy()
    record = project_runtime_event(_event(), policy=policy)
    encoded = json.dumps(record.to_dict(), sort_keys=True)
    attributes = dict(record.attributes)

    assert record.event_type == "operation.planned"
    assert attributes["gen_ai.operation.name"] == "execute_tool"
    assert attributes["agnoclaw.operation.kind"] == "capability"
    assert attributes["agnoclaw.effect.class"] == "non_repeatable"
    assert attributes["agnoclaw.revision"] == 2
    assert attributes["agnoclaw.run.id_hash"].startswith("hmac-sha256:rotation-7:")
    for sentinel in (
        "run-private-1",
        "event-private-1",
        "attempt-private-1",
        "bank.transfer.secret-target",
        "PROMPT_SECRET_SENTINEL",
        "ARGUMENT_SECRET_SENTINEL",
        "SECRET_REASON_SENTINEL",
    ):
        assert sentinel not in encoded
    assert policy.identifier_digest("same", domain="run") != policy.identifier_digest(
        "same", domain="event"
    )
    assert policy.identifier_digest("same", domain="run") != _policy(b"b" * 32).identifier_digest(
        "same", domain="run"
    )
    assert "aaaa" not in repr(policy)


def test_projection_bounds_event_cardinality_and_accepts_only_measurements() -> None:
    custom = project_runtime_event(
        _event(
            event_type="customer.password.secret",
            payload={
                "state": "running",
                "usage": {"input_tokens": 999},
                "cost": {"microusd": 88},
            },
        ),
        policy=_policy(),
    )
    custom_attributes = dict(custom.attributes)
    assert custom.event_type == "runtime.unknown"
    assert "gen_ai.usage.input_tokens" not in custom_attributes
    assert "agnoclaw.cost.microusd" not in custom_attributes

    measured = project_runtime_event(
        _event(
            event_type="operation.settled",
            payload={
                "state": "succeeded",
                "usage": {"input_tokens": 11, "output_tokens": 13, "secret": "never"},
                "cost": {"microusd": 17, "account": "never"},
            },
        ),
        policy=_policy(),
    )
    assert dict(measured.attributes)["gen_ai.usage.input_tokens"] == 11
    assert dict(measured.attributes)["gen_ai.usage.output_tokens"] == 13
    assert dict(measured.attributes)["agnoclaw.cost.microusd"] == 17
    assert "never" not in json.dumps(measured.to_dict())


@pytest.mark.parametrize(
    "constructor",
    [
        lambda: RuntimeTelemetryPolicy(identifier_key=b"short"),
        lambda: RuntimeTelemetryPolicy(identifier_key=b"a" * 32, key_id="unsafe value"),
        lambda: RuntimeTelemetryRecord(
            event_type="run.created",
            occurred_at="not-a-time",
            attributes=(),
        ),
        lambda: RuntimeTelemetryRecord(
            event_type="run.created",
            occurred_at="2026-08-14T08:00:00+00:00",
            attributes=(("unsafe", object()),),
        ),
    ],
)
def test_telemetry_contracts_fail_closed(constructor) -> None:
    with pytest.raises((TypeError, ValueError)):
        constructor()


class _RecordingSink:
    def __init__(self) -> None:
        self.batches: list[RuntimeTelemetryBatch] = []

    async def export(self, batch: RuntimeTelemetryBatch) -> None:
        self.batches.append(batch)


@pytest.mark.asyncio
async def test_transactional_outbox_exports_safe_batch_then_acknowledges(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(
        RunSnapshot(
            run_id="run-secret-sentinel",
            tenant_id="tenant",
            user_id="user",
            metadata={"prompt": "LEDGER_SECRET_SENTINEL"},
        )
    )
    store.append_runtime_event(
        RuntimeEventInput(
            event_id="event-secret-sentinel",
            run_id="run-secret-sentinel",
            event_type="customer.password.secret",
            occurred_at="2026-08-14T08:00:00+00:00",
            payload={"prompt": "OUTBOX_SECRET_SENTINEL"},
        )
    )
    sink = _RecordingSink()
    worker = RuntimeOutboxWorker(
        store=store,
        exporter=RuntimeTelemetryBatchExporter(sink=sink, policy=_policy()),
        config=RuntimeOutboxConfig(owner="telemetry-test"),
    )

    result = await worker.run_once()

    assert result.delivered == 2
    assert len(sink.batches) == 1
    serialized = json.dumps(sink.batches[0].to_dict(), sort_keys=True)
    assert sink.batches[0].event_type_counts == (("run.created", 1), ("runtime.unknown", 1))
    for sentinel in (
        "run-secret-sentinel",
        "event-secret-sentinel",
        "LEDGER_SECRET_SENTINEL",
        "OUTBOX_SECRET_SENTINEL",
        "customer.password.secret",
    ):
        assert sentinel not in serialized
    assert store.lease_outbox(owner="independent") == []


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _FakeCounter:
    def __init__(self) -> None:
        self.additions: list[tuple[int | float, dict[str, object]]] = []

    def add(self, amount, attributes) -> None:
        self.additions.append((amount, attributes))


class _FakeMeter:
    def __init__(self) -> None:
        self.counters: dict[str, _FakeCounter] = {}

    def create_counter(self, name, *, unit, description):
        assert unit and description
        counter = _FakeCounter()
        self.counters[name] = counter
        return counter


@pytest.mark.asyncio
async def test_otel_bridge_emits_flat_safe_logs_and_low_cardinality_metrics() -> None:
    records = (
        project_runtime_event(_event(), policy=_policy()),
        project_runtime_event(
            _event(
                event_id="event-private-2",
                sequence=8,
                event_type="operation.settled",
                payload={
                    "state": "succeeded",
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                    "cost": {"microusd": 4},
                },
            ),
            policy=_policy(),
        ),
    )
    batch = RuntimeTelemetryBatch(
        records=records,
        event_type_counts=(("operation.planned", 1), ("operation.settled", 1)),
        input_tokens=2,
        output_tokens=3,
        cost_microusd=4,
    )
    logger = logging.getLogger("agnoclaw.test.telemetry")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = _CaptureHandler()
    logger.addHandler(handler)
    meter = _FakeMeter()

    await OpenTelemetryRuntimeSink(logger=logger, meter=meter).export(batch)

    assert len(handler.records) == 2
    log_values = json.dumps([record.__dict__ for record in handler.records], default=str)
    assert "PROMPT_SECRET_SENTINEL" not in log_values
    assert handler.records[0].__dict__["agnoclaw.event.type"] == "operation.planned"
    assert meter.counters["agnoclaw.runtime.events"].additions == [
        (1, {"agnoclaw.event.type": "operation.planned"}),
        (1, {"agnoclaw.event.type": "operation.settled"}),
    ]
    assert meter.counters["agnoclaw.gen_ai.token.usage"].additions == [
        (2, {"gen_ai.token.type": "input"}),
        (3, {"gen_ai.token.type": "output"}),
    ]
    assert meter.counters["agnoclaw.gen_ai.cost"].additions == [(4, {"currency": "USD"})]


def test_batch_rejects_tampered_aggregates() -> None:
    record = project_runtime_event(_event(), policy=_policy())
    valid = RuntimeTelemetryBatch(
        records=(record,),
        event_type_counts=(("operation.planned", 1),),
    )
    with pytest.raises(ValueError, match="do not match"):
        replace(valid, event_type_counts=(("operation.planned", 2),))
