# Agno context providers

Status: governed read-query adapter implemented; raw and effectful provider surfaces
remain compatibility-only

Last verified: 2026-08-13 with Agno 2.9.0

Agno context providers are useful adapters over files, workspaces, databases, web
search, calendars, mail, wikis, Google Drive, Slack, and MCP. Agnoclaw supports two
intentionally different ingress paths. Choose the path from the runtime guarantee you
need, not from how short the constructor looks.

| Path | Use for | Guarantees | Limit |
|---|---|---|---|
| `context_providers=[provider]` | Existing named-legacy direct `run/arun` integrations | Agno-native tools/instructions, setup/close, compatibility events, harness single-flight containment | `start()` and explicit-profile convenience calls reject it before run creation because live ownership/effects/recovery are opaque. |
| `capabilities=[context_provider_capability(...)]` | Long-running `start()` work that only queries a certified read-only source | Run-owned provider, immutable identity/schema, admission/scopes, policy/approval, lease, intent-before-query, artifact settlement/replay, redaction/spill, query events, close | The host must attest that the complete query implementation is read-only. Writes and arbitrary underlying tools are deliberately unsupported. |

This split is a safety property. A tool name such as `query_docs`, Agno's `read=True`
flag, or an MCP annotation cannot prove that a custom provider's nested agent and tools
have no external side effects.

## Governed durable query

Use a zero-argument factory that returns a fresh provider. The explicit implementation
digest must identify the provider implementation and everything behaviorally relevant
behind it.

```python
from agno.context.workspace import WorkspaceContextProvider

from agnoclaw import (
    AgentHarness,
    EffectClass,
    HarnessConfig,
    LocalArtifactStore,
    SQLiteRuntimeStore,
    context_provider_capability,
)

workspace_context = context_provider_capability(
    "workspace",
    lambda: WorkspaceContextProvider(root="/srv/projects/acme"),
    version="1.0.0",
    implementation_digest="sha256:replace-with-your-build-or-source-digest",
    effect_class=EffectClass.READ_ONLY,
    required_scopes=("workspace:read",),
    description="Answer focused questions from the Acme project workspace.",
)

harness = AgentHarness(
    model="anthropic:claude-sonnet-4-6",
    config=HarnessConfig.durable(),
    capabilities=[workspace_context],
    runtime_store=SQLiteRuntimeStore("runtime.db"),
    artifact_store=LocalArtifactStore("artifacts"),
    max_inline_output_chars=8_192,
)

run = await harness.start("Find the deployment rollback procedure", context=context)
result = await run.wait()
await harness.aclose()
```

The model sees one stable `query_workspace(question)` function. Remote or
provider-specific tools and schemas do not expand the outer model's base prompt.

The builder requires `effect_class=EffectClass.READ_ONLY` even though that is the only
accepted value. This small explicit decision prevents a convenience API from silently
guessing replay semantics. If the query may send mail, edit a wiki, execute SQL writes,
call an unclassified MCP tool, or trigger any other effect, declare a normal
`CapabilitySpec` with the correct effect, idempotency, recovery, and reconciliation
contract instead.

## Exact lifecycle

For each dispatched query, the durable capability executor:

1. reauthorizes the exact admitted tenant/user/session and declared scopes;
2. validates the question against the frozen schema before materialization;
3. runs guardrails, permissions, policy, and any durable approval;
4. persists the operation intent and renews the active run lease;
5. invokes the factory, verifies the exact provider ID and `read=True`, and calls
   `asetup()`;
6. passes Agno's active `RunContext` to `aquery()`, preserving user, session, metadata,
   and per-run dependencies without placing those live objects in the operation log;
7. validates and bounds the returned `Answer` before policy/redaction and settlement;
8. persists the canonical result artifact, or replays an already settled result without
   constructing or querying the provider again; and
9. calls `aclose()` on success, failure, timeout, or cancellation.

The provider resource is run-lifetime, isolated, recreatable, and serialized only by
the surrounding run semantics. A factory must not return a process-global singleton.
If it does, the host—not agnoclaw—has violated the declared ownership contract.

## Result and trust contract

The model receives a JSON-like envelope:

```json
{
  "type": "agnoclaw.context_provider_answer",
  "schema_version": "1.0",
  "provider": {"id": "workspace", "name": "Workspace"},
  "trust": "untrusted_data",
  "answer": {"text": "...", "results": []}
}
```

Empty `text` or `results` follow Agno's `serialize_answer()` behavior. Results must be
Agno `Document` values. Provider content remains data: it cannot grant scopes, approve
an operation, alter policy, or override system/developer/host instructions. The base
system prompt reinforces that rule for all user, web, MCP, file, provider, and tool
results.

Default bounds are:

- provider ID: 128 characters;
- question: 16,384 characters, configurable up to 65,536;
- answer: 4,194,304 canonical UTF-8 bytes, configurable downward; and
- structured results: 1,000 Agno `Document` objects.

For a smaller model-context ceiling, also set `max_inline_output_chars` with an
`ArtifactStore`. The full already-governed result remains in the scoped operation
artifact; the model receives a bounded preview and can page it through
`read_spilled_output`.

Errors are stable and content-minimized:

| Code | Meaning |
|---|---|
| `CONTEXT_PROVIDER_FACTORY_INVALID` | Factory did not return an Agno `ContextProvider`. |
| `CONTEXT_PROVIDER_CONTRACT_MISMATCH` | Runtime ID changed or the provider is not query-readable. |
| `CONTEXT_PROVIDER_RUN_CONTEXT_REQUIRED` | Dispatch bypassed the governed Agno tool ingress. |
| `CONTEXT_PROVIDER_ANSWER_INVALID` | Return type/results/JSON shape violated the Agno contract. |
| `CONTEXT_PROVIDER_ANSWER_BUDGET_EXCEEDED` | Canonical result exceeded the configured byte limit. |

Provider exception messages are not copied into durable safe diagnostics. Normal
operation/capability error codes still apply for scope, policy, lease, artifact,
approval, and settlement failures.

## Direct compatibility path

The original Agno-native path remains useful for short work:

```python
provider = WorkspaceContextProvider(root=".")
harness = await AgentHarness.create(context_providers=[provider])
result = await harness.arun("Map this repository")
await harness.aclose()
```

It preserves `provider.get_tools()`, provider instructions, async setup/close, duplicate
tool-name rejection, and `context.provider.query.*` / `context.provider.update.*`
events. It does not acquire durable operation semantics. Calling `start()` with raw
providers raises `EXTENSION_TOOL_LIFECYCLE_UNSUPPORTED` before `RuntimeStore.create_run`.

Do not use direct `AgentHarness(...)` instead of `await AgentHarness.create(...)` when a
raw provider needs eager async setup. The governed factory adapter always performs
setup lazily inside its owned dispatch.

## Agno modes and writes

- `ContextMode.default` and `ContextMode.agent` usually expose a provider query wrapper.
  The governed adapter calls `aquery()` directly and does not depend on either mode's
  generated tool shape.
- `ContextMode.tools` can expose arbitrary provider/backend tools. It remains a direct
  compatibility surface; agnoclaw does not infer effects from those functions.
- Provider `update`/`aupdate` is not exposed by the convenience adapter. Model-visible
  writes need an explicit, versioned `CapabilitySpec`, least-privilege scopes, approval,
  provider idempotency or reconciliation, and tests for crash-after-effect.
- `MCPContextProvider` can hide arbitrary server tools behind a nested agent. Do not
  attest it read-only unless server policy independently proves every reachable tool is
  read-only. For general MCP, prefer agnoclaw's native deferred
  [`search_mcp_tools` / `call_mcp_tool`](mcp.md) path, whose remote annotations remain
  untrusted and whose call effect is conservatively non-repeatable.

## Testing a provider integration

A release-ready provider integration should prove:

- the factory is lazy and produces a fresh instance per dispatch;
- ID/read-contract mismatches fail before query;
- setup, query, and close occur on the owned async path;
- close still occurs after query failure, timeout, and cancellation;
- tenant/user/session/dependencies arrive through `RunContext` without cross-run leak;
- required scopes and policy denial prevent factory invocation;
- duplicate operation identity replays without a second query;
- malformed, huge, and prompt-injection-shaped answers remain bounded untrusted data;
- redaction happens before artifact settlement;
- same-session spill paging works and cross-session access fails; and
- the exact supported Agno lanes pass the same suite.

The built-in tests cover the adapter contract and a full durable harness query. Provider
packages should add real-backend authorization, outage, rate-limit, retry, and resource
leak tests because agnoclaw cannot certify an external service from its abstract class.

## Current non-claims

The adapter does not make nested provider execution checkpoint-resumable, prove a
custom implementation is read-only, supply OAuth credentials, govern provider writes,
or certify third-party backend availability. It creates the smallest safe bridge from
Agno's current query abstraction to agnoclaw's durable operation kernel. Broader
context-provider effect/reconciliation adapters remain a release workstream.
