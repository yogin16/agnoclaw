# Agno release-practice audit

Status: primary-source compatibility and architecture input for agnoclaw 0.12.0

Research date: 2026-08-08; live release/package recheck: 2026-08-28;
supported evaluation/learning API source recheck: 2026-08-28

Live-source recheck on 2026-08-28: Agno 3.0.1 is the latest stable package. Agno's live documentation index and
Learning pages still define vector-backed Learned Knowledge, semantic
`<relevant_learnings>` context injection, Agentic as its default mode, and
global/user/custom namespaces. The rendered release page can be stale; release
admission uses exact repository/package evidence and tested artifacts, never a cached
marketing page.

Scope: every stable Agno release from 2.6.0 through 3.0.1, the earlier 3.0.0a1
source audit, and upstream changes that materially affect an embeddable agent
harness. Provider-only additions were reviewed but are recorded here only when they
change a harness contract.

## Executive decision

Agno is not merely a model wrapper underneath agnoclaw. It is a rapidly changing
runtime that now includes learning, human review, background work, scheduling,
protocol surfaces, evaluation environments, filesystem isolation, traces, a durable
job queue, and normalized run persistence.

agnoclaw should therefore use four rules:

1. **Adopt proven Agno primitives.** Prefer an upstream primitive when it meets the
   agnoclaw contract under process-kill, isolation, migration, and load tests.
2. **Adapt at one boundary.** All version-sensitive Agno behavior belongs behind a
   tested compatibility adapter and capability manifest, never scattered `hasattr`
   fallbacks.
3. **Retain agnoclaw guarantees.** The canonical run identity, ordered event cursor,
   operation settlement, tenant scope, policy decisions, and learning provenance must
   remain valid even when an Agno implementation changes.
4. **Do not support by assumption.** Connection reconnection is not process recovery;
   persisted cancellation is not resumable execution; an instructional learning mode
   is not a code-enforced promotion gate.

The current upstream learning guidance also reinforces an agnoclaw-specific safety
choice: Agno documents Agentic Learned Knowledge as exposing `search_learnings` and
`save_learning`, while the default namespace can be global. Agnoclaw's institutional
model path therefore uses explicit trusted namespaces, recall-only context, and no
model-facing save authority; reviewed promotion remains a separate host action. The
local benefit gate exercises the actual vector store/context injection with tools
disabled instead of weakening that boundary merely to match the upstream quickstart.

The production development baseline and lock are Agno 3.0.1. Agno 2.6.4 remains the
legacy boundary and 2.9.0 remains a required stable-v2 lane. Stable 3.0.1 passed the
full token-free contract suite and deterministic operation, tool-checkpoint, approval,
outer-model, and learning process-restart probes before admission. The central
capability report and all three required CI lanes are implemented; this document
remains the release-delta ledger for future bumps.

## What the release history teaches us

### Agno evolves across coupled surfaces

Several releases changed defaults, serialized component shapes, authentication,
session semantics, human-review metadata, or tool exposure. A simple import test is
not a compatibility program. agnoclaw needs contract tests for:

- construction, rehydration, and serialization of Agent, Team, Toolkit, and Workflow;
- session creation, continuation, cancellation, forking, and concurrent ownership;
- sync, async, streaming, and background execution;
- human-review request, persisted wait, actor attribution, edited arguments, and
  continuation;
- learning store configuration, extraction, CRUD, isolation, and deletion;
- tool instructions, structured results, server-managed tools, and async generators;
- AgentOS REST, MCP, WebSocket, AG-UI, and A2A identity propagation;
- filesystem containment across traversal, symlink, Unicode, and Windows path cases.

### Reliability features arrive incrementally

The history repeatedly adds one missing half of a behavior: background reconnect,
then cancellation persistence; workflow continuation, then approval cleanup; component
serialization, then rehydration; scheduler support, then event-loop safety. agnoclaw
must test complete user outcomes rather than infer guarantees from feature names.

### Security defaults cannot be treated as stable policy

Agno has steadily hardened JWT subject binding, MCP authorization, DNS-rebinding
protection, filesystem containment, SQL inputs, and cross-user isolation. agnoclaw
still needs deterministic policy at every boundary because some isolation remains
opt-in and because optional protocols can expose the same run through different
authentication stacks.

### Agno 3 may remove substantial duplication

The 3.0 alpha's per-run rows, queue, worker fencing, cancellation channel, and bounded
queue configuration overlap with planned agnoclaw adapters. We should not rush an
alpha into production, but we should test it early enough to avoid building a second
generic queue or session migration system unnecessarily.

## Release-by-release audit

The “agnoclaw response” column is normative for the 0.12 plan. Links go to the
corresponding official GitHub release.

| Release | Harness-relevant upstream change | agnoclaw response and required evidence |
|---|---|---|
| [2.6.0](https://github.com/agno-agi/agno/releases/tag/v2.6.0) | Added `agno.context`, Agent/Team/Workflow factories, AgentOS background SSE reconnection, Agent Protocol beta, workflow/team human review, and session-type discovery. | Use factories only behind immutable per-run construction. Test disconnect/reconnect separately from worker death. Treat Agent Protocol as an adapter, not the kernel. |
| [2.6.1](https://github.com/agno-agi/agno/releases/tag/v2.6.1) | Added Claude multi-block prompt caching, deterministic tool ordering, Parallel MCP, and changed OpenAI-prefixed model routing toward Responses. | Keep prompt prefixes stable, snapshot resolved model/provider capability, and golden-test tool order and provider routing. Never infer a provider API solely from a model-name prefix. |
| [2.6.2](https://github.com/agno-agi/agno/releases/tag/v2.6.2) | Added rooted WorkspaceTools with destructive human review by default, changed model defaults, and fixed preservation of review state under deep copy. | Candidate workspace adapter only after agnoclaw path/effect tests. Pin explicit model policy. Add clone-isolation and approval-persistence tests. |
| [2.6.3](https://github.com/agno-agi/agno/releases/tag/v2.6.3) | Introduced WorkspaceContextProvider and simplified Slack context with explicit flags and instruction ownership. | Keep context acquisition separate from prompt policy; record every provider contribution in the context manifest. |
| [2.6.4](https://github.com/agno-agi/agno/releases/tag/v2.6.4) | Added WikiContextProvider with filesystem, Git, web, read, and write behavior. This is the repository's starting lock. | Retain as a temporary legacy conformance lane. Require declared network/filesystem effects and policy mediation for context providers. |
| [2.6.5](https://github.com/agno-agi/agno/releases/tag/v2.6.5) | Added Gmail/Calendar context, Mongo scheduler support, workflow cancellation persistence, memory identity fields, URL allowlists, and an MCP JWT-user binding fix. | Persisted cancellation is tested as state, propagation, and resource cleanup. Identity is injected from trusted scope. URL allowlists and MCP subject binding receive adversarial tests. |
| [2.6.6](https://github.com/agno-agi/agno/releases/tag/v2.6.6) | Integrated LearningMachine prompts into teams, expanded `/continue`, warned on duplicate tools, improved approval UI and JWT subject propagation, and quarantined incompatible dependencies. | Test learning prompt ownership in nested teams, continuation metadata, duplicate capability IDs, subject propagation across REST/MCP/WebSocket, and incompatible optional extras. |
| [2.6.7](https://github.com/agno-agi/agno/releases/tag/v2.6.7) | Added opt-in per-user AgentOS isolation, URL-knowledge allowlists, trace-parent identity fixes, async workflow review cleanup, and Gemini stateful interactions. | Service mode remains fail-closed even when upstream isolation is optional. Test cleanup after disconnected reviews and provider-managed state boundaries. |
| [2.6.8](https://github.com/agno-agi/agno/releases/tag/v2.6.8) | Centralized path safety across symlinks, Unicode, and Windows; added managed background research reconnect; repaired long-standing AG-UI team events and provider server-tool history. | Reuse path helpers where possible but retain backend escape tests. Treat managed background jobs as a capability with separate recovery proof. Add protocol event-sequence fixtures. |
| [2.6.9](https://github.com/agno-agi/agno/releases/tag/v2.6.9) | Persisted approval actor/time metadata, honored zero-valued Claude controls, simplified instruction layers, and stopped surfacing server-managed tools as local tools. | Record reviewer identity in the canonical event. Test zero as a meaningful configuration value. Diff prompt layers and distinguish local from provider-managed operations. |
| [2.6.10](https://github.com/agno-agi/agno/releases/tag/v2.6.10) | Persisted cancelled runs and output files, emitted context-provider subagent events, improved registries, MCP failure resilience, and async behavior. | Reconcile Agno files into ArtifactStore handles, preserve child lineage, and chaos-test partial MCP connection and cancellation settlement. |
| [2.6.11](https://github.com/agno-agi/agno/releases/tag/v2.6.11) | Promoted Parallel with task monitoring and introduced Manifest. | Compare Manifest with `CapabilitySpec`; adapt rather than create two public capability schemas. Enforce fan-out, budgets, cancellation, and child artifacts. |
| [2.6.12](https://github.com/agno-agi/agno/releases/tag/v2.6.12) | Added AG-UI state events, RBAC/observability examples, and agentic-state schema support. | Normalize external events into the agnoclaw envelope and verify authorization independently on each protocol. |
| [2.6.13](https://github.com/agno-agi/agno/releases/tag/v2.6.13) | Improved context-provider child events, AgentOS registry loading, workflow review sockets, MCP session close, MultiMCP failure cleanup, and component hashes. | Test close/`aclose`, socket disconnect, partial multi-server startup, and deterministic component identity including metadata. |
| [2.6.14](https://github.com/agno-agi/agno/releases/tag/v2.6.14) | Added AgentOS learning CRUD and fixed Gemini concurrency and structured follow-up instructions. | Adopted through trusted-context `LearningAdminGateway` for personal/session records only. It bypasses exception-swallowing store convenience methods, uses direct sync/async database CRUD, validates schemas and opaque identity, and post-verifies replace/forget. Institutional writes remain governed candidates. |
| [2.6.15](https://github.com/agno-agi/agno/releases/tag/v2.6.15) | Added custom, scoped, identity-aware AgentOS MCP tools; hid JWT subject from model-visible arguments; introduced authorization callbacks and DNS-rebinding controls; refined dependency merging. | Adopt hidden trusted bindings and scoped authorization. Test allowed hosts/origins, dependency precedence, and that model input cannot substitute protected identity. |
| [2.6.16](https://github.com/agno-agi/agno/releases/tag/v2.6.16) | Declared the Parallel API generally available. | Keep delegation preview until durable lineage, bounded parallelism, failure propagation, and artifact handoff pass agnoclaw gates. Upstream maturity is not inherited automatically. |
| [2.6.17](https://github.com/agno-agi/agno/releases/tag/v2.6.17) | Made component loading more resilient, deduplicated toolkits structurally, and improved approval-record resolution. | Fail loudly on ambiguous capability identity; verify rehydrated config equality and one-time approval settlement. |
| [2.6.18](https://github.com/agno-agi/agno/releases/tag/v2.6.18) | Reconstructed database components by reusing registered live models, preserving credentials and endpoints. | Ensure secrets never enter serialized runtime records. Test rehydration against a trusted host registry and reject identity drift. |
| [2.6.19](https://github.com/agno-agi/agno/releases/tag/v2.6.19) | Added tool-batch checkpoints, unified `/continue` for regeneration/forking, session forking, StudioTools, lazy Gemini loading, team cancellation, and serialization fixes. | Run the mandatory real-process-kill continuation spike. Map fork to a new immutable run with lineage. Do not claim resume until settled-boundary and unknown-operation tests pass. |
| [2.6.20](https://github.com/agno-agi/agno/releases/tag/v2.6.20) | Added ClickHouse traces, structured-output capability flags, MCP `structuredContent`/`_meta`, concurrent workflow step isolation, and token-accounting fixes. | Capability-probe structured output; preserve MCP metadata safely; adversarially overlap steps; reconcile provider usage into budget events. |
| [2.6.21](https://github.com/agno-agi/agno/releases/tag/v2.6.21) | Made LocalFileSystem target-restricted by default, fixed registry MCP tool loss, added trusted workflow-factory identity, propagated cancellation, prevented auth-header leakage, and supported async-generator tools. | Preserve safe filesystem defaults, verify headers never reach prompts/events, and test cancellation plus streaming cleanup for async generators. |
| [2.6.22](https://github.com/agno-agi/agno/releases/tag/v2.6.22) | Added base Toolkit timeouts, more path-traversal fixes, and fallback response persistence. | Set timeouts as explicit budgets, run the full path corpus, and label fallback output versus normally settled model output. |
| [2.7.0](https://github.com/agno-agi/agno/releases/tag/v2.7.0) | Added PAT service accounts, `agnoctl`, evaluation Case runner, `/info`, unified AgentOS auth, A2A auth, and trace ownership; reduced the MCP surface from 19 tools to 8 in a breaking cleanup. | Add protocol discovery and surface snapshots, fail-fast authorization tests, trace ownership checks, and a deliberate migration test for tool removals. Use the Case runner as an evaluation adapter. |
| [2.7.1](https://github.com/agno-agi/agno/releases/tag/v2.7.1) | Lockstep Agno/agnoctl package release with no substantive harness delta documented. | Verify package/version interoperability; record “no contract delta” rather than inventing one. |
| [2.7.2](https://github.com/agno-agi/agno/releases/tag/v2.7.2) | Added AgentOS MCP OAuth, AG-UI client tools, improved connection flows, A2A scopes, safer filesystem knowledge, and `@tool` injection. | Test OAuth audience/scope and client-tool trust direction; forbid injected protected arguments; retain filesystem containment tests. |
| [2.7.3](https://github.com/agno-agi/agno/releases/tag/v2.7.3) | Added Valkey, removed redundant SessionContext model use, improved memory extraction and AG-UI review, and fixed structured MCP/error and filesystem containment cases. | Test each supported store, memory extraction limits/identity, remote review semantics, structured errors, and symlink escape. |
| [2.7.4](https://github.com/agno-agi/agno/releases/tag/v2.7.4) | Rejected duplicate AgentOS sessions, scoped Slack sessions by channel, moved workflow selectors toward `run_context`, surfaced errors, supported long-running Firecracker sandboxes, and fixed Postgres schema handling. | Make same-session concurrency explicit, adapt deprecated `session_state`, test sandbox lease/timeout, and certify non-default Postgres schemas. |
| [2.8.0](https://github.com/agno-agi/agno/releases/tag/v2.8.0) | Added Code/Judge/ToolCall scorers, isolated evaluation environments and rollouts, pass@k, diffs, learning zones, SFT export/provenance, and stricter tool-reliability evaluation. | Use these as evaluation adapters. Preserve fresh identity/storage per case, no-write controls, scorer hardening, provenance, and actual successful-tool-call assertions. |
| [2.8.1](https://github.com/agno-agi/agno/releases/tag/v2.8.1) | Added a learning extraction tool-call limit, streaming context providers, nested-team history, explicit member IDs, a schema-visible optional FileTools directory, and empty-argument fixes. | Expose extraction as a learning budget, stream context through bounded artifacts/events, require stable child IDs, and test optional-directory containment plus empty versus absent arguments. |
| [2.8.2](https://github.com/agno-agi/agno/releases/tag/v2.8.2) | Added a durable, private FileSystem with pluggable database/local storage and fail-closed per-user namespaces. | Evaluate as an ArtifactStore/workspace adapter, not an automatic replacement. Require tenant/session/run ownership, retention, quota, path, and deletion conformance. |
| [2.8.3](https://github.com/agno-agi/agno/releases/tag/v2.8.3) | Removed instructions from FileSystem tools so applications compose policy and guidance themselves. | Follow the same separation: tools declare mechanics/effects; capabilities own instructions; policy remains deterministic. |
| [2.8.4](https://github.com/agno-agi/agno/releases/tag/v2.8.4) | Reworked entity memory as a “second brain,” improved nested executor-requirement serialization, and accepted null path arguments in skill file tools. | Rebaseline entity-memory quality and deletion tests; never assume old extraction semantics. Verify nested capability rehydration and reject or normalize null path inputs without weakening containment. |
| [2.8.5](https://github.com/agno-agi/agno/releases/tag/v2.8.5) | Added read-only AgentOSTools for metrics, schedules, evaluations, and pending reviews; introduced SQLite/Postgres operation statistics; fixed SQL injection. | Prefer read-only operator capabilities, mediate them through policy, and use upstream metrics as projections—not the canonical ledger. Run SQL and tenant-scope adversarial tests. |
| [2.8.6](https://github.com/agno-agi/agno/releases/tag/v2.8.6) | Added background/single-flight metrics refresh, improved hot-path version lookup, UTF-8 handling, audio artifacts, and OpenSearch support. | Adopt single-flight refresh and status visibility patterns. Benchmark import/run overhead and test non-ASCII event/artifact round trips. |
| [2.8.7](https://github.com/agno-agi/agno/releases/tag/v2.8.7) | Moved synchronous scheduler database calls off the event loop; added advisor-model feedback and component-aware schedule/history tools; made FileSystem toolkit identity configurable; improved toolkit rehydration, audio-result handling, and top-level review propagation; fixed SQLite team loading and zero-valued parameters; and exposed a dependency-pin break. | Retain its event-loop stall, schedule/history scope, stable toolkit identity, rehydration, nested review, artifact, SQLite, zero-value, and dependency-resolution gates. Treat advisor output as untrusted evidence subject to evaluator-independence, ordering-bias, and self-preference controls—not approval or proof. |
| [2.9.0](https://github.com/agno-agi/agno/releases/tag/v2.9.0) | Added identity-aware, run-only Studio dispatch with caller `user_id`; made dispatch-path component rehydration strict and honor pinned member versions; bound cached tool results to user/session identity; blocked call-time MCP `tool_name` substitution; persisted paused team member runs; preserved toolkit instructions; and repaired A2A metadata plus selected WebSocket workflow versions. | This is the primary 2.x lane. Reuse the upstream security fixes while retaining agnoclaw's stronger invariant: durable tools use the owner-bound operation ledger rather than Agno result caching, and selected capability identity comes from an admitted immutable spec rather than model arguments. Require strict/fail-loud materialization, exact pinned versions, trusted caller propagation, paused-team restart, rehydrated-instruction equality, A2A metadata, and WebSocket-version fixtures. Never expose Studio mutation tools merely to gain run dispatch. |

Cross-lane inspection also confirms that `VectorDb.name_exists` is public on the
2.6.4 legacy, 2.9.0 stable-v2, and 3.0.1 primary lanes. Agnoclaw uses that exact-name contract for
ambiguous Learned Knowledge reconciliation; it does not treat semantic search as proof
of presence or absence. The candidate digest is embedded in the bounded external key,
and the resulting evidence artifact contains digests and a Boolean only.

The same source-level audit confirms the public async `Agent.arun()` response contract,
`RunOutput.content`, and `RunMetrics.input_tokens`, `output_tokens`, `total_tokens`, and
`cost` on both supported lanes. `AgnoEvaluationSubject` uses only those surfaces to run
a host-supplied fresh Agent inside the paired improvement runner. It preserves JSON-like
content and bounded usage evidence while excluding provider-private response state.
The adapter deliberately does not bind to `AgentAsJudgeEval` internals: model judges
remain optional, calibrated evidence and cannot override agnoclaw's deterministic
safety/privacy/control gates or grant learning-promotion authority. Agno 2.8 evaluation
environments, Cases, and Scorers remain candidates for a later isolation/scorer adapter
after their tenant, cache, learning-write, and reproducibility contracts pass.

## Agno 3.0 architecture and stable adoption

The initial [Agno 3.0.0a1](https://github.com/agno-agi/agno/releases/tag/v3.0.0a1)
audit reviewed tagged source and the migration guide because its release notes were
minimal. Stable [Agno 3.0.1](https://pypi.org/project/agno/3.0.1/) is now a supported
production dependency after exact-package compatibility and restart certification.
That support does not make every upstream queue or persistence primitive an agnoclaw
durability boundary; those surfaces still require their own capability evidence.

### Normalized run persistence

The official
[V3 migration guide](https://github.com/agno-agi/agno/blob/v3.0.0a1/libs/agno/agno/db/migrations/V3_MIGRATION_GUIDE.md)
explains that v2 embedded a session's runs in a JSON blob, producing growing writes and
preventing partial run queries. V3 introduces one row per run, indexed run fields,
direct run reads, and non-destructive, idempotent migration while retaining legacy
data for merged reads and rollback.

agnoclaw response:

- use Agno's normalized run rows as the preferred Agno session projection once stable;
- do not equate them with the agnoclaw RuntimeStore until atomic event, cursor,
  operation, lease, tenant, and migration conformance passes;
- require upgrade, mixed-read, retry, partial-failure, explicit cleanup, and rollback
  tests on SQLite and PostgreSQL;
- never delete legacy data automatically during application startup.

### Durable background job queue

The tagged source adds bounded queue depth and concurrency, retry/backoff, timeouts,
deployment affinity, lock grace, polling, retention, database queue storage, worker
fencing, terminal guards, Redis event streams, and distributed cancellation.

agnoclaw response:

- evaluate the stable queue as a `service` Worker/RuntimeStore adapter before creating
  a parallel generic distributed queue;
- require per-tenant fairness, slow-consumer isolation, operation settlement,
  transactional outbox integration, cursor replay, graceful drain, and p95/p99 load
  gates beyond upstream queue correctness;
- retain a local SQLite worker path so durable embedded use has no infrastructure
  prerequisite;
- publish an explicit capability report showing which guarantees come from Agno and
  which remain agnoclaw-owned.

The retained T2b real-process probe used 3.0.0a1 and reclaimed an expired
PostgreSQL job and fenced the stale attempt, but process death after an external effect
and before settlement caused the reclaimed attempt to repeat that effect. Therefore
[ADR-0001](adr-0001-recovery-ownership.md) assigns the canonical operation/effect ledger
to agnoclaw and keeps the stable Agno 3 queue as a future worker/lease adapter candidate.

### Public API removals and consolidation

The alpha source replaces several flat human-review arguments with a `HumanReview`
object, removes deprecated memory/history arguments, adds stable toolkit IDs,
consolidates AgentOS metadata routes, and further isolates evaluation users.

agnoclaw response:

- keep one agnoclaw review/command contract and map it through a version adapter;
- add compile/type fixtures that fail on removed arguments before runtime;
- make toolkit/capability identity explicit and versioned;
- isolate every evaluation rollout by tenant, user, session, run, database namespace,
  cache, and learning write policy.

## Adopt, adapt, and avoid

### Adopt when conformance passes

- Agno Agent/Team model and tool execution;
- LearningMachine stores and extraction budgets;
- Session Context and tool-batch checkpoints;
- evaluation Scorers, Cases, Environments, and rollouts;
- structured MCP results, authorization callbacks, trusted argument injection, and
  protocol authentication primitives;
- normalized run persistence and the job queue after a stable Agno 3 release passes
  the agnoclaw contract;
- upstream path-safety and private-filesystem helpers as defense in depth.

### Adapt behind agnoclaw contracts

- Agno sessions into `session_id`, immutable agnoclaw runs, and attempt lineage;
- background and continuation behavior into the `HarnessRun` state machine;
- human review into typed commands and durable waiting states;
- Agno events/traces/metrics into ordered events and rebuildable projections;
- LearningMachine writes into candidate provenance, consent, scope, review, deletion,
  and rollback;
- Parallel/teams into bounded child runs with budgets and artifact handoff;
- FileSystem into scoped ArtifactStore/workspace interfaces;
- the Agno 3 queue into service Worker/RuntimeStore interfaces.

### Avoid

- private Agno reach-through from CLI, TUI, AgentOS, scheduler, examples, or plugins;
- version checks spread through product code;
- treating SSE reconnect or database status as proof of process recovery;
- accepting optional or instructional isolation as the service security boundary;
- serializing live credentials or opaque Python callables for durable execution;
- admitting prereleases through a broad dependency range;
- implementing a second generic queue, evaluator, memory store, or filesystem before
  testing the corresponding upstream primitive;
- allowing automatic learning promotion, decay, prompt mutation, policy mutation, or
  skill mutation in the stable default.

## Compatibility and release contract

During development:

- primary lane: Agno 3.0.1;
- stable-v2 lane: Agno 2.9.0;
- legacy lane: Agno 2.6.4, the repository's starting lock and published minimum;
- preview lane: the next major prerelease, excluded from normal dependency resolution;
- weekly upstream release scan and a machine-readable adopt/adapt/avoid delta;
- dependency resolver, import, construction, sync/async/streaming, serialization,
  process-kill, learning, protocol, and security contract suites.

At release-candidate freeze:

1. Rebase the primary lane to the newest stable Agno release only after exact-package
   full-suite, restart, migration, and artifact checks pass.
2. Retain older stable boundaries when they do not compromise the public contract.
3. If a new stable major fails a critical contract, keep the published upper bound
   below it rather than advertise support falsely.
4. Keep prereleases out of normal dependency resolution and publish their evidence as
   certification signal only.
5. Generate and sign a compatibility manifest containing exact dependency versions,
   capability probes, unsupported paths, and evidence links.

No release is supported merely because installation succeeds. A supported cell means
the documented agnoclaw behavior passes its full conformance gates.

## Primary sources

- [Agno changelog](https://www.agno.com/changelog)
- [Agno documentation index](https://docs.agno.com/llms.txt)
- [Agno Learned Knowledge](https://docs.agno.com/learning/stores/learned-knowledge)
- [Agno Learning modes](https://docs.agno.com/learning/learning-modes)
- [Agno GitHub releases](https://github.com/agno-agi/agno/releases)
- [Agno 2.9.0](https://github.com/agno-agi/agno/releases/tag/v2.9.0)
- [Agno 2.8.7](https://github.com/agno-agi/agno/releases/tag/v2.8.7)
- [Agno 3.0.1](https://pypi.org/project/agno/3.0.1/)
- [Agno 3.0.0a1](https://github.com/agno-agi/agno/releases/tag/v3.0.0a1)
- [Agno V3 database migration guide](https://github.com/agno-agi/agno/blob/v3.0.0a1/libs/agno/agno/db/migrations/V3_MIGRATION_GUIDE.md)
