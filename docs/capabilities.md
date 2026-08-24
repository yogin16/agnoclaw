# Capability descriptors and lazy discovery

Status: 0.12 development preview; immutable descriptor, registry, bounded discovery,
explicit execution, AgentHarness-owned Agno binding, durable approval-before-effect,
exact approval-decision replay, a certified Agno 2.9 restart envelope, and governed
read-only Agno context queries implemented; raw ingress remains
compatibility-only

Last verified: 2026-08-18

`CapabilitySpec` is agnoclaw's one descriptor for models, built-in tools, caller tools,
context providers, MCP tools, skill commands, elevated commands, child runs, and
scheduled work. The goal is a small developer decision surface with enough metadata to
make concurrency, authorization, recovery, and context budgeting mechanical.

## Declare once

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

lookup = CapabilitySpec(
    name="inventory.lookup",
    version="1.0.0",
    kind=CapabilityKind.TOOL,
    description="Read one inventory record",
    tags=("inventory", "read"),
    effect_class=EffectClass.READ_ONLY,
    trust=CapabilityTrust.VERIFIED,
    lifetime=CapabilityLifetime.RUN,
    concurrency=CapabilityConcurrency.ISOLATED,
    recovery=CapabilityRecovery.RECREATABLE,
    implementation_digest="sha256:...",
    input_schema={"type": "object", "required": ["sku"]},
    required_scopes=("inventory:read",),
    factory=make_lookup_tool,
)
```

The manifest is deeply frozen and deterministic. Its digest includes every persisted
behavioral declaration but excludes the live factory. A factory is invoked only after
explicit selection/materialization.

Durable/service admission rejects opaque/live-only capabilities and non-repeatable
effects without reconciliation. Idempotent effects must declare provider-key support;
passing a key to a capability that did not declare it fails.

## Register and discover lazily

```python
from agnoclaw import CapabilityRegistry

registry = CapabilityRegistry()
registry.register(lookup)

matches = registry.search(
    "inventory",
    granted_scopes={"inventory:read"},
    limit=20,
)
selection = registry.select(
    ["inventory.lookup"],
    profile="durable",
    granted_scopes={"inventory:read"},
)
```

Registration is copy-on-write, safe for concurrent readers, and idempotent for the
same immutable manifest. Reusing `name@version` with a different digest raises
`CAPABILITY_VERSION_CONFLICT`; behavioral changes need a new version. Multiple
versions may coexist and one explicit default resolves a bare name.

Discovery returns a small `CapabilityCatalogEntry`: name, version, kind, effect, trust,
description, tags, and digest. It deliberately excludes schemas and factories. Missing
scopes hide entries before search ranking, preventing the catalog from advertising
authority the caller does not have.

The current lexical index is deterministic and dependency-free. It is a reference
implementation, not a claim of semantic retrieval quality. A host may later supply a
certified index adapter, but scope filtering, result limits, and the immutable registry
remain authoritative.

## Budgets

Selection enforces separate budgets for:

- number of unique selected capabilities;
- canonical UTF-8 bytes of selected input schemas;
- catalog search results;
- characters in the model-facing catalog summary.

`catalog_prompt()` says that schemas load only on selection and stops before its exact
character ceiling. A 1,000-capability contract test proves the registry does not dump
the full catalog or schemas into context.

## Runtime input-schema enforcement

`input_schema` is an execution boundary, not merely a hint shown to the model.
Construction validates and freezes a maximum 65,536-byte, 32-level, 4,096-node object
schema. Execution bounds the argument graph to 32 levels and 10,000 nodes, enforces the
configured canonical byte limit, and then validates the effective arguments before
operation intent or factory materialization. Errors report only capability, assertion
keyword, and schema path; argument values are not copied into diagnostics.

The dependency-free core implements a fail-closed JSON Schema subset:

- object `properties`, `required`, `additionalProperties`, property counts, and
  `dependentRequired`;
- array `items`, `prefixItems`, `contains`, item/count bounds, and `uniqueItems`;
- string length, numeric range/exclusive range/`multipleOf`, `enum`, `const`, and all
  seven JSON types;
- `allOf`, `anyOf`, `oneOf`, `not`, and `if`/`then`/`else`; and
- bounded local JSON Pointer `$ref` plus `$defs`.

Annotations such as `description`, `title`, `default`, `examples`, and `format` remain
annotations. Unknown assertion keywords (including regex-bearing `pattern` and
`patternProperties`) and external references fail construction instead of being
silently ignored. Add support in the validator and its adversarial contracts before
publishing a schema that needs another keyword.

## Operation binding

`CapabilitySpec.operation_intent()` validates the runtime profile and idempotency
contract, then binds the capability digest/profile into an immutable
`OperationIntent`. A changed schema, effect class, implementation digest, trust class,
or recovery declaration therefore cannot silently reuse an older operation identity.

Caller metadata cannot replace the authoritative capability digest or runtime profile.
Idempotent capabilities require an explicit provider idempotency key at invocation,
not merely a declaration that the implementation can accept one.

## Preferred AgentHarness integration

Pass immutable specs once. `AgentHarness` registers them, pins exact versions, converts
model-callable `tool`, `context_provider`, `mcp_tool`, `skill_command`, and validated
`child_run` kinds into provider-safe Agno functions, and routes those generated
functions back through the same executor. `child_run` is deliberately stricter: use
`DeclaredChildTemplate`, which fixes the child authority and exposes only bounded task
plus delegation identity. Relabeling an arbitrary capability fails closed.

```python
harness = AgentHarness(
    capabilities=[lookup],
    permission_mode="default",
    permission_require_approver=True,
    permission_approver=approver,
    runtime_store=runtime_store,
    artifact_store=artifact_store,
)

run = await harness.start(
    "Look up SKU A-100",
    context=trusted_context,
)
result = await run.wait()
```

This path guarantees, in order:

1. the active lifecycle run and its exact admitted tenant/user are reauthorized;
2. declared scopes, guardrails, permission classification, and every policy engine run
   before approval, operation intent, or factory materialization;
3. custom policy engines supply a stable `policy_version` and custom approvers an
   `approval_version`; permission configuration, policy input, constraints, authority,
   capability version, and decision reason become content-minimized operation evidence;
4. policy-constrained effective arguments pass guardrails again, so a policy binding
   cannot introduce a path or network value that skipped preflight;
5. a required approval request and `waiting_for_approval` state commit atomically; an
   approval callback or trusted host settles the exact request and grant durably;
6. the store-issued run/session claim is renewed and the grant is reauthorized at the
   gateway's last no-effect `pre_dispatch` checkpoint;
7. the operation intent/fence commits before the capability is entered;
8. after-call policy and redaction run before the result can be settled or written to
   an ArtifactStore; and
9. replay returns the already governed result without another factory or external call.

### Lossless large results

Add `max_inline_output_chars=8_192` with `artifact_store=` when a capability may return
large JSON-like content. Results within the bound remain unchanged. Larger results are
already redacted, settled, and committed when the model receives a bounded
`agnoclaw.spilled_output` envelope with its artifact ID, checksum, size, safe preview,
and instructions for the harness-owned `read_spilled_output` capability.

The pager is read-only, run-lifetime, page-bounded, and available only inside a
lifecycle run. It reauthorizes exact tenant/user ownership and permits the producing
run or the same trusted session, enabling later-turn recovery while denying
cross-session use. It reads verified artifact JSON and returns a deterministic
character page plus `next_offset`; it never redispatches the producing capability.

The name `read_spilled_output` is reserved whenever spill is enabled. This feature
applies to registered capabilities entering `start()`, including through
explicit-profile non-streaming `run/arun` convenience adapters. It does not convert raw
legacy, built-in, model, media, or child output into a durable result contract. See
[Durable artifacts](artifacts.md#model-context-spill).

Capability policy constraints intentionally have a small grammar:
`arguments` supplies authoritative argument bindings, `max_timeout_seconds` can only
tighten a timeout, and `require_idempotency_key` fails closed when absent. Unknown
constraints are errors. Result constraints require an explicit future output adapter;
they are not silently ignored.

An idempotent generated tool uses its string `idempotency_key` argument as the provider
key and also passes it to the implementation. Its input schema must declare an object.
Names that are not provider-safe receive a deterministic sanitized name plus digest;
`admin_harness_capabilities()` reports the mapping. Duplicate model-exposed versions or
collisions with ordinary tools fail construction.

Generated capabilities deliberately require lifecycle ingress. Use `start()` when the
caller needs identity/control, or non-streaming `run()/arun()` on an explicit `quick`,
`durable`, or `service` profile for the completed-result adapter. Named-legacy direct
calls still reject generated capabilities because they have no store-issued execution
lease. Existing entries supplied through `tools=` retain legacy governance and are not
misrepresented as operation-gated capabilities.

Raw caller `tools=` are normalized at construction into stable opaque
`CapabilitySpec` evidence. Toolkit functions expand individually, missing/invalid names
fail, duplicate names retain compatibility precedence and are marked `shadowed`, and
the surface is capped at 1,000 advertised tools. Because the
runtime cannot infer effects, trust, idempotency, or recovery from a callable name,
every normalized entry is explicitly `opaque_legacy`, `serialized`, `live_only`, and
`non_repeatable`, with no executable factory. Its generic object schema is inventory
evidence, not a provider or runtime validation claim.

Named-legacy `run()/arun()` continues to pass those live tools to Agno behind the
existing single-flight resource classification. `start()` and explicit-profile
convenience calls reject constructor and per-run raw tools with
`LEGACY_TOOL_DURABLE_UNSUPPORTED` before run creation or model-operation intent. Move
effectful or controllable work to an explicit `CapabilitySpec` passed through
`capabilities=`. `admin_harness_capabilities()` reports the advertised name, normalized
reference/digest, trust, and recovery class for migration tooling.

Plugins and packs use that same descriptor path; there is no second plugin-specific
execution contract:

```python
from agnoclaw.plugins import PluginManifest
from inventory_plugin import inventory_lookup

def agnoclaw_plugin() -> PluginManifest:
    return PluginManifest(name="inventory", capabilities=[inventory_lookup])
```

```toml
[provides]
capabilities = ["inventory_pack.capabilities:register"]
```

Each pack registration factory returns one `CapabilitySpec` or a list/tuple of
specs. Registry type, version, digest, schema, name-collision, scope, profile, and
recovery checks are identical regardless of whether the host, plugin, or pack supplied
the descriptor. The old plugin `tools=` and pack `[provides].tools` surfaces remain
direct-Agno compatibility APIs only.

At construction, raw pack, plugin, and Agno context-provider functions are normalized
into a separate extension compatibility inventory. Empty mutable toolkits are included
before discovery, which prevents a configured MCP server from adding an unhooked tool
later. `start()` raises `EXTENSION_TOOL_LIFECYCLE_UNSUPPORTED` before `create_run`; its
details contain the bounded advertised names and sources. Direct `run()/arun()` behavior
is unchanged. Adapt these surfaces to `CapabilitySpec`; do not infer read-only behavior
from a name or an MCP annotation.

For Agno context sources, `context_provider_capability()` implements the common durable
read-query adapter. It requires a provider factory, version, implementation digest, and
explicit `EffectClass.READ_ONLY` attestation; owns setup/query/close; forwards the
active Agno `RunContext`; validates and bounds `Answer`; and labels output as untrusted
data. It deliberately omits writes and `mode=tools`. See
[Agno context providers](context-providers.md).

`capability_catalog(context=...)` returns only scope-visible descriptors without
materializing them. The configured registry is not exposed mutably after construction.

## Durable approval-before-effect

Registered capabilities use the approval ledger introduced in schema v7 and retained
in current schema v12. A request binds
one run and call to the exact capability ID and manifest digest, effect category,
effective-argument digest, policy version, authority digest, tenant, principal,
session, expiry, and nonce. The ledger stores no raw arguments. A decision must match
the request digest and nonce; an approval produces a least-privilege grant with the
same bindings.

When `permission_approver` is configured, its synchronous or asynchronous result is
written as a normal durable decision before the run continues. A service may instead
leave the request pending and settle it through the trusted host API:

```python
import asyncio

from agnoclaw.runtime import ApprovalState

run = await harness.start("Update inventory", context=trusted_context)

while True:
    pending = harness.capability_approvals(
        str(run.run_id),
        context=trusted_context,
        states=(ApprovalState.PENDING,),
    )
    if pending:
        break
    await asyncio.sleep(0.1)

record = pending[0]
harness.decide_capability_approval(
    record.request.request_id,
    run_id=str(run.run_id),
    context=trusted_context,
    approved=True,
    issuer="operator:alice",
    reason_code="CHANGE_TICKET_APPROVED",
)
result = await run.wait()
```

Never expose these administration methods as model-callable tools. If live run context
is still retained, `context=` may be omitted; an out-of-process or later administrator
must supply the original trusted identity context. Owner and authority mismatch, stale
revision, late decision, changed arguments, changed capability digest, changed policy,
expired grant, and raw lifecycle `RESPOND` attempts all fail closed.

Repeating an already settled decision with the exact approved flag, issuer, reason,
and grant scope returns the existing record and emits no second approval event or grant.
A conflicting replay raises `APPROVAL_ALREADY_SETTLED`. This makes operator/API retry
safe without weakening the one-decision contract.

Cancellation, failure, or expiry while waiting atomically tombstones the pending
request, so it cannot be approved later. Approval does not mean dispatch: the active
lease and every grant binding are checked again at the final no-effect boundary before
the factory is materialized.

This is durable authorization evidence, not a serialized Agno/Python stack. A live
worker can wait while its lease heartbeat runs, and another process can settle the
request. On the certified Agno 2.9 path, a replacement worker may continue one governed
sequence only after validating the frozen request checkpoint, settled first-provider
artifact, exact decision/grant, authority/spec, and provider ordinal. It replays that
artifact into a fresh native Agent, executes the capability once, and later requires
Agno's exact `tool-batch` checkpoint. The process-kill oracle is
[`agno_approval_restart_probe.py`](../scripts/agno_approval_restart_probe.py).

That certification requires an agnoclaw-materialized native Agent and a public-factory
model classified as run-owned, isolated, and recreatable,
non-streaming `start()`, durable runtime and Agno databases, a shared ArtifactStore,
and governed registered capabilities on Agno 2.9. Agno 2.6.4 and raw/custom/nested/
parallel/streaming/parser/output-model/direct-opaque-model paths remain conservative.
The outer factory restart/digest-drift gate and this exact factory-backed multi-step
envelope are documented in
[Operations and recovery](operations-and-recovery.md).

## Advanced explicit execution

`CapabilityExecutor` remains the advanced host seam. It composes the same registry and
`OperationGateway` instead of creating a second effect system:

```python
from agnoclaw import CapabilityExecutor, OperationGateway

executor = CapabilityExecutor(
    registry,
    OperationGateway(
        runtime_store,
        worker_id="worker-1",
        artifact_store=artifact_store,
    ),
)

execution = await executor.execute(
    "inventory.lookup",
    operation_id="run-42:tool:1",
    run_id="run-42",
    attempt_id="run-42:attempt:1",
    arguments={"sku": "A-100"},
    profile="service",
    admission=admission,
)
print(execution.value)
```

The authoritative run must already exist in the gateway's `RuntimeStore`. Durable and
service profiles require an `AdmissionEnvelope`; the executor reauthorizes the exact
tenant/user run owner before resolving the capability or preparing an intent. Required
scopes come only from that admission. Quick/legacy hosts may pass `granted_scopes=`
without an envelope, but cannot combine the two authority sources.

Arguments are deep-frozen, bounded, and bound into the request digest. Their content is
not written into the operation ledger. `safe_metadata=` is explicitly persisted and has
a separate small byte limit, so secrets and model/user content do not belong there.
Capability, authority, session, profile, argument-size, and implementation evidence are
content-minimized and immutable. The authority digest intentionally excludes request ID,
trace ID, and client metadata so an otherwise identical retry does not conflict because
observability context changed. Reusing an operation ID with changed arguments,
identity, capability semantics, profile, or idempotency key fails rather than replaying
the wrong result.

### Materialization and concurrency

The factory runs only inside gateway dispatch, after durable intent exists:

| Declared lifetime | Executor behavior |
|---|---|
| `run` | One materialization per actual dispatch; closed after that call. |
| `session` | Reused only under an opaque session digest; `release_session()` closes it when idle. |
| `process_pool` | Reused by this executor until `aclose()`. |

`serialized` resources use a cancellation-safe cross-thread lock. Session/process
resources cannot be released while active. A cancelled wait cannot orphan the lock. A
synchronous Python callable runs in a worker thread; because Python cannot kill that
thread safely, cancellation observes it through completion before releasing or closing
the resource, then propagates cancellation. Use async-native or process-isolated
implementations where hard cancellation is a requirement.

Completed artifact-backed operations can be loaded after executor/process restart
without materializing or redispatching the capability. `recover_interrupted()` requires
fresh admission, reauthorizes ownership, rejects non-capability operations, and only
re-fences effects the operation domain classifies as safe; it never dispatches them.

### Current boundary

The AgentHarness path now automatically governs specs supplied through `capabilities=`,
`PluginManifest.capabilities`, or pack `[provides].capabilities`. Caller and extension
raw tools are truthfully normalized and rejected on the lifecycle path; they are not
silently upgraded. Currently constructed first-party tools have an internal declared
adapter. Context-provider objects, raw pack/plugin tools, and mutable legacy MCP
toolkits still need explicit capability adapters before they can use this path.
Elevated commands and scheduled jobs retain specialized ingress. Declared child runs
now require a capability-only child registry whose configured IDs are an exact subset
of the persisted grant; raw `spawn_subagent` remains named-legacy-only compatibility ingress.
See [Declared child runs](child-runs.md). Durable capability approval
requests/decisions/grants are implemented, while
standalone policy-decision rows, checkpoint coverage outside the certified Agno 2.9
governed sequence, process isolation, and data-classification enforcement remain
T6/T9b/T10b gates. Explicit-profile `run/arun` lifecycle routing is implemented; raw
legacy, nested, and specialized ingress remains separately classified.

Direct `CapabilityExecutor` callers own policy, approval, and active-lease enforcement.
The fully composed guarantee above belongs to the AgentHarness path.

## Error reference

| Code | Meaning |
|---|---|
| `CAPABILITY_VERSION_CONFLICT` | The same name/version was registered with different semantics. |
| `CAPABILITY_NOT_FOUND` | No exact/default registered version exists. |
| `CAPABILITY_SCOPE_REQUIRED` | Selection lacks one or more declared scopes. |
| `CAPABILITY_SELECTION_BUDGET_EXCEEDED` | Too many unique capabilities were selected. |
| `CAPABILITY_SCHEMA_BUDGET_EXCEEDED` | Canonical schemas exceed the configured byte budget. |
| `CAPABILITY_NOT_DURABLE` | Opaque/live-only capability was admitted to durable/service. |
| `CAPABILITY_EFFECT_UNRECOVERABLE` | Non-repeatable durable effect lacks reconciliation. |
| `CAPABILITY_FACTORY_MISSING` | Selection has no executable materializer. |
| `CAPABILITY_ADMISSION_REQUIRED` | Durable/service execution lacks frozen admission. |
| `CAPABILITY_SCOPE_SOURCE_CONFLICT` | Admission scopes and caller scopes were both supplied. |
| `CAPABILITY_SESSION_CONFLICT` | Explicit session identity conflicts with admission. |
| `CAPABILITY_SESSION_REQUIRED` | A session-lifetime resource lacks a session identity. |
| `CAPABILITY_ARGUMENT_BUDGET_EXCEEDED` | Canonical arguments exceed the executor limit. |
| `CAPABILITY_ARGUMENT_NOT_JSON` | Arguments contain a cycle, opaque value, non-string key, or non-finite number. |
| `CAPABILITY_ARGUMENT_SCHEMA_INVALID` | Effective arguments do not satisfy the registered schema. |
| `CAPABILITY_METADATA_BUDGET_EXCEEDED` | Persisted safe metadata exceeds its limit. |
| `CAPABILITY_METADATA_RESERVED` | Caller metadata attempted to replace authoritative evidence. |
| `CAPABILITY_IDEMPOTENCY_KEY_REQUIRED` | An idempotent effect lacks its provider key. |
| `CAPABILITY_INVOKER_REQUIRED` | A non-callable materialization has no host invoker. |
| `CAPABILITY_SESSION_BUSY` | Session resources are still active and cannot be released. |
| `CAPABILITY_EXECUTOR_BUSY` | Process resources are still active and cannot be closed. |
| `CAPABILITY_OPERATION_REQUIRED` | Recovery was requested for a non-capability operation. |
| `CAPABILITY_ACTIVE_RUN_REQUIRED` | No currently executing lifecycle run owns this call. |
| `CAPABILITY_ACTIVE_LEASE_REQUIRED` | The current task lacks its run/session lease claim. |
| `CAPABILITY_ACTIVE_AUTHORITY_CONFLICT` | Supplied authority differs from the leased run. |
| `CAPABILITY_GOVERNANCE_CONTEXT_REQUIRED` | A generated Agno function was called outside its governed hook context. |
| `CAPABILITY_POLICY_VERSION_REQUIRED` | A custom durable policy has no stable version. |
| `CAPABILITY_APPROVAL_VERSION_REQUIRED` | A custom durable permission approver has no stable version. |
| `APPROVAL_CONTEXT_REQUIRED` | Approval administration lacks the original trusted run context. |
| `APPROVAL_IDENTITY_REQUIRED` | Durable approval lacks complete tenant/principal/session authority. |
| `APPROVAL_AUTHORITY_MISMATCH` | Administrator context differs from the requesting authority. |
| `APPROVAL_DECISION_REQUIRED` | A raw lifecycle response tried to bypass the settled approval ledger. |
| `APPROVAL_RUN_NOT_WAITING` | The run left this exact approval wait before settlement or continuation. |
| `APPROVAL_ALREADY_SETTLED` | A retry conflicts with the settled decision; an exact replay returns it without new evidence. |
| `AUTHORIZATION_GRANT_REQUIRED` | Dispatch lacks an approved exact grant. |
| `AUTHORIZATION_GRANT_INVALID` | Capability, arguments, effect, policy, authority, or scope changed before dispatch. |
| `AUTHORIZATION_GRANT_EXPIRED` | The exact grant expired before dispatch. |
| `RUNTIME_STORE_APPROVALS_REQUIRED` | Durable approval was enabled with a store lacking schema-v7 methods. |
| `LEGACY_TOOL_INVALID` | A raw tools entry is neither callable nor an Agno tool. |
| `LEGACY_TOOL_NAME_REQUIRED` | A raw entry has no advertised tool name. |
| `LEGACY_TOOL_NAME_INVALID` | A raw name is empty or exceeds its bounded length. |
| `LEGACY_TOOL_BUDGET_EXCEEDED` | Raw Toolkit expansion exceeds 1,000 functions. |
| `LEGACY_TOOL_DURABLE_UNSUPPORTED` | `start()` received opaque constructor/per-run tools; use explicit `capabilities=`. |
| `EXTENSION_TOOL_LIFECYCLE_UNSUPPORTED` | `start()` found raw plugin/pack/context-provider or mutable dynamic-toolkit ingress; publish explicit capabilities. |
| `CAPABILITY_POLICY_CONSTRAINT_UNSUPPORTED` | Policy returned a constraint the executor cannot enforce. |
| `CAPABILITY_POLICY_CONSTRAINT_INVALID` | A supported constraint has an invalid value. |
| `CAPABILITY_POLICY_IDEMPOTENCY_REQUIRED` | Policy requires a missing provider key. |
| `CAPABILITY_RESULT_CONSTRAINT_UNSUPPORTED` | Output constraints need an explicit adapter. |
| `CAPABILITY_MODEL_VERSION_AMBIGUOUS` | Multiple versions of one capability were exposed to the model. |
| `CAPABILITY_TOOL_NAME_CONFLICT` | Generated and existing Agno tool names collide. |
| `CAPABILITY_INPUT_SCHEMA_INVALID` | A model-callable capability does not use an object schema. |
| `CAPABILITY_INPUT_SCHEMA_UNSUPPORTED` | The schema uses an assertion keyword or reference the core cannot enforce. |
| `CAPABILITY_INPUT_SCHEMA_BUDGET_EXCEEDED` | The schema exceeds byte, depth, or node budgets. |
| `CHILD_CAPABILITY_DECLARATION_INVALID` | A model-visible child spec is not the bounded host-managed/idempotent/reconcilable contract. |
| `OUTPUT_SPILL_ARTIFACT_STORE_REQUIRED` | Capability spill was enabled without an ArtifactStore. |
| `OUTPUT_SPILL_LIMIT_INVALID` | The direct constructor bound is not an integer from 1,024 through 1,000,000. |
| `OUTPUT_SPILL_TOOL_CONFLICT` | A registered capability collides with the reserved reader. |
| `OUTPUT_SPILL_REFERENCE_REQUIRED` | A result lacks the committed artifact required for safe externalization. |
| `OUTPUT_SPILL_SCOPE_MISMATCH` | A page targets output outside the active run/trusted session authority. |
| `BUILTIN_EFFECT_UNCLASSIFIED` | A newly constructed first-party function has no explicit replay declaration. |
| `BUILTIN_ACTIVE_LEASE_REQUIRED` | First-party lifecycle dispatch lacks the exact active run lease. |
| `BUILTIN_POLICY_DRIFT` | Policy or permission authority changed after the pre-tool checkpoint. |
| `BUILTIN_POLICY_CONSTRAINT_UNSUPPORTED` | A pre-tool argument constraint lacks an explicit first-party adapter. |
| `BUILTIN_RESULT_CONSTRAINT_UNSUPPORTED` | An after-tool result constraint lacks an explicit first-party adapter. |
| `BUILTIN_AGNO_CACHE_UNSUPPORTED` | A first-party lifecycle function enabled Agno's local cache instead of ledger replay. |
| `BUILTIN_ENTRYPOINT_UNSUPPORTED` | A declared built-in has no non-streaming callable entrypoint. |
| `BUILTIN_GOVERNANCE_CONTEXT_REQUIRED` | The active built-in call lacks trusted hook context. |

## Verification evidence

The focused registry/domain/executor gate covers immutable version conflicts, explicit
defaults, scope-hidden discovery, lazy factories, stable selection digests,
count/schema/prompt/argument/metadata budgets, 1,000-entry catalogs, concurrent
registration/readers, exact admission/owner/scope checks, content-minimized persistence,
operation identity conflicts, durable result replay without redispatch, every lifetime,
serialized overlap, cancellation while a sync call is active, busy-resource closure,
opaque rejection, idempotency keys, and recovery authorization. AgentHarness integration
adds provider-safe version pinning, bounded fail-closed runtime schema enforcement,
atomic approval wait/request persistence, callback and external-host settlement, exact
grant binding and final-boundary reauthorization, policy-drift rejection, cancellation
tombstones, policy constraints/evidence, pre-dispatch lease loss, post-policy redaction
before artifact commit, generated-tool replay, collision/schema failure, and direct-run
denial. Large-result integration additionally proves unchanged small values, bounded
lossless envelopes, exact reconstruction, clamped pages, one spill event, missing-store
and reserved-name failures, same-session continuation, and cross-session denial.
First-party ingress tests additionally prove fail-closed declaration coverage, exact
read-only classification, intent/lease/policy evidence, duplicate-call replay without
redispatch, pre-persistence redaction, and lossless spill of the committed value. The
extracted approval, governance, adapter, executor, gateway, and permission
modules pass isolated mypy and blocking Ruff. Legacy normalization additionally proves
stable opaque manifests, Toolkit expansion, bounds/conflicts, admin inventory, and
rejection before durable run creation; explicit compatibility execution remains on the
serialized legacy runner.
