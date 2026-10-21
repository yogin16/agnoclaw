# Durability for domain packages

Research and implementation notes, 2026-10-20. These contracts matter for a domain
harness even when it is embedded in another service: a company-metric extraction can
finish before a write, approval can arrive after a deploy, and an event can be delivered
again after the consumer committed its change.

## Lessons from current frameworks

The latest published Agno release reviewed is
[v3.1.6](https://github.com/agno-agi/agno/releases/tag/v3.1.6).
agnoclaw's supported dependency remains `agno>=2.6.4,<3.1`; researching new behavior
does not certify a dependency upgrade.

Agno separates background execution from durable queue admission. Its
[queue contract](https://docs.agno.com/agent-os/background-execution/durable-queue)
persists eligible submissions before acknowledgment, fences attempts, and defaults
to one attempt. Increasing attempts repeats the run, including tools; submission
deduplication is not effect deduplication. It also leaves ordering between runs in
one session to the application. Non-queueable submissions can fall back to in-process
execution, so a background response alone is insufficient evidence of durability.
[Background execution](https://docs.agno.com/agent-os/background-execution/overview)
describes those eligibility limits.

Agno's [human continuations](https://docs.agno.com/agent-os/background-execution/hitl-continuations)
retain the same run/job identity and route continuation through its owner. Competing
inline continuation paths are refused; paused jobs survive normal retention.

Flue's [durability contract](https://flueframework.com/docs/guide/durability/)
distinguishes admitted submissions, attempts and terminal outcomes. Completed durable
steps replay recorded values; unfinished ordinary tools have explicitly uncertain
outcomes. A step can execute again if its effect happened before its result was
recorded. External effects therefore still need idempotency. Conversation persistence,
workspace persistence and a caller's waiting promise are separate concerns. Flue also
keeps orchestration outside the agent under the host's
[workflow engine](https://flueframework.com/docs/guide/workflows/).

Our design inference: reuse existing runtime ownership and effect machinery, and make
domain meaning part of every resumed request. A second scheduler inside a package
would introduce competing ownership without solving semantic drift.

## What the current package API guarantees

| Boundary | Current mechanism | What the embedding host supplies |
|---|---|---|
| Definition identity | Compiled manifest pins dependencies, includes, aliases and implementation provenance | Retain the corresponding installed package/build |
| Planned work | `WorkflowPlan` is immutable, JSON-exportable and pins workflow, inputs, tenant and request ID | Persist it before acknowledgment and select the original plan on retry |
| Step execution | Stable operation IDs, capability and execution digests, existing operation ledger | Stable run ownership, admission and authoritative business guards |
| Completed results | Existing artifact-backed operation replay, revalidated against output schema | Configure durable result storage and retention |
| Interrupted effects | Existing effect classes and explicit recovery/reconciliation | Provider idempotency or evidence-based reconciliation; no blanket retry |
| Human review | Existing exact-subject action reviews; interactions reject stale/expired responses | Authenticated review storage, continuation ownership and current revision checks |
| Events | Scoped consumer identity and existing outbox retry adapter | Transactional business receipt/write, outbox retention and worker lifecycle |
| Files | Immutable workspace byte hashes and explicit overlay conflicts | Persist snapshots separately and retain versions used by unfinished runs |

`InterfaceBinding.execute()` binds its contract into the operation request digest.
`Workflow.execute()` also binds the complete plan. A caller cannot regenerate a changed
workflow under an old request ID and silently reuse results merely because its tool
arguments and capability stayed the same. Calls to `CapabilityExecutor.execute()`
without the new optional `execution_digest` retain their previous request identities.
When a replacement worker supplies a new `attempt_id`, the workflow retains original
attempt attribution for existing steps and uses the new attempt for newly planned
steps. Reads are owner-scoped; in-flight fences still require explicit host recovery.

Persist and restore an ordinary plan without serializing Python functions:

```python
import json
from agnoclaw.domain import WorkflowPlan

plan = workflow.plan(inputs, tenant_id=tenant_id, request_id=request_id)
serialized = json.dumps(plan.to_dict())  # persist with the host's admitted run
restored = WorkflowPlan(**json.loads(serialized))
result = await workflow.execute(
    executor, restored, run_id=run_id, attempt_id=attempt_id,
    admission=admission, pre_dispatch=guards,
)
```

The host loads the pinned workflow, manifest and workspace before this call; an alias
such as `latest` must not select a new implementation during recovery. Persist those
manifest/workspace digests with the run and compare them explicitly. A domain package
does not atomically commit a host run, business database change and provider effect.
The current workflow is a sequential adapter, not arbitrary Python checkpointing.

## Roadmap and acceptance tests

1. **Current preview:** reject definition/intent drift and replay completed results
   through the existing executor. Tests reopen SQLite with artifact-backed results,
   restore a serialized plan, verify no redispatch and keep uncertain external writes
   blocked for reconciliation. Existing runtime recovery and
   outbox tests remain the authority for effect and lease semantics.
2. **Clarion adoption:** persist the plan, manifest/workspace pins and existing run
   identity together at admission. Exercise interruption between extraction, review,
   business commit and outbox acknowledgment. An accepted response must correspond to
   a persisted receipt; an observer notification is insufficient.
3. **Agno adapter certification:** test the supported version matrix before widening
   the dependency bound. Verify queue eligibility, paused continuation ownership,
   cancellation, stale-worker fencing and duplicate submissions. Choose one run owner;
   do not let both AgentOS and an agnoclaw host independently reclaim the same run.
4. **General workflow composition:** add branches, waits and delegated runs only with
   retained step identities, bounded attempts/deadlines, explicit uncertainty and
   upgrade behavior. Require process-kill tests before claiming crash recovery of a
   new adapter. Preserve paused approvals and their pinned definitions during retention.

For Clarion, an expired review or changed company revision should produce a new review
request. A committed metric write followed by a crash should recover its receipt and
result. A provider call with an unknown outcome should stop for reconciliation. Those
are domain-specific recovery policies over generic durable mechanics.
