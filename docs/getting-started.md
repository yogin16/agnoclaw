# Getting started

This tutorial takes one agent from installation through a scoped session and a
controllable lifecycle run. It uses public APIs only.

## 1. Choose a model path

You need Python 3.11–3.14 and one of:

- a provider credential plus its agnoclaw extra;
- a running local Ollama server plus `agnoclaw[local]`; or
- a prebuilt Agno `Model` for `quick`/`legacy`, or an `AgnoModelFactory` that creates
  fresh transports for concurrent/recoverable `durable` and `service` runs.

For Claude:

```bash
python -m venv .venv
source .venv/bin/activate
pip install "agnoclaw[anthropic]"
export ANTHROPIC_API_KEY="..."
```

For local Ollama:

```bash
ollama serve
ollama pull qwen3:8b
pip install "agnoclaw[local]"
```

Provider SDKs are intentionally absent from the core wheel. A missing SDK fails before
agent construction with `MODEL_PROVIDER_DEPENDENCY_MISSING` and names the required
extra.

Enterprise gateways, replay models, and deterministic test models should use an
`AgnoModelFactory` with explicit profiles. Its canonical implementation digest enters
the harness spec, credentials remain inside trusted host code, and every invocation
must return a fresh Agno `Model`. Reconstructing the same factory identity supports the
certified outer-operation restart path; changed implementation digests fail closed
before dispatch. See [Configuration](configuration.md#custom-agno-model-factories).

## 2. Make one call

```python
from agnoclaw import AgentHarness, HarnessConfig

harness = AgentHarness(
    "anthropic:claude-sonnet-4-6",
    config=HarnessConfig.quick(),
)
result = harness.run("Reply with exactly: harness ready")
print(result.content)
```

Use `AgentHarness("ollama:qwen3:8b")` for the local path. Provider output can vary; the
important first check is that construction and one call complete without reaching into
`harness._agent`.

## 3. Add trusted request identity

For an embedded service, authenticate outside the harness and create one context from
trusted claims. Do not copy tenant or user authority from model-controlled text or an
untrusted request body.

```python
import asyncio

from agnoclaw import AgentHarness, ExecutionContext, HarnessConfig


async def main() -> None:
    harness = AgentHarness(
        "anthropic:claude-sonnet-4-6",
        config=HarnessConfig.local_safe(profile="quick"),
        workspace_dir="./agent-workspace",
        permission_mode="default",
        permission_require_approver=True,
    )
    context = ExecutionContext.create(
        tenant_id="acme",
        user_id="alice",
        session_id="onboarding-1",
        workspace_id="support",
        roles=["analyst"],
        scopes=["agents.run"],
        request_id="request-1",
    )
    result = await harness.arun("Summarize this session", context=context)
    print(result.content)
    await harness.aclose(policy="drain")


asyncio.run(main())
```

The explicit quick/local-safe presets deliberately reject permission-gated effects
until the host supplies an approver. For a read-only first integration, use
`permission_mode="plan"`. The no-argument `legacy` compatibility posture retains the
old `bypass` behavior during the preview; new embeddings should not rely on it.

## 4. Start controllable work

For recoverable controllable work, construct the durable profile with explicit stores.
Configuration contains serializable intent; live stores remain constructor resources:

```python
from agnoclaw import LocalArtifactStore, SQLiteRuntimeStore

await harness.aclose()
harness = AgentHarness(
    "anthropic:claude-sonnet-4-6",
    config=HarnessConfig.durable(),
    include_default_tools=False,
    runtime_store=SQLiteRuntimeStore("./runtime.db"),
    artifact_store=LocalArtifactStore("./artifacts"),
)
```

`start()` commits the logical run before returning a handle:

```python
run = await harness.start(
    "Investigate the incident and produce a concise timeline",
    context=context,
    idempotency_key="incident-7:timeline:v1",
    persist_output=True,
)

last_output_cursor = None
async for segment in run.output():
    print(segment.content, end="")
    last_output_cursor = segment.cursor

result = await run.wait()
```

Retrying the same idempotency key with the same request reattaches to the logical run;
a different request fails with an idempotency conflict. `get_run(run_id,
context=context)` reauthorizes before reattachment. `run.events()` remains the
content-minimized lifecycle stream. `run.output(after=last_output_cursor)` independently
resumes artifact-backed provider text; its cursor is a run-bound runtime-event cursor.

Current lifecycle durability covers state, events, operation intent/settlement, and
artifact-backed known-success recovery. A caller may explicitly use `recover_run()` to
continue a settled pre-model request checkpoint only while provider dispatch has not
begun. At service startup, `recover_pending_runs(context=context)` scans one bounded,
exact-owner page and returns an owner-bound continuation cursor. It does not run on
harness construction or resume an arbitrary model/tool/approval stack. Read
[Run lifecycle](runtime-lifecycle.md) before running unattended jobs, and configure
artifact encryption/retention for the checkpointed request data.

An ambiguous dispatched effect parks at `waiting_for_reconciliation`; `wait()` raises
`RUN_RECONCILIATION_REQUIRED`. The host must run a bounded
`reconcile_pending_operations()` sweep with a versioned read-only provider observer and
exact-owner evidence. The harness never asks a model to guess whether its own effect
happened and never turns repeated cancellation into a false clean outcome.

## 5. Continue a session

```python
session = harness.session(context=context)
first = await session.start("Draft the incident report")
await first.wait()
follow_up = await session.start("Now add remediation owners")
```

A follow-up is a new immutable run in the same session, not a mutation of a terminal
run. Same-session lifecycle work is serialized; unrelated sessions can use the bounded
process concurrency limit.

## 6. Add learning intentionally

```python
from agnoclaw import LearningProfile

harness = AgentHarness(
    "anthropic:claude-sonnet-4-6",
    learning=LearningProfile.personal_and_session(
        user_profile="always",
        user_memory="agentic",
        session_context="always",
    ),
)

await harness.arun(
    "Remember that Alice wants incident summaries under 200 words",
    context=context,
    learning_consent=True,
)
```

Personal/session learning is scoped from the trusted context. Institutional learning
uses candidates and reviewed promotion; it is not a switch for silent global writes.
See [Learning](learning.md) and [Learning administration](learning-administration.md).

## 7. Bound a long session's model context

For an opt-in host-local long session, supply both a truthful model budget and a
recoverable artifact store:

```python
harness = AgentHarness(
    "anthropic:claude-sonnet-4-6",
    session_id="incident-7",
    user_id="alice",
    artifact_store=LocalArtifactStore("./context-artifacts"),
    max_context_tokens=120_000,
    auto_compact_context=True,
    max_inline_output_chars=8_192,
)

result = await harness.arun("Continue the incident investigation")
```

At 90%, the next run archives the complete trajectory before replacing live history;
at 97%, the preservation path avoids extra model calls. Both must release below 70% or
fail without changing the session. The feature is off by default and does not yet
coordinate replacement across processes. A typed provider overflow gets one exact
non-streaming model-invocation retry after emergency compaction; streams and runs that
already observed a tool call are never replayed. See [Context management](context-management.md).

The separate inline-output bound externalizes only oversized governed
`CapabilitySpec` results on lifecycle runs. The model gets a safe preview and can page
the exact verified artifact with `read_spilled_output`, including in a later turn of
the same trusted session. Raw tools, built-ins, media, model results, and child runs are
not yet covered; see [Durable artifacts](artifacts.md#model-context-spill).

## 8. Add a governed capability

For tools that need durable policy, lease, and replay semantics, declare a
`CapabilitySpec` and pass it to the harness instead of adding another raw Agno tool:

```python
from agnoclaw import (
    CapabilityConcurrency,
    CapabilityKind,
    CapabilityLifetime,
    CapabilityRecovery,
    CapabilitySpec,
    CapabilityTrust,
    EffectClass,
)


async def lookup_inventory(*, sku: str) -> dict[str, str]:
    return {"sku": sku, "status": "available"}


inventory_lookup = CapabilitySpec(
    name="inventory.lookup",
    version="1.0.0",
    kind=CapabilityKind.TOOL,
    description="Read one inventory record",
    effect_class=EffectClass.READ_ONLY,
    trust=CapabilityTrust.VERIFIED,
    lifetime=CapabilityLifetime.RUN,
    concurrency=CapabilityConcurrency.ISOLATED,
    recovery=CapabilityRecovery.RECREATABLE,
    implementation_digest="sha256:replace-with-your-build-digest",
    input_schema={
        "type": "object",
        "properties": {"sku": {"type": "string"}},
        "required": ["sku"],
    },
    required_scopes=("inventory:read",),
    factory=lambda: lookup_inventory,
)

harness = AgentHarness(
    "anthropic:claude-sonnet-4-6",
    capabilities=[inventory_lookup],
    permission_mode="default",
)
run = await harness.start("Is SKU A-100 available?", context=context)
result = await run.wait()
```

The model sees a provider-safe function, while agnoclaw keeps the exact
`inventory.lookup@1.0.0` identity. Before materialization it reauthorizes the active
run, checks declared scopes, validates effective arguments against `input_schema`,
checks policy, and renews the store-issued lease. It commits operation intent before
dispatch and applies result redaction before settlement.

Generated capabilities require lifecycle ingress. Prefer `start()` for identity and
control; non-streaming `run()/arun()` on an explicit profile now uses the same kernel
and returns only the completed result. Named-legacy direct calls and existing raw
`tools=` do not gain these operation-level guarantees. See
[Capabilities](capabilities.md) before using effectful or long-running capabilities.

For an Agno `ContextProvider`, use `context_provider_capability()` to build the common
run-owned read-query spec. The builder requires an explicit read-only effect
attestation and never exposes provider writes or arbitrary `mode=tools` functions.
See [Agno context providers](context-providers.md) for the factory, bounds, lifecycle,
and migration recipe.

## 9. Approve an exact call from your host

When a registered capability requires approval and there is no callback, the run waits
on a durable request. An operator loop can inspect and decide it without exposing
approval authority to the model:

```python
import asyncio

from agnoclaw.runtime import ApprovalState

while True:
    pending = harness.capability_approvals(
        str(run.run_id),
        context=context,
        states=(ApprovalState.PENDING,),
    )
    if pending:
        break
    await asyncio.sleep(0.1)

harness.decide_capability_approval(
    pending[0].request.request_id,
    run_id=str(run.run_id),
    context=context,
    approved=True,
    issuer="operator:alice",
    reason_code="CHANGE_REVIEW_APPROVED",
)
result = await run.wait()
```

The request contains digests and identity bindings rather than raw capability
arguments. Approval is rechecked after lease renewal immediately before external
dispatch. Cancellation or expiry closes the request, and a late response cannot revive
it. This supports live cross-process decision handling; arbitrary checkpoint restart
of the suspended model call remains a later release gate.

## 10. Schedule unattended work

Reuse the durable runtime database rather than the compatibility JSON file:

```bash
agnoclaw schedule add hourly-check \
  --runtime-db ./runtime.db \
  --schedule 1h \
  --prompt "Check the queue and write a concise status report" \
  --isolated \
  --max-retries 2

agnoclaw schedule worker \
  --runtime-db ./runtime.db \
  --artifacts ./artifacts \
  --model claude-sonnet-4-6 \
  --provider anthropic
```

The worker claims an occurrence with the database clock, launches it through
`AgentHarness.start()`, persists the lifecycle run ID, and renews a fenced lease. A
crash reclaims the same logical attempt; only a known retryable failure creates a new
attempt. Add `--learning-consent` only when this trusted job is allowed to use its
configured personal/session learning policy. Read [Durable scheduling](durable-scheduling.md)
before service deployment or JSON-store migration.

## 11. Choose the next guide

- Embed in an application: [Embedding agnoclaw](embedding/README.md)
- Configure files and environment: [Configuration](configuration.md)
- Use terminal commands: [CLI reference](cli.md)
- Add workspace instructions: [Workspace files](workspace.md)
- Add a skill: [SKILL.md reference](skills.md)
- Operate PostgreSQL: [PostgreSQL RuntimeStore](postgresql-runtime-store.md)
- Run unattended jobs: [Durable scheduling](durable-scheduling.md)
- Understand current limitations: [Harness gap analysis](harness-gap-analysis.md)
