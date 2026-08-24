# Declared child runs

Status: 0.12 development preview; lineage, authority, joins, propagation, active-worker
wall deadlines, settlement-time Agno usage assessment, declaration-bound structured
results, lossless typed result/artifact handoff, governed synthesis, host-declared
model tools, and authenticated remote ingress are implemented and tested; all current
PostgreSQL runtime/learning parity cases pass against a disposable PostgreSQL 17 service

Last verified: 2026-08-11

Declared child runs are the durable delegation primitive. They reuse the ordinary run
lifecycle instead of introducing a second orchestration engine. The existing
`spawn_subagent` tool is available only on the named `legacy` profile: it returns
bounded text and is never silently represented as a durable child. Explicit profiles
omit it from default tools and reject named raw subagents before construction.

## When to delegate

Prefer one agent with good capabilities. Create a child only when at least one of these
is materially useful:

- the task needs an isolated context window;
- specialized model, instructions, or capabilities reduce error or cost;
- independently observable/cancellable work is required;
- parallel work has a measured latency benefit.

Delegation adds lifecycle, synthesis, and failure-handling cost. It is not a default
reasoning pattern.

## Public contract

`HarnessRun.child()` admits one child through a second `AgentHarness` sharing the same
`RuntimeStore`. The parent must be authoritatively `running`, and the trusted context
must have the parent's exact tenant and user.

```python
from agnoclaw import AgentHarness, ChildRunBudget, ChildJoinPolicy

# research_capability is a classified CapabilitySpec from the host registry.
child_harness = AgentHarness(
    model="openai:gpt-5-mini",
    runtime_store=runtime_store,
    artifact_store=artifact_store,
    include_default_tools=False,
    capabilities=(research_capability,),
)

child = await parent.child(
    child_harness,
    "Collect primary-source evidence for the disputed claim.",
    context=trusted_parent_context,
    delegation_id="primary-source-check-v1",
    purpose_code="research",
    budget=ChildRunBudget(
        max_depth=2,
        max_fanout=4,
        timeout_seconds=300,
        max_tokens=40_000,
        max_cost_microusd=2_000_000,
    ),
    capability_allowlist=("research.lookup@1.0.0",),
    join_policy=ChildJoinPolicy.ALL_SUCCESS,
    learning_allowed=False,
    result_schema={
        "type": "object",
        "properties": {
            "finding": {"type": "string", "minLength": 1},
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
            },
        },
        "required": ["finding", "sources"],
        "additionalProperties": False,
    },
    parent_step_id="step-12",
    parent_tool_call_id="call-4",
    persist_output=False,
)

result = await child.wait()
direct_children = await parent.children()
results = await parent.child_results(require_terminal=True)
results.require_all_succeeded()
synthesis_input = results.synthesis_payload(max_inline_result_chars=8_000)

synthesis = await parent.synthesize_children(
    synthesis_harness,
    "Reconcile the findings and identify any disagreement.",
    context=trusted_parent_context,
    delegation_id="research-synthesis-v1",
    result_schema={
        "type": "object",
        "properties": {"summary": {"type": "string", "minLength": 1}},
        "required": ["summary"],
        "additionalProperties": False,
    },
)
combined = await synthesis.wait()
```

Use one stable `delegation_id` for one logical delegation. The child ID is derived from
the parent run and delegation ID. A repeated call with the same complete request
reattaches to the original child; changed message, context, budget, capabilities, or
other bound input fails with `RUN_START_IDEMPOTENCY_CONFLICT`.

The child harness is caller-owned. Close it according to the same `drain`, `detach`, or
`cancel` ownership rules as any other harness.

## Model-visible declared delegation

`DeclaredChildTemplate` is the small, shared declaration for model and remote ingress.
The host fixes the child harness, purpose, finite budget, capability allowlist, join
policy, learning consent, result schema, persistence choice, scopes, and inline-result
bound. The model receives only `task` and a stable `delegation_id`:

```python
from agnoclaw import AgentHarness, ChildRunBudget, DeclaredChildTemplate

research_child = AgentHarness(
    model="openai:gpt-5-mini",
    profile="service",
    runtime_store=runtime_store,
    artifact_store=artifact_store,
    include_default_tools=False,
    capabilities=(research_lookup,),
)

delegate_research = DeclaredChildTemplate(
    name="delegate_research",
    child_harness=research_child,
    purpose_code="research",
    budget=ChildRunBudget(
        max_depth=2,
        max_fanout=4,
        timeout_seconds=300,
        max_tokens=40_000,
        max_cost_microusd=2_000_000,
    ),
    capability_allowlist=("research.lookup@1.0.0",),
    learning_allowed=False,
    result_schema={
        "type": "object",
        "properties": {
            "finding": {"type": "string", "minLength": 1, "maxLength": 20_000},
            "sources": {
                "type": "array",
                "items": {"type": "string", "maxLength": 2_000},
                "maxItems": 20,
            },
        },
        "required": ["finding", "sources"],
        "additionalProperties": False,
    },
    required_scopes=("research:delegate",),
    max_inline_result_chars=8_000,
)

parent_harness = AgentHarness(
    model="anthropic:claude-sonnet-4-6",
    profile="service",
    runtime_store=runtime_store,
    artifact_store=artifact_store,
    include_default_tools=False,
    capabilities=(delegate_research,),
    max_inline_output_chars=12_000,
)
```

The constructor converts the template into an immutable `CapabilitySpec` of kind
`child_run`. The generated Agno tool crosses the ordinary capability permission,
policy, scope, lease, operation-journal, result-policy, and output-spill gates before
calling `HarnessRun.child()`. The final, policy-constrained `delegation_id` is also the
idempotency key. A tool call records the exact parent step/tool-call lineage and waits
for that child to settle before returning one typed outcome. Results beyond the
template's inline bound become complete artifact pointers; they are never cut into an
apparently complete answer.

A model-visible child capability is admitted only when it is host-managed,
idempotent, run-scoped, isolated, reconcilable, and has the bounded
`task`/`delegation_id` schema. Merely assigning `kind="child_run"` to an arbitrary tool
fails construction with `CHILD_CAPABILITY_DECLARATION_INVALID`. Use a distinct child
harness instance; binding a parent to itself fails with `CHILD_HARNESS_REENTRANT`
instead of risking a single-flight lane deadlock.

The model tool deliberately waits because the parent model needs a result in the same
turn. For unattended work that should outlive a model/tool waiter, use the host
`HarnessRun.child()` call or authenticated remote route below; both return the child
handle immediately.

## Authenticated remote delegation

Register the same templates explicitly at the server trust boundary. A remote caller
selects only a registered template and supplies task/delegation identity; it cannot
send model, budget, capability, learning, schema, or persistence options.

```python
from agnoclaw import RemoteHarnessClient
from agnoclaw.runtime import create_agentos_app

app = create_agentos_app(
    [parent_harness],
    child_templates={
        "parent-agent-id": {delegate_research.name: delegate_research},
    },
)

async with RemoteHarnessClient(base_url, api_key=token) as client:
    parent = await client.get_run(parent_run_id, agent_id="parent-agent-id")
    child = await parent.child(
        "delegate_research",
        "Collect primary-source evidence for the disputed claim.",
        delegation_id="primary-source-check-v1",
    )
    children = await parent.children()
    results = await parent.child_results(max_inline_result_chars=8_000)
    result = await child.wait()
```

`POST .../{run_id}/children` needs `agents:run` plus every template-specific scope.
Listing children and typed results needs `agents:read`. Owner claims are reauthorized
against the parent before template lookup/dispatch. An unknown or unregistered
template is hidden as `CHILD_TEMPLATE_NOT_FOUND`; the server never accepts a serialized
template from the request. The returned `RemoteHarnessRun` is an ordinary child handle,
so status, result, events, output, cancellation, and commands use the same authenticated
lifecycle routes. `child_results()` validates lineage and derived state at the client
boundary and returns small results inline or lossless artifact pointers.

## Durable identity and context

Schema v11 stores a first-class relation containing:

- `child_run_id`, `parent_run_id`, `root_run_id`, and `child_depth`;
- stable `delegation_id`, purpose code, and complete child-spec digest;
- optional parent step/tool-call linkage;
- finite tree/resource grant, capability allowlist, join policy, cancellation policy,
  learning permission, and optional canonical result schema plus digest.

The child gets a distinct `child:<run_id>` session. Tenant, user, organization, team,
roles, scopes, request, trace, trusted permission channels, and metadata are inherited
without escalation. Its identity source becomes `internal_parent`. A descendant may
only reduce its ancestor's budget, capabilities, and learning permission.

Task text and model output are not copied into parent lifecycle events. Parent events
carry IDs, policy values, states, and digests. Use scoped artifacts/output segments for
content.

## Admission and capability safety

The preview declared-child path is capability-only. The child harness must expose an
immutable capability registry, and every configured `name@version` must be present in
the declaration's allowlist. Raw caller tools, default tools, and opaque compatibility
surfaces fail before child creation with `CHILD_UNDECLARED_TOOLS` or
`CHILD_CAPABILITY_GRANT_MISMATCH`.

This is intentionally narrower than `spawn_subagent`. It prevents a durable label from
hiding a live-only or unclassified tool path. Move each required tool through
`CapabilitySpec` and the operation gateway before granting it to a declared child.

## Join and terminal settlement

Every terminal child transition atomically appends `run.child.settled` to its direct
parent's event stream. A parent `complete` transition is rejected while any direct
child is non-terminal:

- `all_success` also rejects completion if any child terminated outside `completed`;
- `collect` accepts any terminal child state and leaves interpretation to parent/host
  synthesis.

The check, child terminal record, child state event, parent settlement event, and both
outbox projections share the store transaction. Repeating the child terminal
transition does not duplicate settlement evidence.

Parent failure, expiry, or cancellation is not blocked on child settlement. Those
paths first request cancellation across all live descendants and then settle according
to their own lifecycle rules. Operators can inspect each child independently.

`HarnessRun.child_results()` collects direct children in stable creation/ID order. Each
`ChildRunOutcome` carries declaration identity, current state, the authoritative safe
terminal projection, bounded content-minimized artifact links, and the exact outer-model
result artifact when one exists. `pending`, `successful`, and `failed` partitions plus
`require_all_terminal()` and `require_all_succeeded()` make partial-failure handling
explicit; collection never waits, cancels, or invents a result.

`ChildResultSet.synthesis_payload()` is a deterministic host-side preparation step, not
another model call. Small finite results remain inline. A result over the configured
inline bound becomes a typed pointer to its complete `operation_result` artifact; if no
lossless artifact exists, synthesis fails with `CHILD_RESULT_ARTIFACT_REQUIRED` instead
of silently truncating. The parent can page a referenced direct-child artifact through
`read_child_artifact()`; non-direct or cross-owner references fail scope/owner checks.
Hosts remain responsible for choosing the synthesis model/prompt and deciding whether
to require the declaration-bound domain schema described below.

## Declaration-bound structured results

`result_schema=` makes the child result shape part of the immutable delegation rather
than a prompt convention. It accepts the same bounded, fail-closed object-schema
subset as registered capability arguments: unsupported assertion keywords, invalid
references, live cycles, or schemas beyond the byte/depth/node budgets fail admission
with `CHILD_OUTPUT_SCHEMA_INVALID`. The canonical schema and its digest are included in
child-spec schema `1.1`; the current reader still decodes stored `1.0` declarations
without a schema.

Use Agno's native `output_schema`/structured-output support on the child harness when a
provider can generate typed output. The two controls are complementary: Agno guides
and parses the provider response, while `result_schema=` is the harness-owned durable
settlement boundary. Agnoclaw validates the normalized `content` only after the model
operation has a successful settlement and after reported usage assessment, but before
the child can become `completed`. The result graph is bounded before schema traversal.

Validation appends one idempotent, content-free `run.child.output.validated` event with
the child-spec/schema digests, a boolean decision, and only the failed keyword/schema
path. A mismatch fails the child with `CHILD_OUTPUT_SCHEMA_MISMATCH`; it does not
rewrite the already-successful external model operation. If an operation-result
artifact exists, it remains available for authorized diagnosis. Known-success restart
completion re-applies both the budget and output contracts before settling the child,
so recovery cannot bypass them.

## Governed synthesis execution

`HarnessRun.synthesize_children()` is the explicit model-execution companion to
`ChildResultSet.synthesis_payload()`. It:

- snapshots bounded direct-child outcomes and requires every source to be terminal;
- rejects any failed source unless the host explicitly sets
  `allow_partial_failures=True`;
- keeps finite results inline and uses complete result-artifact pointers instead of
  truncation;
- enforces both the per-result inline bound and a 1,024–1,000,000-character total JSON
  evidence bound;
- frames the host instruction as trusted and every child outcome as untrusted JSON
  evidence, so instructions inside a child result have no authority;
- launches synthesis as an ordinary capability-only declared child with purpose
  `synthesis`, a finite budget, allowlisted capabilities, learning consent, operation
  settlement, output-schema validation, and all-success join behavior.

The `delegation_id` remains the idempotency key. Repeating the exact synthesis request
reattaches even while that same synthesis child is pending; changed evidence,
instruction, grant, or schema under the same ID conflicts rather than redispatching.
The target synthesis child is excluded from its own source snapshot.

Artifact pointers are references, not ambient read authority. `read_child_artifact()`
is a host SDK method, not automatically a model tool. If synthesis must dereference a
large source, the host must expose and grant a separately governed, owner-checking
reader capability (or page and pre-process the evidence itself). Agnoclaw does not
silently broaden the synthesis child's capability grant.

## Cancellation propagation

The only supported cancellation policy is `propagate`. A parent `request_cancel`,
`fail`, `fail_with_unknown_effects`, or `expire` transition atomically moves every
eligible descendant to `cancelling` with reason
`PARENT_CANCELLATION_PROPAGATED`. Pending child approvals are cancelled in the same
transaction.

An active worker observes this authoritative state after its next lease renewal and
cancels its local task. Cancellation of an in-flight non-repeatable operation can still
settle at `waiting_for_reconciliation`; parent cancellation never rewrites an unknown
external effect as success or clean cancellation.

Propagation is cooperative and lease-heartbeat bounded. It is not an operating-system
kill guarantee.

## Budget semantics

All fields are finite and validated at admission. `max_depth` and `max_fanout` are
enforced by the authoritative store. Descendant budget values cannot exceed the parent
grant.

`timeout_seconds` is enforced by an active child worker after it acquires its execution
lease. At expiry, a cancellation request with reason `CHILD_TIMEOUT_EXCEEDED` commits
before the worker is cancelled. If the outer non-repeatable model request may already
have reached the provider, the operation becomes `unknown` and the child stops at
`waiting_for_reconciliation`; timeout never invents a clean cancellation. Store or
revision failure in the deadline supervisor cancels the worker and fails closed as
`RUNTIME_SUPERVISOR_FAILED` unless an ambiguous effect requires reconciliation.

The successful outer-model settlement extracts only stable Agno `RunMetrics` fields,
an optional bounded provider request ID, and USD cost rounded upward to integer
microdollars. Before a child can complete, the worker writes one idempotent
`run.child.budget.observed` event and compares reported total tokens and cost with the
grant. A reported excess fails the child with `CHILD_RESOURCE_BUDGET_EXCEEDED`, so an
`all_success` parent cannot complete. Missing provider usage or cost is recorded as an
`unverified_dimensions` entry; it is never treated as zero or fabricated from text.
An evidence-extractor failure is content-minimized and cannot turn an otherwise
successful provider effect into an ambiguous operation.

These are truthful operational bounds, not prepaid hard ceilings. The wall deadline
currently starts at worker/lease execution, not admission, and is not yet persisted as
an absolute database-clock deadline across restart. Token/cost checks happen after the
provider response and cannot undo spend already incurred. Provider adapters that do
not report Agno metrics remain unverified. Services needing admission-to-terminal
deadlines, prepaid cost reservation, or hard token limits must enforce those controls
at their provider boundary and retain the resulting evidence.

## Learning

Learning defaults off for children. `learning_allowed=True` passes explicit learning
consent into the child lifecycle, but it does not widen Agno learning scope or bypass
agnoclaw's learning prerequisites. The child harness still needs a valid
`LearningPolicy`, correctly scoped Agno storage, and vector-backed `Knowledge` for
institutional learning. A descendant cannot enable learning if its parent declaration
denied it.

Child outcomes should normally enter the governed candidate pipeline rather than write
directly to shared institutional knowledge. See [Learning and self-improvement](learning.md)
and [Governed learning candidates](learning-candidates.md).

## Events and operations

The durable relation adds these lifecycle signals:

| Stream | Event | Meaning |
|---|---|---|
| child | `run.created` | child identity, lineage, and declaration digest committed |
| parent | `run.child.created` | direct child relation committed |
| child | `run.state.changed` | ordinary lifecycle transitions, including propagated cancellation |
| child | `operation.settled` | content-minimized provider request, usage, and cost evidence when reported |
| child | `run.child.budget.observed` | exact declared limits, reported measurements, excesses, and unverified dimensions |
| child | `run.child.output.validated` | content-free result-schema decision bound to child/spec digests |
| parent | `run.child.settled` | direct child reached a terminal state |

The child then uses the same request checkpoint, operation intent, lease/fence,
approval, artifact, output, recovery, and terminal contracts as a root run. Event
payloads are content-minimized; authoritative child results remain on the child
terminal/artifact surface.

## Failure handling

Common fail-closed codes include:

| Code | Required action |
|---|---|
| `CHILD_PARENT_NOT_RUNNING` | wait for the parent safe point or do not delegate |
| `CHILD_PARENT_TERMINAL` | start a new root/follow-up run; terminal runs do not reopen |
| `CHILD_OWNER_ESCALATION` | repair trusted owner propagation |
| `CHILD_DEPTH_LIMIT` / `CHILD_FANOUT_LIMIT` | reduce the tree or grant a reviewed bound at its authority source |
| `CHILD_BUDGET_ESCALATION` | make the descendant grant a parent-budget subset |
| `CHILD_CAPABILITY_ESCALATION` | grant only ancestor-approved capabilities |
| `CHILD_LEARNING_ESCALATION` | keep learning off or enable it at the reviewed ancestor |
| `CHILD_JOIN_PENDING` | observe/wait/cancel remaining children before completing |
| `CHILD_JOIN_FAILED` | handle failed all-success children or use a deliberate collect policy |
| `CHILD_RESOURCE_BUDGET_EXCEEDED` | inspect reported token/cost evidence; narrow work or issue a reviewed new grant |
| `CHILD_TIMEOUT_ENFORCEMENT_CONFLICT` | investigate store contention/health; the supervisor cancelled the worker and failed closed |
| `CHILD_RESULTS_PENDING` | observe or wait for direct children; collection itself never blocks or cancels |
| `CHILD_RESULTS_FAILED` | apply the caller's explicit partial-failure policy before synthesis |
| `CHILD_RESULT_ARTIFACT_REQUIRED` | configure an artifact store or keep the result within the reviewed inline synthesis bound |
| `CHILD_OUTPUT_SCHEMA_INVALID` | replace the declaration with a bounded supported object schema |
| `CHILD_OUTPUT_SCHEMA_MISMATCH` | inspect safe schema-path evidence and the authorized result artifact; issue new work under a new delegation ID if needed |
| `CHILD_SYNTHESIS_PAYLOAD_TOO_LARGE` | narrow the bounded source set or explicitly raise the reviewed total evidence limit |
| `CHILD_CAPABILITY_DECLARATION_INVALID` | construct model-visible delegation with `DeclaredChildTemplate` instead of relabeling a tool |
| `CHILD_HARNESS_REENTRANT` | use a distinct child harness instance so parent/child execution lanes cannot self-deadlock |
| `CHILD_TEMPLATE_NOT_FOUND` | register the template for that exact AgentOS harness ID; never accept client-supplied declarations |
| `CHILD_RESULT_TERMINAL_MISSING` | treat the store as inconsistent and investigate before continuing |
| `CHILD_ARTIFACT_SCOPE_MISMATCH` | use only an artifact handed off by a direct child of this parent |
| `CHILD_RECOVERY_LINEAGE_INVALID` | repair or restore the durable child relation; recovery will not dispatch an uncertified chain |
| `CHILD_RECOVERY_ANCESTOR_TERMINAL` | the child was reaped without dispatch because an ancestor was already terminal |

Do not retry a changed declaration under the same delegation ID. Choose a new logical
delegation ID only when the host intentionally creates new work.

## Verification and remaining gates

The local contract suite covers lineage reopen, exact ownership, idempotency conflicts,
depth/fan-out and descendant non-escalation, capability-only admission, both join
policies, terminal settlement, recursive cancellation, lease-heartbeat observation,
real Agno 2.6/2.8 metrics extraction, upward cost rounding, missing-evidence honesty,
extractor failure isolation, idempotent budget events, reported overage failure,
all-success join blocking, active-worker timeout/reconciliation, deadline revision
conflict exhaustion, deterministic typed collection, pending/failure partitions,
lossless large-result pointers, direct-child artifact paging, hidden-truncation refusal,
schema `1.0` decode compatibility, invalid/bounded schema admission, valid and invalid
end-to-end result settlement, content-free event ordering, recovery-time revalidation,
typed/idempotent synthesis, pending-synthesis reattachment, injection framing,
explicit partial-failure synthesis, model-visible dispatch through capability
governance/idempotency/lineage/output projection, authenticated remote start/list/result
round trips, template hiding and template-specific scopes, plus injected rollback after
relation, propagation, and settlement writes. Planned-child recovery additionally
proves exact declaration/timeout/output restoration in both pre-model states, while a
depth-16 tree proves full-chain certification and terminal-ancestor reaping without a
model call. PostgreSQL has matching schema/transaction code and parity cases.

The following still block a stable/world-class delegation claim:

- PostgreSQL primary failover, partition, broader concurrency/deadlock stress,
  backup/restore, and multi-worker soak (the current 36-case real-service command plus a
  single-node whole-database restart probe pass);
- persisted database-clock admission-to-terminal deadlines, provider-preflight token/
  cost reservation, certified provider-specific usage/receipt adapters, and restart/
  failover proof for the active-worker supervisor;
- a first-party governed cross-child artifact-reader capability for synthesis that
  needs to dereference large handoffs (host-provided readers are supported today);
- service-wide owner enumeration and distributed orphan-sweep scheduling;
- post-dispatch steering/checkpoint continuation and provider-level cancellation proof.

Until those gates pass, describe the feature as **declared child-run preview**, not
general durable subagent parity.
