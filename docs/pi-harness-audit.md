# Pi 0.84 harness audit

Status: primary-source competitor input for agnoclaw 0.12

Reviewed: 2026-08-11

## Verdict

Pi remains the clearest benchmark for a small, composable coding-agent core, but its
latest release materially changes the durability comparison. Pi 0.84.1 contains a new
lane-based v4 session/storage contract and promotes an `AgentHarness` v2 surface. The
released design is unusually rigorous about operation records, provisioned identities,
crash sites, deterministic effect stepping, shared sequencing, and backend conformance.

The maturity label matters: Pi's own promotion matrix says the new durable harness is
still a scaffold and that multiple runtime paths deliberately raise
`HarnessNotImplemented` until named implementation packages land. Its coding agent is
usable; every goal in the durable design document is not yet shipped behavior. This
audit therefore separates released data contracts, released coding-agent behavior,
documented-but-incomplete harness paths, and unreleased main-branch design.

## Released 0.84.1 signals

The [0.84.0 release](https://github.com/earendil-works/pi/releases/tag/v0.84.0)
introduced the large storage/harness change; [0.84.1](https://github.com/earendil-works/pi/releases/tag/v0.84.1)
is the latest stable release at this review.

### Session and storage model

The released [durable harness design](https://github.com/earendil-works/pi/blob/v0.84.1/packages/agent/docs/harness-v2.md)
defines one session as four deliberately separate structures:

- an append-only conversation tree;
- named lanes, each with one leaf and at most one open operation;
- a chronological operation log per lane; and
- append-only latest-value global facts.

All four share one monotonic sequence. Lanes may run in parallel, while one harness is
the only session writer. Operation records never enter model context. Tree entries are
immutable and branch by parent identity rather than copying history. This is a strong
separation of conversation truth, orchestration state, and application facts.

The repository contract has memory and atomic JSONL implementations plus a separately
packaged SQLite backend. JSONL publication uses same-filesystem replacement; malformed
interior records fail, and torn-tail recovery is explicit. Backend conformance covers
lanes, shared sequence ordering, branch queries, latest facts, forks, and statistics.

### Durable operation pattern

The design persists acceptance before execution and gives every future entry a
provisioned identity. Before an effect, the lane records intent and the identity the
result must fulfill; recovery checks that exact identity rather than inferring success
from adjacency or a status string. Provider, tool, hook, timer, and storage effects
cross one injected boundary. Manual drive can park before each boundary so tests can
exercise every crash prefix and both orders of defined races.

Recovery discovers open operations with bounded indexed queries. It restores each lane
independently and classifies provider attempts, tool batches, queues, deferred writes,
abort state, and overflow recovery from the relevant operation prefix. Ordinary
provider streams are not resumed. Deferred provider requests are different: a durable
handle is persisted and later redeemed rather than purchasing another request.

Hooks are explicitly at-least-once unless their external side effects are idempotent by
operation identity. Snapshot-plus-live-event attachment is the UI model; released
events are not a reconnect log, so a reconnect obtains a new snapshot.

### Runtime and remote ergonomics

The low-level agent provides parallel/sequential tool batches, validated preflight and
postprocessing hooks, graceful `shouldStopAfterTurn`, steering/follow-up queues, awaited
event subscribers, dynamic credentials, and a small stateful API. Pi 0.84.1 adds:

- `pi auth check` credential readiness;
- `terminate` on blocked tool results so an all-terminating batch avoids an unnecessary
  follow-up model call; and
- an idle-only `Agent.reset()` guard.

The experimental transport-neutral client uses CBOR and Unix sockets with remote
session metadata and transcript reducers. Pi also changed JSON/RPC streaming to emit
assistant deltas rather than cumulative messages, avoiding quadratic output growth.

### Released maturity boundary

The [promotion test matrix](https://github.com/earendil-works/pi/blob/v0.84.1/packages/agent/docs/harness-v2-test-matrix.md)
is candid: the legacy behavior-complete harness was replaced by the new scaffold, many
runtime/recovery/compaction paths remain assigned to future packages, and uncovered
tests are named rather than silently discarded. That honesty is itself a best practice
for agnoclaw's compatibility and progress ledgers.

## Unreleased main-branch signal

Pi main now contains an unshipped
[`AgentHarness` v3 implementation specification](https://github.com/earendil-works/pi/blob/main/packages/agent/docs/harness-v3.md).
It proposes a simpler three-store model:

- write-once conversation entries;
- mutable registers as the only orchestration/program-counter state; and
- an append-only usage ledger.

Atomic transactions, conditional register writes, explicit operation program counters,
hot-path indexed recovery, complete terminal cleanup, external-finalization handling,
and a finite race catalog replace recovery by folding a long operation history. The
spec also requires exact transaction-write oracles, automatic/manual drive equivalence,
backend conformance, and reopen-from-every-state testing.

This is high-quality architecture research, not released proof. agnoclaw should monitor
it and reuse the invariants that survive implementation, but should not replatform its
schema-v11 ledger around an unreleased competitor specification.

## Adopt, adapt, avoid

### Adopt now

- Persist future result identities before effects and verify exact fulfillment.
- Keep conversation data out of orchestration records and permission/control state.
- Add a deterministic effect-boundary drive for crash-prefix and race-order tests.
- Maintain an explicit uncovered-test ledger when replacing a subsystem.
- Treat all-terminating tool batches as a graceful completion signal after durable tool
  settlement.
- Keep provider deferred handles distinct from ordinary stream reconnection.

### Adapt to agnoclaw

- Pi lanes are named branches inside one single-writer session. agnoclaw already has
  service-wide exact-session leases and declared child runs; named parallel lane views
  should be considered only if they preserve PostgreSQL multi-process fencing, tenant
  authority, and the small `HarnessRun` grammar.
- Pi's shared sequence is a useful invariant. agnoclaw's authoritative run event
  sequence and separate operation ledger should gain explicit cross-record causality
  rather than being replaced by one local JSONL format.
- Pi's snapshot-plus-live model suits a local UI. agnoclaw retains cursor-replayable
  lifecycle/output events because remote embedders need gap detection and retention
  semantics across process loss.
- Pi's exact race catalog should become a required release artifact for every agnoclaw
  state-machine addition, including child recovery, approval, operation reconciliation,
  and learning promotion.

### Avoid

- Do not inherit a single-writer, single-placement restriction in the service profile;
  PostgreSQL leases/fences and bounded multi-worker recovery remain required.
- Do not equate a detailed design document or scaffold with production durability.
- Do not copy coding-agent omissions—authorization, MCP, institutional learning,
  multi-tenant isolation, and artifact governance are core agnoclaw responsibilities.
- Do not add lanes, registers, records, and runs as four competing top-level user APIs.
  The operator manifest may be rich while embedding stays simple.

## Plan reconciliation

| Pi finding | Current agnoclaw coverage | Required delta |
|---|---|---|
| Intent plus provisioned result identity | Every `OperationIntent` now derives and persists a canonical result slot before dispatch; success binds that slot to its settlement/artifact and mismatches fail closed | Preserve the invariant as future provider/tool/child entry types join the operation contract; expand physical-corruption repair tests. |
| Named lanes and one open operation per lane | Exact session leases serialize service work; child runs provide independent lineage | Keep current default; evaluate named lanes only behind store-fenced conformance and without weakening owner scope. |
| Bounded indexed restore | Partial indexes now cover executable runs, reconciliation waits, dispatchable/reconcilable operations, and child lineage; SQLite plan gates reject scans/sorts, three batched 1,000→10,000 terminal-history runs stayed within 0.932–1.185 p95 growth, and PostgreSQL uses indexed plans over 10,000 terminal rows | Retain these gates; add production-scale PostgreSQL latency/concurrency, primary-failover, and noisy-neighbor evidence. |
| Deterministic effect drive | `agnoclaw.testing` supplies exact-occurrence crashes, finite-timeout store barriers, and manual pre-dispatch/before-effect/after-effect gates; one shared SQLite/PostgreSQL matrix covers all four effect classes, every operation state, transaction rollback/exact retry, close/reopen, and both cancellation-versus-commit orders | Require every future provider-handle/task/job operation entry type to join the matrix; expand from database-process faults to primary failover and provider-observer chaos. |
| Deferred provider handles | Unknown-effect reconciliation and artifact-backed known success | Add a provider-handle capability only after fetch/cancel/idempotency contracts are certified. |
| Atomic JSONL and SQLite conformance | SQLite/PostgreSQL shared RuntimeStore semantics and transaction rollback tests | Retain backend-neutral conformance; add exact transaction-write oracles and corruption/repair gates. |
| Explicit promotion debt matrix | v0.12 progress ledger and compatibility lanes | Record removed/uncovered tests by name during every subsystem replacement. |
| Main v3 entries/registers/usage split | Events, snapshots, operation records, terminal projection, usage evidence | Monitor; adopt its “one payload, one durable home” and terminal-cleanup invariants without schema churn before evidence. |

## Sources

- [Pi 0.84.1 release](https://github.com/earendil-works/pi/releases/tag/v0.84.1)
- [Pi 0.84.0 release](https://github.com/earendil-works/pi/releases/tag/v0.84.0)
- [Pi agent-core README at 0.84.1](https://github.com/earendil-works/pi/blob/v0.84.1/packages/agent/README.md)
- [Released durable harness design](https://github.com/earendil-works/pi/blob/v0.84.1/packages/agent/docs/harness-v2.md)
- [Released promotion test matrix](https://github.com/earendil-works/pi/blob/v0.84.1/packages/agent/docs/harness-v2-test-matrix.md)
- [Pi client protocol](https://github.com/earendil-works/pi/blob/v0.84.1/packages/client/README.md)
- [Pi wire protocol](https://github.com/earendil-works/pi/blob/v0.84.1/packages/protocol/README.md)
- [Unreleased main-branch harness v3 specification](https://github.com/earendil-works/pi/blob/main/packages/agent/docs/harness-v3.md)
