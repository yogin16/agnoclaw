# ADR-0001: Recovery and external-effect ownership

Status: accepted for 0.12 implementation

Date: 2026-08-07

Decision owners: agnoclaw maintainers

Schema/API freeze remains gated by the T10a security foundation.

## Context

`agnoclaw` needs one honest recovery contract for short and long-running agents. Agno
2.x persists sessions and exposes continuation APIs. Agno 3 ships normalized run rows
and a durable job queue with retries, leases, heartbeats, stale-attempt reclaim,
and fencing. Names and database rows alone do not prove that a killed process can resume
from a settled model/tool boundary without losing work or duplicating an external
effect.

The release plan therefore required real process-kill probes before the runtime schema
or lifecycle API could freeze. The executable probe is
[`scripts/agno_recovery_spike.py`](../scripts/agno_recovery_spike.py).

## Evidence

### Stable Agno 2.x

Command:

```bash
uv run python scripts/agno_recovery_spike.py stable
```

The child process was killed after the model request entered the provider boundary but
before any response. Neither the session nor the run was persisted. More importantly,
Agno's stored session/run projection does not record provider dispatch, receipt, effect
settlement, or a certified checkpoint. `continue_run` is a pause/fork/regenerate
surface, not a process-death operation-recovery protocol.

Result: **no certified stable-2.x resume boundary**.

### Agno 3.0.0a1 queue probe (retained evidence)

The preview probe used a real ephemeral PostgreSQL 17 database and Agno's actual
`PostgresDb` queue. It killed a worker immediately before and immediately after a
durable external effect, waited for lease expiry, and let a second worker reclaim the
job.

Agno correctly:

- reclaimed both abandoned jobs as attempt 2;
- rejected the stale attempt's terminal write through fencing; and
- accepted the current attempt's terminal write.

The before-effect case produced one external effect. The after-effect case produced
two: attempt 1 completed the external action and died before settlement, so attempt 2
repeated the action. Queue fencing protects queue state; it cannot make an arbitrary
external system exactly-once.

Result: **queue reclaim and attempt fencing are promising; exactly-once external
effects are not certified**.

The preview queue also has important deployment qualifications: durability requires a
conforming PostgreSQL or Redis store; unfenced projections weaken guarantees; a
blocking synchronous activity can starve an event-loop heartbeat; and effect
reconciliation remains outside the queue contract.

### agnoclaw-owned implementation proof

The decision is now exercised end-to-end by
[`scripts/agno_stack_restart_probe.py`](../scripts/agno_stack_restart_probe.py). It
starts the public `AgentHarness.start()` lifecycle with an actual Agno `Agent`, kills
real processes at planned, in-provider, settled-before-run-complete, and changed-
factory-digest boundaries, then reopens the Agno database, runtime ledger, and artifact
store with a new harness and discovers each run through bounded owner-scoped startup
recovery. The retained four-scenario gate proves safe pre-dispatch
continuation, fail-closed in-flight reconciliation, completion from a committed result
with zero duplicate provider calls, and pre-dispatch refusal of implementation drift.
This is the single-operation implementation proof for the table below.

Two additional real-process gates now exercise the first deliberately narrow
post-dispatch continuation envelope. On Agno 2.9, an agnoclaw-materialized native Agent
with a public-factory model verified as run-owned, isolated, and recreatable, durable
runtime/Agno databases, a shared
ArtifactStore, non-streaming `start()`, and governed registered capabilities persists
each provider call separately. After a settled tool result, recovery validates Agno's
exact `tool-batch` checkpoint before continuing; before that checkpoint, an approval
wait reconstructs only from the exact request checkpoint, first-provider artifact,
approval ledger, and frozen harness/authority evidence. The tool-checkpoint gate kills
three processes after the checkpoint, during the second provider call, and after its
settlement; it produces two completions, one reconciliation wait, and no duplicate
provider call or tool effect. The approval gate kills immediately after the atomic
approval wait/request commit; a fresh host settles the request, proves exact decision
retry idempotency, executes the capability once, and completes with no duplicate
request, provider call, or effect.

This does not serialize a Python stack or create general exactly-once execution. Agno
2.6.4 retains the conservative path, and raw/custom tools, parser/output-model or
persisted-stream paths, cloud-provider receipts, power loss, multi-host recovery, and
production-duration soak remain separate certification requirements. Executable
evidence lives in
[`scripts/agno_tool_checkpoint_restart_probe.py`](../scripts/agno_tool_checkpoint_restart_probe.py)
and
[`scripts/agno_approval_restart_probe.py`](../scripts/agno_approval_restart_probe.py).

## Decision

For 0.12, `agnoclaw` owns the thinnest orchestration boundary needed to make recovery
and effect claims true:

1. `RuntimeStore` is the canonical authority for immutable runs, attempts, ordered
   events, certified checkpoints, operation intents/settlements, and an outbox. Agno
   session/run storage is a rebuildable projection, not the lifecycle authority.
2. Every nondeterministic model or capability call crosses one `OperationGateway`.
   It records a stable operation ID and idempotency/reconciliation metadata before
   dispatch, then records receipt and settlement atomically with the corresponding
   state transition where possible.
3. External adapters declare their recovery class. Durable automatic retry requires an
   idempotency key accepted by the external system or a trustworthy reconciliation
   query. Opaque effects are rejected in durable/service profiles or require an
   explicit operator decision after ambiguity.
4. A checkpoint is resumable only after all preceding operations are settled or
   explicitly classified. A stored Agno status, provider stream reconnect, or queue job
   state is never documented as sufficient proof.
5. The embedded durable reference uses a small SQLite coordinator/worker implementation
   of these contracts. This is not a second general-purpose queue product.
6. Once an Agno 3 stable release passes the same process-kill, tenancy, atomicity,
   cursor, overload, migration, and load gates, its PostgreSQL/Redis queue may implement
   the service worker/lease adapter. It will not replace the canonical operation/effect
   ledger unless its contract later proves equivalent.

Recovery by boundary is therefore:

| Last durable boundary | Recovery behavior |
|---|---|
| Intent recorded; dispatch not started | Retry is safe under the recorded operation ID. |
| Dispatch may have happened; no trustworthy receipt/settlement | Mark `unknown`; reconcile, use external idempotency, or request operator resolution. Never blind retry. |
| Receipt and settlement committed | Resume from the next certified operation/checkpoint. |
| Stale worker returns after lease transfer | Fencing rejects its state/event/settlement writes. |

“Certified checkpoint” is capability- and version-specific. For the Agno 2.9 envelope
above it means either the validated native `tool-batch` checkpoint after the registered
capability result, or deterministic pre-tool reconstruction from the exact provider
artifact and approval evidence. Absence, drift, or ambiguous dispatch still fails
closed; a stored approval flag or Agno status alone is never enough.

Model requests follow the same rules. If a provider cannot accept an idempotency key or
look up a possibly completed request, an interrupted request is an ambiguous cost/output
operation; policy decides whether to start a new attempt, wait for reconciliation, or
require human review.

## Alternatives rejected

- **Treat Agno 2.x continuation as durable resume.** It does not expose the required
  dispatch/receipt/settlement boundary and failed the killed-process probe.
- **Adopt Agno 3 alpha as the production runtime.** It is prerelease software and its
  queue does not settle arbitrary external effects.
- **Build a separate distributed queue now.** Agno 3 already has promising generic
  queue mechanics; agnoclaw should own only its invariant-specific ledger and embedded
  worker while keeping the service adapter replaceable.
- **Set maximum attempts to one.** This prevents duplicate retries by abandoning work;
  it is not recovery.
- **Claim exactly-once execution.** Transactions cannot span arbitrary providers and
  tools. The honest contract is fenced attempts plus idempotent or reconciled effects.

## Consequences

- T3 may freeze the run kernel only after T10a freezes identity, classification, policy,
  key, diagnostic, and threat-model contracts.
- T4-T6 must model `run_id`, `attempt_id`, `operation_id`, settlement state, checkpoint,
  and fencing token explicitly.
- Chaos tests kill real processes at every intent/dispatch/receipt/settlement/commit
  boundary on SQLite and PostgreSQL.
- Documentation must distinguish conversation continuation, queue retry, checkpoint
  resume, reconstruction, fork, and external-effect reconciliation.
- Agno 3 queue adoption remains an evidence-gated adapter decision, preserving the
  small public `AgentHarness`/`HarnessRun` grammar.

## Sources

- [Agno 2.6.19 continuation and checkpoint release](https://github.com/agno-agi/agno/releases/tag/v2.6.19)
- [Agno 3.0.1 stable package](https://pypi.org/project/agno/3.0.1/)
- [Agno 3.0.0a1 release](https://github.com/agno-agi/agno/releases/tag/v3.0.0a1)
- [agnoclaw Agno release-practice audit](agno-release-practices.md)
- [Harness evaluation and release gates](evaluation.md)
