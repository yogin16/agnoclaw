# Embedding agnoclaw

This folder documents how to embed the current harness core inside another service.
The API is useful today: the model/registered-capability, immutable data-seed, and
run-owned manager slice has an overlapping isolated-Agent path. Supported host-local
built-ins get fresh tools/Agents and exact release but remain intentionally single-flight
with other stateful/custom/streaming surfaces.
Registered capabilities and currently constructed first-party tools use governed nested
operation settlement inside lifecycle runs. Lifecycle persistence remains preview, and
arbitrary checkpoint continuation is not yet a stable contract. Read the limitations
below before multi-tenant use.

## What embedding gives you

- Stable `run/arun` harness API.
- Typed execution context (`ExecutionContext`).
- Policy checkpoints across run and tool boundaries.
- Structured events via pluggable event sinks.
- Runtime path/network guardrails.
- Single-backend runtime injection for built-in tools, skills, and browser.
- Optional first-party `LLMSandboxBackend` for Docker-first sandbox execution.
- Optional authenticated AgentOS lifecycle server and claim-to-context adapter.
- Preview `start/get_run`, `HarnessRun`, and transactional SQLite lifecycle authority.
- Version-pinned registered capabilities with async policy/permission checks, active
  lease fencing, durable approval-before-effect, operation replay, and pre-settlement
  result redaction.

## Minimal embedding pattern

```python
from agnoclaw import AgentHarness, HarnessConfig
from agnoclaw.runtime import ExecutionContext

harness = AgentHarness(
    workspace_dir="/srv/agent/workspace",
    config=HarnessConfig.local_safe(profile="quick"),
)

ctx = ExecutionContext.create(
    user_id="u-123",
    session_id="s-123",
    workspace_id="/srv/agent/workspace",
    tenant_id="tenant-a",
    org_id="org-a",
    roles=["employee"],
    scopes=["agents.run"],
    request_id="req-123",
    trace_id="trace-123",
)

result = harness.run("Summarize open pull requests", context=ctx)
print(result.content)
```

## Runtime behavior to rely on

- Harness emits lifecycle events for run/prompt/model/policy/tool checkpoints.
- Policy denials return typed `HarnessError` values.
- Tool calls are checked by guardrails before execution.
- Built-in tools and skill installs can target one coherent custom backend.
- Run metadata includes normalized execution context for downstream tooling.
- `AgentHarness(capabilities=[...])` exposes provider-safe Agno functions without
  surrendering the registered capability's exact version, scope, or effect contract.

## Current embedding constraints

- Factory-backed registered capabilities, immutable dependencies/session seeds, and
  run-owned compression/summary managers can use the overlapping distinct-Agent path.
  Supported quick/legacy host-local built-ins, including configured MCP clients, are
  distinct and run-owned too, but their effects remain conservative. Arbitrary custom
  tools, contexts, packs/plugins, caller-supplied MCP, other mutable resources, and streams fail overlap with
  `HARNESS_RUN_IN_PROGRESS`; exhaust or close streams and use a harness per worker lane
  for those surfaces.
- `start/get_run` persist lifecycle state, events, outer-model operation settlement,
  and governed nested operations for registered capabilities. Known successful
  artifact-backed results can reattach without redispatch. Arbitrary model/tool
  checkpoint continuation, ambiguity reconciliation, and universal legacy-tool
  normalization are not implemented.
- Context thresholds return typed recommendations. With a positive budget and
  `artifact_store`, opt-in automatic mode archives before replacement at 90%, uses a
  deterministic emergency path at 97%, and permits one fenced exact-invocation retry
  for an Agno-classified non-streaming overflow. Manual `compact_session()`, scoped
  lexical search, and selective rehydration remain available. Setting
  `max_inline_output_chars` additionally gives registered lifecycle capabilities and
  first-party tools—including configured MCP calls—lossless artifact envelopes and
  bounded same-session paging. Custom/plugin/context/raw-MCP and outer-model spill, cross-process fencing,
  live-provider proof, and repeated-drift certification remain open.
- Legacy institutional `enable_learning=True` requires
  `learning_knowledge=Knowledge(vector_db=...)` and fails before a model call when
  absent. Scoped candidate capture, review, promotion/rollback, and evidence-bound
  reconciliation are available through an explicit `LearningProfile` plus learning
  ledger/artifact store. Automatic promotion and measured benefit remain uncertified.
- The no-argument `legacy` compatibility profile retains `bypass`. Explicit quick,
  durable, and service presets use fail-closed policy/approval defaults; service hosts
  must still provide their approver and tenant policy.
- Registered-capability approval requests, decisions, expiry/cancellation, and exact
  grants are durable. A live worker can be decided by another host sharing the store;
  reconstructing the suspended Agno/model stack after worker-process death remains open.
- Raw entries supplied through `tools=` normalize to bounded opaque/live-only evidence.
  Named-legacy `run/arun` remains serialized; `start()` and every explicit-profile
  convenience call reject them before run creation. Convert them to explicit specs
  rather than assuming operation/lease/replay guarantees.
- Events can contain sensitive metadata or summaries. Supply a redacting sink and data
  retention policy.

These are tracked in [Harness gap analysis](../harness-gap-analysis.md).
The precise implemented lifecycle contract is in
[Run lifecycle and RuntimeStore](../runtime-lifecycle.md).
The matching authenticated HTTP grammar, scopes, compatibility boundary, and remote
client are in [AgentOS and remote lifecycle adapter](./agentos-adapter.md).

## Recommended current service shape

```python
harness = AgentHarness(
    config=HarnessConfig.service(
        storage={"backend": "postgres", "postgres_url": postgres_url},
    ),
    runtime_store=postgres_runtime_store,
    artifact_store=artifact_store,
    db=agno_postgres_db,
    include_default_tools=False,
    workspace_dir="/srv/agent/workspace",
    tenant_id="tenant-a",
    policy_engine=tenant_policy,
    event_sink=redacting_event_sink,
    permission_mode="default",
    permission_approver=approval_backend,
    permission_require_approver=True,
    capabilities=tenant_capabilities,
    enable_learning=False,
)
```

Use a stable `ExecutionContext` for every request, keep user/tenant authorization in
deterministic application code, and put hosted queueing/scheduling plus approval UI in
AgentOS or your platform. Settle approval only through the trusted harness host API.
Give every custom policy engine a stable `policy_version`, give custom approvers an
`approval_version`, declare capability scopes/effects honestly, and invoke
lease-governed generated capabilities through `start()` when control is needed, or
through non-streaming `run()/arun()` on an explicit profile for a completed result.

## Related docs

- [World-class Harness Strategy](../world-class-harness.md)
- [Target Harness Architecture](../architecture.md)
- [Learning and Self-improvement](../learning.md)
- [Context Management](../context-management.md)
- [Harness Evaluation](../evaluation.md)
- [Current Harness Gap Analysis](../harness-gap-analysis.md)
- [Policy and Guardrails](./policy-and-guardrails.md)
- [Runtime Backends](./workspace-backends.md)
- [AgentOS Adapter](./agentos-adapter.md)
- [PE Risk Testing Plan](./pe-risk-testing-plan.md)
- [PE Simulation Package](../../examples/pe_risk_platform/README.md)
- [v0.2 Spec](../../spec/v0.2-harness-core.md)
