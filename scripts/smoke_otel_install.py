"""Exact-wheel smoke for the optional OpenTelemetry integration."""

from __future__ import annotations

import asyncio
import logging

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk.metrics import MeterProvider

from agnoclaw import OpenTelemetryRuntimeSink
from agnoclaw.runtime.telemetry import RuntimeTelemetryBatch, RuntimeTelemetryRecord


def main() -> None:
    logger_provider = LoggerProvider()
    meter_provider = MeterProvider()
    logger = logging.getLogger("agnoclaw.otel-wheel-smoke")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(LoggingHandler(logger_provider=logger_provider))
    sink = OpenTelemetryRuntimeSink(
        logger=logger,
        meter=meter_provider.get_meter("agnoclaw-wheel-smoke"),
    )
    record = RuntimeTelemetryRecord(
        event_type="run.created",
        occurred_at="2026-08-14T00:00:00+00:00",
        attributes=(("agnoclaw.event.type", "run.created"),),
    )
    asyncio.run(
        sink.export(
            RuntimeTelemetryBatch(
                records=(record,),
                event_type_counts=(("run.created", 1),),
            )
        )
    )
    assert OTLPSpanExporter
    logger_provider.shutdown()
    meter_provider.shutdown()


if __name__ == "__main__":
    main()
