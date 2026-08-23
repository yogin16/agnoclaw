# Harness evaluation and release gates

Status: target quality contract

Last reviewed: 2026-08-23

A world-class harness is defined by repeatable outcomes under failure, scale, and hostile
inputs—not by the number of tools it exposes. This document specifies the evidence
required for a capability to be marked stable.

## Maturity labels

Every public capability has one label:

| Label | Meaning |
|---|---|
| **stable** | Public contract documented; deterministic conformance, integration, failure, and upgrade tests pass on supported versions. |
| **preview** | Useful and tested on the locked stack, but missing one or more production contracts or compatibility coverage. |
| **experimental** | API/behavior may change; not suitable for a critical dependency. |
| **planned** | Design only; no claim that the behavior exists. |

“Implemented” is not a maturity label. A code path can exist and still be experimental.

## Test pyramid

### 1. Contract tests

Fast, deterministic tests for public types and invariants:

- sync/async parity;
- event and error schemas;
- policy checkpoints;
- tool effect normalization;
- context reduction and tool-call/result pairing;
- snapshot serialization;
- scope and identity propagation;
- capability activation and restoration;
- learning prerequisites and store configuration;
- adapter behavior against recorded fixtures.

### 2. Scenario evaluations

Versioned tasks with expected outcomes and acceptable trajectories:

- short answer/extraction;
- multi-tool repository change;
- research with large pages and citations;
- long-running project across compaction boundaries;
- human approval and edited tool arguments;
- child-agent delegation and synthesis;
- personal-memory recall and tenant isolation;
- resumed work after worker restart;
- model/provider substitution.

Score final correctness, required evidence, expected/forbidden tools, policy behavior,
state transitions, latency, tokens, and cost.

### 3. Chaos and recovery tests

Inject failure at every boundary:

- process kill before/after model request;
- kill before, during, and after a side-effecting tool;
- event/snapshot/artifact store outage;
- provider timeout, rate limit, malformed stream, and context overflow;
- MCP disconnect, server restart, tool-list change, and expired authorization;
- approval client disconnect;
- scheduler duplicate delivery and worker lease expiry;
- scheduler crash before lifecycle admission, after lifecycle binding, after lifecycle
  settlement, and before scheduler acknowledgement;
- due-job update versus claim, stale-fence mutations, future-jitter backlog, misfire,
  overlap, retry, and database-clock skew;
- disk-full and corrupted/truncated local event tail;
- cancellation during model, tool, subagent, and waiting states.
- provider response received before/after OperationGateway settlement;
- RuntimeStore transaction commit before/after outbox publication;
- PostgreSQL lease expiry, stale-worker fencing, failover, backup restore, and migration
  interruption.
- child deadline before dispatch, during non-repeatable dispatch, after settlement,
  across supervisor/store failure, and across worker restart; missing, malformed,
  delayed, or bill-disagreeing provider usage evidence;
- child result-schema validation during live completion and known-success recovery;
  synthesis retry while pending, failed-source policy, oversized evidence, hostile
  child-result instructions, and artifact-reader scope denial.

The opt-in `agnoclaw.testing` module drives the real gateway/store implementations; it
does not substitute a fake runtime. `StoreFaultScript` raises a content-free
`InjectedRuntimeCrash` at one exact named persistence checkpoint and occurrence.
`StoreBarrierScript` instead pauses that checkpoint on the database worker thread until
the test releases it; a finite internal timeout converts a malformed script into a
failure rather than a hung suite. This makes cancellation-versus-commit order explicit
without replacing or monkeypatching the store mutation.
`DeterministicEffectDriver` pauses an ordinary `OperationGateway.execute()` at the
known-no-effect pre-dispatch check, immediately before the external callable, and
after the callable returns but before result settlement:

```python
import asyncio

from agnoclaw.testing import DeterministicEffectDriver, EffectBoundary

driver = DeterministicEffectDriver()
task = asyncio.create_task(
    gateway.execute(
        intent,
        driver.wrap_effect(provider_call),
        pre_dispatch=driver.pre_dispatch,
    )
)

await driver.wait_for(EffectBoundary.PRE_DISPATCH)
assert store.get_operation(intent.operation_id).state == "dispatching"
await driver.advance(EffectBoundary.PRE_DISPATCH)
```

Tests then choose whether cancellation, completion, lease loss, or simulated process
death wins. The current backend-neutral matrix drives all four effect classes at all
three external boundaries; rolls back and exactly retries prepare, dispatch, every
terminal settlement, recovery, and reconciliation crashes; closes/reopens each
authoritative state; and proves both database-commit orders on SQLite and PostgreSQL.
A driver is bound to one event loop, enforces arrival order, and times out instead of
hanging a suite. Future operation entry types must join this same matrix.

The PostgreSQL lanes also run five bounded operational probes. The load probe
creates and removes 10,000 terminal rows while measuring exact-owner isolation,
p50/p95/p99 latency, noisy-neighbor slowdown, process fairness, and hard pool
saturation. The restore probe requires a loopback test source, a distinct
`restore`+`test` target, one exact container, and `--allow-target-reset`; it compares
all runtime rows plus schema/index/constraint/sequence metadata and verifies ordered
events and idempotent replay after native dump/restore. Its local timing is regression
evidence only. An independent Docker topology gate creates a PostgreSQL 17 hot standby,
forces positive replay lag, catches up to the exact acknowledged LSN, fences the old
primary, requires a bounded no-writer interval, promotes, and proves existing/fresh
read-write pool continuity. It certifies only that planned fenced-promotion path. The
synchronous companion below covers one exact-standby/single-abrupt-loss path, and the
role-rotation companion rewinds/rejoins both former writers across a complete return
rotation. These probes do not certify a production failover controller, external
fence, true network split brain, simultaneous/multiple failures, production
synchronous policy/SLO, endpoint-discovery/rejoin automation, memory budgets, PITR,
artifact/key recovery, corruption response, or production RTO.

### 4. Soak, load, and adversarial tests

- 100+ turn context soak;
- hours-long durable run with restart;
- many concurrent threads and repeated same-thread messages;
- large tool catalogs with deferred discovery;
- nested subagents at depth and fan-out limits;
- memory growth and retention over simulated months;
- prompt injection, tool injection, data exfiltration, secret leakage, path traversal,
  SSRF, approval confusion, and memory poisoning.
- bounded queue overload, per-tenant fairness, noisy neighbors, slow event consumers,
  graceful worker drain, and p95/p99 recovery latency.

### 5. Upstream compatibility tests

Run against:

- repository-legacy Agno 2.6.4 during the migration window;
- primary stable Agno 2.9.0 during development and the newest stable at RC;
- newest Agno 3 prerelease in a non-production preview lane;
- supported Python versions;
- current supported MCP SDK/protocol revision;
- major model-provider adapters used in examples.

Any compatibility branch selected through feature detection must have tests for both
paths. Silent `hasattr` fallbacks without a contract test are prohibited.
Each harness-relevant upstream release delta also needs a recorded adopt/adapt/avoid
decision and a linked contract. See
[Agno release-practice audit](agno-release-practices.md).

## Core release gates

### API and isolation

- `run`, `arun`, streaming, and `HarnessRun.wait()` agree on terminal output and errors.
- Concurrent runs on one harness cannot observe another run's prompt, tools, schema,
  argument bindings, dependencies, session state, user/tenant ID, approvals, or events.
- Same-thread runs are serialized or rejected with an explicit contract.
- Configuration objects are immutable or safely copied.
- Cancellation and timeouts release runtime resources.
- Explicit-profile non-streaming first-party clients enter `start()/wait()`. Async
  REPL/TUI model work enters the same operation gateway with a bounded non-authoritative
  presentation attachment and reconciles to terminal truth; named-legacy raw streams
  use their documented compatibility route and explicit sync streaming fails closed.
- A slow/closed presentation consumer cannot backpressure or cancel the logical run;
  interactive worker cancellation invokes explicit lifecycle cancellation instead.
- Persisted output segments are size/count bounded, owner-scoped, artifact-verified,
  content-free in the runtime ledger, gap-free, cursor resumable after reopen, and
  paged through the authenticated remote boundary. Cancellation flushes consumed text;
  a killed process loses no more than the one documented partial-segment RPO.
- Segment rollback, artifact corruption/binding drift, wrong-run cursors, oversized
  remote pages/content, sequence gaps, and malformed peer cursors fail closed.
- Cancelling a scheduler waiter after lifecycle admission retains the runtime run ID,
  records detachment, and does not implicitly invoke `cancel()`.
- SQLite/PostgreSQL workers atomically create one deterministic occurrence/attempt,
  reject stale-fence renew/bind/finish/release, and reattach the same lifecycle run after
  claim loss. Only known retryable terminal failures spend retry budget.
- Concurrency-group queue mode holds at most one pending/retry backlog even when jitter
  delays the first item; skip/misfire decisions produce terminal history without model
  execution.
- CLI and isolated scheduler harnesses close on success, failure, and cancellation on
  their owning async loop; child compatibility harnesses keep their existing `finally`
  close contract.

### Durability and recovery

- Every committed event has a unique ID and monotonic per-run sequence.
- A client can reconnect from an event cursor without gaps or duplicates beyond the
  documented at-least-once boundary.
- Only settled snapshots are resumed by default.
- Tool calls with unknown post-crash effects remain explicitly unknown and are never
  blindly retried.
- Every intent provisions a canonical future result identity before dispatch; success
  fulfills that exact slot, and mismatched/corrupt fulfillment fails closed.
- Forking preserves prior history and creates lineage without rewriting it.
- A waiting approval/input survives worker restart in durable profiles.
- Restart tests pass at every model/tool step boundary.
- Provider/model calls cross the same OperationGateway settlement boundary as tool
  effects; ambiguous provider outcomes follow a tested reconciliation/cost policy.
- Declared-child timeout commits authoritative cancellation before local task
  cancellation. Reported token/cost excess blocks successful child settlement and an
  all-success parent join; missing provider evidence remains visibly unverified.
- A declaration-bound output mismatch fails the child after preserving successful
  operation truth and content-free validation evidence. Governed synthesis is bounded,
  all-success by default, idempotent, and gives untrusted child content no instruction
  or artifact-read authority.
- Recovery discovery remains partial-index-backed for executable, reconciliation, and
  child-lineage paths as terminal history grows. SQLite query-plan tests reject table
  scans/temporary sorts; PostgreSQL `EXPLAIN (ANALYZE, BUFFERS)` rejects ledger
  sequential scans with at least 10,000 terminal runs and operations.
- `uv run python scripts/benchmark_recovery_index.py` must keep each 10,000-versus-1,000
  SQLite p95 growth ratio at or below 2.0 across 100 samples of 100 calls. This is a
  history-growth regression gate; production p50/p95/p99 SLOs remain separate.

### Context

- Tool-call/result pairs remain valid after every reducer.
- Large outputs are preserved in the artifact store before context reduction.
- Bounded paged reads can recover any spilled text used by the task.
- Compaction summaries preserve goal, completed work, decisions, blockers, tests, and
  relevant files for the benchmark task.
- Full trajectory remains searchable after compaction.
- Provider overflow triggers at most one automatic compact-and-retry per threshold
  cycle; repeated failure returns a typed error.
- The context manifest reconciles with provider usage within a documented tolerance.

The current registered-capability and lifecycle first-party-tool slice already gates
small-value preservation, lossless bounded envelopes, exact paged reconstruction,
same-session continuation, cross-session denial, first-party replay without
redispatch, pre-persistence redaction, and spill/failure provenance through compaction.
The broader direct-compatibility/context/raw-MCP and outer-model benchmark below
remains mandatory before claiming universal output spill. Plugin/pack registrations
already join the governed benchmark when they publish `CapabilitySpec`; their raw tools
fail lifecycle admission.

### Tools, skills, and MCP

- Tool effects and required scopes are present for every first-party tool.
- Unknown/custom tool defaults are conservative.
- Exact normalized actions are shown for approval and recorded through the
  OperationGateway's operation ledger.
- Skill metadata is cheap at discovery; full content loads once on activation and
  survives compaction as designed.
- Untrusted project/community skills cannot grant themselves tools, install code, or
  bypass sandbox/policy.
- Tool selection success remains within the target as the catalog scales from 10 to
  50, 200, and 1,000 tools.
- MCP tool tests cover SDK 2.0/current negotiation and fallback, the fixed deferred
  surface, stdio, Streamable HTTP, explicit legacy SSE, pagination/bounds, structured
  content, private metadata, tool-list/schema drift, network denial, async ownership,
  run cleanup, and operation effects. Real reference servers, reconnect, auth expiry,
  malicious servers, extensions, and soak remain separate blocking lanes.

### Safety and privacy

- Service profiles deny missing/invalid tenant and identity scope.
- Read, write, destructive, idempotent, and open-world effects are policy tested.
- Secrets do not appear in model prompts, default events/traces, errors, summaries, or
  artifacts without explicit secure configuration.
- External web/MCP/tool content is labeled untrusted and cannot change deterministic
  policy.
- Path, symlink, network, SSRF, and shell bypass suites pass for each runtime backend.
- Cross-tenant session, artifact, event, and learning access tests always fail closed.
- Approval replay or argument substitution after approval is rejected.

### Learning

- Every enabled store passes write/recall/delete/isolation tests.
- Learned Knowledge fails construction without vector-backed Agno `Knowledge`.
- Candidate provenance points to retained evidence.
- High-risk promotion requires a code-enforced approval.
- Relevant learning improves or preserves the chosen quality metric versus control.
- Irrelevant, stale, contradicted, and poisoned memories are withheld or quarantined.
- Negative feedback can identify and roll back the implicated learning.
- Stable automatic promotion, decay, prompt/policy mutation, and skill rewriting are
  absent; experimental versions are disabled by default and have explicit opt-in gates.
- Failure records preserve verifier cause, causal agent behavior, and implicated harness
  component; terminal labels alone cannot drive an edit.
- Every experimental edit has an immutable, falsifiable change hypothesis and is
  constrained to its authorized component/file surface.
- Proposers cannot modify benchmark cases, verifiers, raw traces, models/configuration,
  budgets, or permissions. Such control-plane drift invalidates the evaluation.
- Targeted held-in, untouched held-out, and frozen cross-task transfer gates pass with
  passing behaviors preserved and no safety/privacy regression.
- Candidate acceptance uses a Pareto view of quality, safety, cost, latency, and added
  complexity; negative results and diversity-collapse metrics are retained and
  owner-queryable through a content-free read model without notes, raw metrics, content,
  or artifact IDs.
- LLM-judge comparisons balance candidate order, record disagreement, and escalate
  ambiguous samples. Deterministic verifiers take precedence.

### Observability and budgets

- Events correlate thread, run, parent run, step, tool call, trace, model, and
  capability versions.
- Usage includes input, output, reasoning where available, cache read/write, tool time,
  wall time, and cost when pricing is known.
- Budgets stop or pause runs predictably: wall time, model calls, tool calls, tokens,
  cost, subagent depth/fan-out, parallel tools, artifact bytes, and retry count.
- Every budget declares whether it is admission-time, active-worker, settlement-time,
  provider-preflight, or post-hoc. A settlement-time overage may prevent successful
  completion but must never be advertised as a prepaid or reversible spending ceiling.
- Billing-grade claims require provider receipt reconciliation; generic framework
  metrics are content-minimized operational evidence and missing fields fail visible.
- Telemetry exporters backpressure or fail according to profile without silently
  corrupting the trajectory.
- Default observability does not record sensitive content.

## Benchmark suites

### Short-run efficiency

Measure on small deterministic tasks:

- construction time;
- time to first model request and first token;
- harness CPU/memory overhead;
- fixed system/tool-schema tokens;
- prompt cache hit stability;
- result accuracy.

Gate: `quick` must remain meaningfully close to using Agno directly. The initial target
should be set from a measured baseline, then enforced as a regression budget rather than
inventing a percentage in advance.

### Long-run continuity

Use an initializer/worker pattern similar to long-running coding-agent research:

1. initialize a repository and feature specification;
2. complete work across multiple model context windows;
3. interrupt processes and resume;
4. require tests and a final evidence report.

Measure:

- task completion;
- duplicate/repeated work;
- lost requirements and decisions;
- invalid tool pairing;
- compaction count and tokens reclaimed;
- recovery time and replayed side effects;
- human interventions;
- final repository correctness.

The deterministic local harness gate isolates context mechanics from provider quality:

```bash
uv run python scripts/long_run_continuity_probe.py \
  --turns 100 \
  --restart-turns 30,70 \
  --max-context-tokens 1800 \
  --tool-every 10
```

It uses a host-supplied deterministic Agno `Model`, the real `AgentHarness`, a
disposable Agno SQLite database, and the public context APIs. The 2026-08-17 retained
run passed 100 turns, 11/11 deterministic Agno-native function calls, nine automatic
compactions plus one final archival boundary, three database reopens, contiguous
manifest/checkpoint sequences, checksum-verified artifacts, exact head/middle/tail
input and tool-result search/rehydration, exactly-once persisted injection, and a
bounded 909/1,800-token final live context. Tool-result markers originate only in the
tool response, so their recovery does not merely rediscover prompt text. Stdout is one
content-free JSON record; optional retained evidence must target an empty directory.

This proves that exact local mechanism, including Agno's real function-call executor
and agnoclaw's tool lifecycle hooks, and caught a real first-turn Agno boundary bug. It
does not prove semantic summary quality, external or irreversible effects, abrupt
process death during a tool, a live provider's overflow/tool-selection behavior,
multi-host fencing, production storage, or hours-long unattended task correctness.
Keep those as separate gates rather than inflating this deterministic result.

Summary synthesis has a separate trust oracle: transcript bytes are untrusted quoted
data, the internal run receives no callable tools and no injected Agno history, and any
tool-shaped or empty result is discarded for the bounded deterministic fallback.
Archival projects protected summary/memory-maintenance run metadata onto cloned
messages, so those prompts remain auditable without becoming user intent. Focused
contracts also prove that repeated identical carried continuation values keep only the
newest bounded search hit while every immutable artifact remains intact.

The same probe has an explicit live-provider mode. Run it only against an operator-owned
Ollama origin; a non-loopback origin additionally requires `--allow-remote-ollama`:

```bash
uv run --isolated --extra local python -W error::ResourceWarning \
  scripts/long_run_continuity_probe.py \
  --turns 100 \
  --restart-turns 30,70 \
  --max-context-tokens 1800 \
  --tool-every 10 \
  --provider ollama \
  --model qwen2.5:7b \
  --allow-live-model
```

The retained Agno 2.9.0 run pinned Ollama model digest
`sha256:845dbda0ea48ed749caafd9e6037047aa19acfcfd82e7047ca97d631a0b697e`
and configuration digest
`sha256:c62c21d8894faf0f13455800ac7807b334077607ff1a85527c56d52611b2ddf8`.
It passed 100 real provider-backed turns, 11/11 native tool calls, 14 compactions with a
contiguous manifest/checkpoint chain, three reopens, all 14 artifact-integrity loads, typed head/middle/tail
input and canonical tool-result recovery, exactly-once injection, and a final
1,038/1,800-token live context. At least 111 provider inferences were observed by
protocol construction; the report does not pretend this is billing-grade usage.

The same gate correctly rejected `qwen3:0.6b` after it skipped the required turn-10
tool call. That is model-configuration evidence, not a reason to weaken the harness
oracle. The passing local configuration proves one live tool-selection/overflow path;
it does not prove cloud-provider breadth, semantic summary fidelity, irreversible
effects, process death during inference or a tool, multi-host fencing, provider
receipts, or production-duration unattended work.

After the 2026-08-18 summary-isolation, automatic-goal-carry, carried-state-search, and
narrative-release-fitting changes, the deterministic gate passed again in 85.52
seconds and the same pinned live gate passed in 924.54 seconds. The live test parsed
the content-free report and reasserted 100 provider turns, 11/11 exact native tool
calls, three reopens, repeated compaction, bounded final context, and exact canonical
recovery; the slower wall time is provider inference, not a packaging benchmark.

A fresh deterministic release-closeout run after the provider/tool-checkpoint recovery
changes passes 100 turns with 141 deterministic model calls including maintenance, 13
automatic compactions plus one final archive, 14 integrity loads, three reopens, 11/11
exact-once native tool calls, all six input/tool-result retrieval/rehydration checks,
exactly-once persisted injection, and 1,253/1,800 final live tokens.

### Real-process SQLite crash recovery

Run the bounded disposable-database probe before an RC and after SQLite runtime-store
changes:

```bash
uv run python scripts/sqlite_runtime_crash_probe.py \
  --iterations 10 \
  --allow-process-crash
```

Each iteration starts a real child process and calls `os._exit()` inside five open
transactions: run creation, lifecycle transition, operation prepare, dispatch, and
settlement. The parent reopens the database, proves the partial mutation is invisible,
retries it twice, requires exactly one canonical event, runs SQLite `integrity_check`,
and checkpoints the WAL. The retained local gate passes 50/50 crashes. This proves
single-node SQLite transaction recovery for those exact boundaries; it does not prove
host-power-loss behavior, filesystem durability, distributed failover, or provider-side
effect truth.

### Real-process capability-effect crash safety

Run the bounded two-ledger probe after changes to `OperationGateway`, recovery, effect
classification, or capability dispatch:

```bash
uv run python scripts/operation_effect_crash_probe.py \
  --iterations 1 \
  --allow-process-crash
```

The child dies at two external-effect boundaries—before provider dispatch and after
provider commit but before operation settlement—for `read_only`, `idempotent`,
`compensatable`, and `non_repeatable` operations. The retained run passes eight real
process crashes: four safe retries, four conservative reconciliation blocks, four
external commits, zero duplicate external effects, and zero blind ambiguous
redispatches, with both SQLite ledgers intact. The JSON report is content-free.

This certifies the exact `OperationGateway` capability path and provider-ledger oracle.
It does not certify a live provider, arbitrary Agno model/tool continuation, provider
billing receipts, power-loss durability, multiple hosts, or production-duration
chaos/soak.

### Real-process AgentHarness/Agno stack restart

Run the bounded full-stack probe after changes to `AgentHarness.start()`, request
checkpoints, model-operation settlement, execution leases, or recovery:

```bash
uv run python scripts/agno_stack_restart_probe.py \
  --allow-process-crash
```

The probe starts actual Agno Agents behind the public lifecycle API and a public
`AgnoModelFactory`. It kills four disposable processes: after a planned model intent,
during provider dispatch, after successful result/operation settlement but before run
completion, and again at the planned boundary before intentionally changing the
factory implementation digest. A fresh harness reopens the same Agno session database,
runtime store, and artifacts, then uses bounded owner-scoped
`recover_pending_runs()` discovery for every crash. The retained gate passes all four
crashes: the planned
request continues exactly once; the in-flight request parks for reconciliation without
redispatch; the settled result completes without a second provider call; and digest
drift terminates with `RUN_RECOVERY_SPEC_MISMATCH` before provider dispatch. It records
three total provider calls, only one after restart, zero duplicates, zero blind
ambiguous redispatches, thirteen unique factory transports, five-of-five clean-recovery
closures, four-of-four startup-scan recoveries, and valid runtime, provider, and Agno
SQLite integrity.

This certifies one factory-backed, provider-neutral outer Agno model operation through
the complete harness lifecycle. It does not certify a cloud-provider receipt, power
loss, multiple hosts, or production soak. The narrower factory-backed Agno 2.9 tool
and approval continuation envelopes below have independent gates.

### Real-process learning-reconciliation worker restart

Run the bounded learning-maintenance crash gate after changes to candidate leases,
observer coordination, evidence settlement, or worker restart behavior:

```bash
uv run python scripts/learning_reconciliation_restart_probe.py \
  --allow-process-crash
```

The child terminates inside the read-only observer while holding a three-second
database-clock lease. The oracle proves a competitor cannot steal that active lease;
after expiry, a reopened worker advances fence 1→2, observes once, settles exactly one
evidence-bound reconciliation, returns the candidate to `qualified`, releases its
lease, and leaves the ledger intact. It reports zero promotion redispatches and zero
duplicate reconciliations. This is one local SQLite process-death/reclaim boundary,
not PostgreSQL partition, power-loss, multi-host, custom-observer, or soak evidence.

### Real-process Agno tool-batch checkpoint restart

Run after changes to provider-operation ordinals/artifacts, Agno checkpoint feature
detection, registered capability execution, or recovery:

```bash
uv run python scripts/agno_tool_checkpoint_restart_probe.py \
  --allow-process-crash
```

Each scenario drives an actual native Agno Agent through provider call 1, one governed
registered capability, and provider call 2. Children die after the durable tool-batch
checkpoint, during provider call 2, and after provider call 2 settles but before the
outer run completes. The oracle requires the capability effect and provider artifacts
to have exact ordinals/digests, validates the native Agno checkpoint, reopens all three
SQLite databases, and rejects any duplicate.

The retained matrix passes three real crashes with two completed continuations and one
`waiting_for_reconciliation` outcome. It observes six provider calls, only one after
restart, three tool effects, zero duplicate provider calls, zero duplicate effects, and
valid database integrity. This proves that an earlier checkpoint cannot hide a later
ambiguous provider operation: the during-provider-2 case blocks without redispatch;
the already-settled provider-2 case completes from its artifact.

### Real-process durable approval restart

Run after changes to atomic approval waiting, decision/grant idempotency, first-provider
artifact replay, registered capabilities, or recovery:

```bash
uv run python scripts/agno_approval_restart_probe.py \
  --allow-process-crash
```

The child exits immediately after the authoritative `waiting_for_approval` transition
and approval-request insert commit together. The oracle first proves that provider call
1 is settled, there is one pending request, and neither a tool effect, provider call 2,
nor a valid tool-result checkpoint exists. A new host settles the exact request, repeats
the same decision idempotently, and recovers from the frozen request plus provider-1
artifact. The run must execute the capability once, make provider call 2 once, and
complete.

The retained gate passes one real crash and reports one approval-wait recovery, two
provider calls with one after restart, one request, one approved record, one tool
effect, zero duplicate requests/calls/effects, and three intact databases. A conflicting
decision retry must fail rather than create new evidence.

Both gates are conditional certification, not a blanket Agno claim. They require Agno
2.9's `tool-batch` checkpoint/cancel/continue contract, an agnoclaw-materialized native
Agent and public-factory model whose manifest is run-owned, isolated, and recreatable,
non-streaming `start()`, durable runtime and Agno databases, a shared ArtifactStore,
and governed registered capabilities. Agno 2.6.4, raw/custom or extension tools,
directly injected opaque models, parser/output-model and
persisted-stream paths, live cloud receipts/billing, host power loss, multi-host/network
partitions, and production-duration soak remain outside the certified matrix.

### Recovery-index scaling

Run the safe temporary-database benchmark before a release candidate:

```bash
uv run python scripts/benchmark_recovery_index.py
```

Retain its JSON output with the release evidence. A passing ratio says discovery cost
did not scale with a tenfold increase in irrelevant terminal history; it does not claim
service latency, throughput, database failover, or tenant-fairness certification.
The 2026-08-18 local run passed with 10,000-versus-1,000 p95 ratios of 0.996 for
recoverable operations, 0.993 for recoverable runs, and 0.997 for reconciliation
operations; the largest measured p95 was 0.043604 ms.

### Bounded PostgreSQL service regression

Against a disposable loopback database whose name contains `test`, run:

```bash
uv run python scripts/benchmark_postgres_runtime.py \
  --dsn postgresql://postgres:secret@127.0.0.1:5432/agnoclaw_test
```

The executable oracle uses a random, exactly cleaned prefix and 10,000 terminal rows.
It requires exact-owner recovery under three noisy workers, 400 completed probe calls,
no p99 above 25 ms, no p95 slowdown above 6x on shared hosted runners (4x remains
the local target; the CI limit was widened on 2026-08-20 after healthy runners
measured unstable ratios), four typed overloads when a two-connection
pool plus two-request queue is saturated, and cool-tenant admission after at most one
ready hot-tenant turn. Retain the JSON output from three consecutive passes. This gate
is intentionally narrower than production certification: cross-process weighted
fairness, slow exporters, production memory/connection budgets, partitions, primary
promotion, and timed RPO/RTO drills remain mandatory service gates.

### Bounded learning evaluation-archive regression

Against the same kind of disposable loopback test database, run:

```bash
uv run python scripts/benchmark_learning_evaluation_archive.py \
  --dsn postgresql://postgres:secret@127.0.0.1:5432/agnoclaw_test
```

The gate refuses non-loopback hosts or a database name without `test`. It creates two
random exact owners with 10,000 immutable evaluations each, reads two disjoint 50-item
descending keyset pages for every sample, applies evaluator/reason/mechanism/target/
safety filters, and runs three concurrent noisy workers against the second owner. It
fails on an owner leak, duplicate/overlapping cursor page, p99 above 100 ms locally
(150 ms on shared hosted runners), slowdown above 5x, or inexact cleanup. Since
2026-08-20 the hosted CI lane runs this measurement as a non-blocking advisory
step (`continue-on-error`, surfaced in the run summary) after materially
different timings on otherwise healthy shared runners; the functional
archive/index, migration, owner-isolation, and store contracts stay blocking.

The latest PostgreSQL 17 loopback run completed 309 hot queries and passed at 49.17 ms
noisy p50, 56.64 ms p95, 58.87 ms p99, 60.78 ms maximum, and 0.974x slowdown; cleanup
removed exactly 200 candidates and 20,000 evaluations. Retain the JSON output with the
release evidence. The output deliberately marks `production_certification` false:
this bounds schema-v5 projection/index regressions at the measured distribution, but
does not certify production cardinality/skew, memory and connection budgets,
multi-AZ failover/partition behavior, retention/rotation, or deployment-specific
additional-index policy.

### Bounded PostgreSQL outage regression

Run the destructive stop/start drill only against one exact loopback test container,
after other service gates:

```bash
uv run python scripts/postgres_restart_probe.py \
  --dsn postgresql://postgres:secret@127.0.0.1:5432/agnoclaw_test \
  --container agnoclaw-postgres-test
```

The gate requires an outage response inside the configured finite bound, typed
capacity or connection-loss semantics, unchanged acknowledged state, exact lease-fence
continuity, existing/fresh pool recovery, contiguous post-outage events, at most two
application connections, container healing, and exact marker cleanup. Retain repeated
JSON output. `RUNTIME_STORE_CONNECTION_LOST` is deliberately non-retryable because a
mutation can be ambiguous; re-read/reconcile first. A stopped single primary is not a
network partition, split-brain, replica-promotion, or production RPO/RTO drill.

### Fenced PostgreSQL promotion regression

```bash
uv run pytest tests/test_postgres_failover_probe.py -q
uv run python scripts/postgres_failover_probe.py \
  --allow-topology-create --image postgres:17-alpine \
  --timeout 90 --outage-timeout 1
```

The pure contracts gate resource authority, exact image and naming, all-stage cleanup,
sensitive Docker-error normalization, numeric LSN ordering, catch-up, old-primary
fencing, and read-write multi-host routing. The live gate must show strict standby
write rejection, positive paused-replay lag, absence of the new run and learning
candidate before replay, exact acknowledged runtime and learning state after catch-up,
bounded failure for both pools while no writer exists, old-primary stopped before and
after promotion, existing/fresh pool selection of the promoted writer, post-promotion
run and learning mutations, contiguous events, and bounded connections. Five consecutive
local runs passed on 2026-08-12; worst no-writer failure was 1.003 seconds, worst
promotion 0.124 seconds, and worst end-to-end cleanup 4.916 seconds.
The extended runtime-plus-learning path passed on 2026-08-18 against PostgreSQL 17.11
with typed no-writer failures, exact candidate/evaluation replay, existing/fresh learning
pool recovery, a revision-2 quarantine transition, and complete resource cleanup.

Passing this gate removes planned fenced replica promotion and measured lag from the
local/CI gap list. It does not remove production controller/fence, network partition,
split-brain, unplanned-loss RPO, synchronous replication, rewind/rejoin, repeated role
rotation, or production load/RTO gates.

### Synchronous acknowledgement and abrupt-loss regression

```bash
uv run pytest tests/test_postgres_synchronous_failover_probe.py -q
uv run python scripts/postgres_synchronous_failover_probe.py \
  --allow-topology-create --image postgres:17-alpine \
  --timeout 90 --outage-timeout 1 --blocked-observation 0.5
```

The gate requires `remote_apply` on the actual RuntimeStore connection, one exact named
`streaming/sync` standby, an unacknowledged RuntimeStore commit throughout the exact
replication-network partition, exact named WAL-sender termination after partition,
acknowledgement only after network rejoin and replay, immediate standby visibility, an
exact acknowledged state/event/lease-fence manifest, abrupt primary `SIGKILL`, a
bounded no-writer interval, explicit promotion with the old primary still fenced, and
byte-equivalent serialized events plus state through existing/fresh pools under a two-
connection cap. A false acknowledgement, missing manifest row/event, policy mismatch,
surviving old primary, connection-bound breach, or resource leak is a hard failure
when the probe runs. Since 2026-08-20 the hosted CI lane runs this container drill
as a non-blocking advisory step (`continue-on-error`, surfaced in the run summary)
because GitHub's runner did not restore the published host port after an
intentional Docker network detach; the pure contracts and the other etcd,
promotion, role-rotation, and split-brain gates stay blocking, and the drill
remains a blocking local gate.

Five completed hardened runs passed on 2026-08-12. The partition withheld
acknowledgement for 0.500–0.504 seconds; rejoin took 0.231–0.294 seconds;
acknowledgement returned only after rejoin at 0.735–0.794 seconds; primary loss took
0.214–0.355 seconds; no-writer response took 1.001–1.005 seconds; and promotion took
0.116–0.122 seconds. Every run observed one or two application connections, zero lost
acknowledged state/events, and six-of-six resource cleanup. Registry/image-resolution
time is reported separately from the 4.869–10.878-second topology/drill window. This
upgrades the local single-fault RPO evidence; production controller/fence integration,
true split-brain, multiple/simultaneous failure, storage corruption, production sync-
latency/availability policy, and production-managed rejoin/rotation remain open.

### Old-primary rejoin and round-trip role-rotation regression

```bash
uv run pytest tests/test_postgres_role_rotation_probe.py -q
uv run python scripts/postgres_role_rotation_probe.py \
  --allow-topology-create --image postgres:17-alpine \
  --timeout 90 --outage-timeout 1
```

The gate requires checksums, `full_page_writes`, `remote_apply` on the actual store
connection, and an exact forced-checkpoint/replay boundary before each cutover. It
must abruptly fence and promote once, rewind/rejoin that former primary read-only and
synchronous, commit a newer state/fence, cleanly fence and promote back, rewind/rejoin
the other former writer, and compare exact final state/events through existing and
fresh pools. Both no-writer windows are bounded. The rewind helper must touch only an
exact stopped owned volume, persist exactly one password-free `primary_conninfo`, and
require a fresh base backup after any rewind failure.

Five hardened runs passed on 2026-08-12: no-writer 1.001–1.005 seconds, promotion
0.115–0.125 seconds, rewind 0.201–0.603 seconds, pool recovery within one to six
observations, one or two connections, zero observed acknowledged loss, and eight-of-
eight cleanup. This closes the exact local two-node double-rewind regression, not
automatic election/fencing, split brain/quorum, multiple faults, endpoint discovery,
archive/large-data recovery, or production RPO/RTO.

### Dual-writer application-authority regression

```bash
uv run pytest tests/test_postgres_writer_authority.py \
  tests/test_postgres_writer_authority_etcd.py \
  tests/test_etcd_writer_authority_probe.py \
  tests/test_etcd_secure_quorum_probe.py \
  tests/test_postgres_split_brain_authority_probe.py -q
uv run python scripts/etcd_writer_authority_probe.py \
  --allow-container-create \
  --image quay.io/coreos/etcd@sha256:dfd3941bf6ced5fdb700f9b2d98b22b7bca7ceee13aec16224f93ff30d9a59c4 \
  --timeout 60 --lease-ttl 3
uv run python scripts/postgres_split_brain_authority_probe.py \
  --allow-topology-create --image postgres:17-alpine \
  --etcd-image quay.io/coreos/etcd@sha256:dfd3941bf6ced5fdb700f9b2d98b22b7bca7ceee13aec16224f93ff30d9a59c4 \
  --timeout 90 --outage-timeout 1
uv run python scripts/etcd_secure_quorum_probe.py \
  --allow-topology-create \
  --image quay.io/coreos/etcd@sha256:dfd3941bf6ced5fdb700f9b2d98b22b7bca7ceee13aec16224f93ff30d9a59c4 \
  --timeout 90 --lease-ttl 15
```

The live gate must first prove two unguarded pools can commit different rows on the two
writable timelines. The guarded phase must bind every transaction to a fresh external
etcd revision and exact `cluster_name`, deny the stale writer, commit only through the
named writer, deny both writers when the exact etcd process stops, recover at the same
endpoint, roll back a mutation when its lease/revision changes immediately before
commit, and abort an over-lease transaction using PostgreSQL's server timeout. The
standalone gate separately proves revision advancement, revoke, natural expiry, and
control-plane loss against the immutable official etcd 3.6.14 image. The secure
three-voter gate additionally proves unique client/peer certificates, mTLS, TLS 1.2,
RBAC, exact-key positive and negative permissions, endpoint-bound gateway token
authentication, one-voter availability, majority-loss fail closure, recovery through a
different member endpoint, and monotonic fence advancement. The first hardened
combined run passes with stale denial at 0.004 seconds, outage denial at 0.002–0.003
seconds, timeout at 0.516 seconds, commit-boundary rollback, one connection per pool,
and seven-of-seven cleanup. The first secure run passes in 7.328 seconds, denies
majority loss as `etcd_timeout` in 1.003 seconds, advances fence 3→4, and removes all
four owned resources plus its temporary certificate workspace.

Passing certifies the owned local mTLS/RBAC/quorum regression, not the external
election/controller, a durable multi-AZ production topology, network-partition or
latency chaos, certificate/key rotation, endpoint discovery, arbitrary-client/physical
fencing, host pause, backup/restore, or production SLOs. Those require a production
deployment controller plus watchdog/STONITH and separate chaos evidence.

### Concurrency isolation

Run randomized overlapping tasks with distinct:

- tenants/users/sessions;
- active skills and allowed tools;
- schema overrides and argument bindings;
- system/workspace context;
- permission approvers and preapprovals;
- learning namespaces.

Record any cross-run observation as a critical release blocker.

### Tool discovery

For catalogs of 10, 50, 200, and 1,000 tools, measure:

- correct tool/schema selected;
- calls to nonexistent tools;
- tokens and latency before first useful action;
- false activation rate;
- capability search recall;
- policy correctness after lazy loading.

Compare eager schemas, lexical/semantic search, and provider-native deferred tools where
available.

### Context and artifact fidelity

Generate oversized logs, JSON, web pages, media metadata, and subagent reports. Require
the agent to retrieve facts from the head, middle, and tail after spill and compaction.
Measure retrieval correctness and context cost.

### Stateful learning

Use paired control/treatment trajectories:

- personal preference remembered across sessions;
- session plan retained across compaction/restart;
- reusable insight learned, recalled, and applied by a different session in the same
  allowed scope;
- same query in another tenant cannot retrieve it;
- contradictory outcome reduces confidence or quarantines the insight;
- deletion prevents future recall.

Use deterministic graders first and model judges only for dimensions that cannot be
expressed deterministically.

### Harness self-improvement

Use a propose-evaluate-accept benchmark with immutable control-plane inputs:

1. collect full trajectory files and produce per-run causal diagnoses;
2. aggregate recurrent failures without merging distinct mechanisms that share a
   superficial status;
3. propose bounded changes against explicit component manifests and preserve a
   falsifiable prediction plus regression risks;
4. run held-in, held-out, safety, cost/latency, complexity, and frozen-transfer cases;
5. retain qualified candidates on a Pareto frontier and retain rejected/negative
   candidates for future diagnosis;
6. verify evaluator/model/config/budget digests did not change and that no forbidden
   control-plane file was written.

This benchmark certifies an experimental improvement policy, not stable autonomous
source-code mutation. See the
[Lilian Weng research reconciliation](lilian-weng-harness-audit.md). The preview
[`ImprovementEvaluationGate`](self-improvement-evaluation.md) now implements the
immutable record, held-in/out/transfer comparability, frozen-control, budget,
judge-audit, novelty/diversity, and five-objective Pareto contracts. The first-party
preview runner adds fresh-resource paired execution, exact scoped evidence verification,
per-case artifacts, live budgets, and paired 95% confidence bounds. A public
provider-neutral adapter now executes host-supplied fresh Agno Agents through the same
runner while preserving the independent verifier and frozen gates. A content-free
versioned corpus manifest now binds exact ordered cases, source/usage/retention evidence,
split exposure and semantic lineage, independent curation, selection/sampling/access
controls, and a zero-known/unresolved-overlap audit before any subject is built; the
default gate rejects ungoverned reports. Runner schema 1.2 adds paired, digest-bound
fresh-process subjects with empty-by-default environment, temporary working directories,
bounded streams, timeout cleanup, and POSIX descendant reaping. A strict Docker subject
adds immutable image/platform verification, no network/environment/mounts, read-only
root, non-root execution, zero capabilities, `no-new-privileges`, built-in seccomp,
resource limits, and exact-owner cleanup. Re-run its deployment proof with
`scripts/docker_evaluation_probe.py --allow-live-docker`. VM isolation, controlled
provider egress and credential brokering, a managed corpus registry with enforced
sealed-case ACLs, semantic near-duplicate tooling, richer statistical policies, and
measured multi-provider/long-duration model-backed benefit evidence remain release
gates. The local Agno learning-benefit gate below is the first narrow model-backed
proof; it does not waive those broader gates.

## Live Agno learning-benefit gate

`scripts/learning_benefit_probe.py` measures whether Agno Learned Knowledge changes
task outcomes, rather than treating a successful save or retrieval as benefit:

```bash
uv run --isolated --extra local --extra rag python \
  -W error::ResourceWarning \
  scripts/learning_benefit_probe.py \
  --allow-live-model
```

The probe uses synthetic facts only. It saves them through Agno's actual
`LearningMachine.learned_knowledge_store`, retrieves them through vector-backed
`Knowledge`, and runs six fresh candidate/control Agent pairs through the first-party
`ImprovementEvaluationRunner`. The candidate receives read-only
`<relevant_learnings>` context with model-facing search/save tools disabled; the
control has no learning. Model, instructions, prompts, decoding controls, budgets, and
objective verifier are otherwise identical. Two cases belong to each of held-in,
held-out, and alias-transfer; execution order alternates. The verifier sees the
expected synthetic token, but `_input_builder` never sends it to either model.

Safety boundaries:

- `--allow-live-model` is mandatory;
- Ollama defaults to loopback, and a non-loopback origin additionally requires
  `--allow-remote-ollama`;
- the exact resolved model and embedder tags plus their Ollama content digests enter
  the frozen model-config evidence;
- stdout is one content-free JSON record; dependency logs go to stderr;
- the documented `uv run --isolated` invocation keeps optional vector/model packages
  out of the project's normal development environment;
- detailed prompts/responses live only in scoped case artifacts. They are discarded
  with the temporary directory unless the operator supplies an empty
  `--evidence-dir`; a non-empty directory is refused rather than overwritten;
- candidate qualification additionally requires positive benefit with no loss in all
  three slices, beyond the normal improvement gate.

The retained 2026-08-16 local configuration used Agno 2.9.0,
`qwen3:0.6b` at
`sha256:7df6b6e09427a769808717c0a93cadc4ae99ed4eb8bf5ca557c90846becea435`,
and `nomic-embed-text:latest` at
`sha256:0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f`.
Three consecutive runs of the frozen decoding configuration qualified: each produced
6/6 paired wins, zero losses, mean delta `+1.0`, and a paired 95% interval of
`[1.0, 1.0]` in every slice. The final observed model-config digest was
`sha256:9e8f073e13f313b768909fb877659d70d584766a44e47a66244990a287b42c0a`.

This is a mechanism/task smoke for one exact local model and vector backend. It is not
evidence for arbitrary prompts, providers, models, production data, long-running
learning drift, prior-version superiority, deletion, or production failover. Those
need larger sealed corpora, power-aware statistics, previous-version and no-learning
controls, more providers/task classes, and repeated production-duration runs. Agno's
upstream contracts are documented in
[Learned Knowledge](https://docs.agno.com/learning/stores/learned-knowledge),
[Learning modes](https://docs.agno.com/learning/learning-modes), and
[Knowledge](https://docs.agno.com/knowledge/overview).

## Competitive comparison rules

Comparison matrices must use scenarios and evidence:

- compare a behavior, not a feature name;
- link the upstream primary source and exact tested version/commit;
- record configuration and model;
- distinguish built-in, extension, and platform-provided capability;
- distinguish conversation continuation, checkpoint resume, and workflow durability;
- include overhead and failure behavior;
- label unknown instead of assuming absence;
- update or remove stale claims.

An example row:

| Behavior | agnoclaw | Comparator | Evidence |
|---|---|---|---|
| Resume after process death during a tool call without duplicating an unknown effect | planned | Pydantic Step Persistence records `unknown_after_crash`; full graph-state resume explicitly out of scope | linked scenario and upstream doc |

This is more useful than marking both products “durable: yes.”

## CI lanes

Only the pull-request and release lanes exist in `.github/workflows/` today
(`ci.yml`, `publish.yml`); the nightly and weekly lanes below are the target
structure, not scheduled workflows.

### Pull request

- unit and contract tests;
- docs links and examples;
- deterministic scenario subset;
- security static checks;
- Agno 2.9.0 primary plus affected compatibility fixtures.

### Core coverage gate

The per-commit suite enforces `--cov-fail-under=80` on the core scope defined in
`pyproject.toml`. Since 2026-08-20 that scope excludes the optional service
adapters exercised in dedicated dependency/PostgreSQL lanes
(`runtime/http_lifecycle.py`, `runtime/postgres_store.py`,
`learning_postgres.py`), so measurements from before and after that change
(for example 82.61% vs 82.42%) are not directly comparable.

### Nightly

- current supported models with strict cost caps;
- Agno 2.6.4 legacy, primary stable, newest Agno 3 prerelease, and current MCP;
- concurrency and chaos subsets;
- long-context and tool-catalog benchmarks;
- learning isolation suite.

### Weekly

- upstream Agno release scan with machine-readable adopt/adapt/avoid delta;
- hours-long soak and restart suite;
- full red-team corpus;
- multi-worker durable backend tests;
- performance trend report;
- flaky scenario quarantine review.

### Release candidate

- all stable capability gates;
- newest stable Agno full certification and Agno 3 adoption/deferral decision;
- upgrade/downgrade and stored-schema migration tests;
- example and documentation execution;
- compatibility matrix refresh;
- known limitations and maturity table refresh;
- signed benchmark report retained with the release.

### Provider-free public API journey

Before packaging, run the source-side public grammar gate:

```bash
uv run pytest tests/test_public_api_journey_probe.py -q
uv run python scripts/public_api_journey_probe.py
```

It must complete quick, durable session/reopen, governed candidate evaluation/promotion,
and local migration/rollback using top-level public imports only; close every owned model;
emit no response, learning, user, artifact, or path content; refuse nonempty operator
roots; and report zero provider network calls. See [Provider-free public API
journey](public-api-journey.md) for the exact oracle and limitations.

The release workflow reruns the same file unchanged against the exact wheel inside a
read-only Docker container with `--network none`. The same installed, network-denied
container executes the four-crash real-process restart probe. This is bounded
single-host release evidence, not a live-provider or production-resilience claim.

## Documentation gates

A public feature is not complete until documentation includes:

- purpose and non-goals;
- minimal example;
- complete reference for public inputs/outputs/errors/events;
- short versus durable semantics;
- security and data-retention behavior;
- concurrency guarantees;
- supported versions/backends;
- limitations and failure behavior;
- conformance test or evaluation link;
- migration/deprecation guidance where applicable.

Documentation examples must use public APIs. Reaching into `harness._agent` is a signal
that the public harness contract is incomplete.

## Continuous intelligence loop

To stay competitive:

1. Monitor Agno, MCP, Agent Skills, OpenTelemetry GenAI conventions, and the primary
   harness sources in [World-class harness strategy](world-class-harness.md).
2. Triage each upstream change as adopt, adapt, delegate to platform, or reject.
3. Reproduce useful behavior in a scenario before planning an implementation.
4. Add the scenario and regression budget first.
5. Implement behind a capability/profile boundary.
6. Promote maturity only after the full gate passes.
7. Remove or simplify defaults when evidence shows low value or excess overhead.

This loop is the practical replacement for a promise to be “worry-free forever.”

## Primary references

- [Agno evaluations](https://docs.agno.com/evals/overview)
- [Agno release-practice audit](agno-release-practices.md)
- [Agno 2.9.0](https://github.com/agno-agi/agno/releases/tag/v2.9.0)
- [Agno 3.0.0a1](https://github.com/agno-agi/agno/releases/tag/v3.0.0a1)
- [Agno Agent Platform evaluations](https://docs.agno.com/tutorials/agent-platform/evals)
- [Letta stateful-agent evaluation concepts](https://docs.letta.com/guides/evals/concepts/overview)
- [LangGraph persistence and recovery](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Pydantic AI Harness step persistence](https://pydantic.dev/docs/ai/harness/step-persistence/)
- [OpenAI Harness engineering](https://openai.com/index/harness-engineering/)
- [Anthropic long-running harnesses](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [Lilian Weng: Harness Engineering for Self-Improvement](https://lilianweng.github.io/posts/2026-07-04-harness/)
- [Lilian Weng: Reward Hacking in Reinforcement Learning](https://lilianweng.github.io/posts/2024-11-28-reward-hacking/)
