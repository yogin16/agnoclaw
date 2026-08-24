# Observability and safe run inspection

Status: 0.12 development preview; durable content-free logs/metrics and owner-authorized
inspection implemented, live span coverage and production telemetry certification open

Last verified: 2026-08-14

This is the operational contract for seeing what the harness is doing without turning
telemetry into a second prompt/output database. The default design is intentionally
small:

```text
RuntimeStore transaction
  -> ordered outbox event
  -> RuntimeOutboxWorker (at least once)
  -> RuntimeTelemetryBatchExporter (strict safe projection)
  -> OpenTelemetryRuntimeSink
       +-> one structured log per committed event
       +-> low-cardinality event/token/cost counters
```

The same durable store also supports a separate, authenticated inspection path:

```text
trusted ExecutionContext + runtime:run:inspect + exact owner
  -> RuntimeRunInspector
  -> bounded state/recovery report with HMAC-linked identifiers
```

Telemetry never grants inspection authority. Inspection never changes run state or
automatically performs its recovery recommendation.

## Privacy and cardinality contract

Schema `1.0` is content-free by construction. The projection uses an allowlist rather
than attempting to redact an arbitrary event body after the fact.

| Input | Telemetry behavior |
|---|---|
| run/event/attempt IDs | domain-separated HMAC-SHA256; never raw |
| registered runtime event type | emitted as one bounded token |
| unregistered/caller-controlled event type | collapsed to `runtime.unknown` |
| run/operation/approval state, transition, operation kind/effect, reconciliation verdict | emitted only when the value is a known enum member |
| revision, dispatch attempt, child depth | bounded non-negative integer |
| provider token/cost evidence | only bounded numeric fields from settled-operation or child-budget evidence |
| prompt, system message, response, tool target/arguments, metadata, exception text, approval reason, artifact bytes | never copied |

There is deliberately no “include content” switch in schema `1.0`. HMAC identifiers
appear only in logs/reports, never metric labels. Metrics use only registered event
type, `input`/`output` token type, and `USD` currency. This bounds cardinality even when
a caller appends adversarial custom events.

`RuntimeTelemetryPolicy.identifier_key` must contain at least 32 bytes and is excluded
from `repr` and equality. Load it from a secret manager or protected environment, not a
source file or command-line argument. `key_id` is a non-secret rotation label. Rotating
the key intentionally breaks correlation with older hashes; retain the old key only for
the approved investigation/retention window.

The upstream OpenTelemetry GenAI semantic conventions currently classify their agent
spans and broader GenAI conventions as **Development**. They define operations such as
`invoke_agent`, `execute_tool`, and model chat, while content fields are opt-in because
they may contain sensitive data. Agnoclaw maps its safe operation evidence to those
operation names but pins its own projection schema instead of treating a development
convention as a stable wire contract. See the current upstream
[GenAI agent spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md)
and [GenAI conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/README.md).

## Install and configure OpenTelemetry

The projection and sink interface have no core dependency. Install the SDK/exporter
only in the telemetry worker process:

```bash
pip install "agnoclaw[otel]"
```

The `otel` extra is locked and exact-wheel tested with OpenTelemetry `1.44.x`. The host
owns endpoint credentials, TLS, resource attributes, sampling, batching, shutdown, and
Collector deployment. `OpenTelemetryRuntimeSink` never changes global providers or
constructs a network exporter.

This standalone worker example uses OTLP/HTTP environment configuration and keeps
provider ownership explicit:

```python
import asyncio
import logging
import os

from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from agnoclaw import (
    OpenTelemetryRuntimeSink,
    RuntimeOutboxConfig,
    RuntimeOutboxWorker,
    RuntimeTelemetryBatchExporter,
    RuntimeTelemetryPolicy,
    SQLiteRuntimeStore,
)

resource = Resource.create({"service.name": "incident-agent"})
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
meter_provider = MeterProvider(resource=resource, metric_readers=(metric_reader,))

logger = logging.getLogger("agnoclaw.runtime")
logger.setLevel(logging.INFO)
logger.addHandler(LoggingHandler(logger_provider=logger_provider))

sink = OpenTelemetryRuntimeSink(
    logger=logger,
    meter=meter_provider.get_meter("agnoclaw.runtime"),
)
exporter = RuntimeTelemetryBatchExporter(
    sink=sink,
    policy=RuntimeTelemetryPolicy(
        identifier_key=os.environ["AGNOCLAW_TELEMETRY_IDENTIFIER_KEY"].encode(),
        key_id="2026-q3",
        service_name="incident-agent",
    ),
)
store = SQLiteRuntimeStore("runtime.db")
worker = RuntimeOutboxWorker(
    store=store,
    exporter=exporter,
    config=RuntimeOutboxConfig(owner="otel-worker-1"),
)

stop = asyncio.Event()
try:
    await worker.run(stop=stop)
finally:
    logger_provider.shutdown()
    meter_provider.shutdown()
    store.close()
```

OpenTelemetry recommends an SDK plus Collector/exporter rather than baking a backend
into application code; its Python trace/metric APIs are stable while logs remain a
developing signal. Review the current [Python exporter
guidance](https://opentelemetry.io/docs/languages/python/exporters/) before production
rollout.

### Emitted signals

Structured logs use message `agnoclaw.runtime.event` and flat safe attributes including:

- `agnoclaw.event.type`, sequence, occurrence time, and HMAC identifiers;
- `agnoclaw.state*`, transition, operation kind/effect, revision/dispatch attempt;
- `gen_ai.operation.name` and reported `gen_ai.usage.*` when applicable;
- `agnoclaw.cost.microusd` when the provider reported a normalized value.

Metrics are monotonic counters:

- `agnoclaw.runtime.events` by registered event type;
- `agnoclaw.gen_ai.token.usage` by input/output;
- `agnoclaw.gen_ai.cost` in USD micro-units.

Delivery is at least once. A telemetry backend must deduplicate logs by the HMAC event
identifier when exact counts matter. Metric retries can double-count because standard
counter export is not a transactional receipt; use ledger-derived offline aggregation
for billing or compliance truth. The runtime ledger, never an observability backend,
remains authoritative.

## Inspect a run

The SDK requires an authenticated `ExecutionContext`, exact owner, inspection scope,
and the same HMAC policy used for operator correlation:

```python
from agnoclaw import (
    RUN_INSPECT_SCOPE,
    ExecutionContext,
    RuntimeRunInspector,
    RuntimeTelemetryPolicy,
)

context = ExecutionContext.create(
    tenant_id="tenant-42",
    user_id="user-7",
    session_id="incident-8842",
    workspace_id="production-ops",
    scopes=(RUN_INSPECT_SCOPE,),
)
report = await RuntimeRunInspector(
    store=runtime_store,
    policy=RuntimeTelemetryPolicy(identifier_key=key_bytes, key_id="2026-q3"),
).inspect("run_123", context=context)
print(report.recommendation.value)
print(report.to_dict())
```

The report contains bounded event/operation/approval/child/artifact counts, known state
and operation enums, timestamps, terminal result/error **presence**, and a deterministic
recovery recommendation. It does not contain snapshot metadata, session/user/tenant
values, operation target/request metadata, approval capability/arguments/reason,
artifact address/metadata, terminal value/error, or arbitrary event payload.

Owner mismatch and not-found intentionally produce the same
`RUN_INSPECTION_NOT_AUTHORIZED` error. The first-party CLI additionally opens the store
read-only:

```bash
export AGNOCLAW_TELEMETRY_IDENTIFIER_KEY='load-a-real-32-byte-secret-from-your-vault'

agnoclaw inspect run run_123 \
  --sqlite-db ./runtime.db \
  --tenant-id tenant-42 \
  --user-id user-7

export RUNTIME_DSN='postgresql://...'
agnoclaw inspect run run_123 \
  --postgres-credential-env RUNTIME_DSN \
  --tenant-id tenant-42 \
  --user-id user-7 \
  --json
```

The command accepts credential/key **environment names**, not secret values. SQLite
uses URI read-only plus `PRAGMA query_only`; PostgreSQL skips migration and requests
`default_transaction_read_only=on`. Both require the current runtime schema and expose
typed mutation refusal. JSON success goes to stdout; structured errors go to stderr:

| Exit | Meaning |
|---:|---|
| `0` | report produced |
| `77` | run absent or exact owner not authorized |
| `78` | missing dependency, credential/key, backend selection, or current schema |
| `75` | transient runtime-store availability/capacity failure |
| `1` | other safe typed inspection failure |

`at_limit: true` is conservative: the report reached its configured bound, so more
records may exist. It is not proof of truncation. Recommendations are explanations,
not mutation authority: `review_approval`, `reconcile`, `resume`, `respond`, or `start`
must still enter the ordinary authenticated lifecycle APIs with their own checks.

## Deployment checklist

- Run the outbox worker under supervision with a stable owner and alert on dead letters.
- Use a per-environment HMAC key from a secret manager; document rotation/retention.
- Send OTLP over authenticated TLS to a Collector with tenant-aware routing and bounded
  queues; test backend outage and backpressure.
- Apply log/metric retention and regional policy independently from runtime retention.
- Never use telemetry counters for billing, effect settlement, or recovery authority.
- Keep Collector processors from adding raw request/response bodies.
- Grant the CLI/worker database role only required reads; `read_only=True` is defense in
  depth, not a replacement for database RBAC.
- Test dashboards/alerts against `runtime.unknown`, outbox quarantine, ambiguous
  operations, approval waits, and missing provider measurements.

## Honest remaining boundary

Implemented and tested: SQLite/PostgreSQL durable source events, at-least-once outbox,
strict allowlist projection, HMAC domain separation/rotation behavior, registered event
cardinality, content-leak sentinels, safe flat logs, low-cardinality metrics, exact-wheel
OpenTelemetry import/smoke, owner/scope inspection, stable CLI JSON/errors, and database-
enforced read-only inspection.

Still open for the final 0.12 release:

- live `invoke_agent`/model/`execute_tool` spans and durable trace/span correlation;
- outbox lag/retry/dead-letter/self-observation metrics and SLO dashboards;
- Collector outage/backpressure/queue-loss soak and production volume/cardinality proof;
- multi-destination independent acknowledgement and backend-specific dedupe recipes;
- a redacted support-bundle format, independent audit anchor, and operator UI;
- production TLS/RBAC/key-rotation/retention exercises and alert-response drills;
- model-backed quality/learning-benefit dashboards without promoting telemetry to
  learning authority.

See [Durable event export](event-export.md), [Operations and recovery](operations-and-recovery.md),
[Security](security.md), and the [0.12 release progress ledger](releases/v0.12.0-progress.md).
