# Learning and self-improvement

Status: current v0.12 preview contract, legacy migration, and remaining gates

Last verified against Agno 2.6.4, 2.9.0, and 3.0.1 contract lanes

Research date: 2026-08-08; upstream/live implementation evidence rechecked: 2026-08-18

This guide defines how `agnoclaw` should use Agno's Learning Machine safely and how
learning differs from session history, workspace memory, and skill improvement.

## Current implementation and limits

The recommended v0.12 API is an immutable policy plus a scope resolved from trusted
run identity:

```python
from agnoclaw import AgentHarness, ExecutionContext, LearningProfile

harness = AgentHarness(
    learning=LearningProfile.personal_and_session(
        user_profile="always",
        user_memory="agentic",
        session_context="always",
        max_updates_per_run=5,
    )
)
context = ExecutionContext.create(
    tenant_id="acme",
    user_id="user-123",
    session_id="case-42",
    workspace_id="support",
)
result = await harness.arun(
    "Help with my account",
    context=context,
    learning_consent=True,
)
```

`LearningPolicy` is frozen and contains intent, modes, update bounds, retention, consent,
and promotion policy—not caller identity. Immediately before the model call,
`LearningScope` validates the trusted `ExecutionContext` and derives deterministic,
collision-resistant opaque Agno namespace/user/session keys. A new LearningMachine and
Agent view are materialized for that admitted scope. Missing tenant, user, session, or
consent fails with a stable `LEARNING_SCOPE_*`/`LEARNING_CONSENT_REQUIRED` error before
model dispatch. The safe scope descriptor is recorded with `run.started`; raw identity is
not embedded in its storage keys.

Institutional policy requires an explicit namespace and vector-backed Knowledge:

```python
from agno.knowledge import Knowledge
from agnoclaw import AgentHarness, LearningProfile

harness = AgentHarness(
    name="researcher-v2",
    learning=LearningProfile.institutional(
        namespace="cloud-architecture",
        knowledge=Knowledge(vector_db=vector_db),
        learned_knowledge="agentic",
        decision_log="agentic",
        promotion="reviewed",
    ),
)
```

Institutional stores are recall-only on the direct Agno path. The proposing model has no
direct save authority: Entity Memory tools are disabled, and Learned Knowledge/Decision
Log expose search without save. Their writes are declared `candidate`. The v0.12 preview
now has artifact-backed SQLite/PostgreSQL candidate ledgers, exact-scope host APIs,
evidence evaluation, immutable supersession edits, quarantine/tombstones, reviewed
promotion, and intent-first rollback. See
[Governed learning candidates](learning-candidates.md).

Both SQLite and bounded-pool PostgreSQL candidate ledgers implement the same domain
contract. The built-in reversible Agno adapter is intentionally narrower than the policy: it can
promote and roll back uniquely named Learned Knowledge. Entity Memory merges and
Decision Log writes fail closed until a genuinely reversible adapter exists.
Evidence-backed manual reconciliation, restart-safe owner-bound discovery, and a
bounded host-observer coordinator are implemented. Schema-v12 durable scheduling can
now execute a consented job through the same lifecycle and learning policy. The
coordinator verifies immutable
evidence bytes and exact scope, binds observations to one candidate revision, and
settles by CAS without replaying the effect. The first-party Agno Learned Knowledge
observer now checks the exact candidate-derived vector name and stages content-free
evidence; `AgentHarness` selects it automatically when its default promotion adapter is
active. A dedicated owner-scoped worker now adds SQLite/PostgreSQL database-clock
leases, monotonic takeover fences, heartbeat renewal, and durable sweep cursors without
replaying the ambiguous effect. Independent-pool exclusion and actual process-death
reclaim now have a retained oracle: a worker dies inside observation, an active-lease
steal is denied, a replacement advances fence 1→2 after database-clock expiry,
reconciles once with zero promotion redispatch, and releases cleanly. The shared
owned two-node PostgreSQL gate now also preserves an evaluated
candidate across measured lag and fenced planned promotion, fails the learning pool
closed during the no-writer interval, reconnects existing/fresh pools, and commits one
post-promotion transition with contiguous events. Custom-backend observers and
production database partition/failover/soak certification are still open. Personal/session
stores now have exact-scope
host read/replace/forget with schema validation and a post-operation active-database
read, but its receipt proves only point-in-time absence—not cross-process writer
fencing or backup purge. Content-safe local and PostgreSQL migration lifecycles now
cover bounded preflight, transformation, apply, independent verification, cutover
receipt, and exact rollback. Service-wide deletion/retention proof, production
migration certification and production reconciliation-worker certification remain
release gates. A narrow local model-backed Learned Knowledge versus no-learning proof
now passes; previous-version, multi-provider, larger-corpus, and long-duration benefit
certification remain open, so this preview still does not claim complete
self-improvement. See
[Personal and session learning administration](learning-administration.md).

Harness-change experiments now have a separate first-party preview runner: it executes
fresh-resource baseline/candidate pairs, verifies exact scoped upstream artifacts,
stages per-case evidence, enforces the frozen experiment budget, and emits paired 95%
confidence records for the pure evaluation gate. A provider-neutral public adapter now
executes host-supplied fresh Agno Agents and projects JSON-like content plus bounded
token/cost metrics into that runner on both supported Agno lanes. This does not add
automatic learning promotion, managed/enforced sealed-corpus operations, or
general model-backed benefit evidence. The opt-in local probe described below now
provides one exact model/embedder/corpus benefit smoke. Runner schema 1.2 now adds
one fresh local child per rollout with empty-by-default environment, temporary working
directory, bounded protocol streams, contract digests, and POSIX process-group cleanup.
A strict Docker subject adds immutable exact-platform images, no network/host
environment/mounts, read-only non-root execution, zero capabilities, built-in seccomp,
resource limits, and exact-owner cleanup. Provider-network/credential-broker and VM
profiles remain deployment work.
A content-free corpus manifest now binds
exact cases, provenance, exposure/lineage, independent curation, and decontamination
evidence; the default evaluation gate refuses ungoverned qualification. See
[Evidence-gated harness self-improvement](self-improvement-evaluation.md).

The storage-shaped `enable_learning`, `enable_user_memory`, `enable_session_context`,
`learning_mode`, `learning_namespace`, and `learning_knowledge` parameters remain as a
direct-Agno compatibility adapter. Enabling that adapter emits a `DeprecationWarning`;
removal is no earlier than 0.14.0. Mixing it with `learning=LearningPolicy` fails with
`LEARNING_CONFIGURATION_CONFLICT`. Invalid legacy modes now fail with
`LEARNING_MODE_UNSUPPORTED` rather than silently becoming Agentic.

Additional current limitations:

- the legacy adapter still uses booleans/defaults, a harness-name namespace, and direct
  Agno writes; use it only while migrating;
- the nonexistent periodic `LearningMachine.optimize_memories()` path and its maintenance
  promise have been removed; no Curator operation is claimed until its behavior is
  certified across both supported Agno lanes;
- Agno's Curator support is version-sensitive. The Agno documentation notes a User
  Profile schema limitation in 2.7.2; maintenance must be verified against each supported
  Agno version rather than assumed;
- workspace `MEMORY.md`/daily logs, Agno Learning Stores, and the `.learnings/`
  self-improving skill are separate systems with no shared provenance or conflict rules;
- the bundled self-improving skill is not automatically invoked by the runtime. It runs
  only when explicitly activated or when a model with an actual activation path invokes
  it in a future implementation.
- a scheduled job receives learning write consent only when its trusted host definition
  opts in. Consent does not create scope, enable stores, bypass update budgets, or grant
  institutional promotion; see [Durable scheduling](durable-scheduling.md).

Until the remaining T8/T12 CRUD, reconciliation automation, stronger VM/provider-egress,
richer statistical-policy, and broad model-backed benefit gates land:

- prefer `LearningProfile.personal*` with explicit trusted identity and consent;
- treat Entity Memory, Decision Log, and Learned Knowledge as experimental and inspect
  their behavior against the certified Agno version;
- provide a real vector-backed `Knowledge` and run behavioral save/recall/delete tests
  for your selected vector database before production use;
- do not use implicit namespace defaults for sensitive multi-tenant data;
- use workspace memory as transparent operator-managed context, not as proof that Agno
  learning is functioning;
- keep high-stakes learnings behind application-enforced review.

## Recent Agno learning changes that affect the target

The prerequisite validation above is verified against the 2.6.4 legacy, 2.9.0
stable-v2, and 3.0.1 primary lanes. Recent releases still change the implementation plan:

- Agno 2.6.5 added memory identity fields; 2.6.6 integrated LearningMachine context
  into teams; 2.6.14 added AgentOS learning CRUD. agnoclaw must preserve trusted scope
  and provenance across both direct and AgentOS paths.
- Agno 2.8.0 added isolated evaluation environments, learning zones, provenance-aware
  exports, and scorers. agnoclaw should use these for learning-vs-control evaluation
  instead of inventing a second rollout engine.
- Agno 2.8.1 added a learning extraction tool-call limit. It maps to an explicit
  learning budget where supported.
- Agno 2.8.4 materially revised Entity Memory. Entity quality, conflicts, deletion, and
  migration must be rebaselined rather than assumed compatible.
- Agno 2.9.0 binds cached tool results to user/session identity and propagates caller
  identity through run-only Studio dispatch. Agnoclaw still forbids Agno tool-result
  caching on durable ingress and derives learning identity only from trusted
  `ExecutionContext`; the upstream fixes are defense in depth, not a substitute for
  the owner-bound operation and learning ledgers.
- Agno 3 removes deprecated memory/history parameters. The stable-v3 compatibility
  lane keeps that delta behind the same agnoclaw `LearningPolicy`; upstream version
  details do not leak into user code.

See [Agno release-practice audit](agno-release-practices.md) for the complete
release-by-release evidence and certification policy.

## The four different persistence concerns

These are related but not interchangeable:

| Concern | Purpose | Typical lifetime | Correct home |
|---|---|---|---|
| Conversation/trajectory | Exact record of messages, model steps, tools, results, and run states | Thread/run retention | agnoclaw RuntimeStore / Agno session projection |
| Working state | Current goal, plan, progress, open questions, and next step | One long-running session | Agno Session Context plus settled snapshots |
| Memory/knowledge | Selected facts or insights worth recalling later | Cross-turn or cross-session | Scoped Agno Learning Stores |
| Self-improvement | A proposed behavioral/process change supported by outcomes | Versioned until superseded | Learning candidate/promotion system; sometimes a reviewed workspace or skill update |

Compaction is also not learning. It changes the active context view while retaining the
underlying trajectory.

## Choose the Agno store by intent

| Store | Use it for | Scope key | Recommended mode | Do not use it for |
|---|---|---|---|---|
| User Profile | Stable structured facts: name, role, preferences | tenant + user | Always for low-risk fields; Agentic for explicit control | Session progress or broad knowledge |
| User Memory | Useful unstructured observations about one user | tenant + user | Always or Agentic, based on privacy/cost | Organization-wide rules |
| Session Context | Goal, plan, progress, blockers, next steps | tenant + session | Always with planning for long-running agents | Long-term user preferences |
| Entity Memory | Facts/events/relationships about projects, companies, people, APIs | tenant + namespace/entity | Always for extraction or Agentic for explicit edits | Free-form institutional tips |
| Learned Knowledge | Reusable insights and patterns discovered through experience | tenant + agent/capability namespace | Agentic; Propose plus code approval for reviewed promotion | Facts without a vector-backed `Knowledge` store |
| Decision Log | Consequential decisions, rationale, and later outcome | tenant + agent, linked to run | Agentic or explicit application logging | General conversation summaries |

Agno's modes have important semantics:

- **Always** extraction starts with the conversation through the current user message;
  it does not see the assistant response or tool outcomes from that same turn. It is not
  suitable for learning “what worked” unless outcome processing happens separately.
- **Agentic** gives the model save/search tools. It may miss implicit information.
- **Propose** is enforced through instructions, not a hard application approval gate.
  It must not be the only control for regulated or high-stakes learning.

## Explicit public API (v0.12 preview)

`LearningProfile` is a small set of constructors for immutable `LearningPolicy`;
identity and storage scope are resolved from trusted run input:

```python
from agnoclaw import AgentHarness, ExecutionContext, LearningProfile

learning = LearningProfile.personal_and_session(
    user_profile="always",
    user_memory="agentic",
    session_context="always",
)

harness = AgentHarness(model=model, learning=learning)
result = await harness.arun(
    "Help with my account",
    context=ExecutionContext.create(
        tenant_id="acme",
        user_id="user-123",
        session_id="case-42",
        workspace_id="support",
    ),
    learning_consent=True,
)
```

Institutional knowledge must require its storage prerequisite:

```python
from agno.knowledge import Knowledge
from agnoclaw import AgentHarness, LearningProfile

learning = LearningProfile.institutional(
    namespace="cloud-architecture",
    knowledge=Knowledge(vector_db=vector_db),
    learned_knowledge="agentic",
    decision_log="agentic",
    promotion="reviewed",
)

harness = AgentHarness(model=model, name="research-agent-v2", learning=learning)
```

The API fails construction or run admission when:

- Learned Knowledge has no vector-backed `Knowledge`;
- an institutional or tenant-required policy has no trusted tenant scope;
- a user store has no user ID;
- Session Context has no stable session ID;
- an institutional store requests direct model writes;
- a configured store/mode is unsupported by the tested Agno version;
- explicit and legacy learning configuration are mixed.

Candidate capture, evaluation, Learned Knowledge promotion/rollback, tombstones,
transactional event/outbox export, unknown-effect scanning, and bounded host-observer
coordination are implemented in the SQLite/PostgreSQL preview. Direct-store deletion
propagation, certified backend observers with durable scheduling, and model-backed
previous-version/multi-provider/long-duration benefit remain release gates rather than
implied behavior. The exact local no-learning smoke does not waive them.

## Scope model

Do not overload one string namespace with every isolation concern. The harness owns a
structured scope:

```text
tenant_id / org_id / agent_id / capability_id / user_id / session_id / namespace
```

It maps only the relevant parts into each Agno store. Rules:

1. Tenant is the outer security boundary in service mode.
2. User Profile and User Memory are never globally shared.
3. Session Context is bound to one durable session.
4. Learned Knowledge is shared only inside an explicit tenant plus agent/capability
   namespace.
5. Cross-tenant promotion requires a separate administrative pipeline and sanitized
   content; an agent cannot opt itself into it.
6. Scope keys appear in events, candidate provenance, evaluation fixtures, deletion
   jobs, and storage filters.

## Learning pipeline

Direct model writes are useful for low-risk Agentic memory, but they are insufficient
for “self-improvement.” Use a governed pipeline:

The pipeline separates generation, diagnosis/reflection, curation, and evaluation.
Each stage emits a typed artifact with its mechanism version and input provenance.
Reflection text or hidden reasoning is never treated as proof that a change worked.

### 1. Observe

Collect evidence from a completed trajectory:

- explicit user correction or rating;
- deterministic task result or test outcome;
- tool success/failure and workaround;
- decision outcome;
- repeated behavior across independent runs;
- operator-authored knowledge.

### 2. Create a candidate

A candidate is typed and immutable:

```json
{
  "candidate_id": "lc_...",
  "store": "learned_knowledge",
  "scope": {"tenant_id": "acme", "agent_id": "reviewer-v3"},
  "content": "...",
  "source_run_ids": ["run_..."],
  "evidence_artifact_ids": ["artifact_..."],
  "confidence": 0.82,
  "risk": "low",
  "created_by": "agent|user|operator|rule",
  "schema_version": "1.0",
  "expires_at": null
}
```

Candidates do not enter recall context.

For a harness or durable behavioral change, the candidate also carries a
`HarnessComponentManifest` reference and immutable `ChangeHypothesis`: causal failure
cluster, bounded target files/components, evidence, predicted fixes, at-risk
regressions, passing behavior to preserve, model/config/evaluator digests, evaluation
budget, and rollback target. The seven component classes are system prompt, tool
description, tool implementation, middleware, skill, subagent configuration, and
long-term memory. Runtime identity, policy, permissions, evaluator, raw traces, model
configuration, and budgets are never proposer-editable components.

### 3. Validate

- deduplicate semantically and by source;
- group failures by verifier-grounded causal mechanism rather than terminal status;
- check conflict with current policy, workspace facts, and existing memories;
- replay targeted held-in, untouched held-out, and frozen-transfer tasks with and
  without the candidate;
- reject secrets, personal data outside scope, prompt injections, transient state, and
  unsupported claims;
- require multiple independent observations for general behavioral rules unless an
  authorized user/operator explicitly requests them.
- keep the proposer unable to edit benchmark cases, verifiers, raw runs, model/config,
  budget, or permissions; calibrate LLM judges with balanced ordering and disagreement
  review, using deterministic external feedback wherever possible.

### 4. Promote

Promotion policy depends on risk:

| Risk | Example | Promotion |
|---|---|---|
| low | User formatting preference | trusted host/user approval inside user scope |
| medium | Tool workaround or domain heuristic | evidence gate plus host/operator review |
| high | Policy, financial/legal guidance, cross-user rule | application-enforced human approval |
| prohibited | Credential, private cross-tenant data, instruction injection | reject |

Promotion emits an event and stores provenance. Updating a workspace file or skill is a
code/configuration change with review, versioning, and rollback—not a silent memory save.
Automatic promotion is experimental and disabled by default in 0.12, including for
low-risk candidates.

Qualified harness changes remain in a Pareto archive across quality, safety, cost,
latency, and complexity. Rejected and negative candidates remain searchable, and
novelty/diversity checks prevent a population of cosmetic variants. Ambiguous or
heuristic-only evaluation may produce a review candidate but cannot justify autonomous
promotion or a broad self-improvement claim. The implemented typed gate and ledger
handoff plus the host-only `query_learning_evaluation_archive()` read model are
documented in
[Evidence-gated harness self-improvement](self-improvement-evaluation.md); the host
still owns benchmark execution, evidence-artifact verification, and every promotion
decision. The archive projection is content-free and defaults to rejected/inconclusive
verdicts; it is not exposed as a model tool. Learning-ledger schema v6 retains the v5
typed safe filters and validated reason codes and adds append-only application/outcome
attribution outside the promotion CAS row. A bounded PostgreSQL
17 noisy-neighbor benchmark now guards the measured 10,000-evaluation-per-owner path.
That development gate does not replace production-volume, failover, or retention proof.

### 5. Recall and apply

- retrieve only inside the exact scope;
- attach source, confidence, freshness, and conflict metadata;
- cap memory tokens separately from conversation and tool context;
- let the model distinguish operator policy, verified facts, user preference, and
  unverified learned heuristic;
- log which memories were retrieved and which were cited/applied, without exposing
  private content in telemetry.

### 6. Measure, decay, and remove

- compare task quality, tool errors, latency, tokens, and cost against a no-learning
  control;
- collect explicit negative feedback and propose quarantine of implicated learnings;
- reduce confidence when outcomes contradict the learning;
- expire time-sensitive knowledge;
- support user/tenant deletion and provenance-based cascading removal;
- keep audit tombstones where legally appropriate without retaining deleted content.

The v0.12 preview now implements the measurement input, not automatic mutation.
`observe_learning_application()` binds a promoted target and exact authorized run to
immutable evidence and distinguishes `retrieved` from `applied`.
`observe_learning_outcome()` accepts one independently evaluated, evidence-backed
outcome per applied record. `summarize_learning_effectiveness()` requires a minimum
number of outcomes and independent runs before returning a read-only
`retain`/`review`/`quarantine` recommendation. Feedback rows are append-only and do not
increment the candidate revision, avoiding contention with promotion/rollback.

This closes the durable attribution loop but does not by itself prove learning benefit:
a provider/model/corpus must still run no-learning and previous-version controls. A
recommendation never changes confidence or Agno state automatically.

The first such no-learning control is now executable for one local synthetic task:

```bash
uv run --isolated --extra local --extra rag python \
  -W error::ResourceWarning \
  scripts/learning_benefit_probe.py \
  --allow-live-model
```

It uses Agno 2.9.0's real vector-backed Learned Knowledge store, six synthetic protocol
facts, fresh Agents, frozen Ollama model/embedder content digests, identical prompts and
decoding, balanced held-in/held-out/alias-transfer order, and an exact-token verifier.
The candidate receives Agno's injected `<relevant_learnings>` block but no learning
write/search tools; the baseline receives no LearningMachine. Three consecutive runs
of the frozen local `qwen3:0.6b`/`nomic-embed-text` configuration produced 6/6 wins,
zero losses, and `+1.0` paired mean quality in every slice. See
[Harness evaluation and release gates](evaluation.md#live-agno-learning-benefit-gate)
for the command, digests, evidence retention, and nonclaims.

This closes only the “can the exact Agno context mechanism improve this exact objective
task?” smoke. It does not close previous-version comparison, model/provider diversity,
production vector-store behavior, long-run drift, causal outcome coverage, or automatic
promotion. That distinction matches Agno's documented separation between
[Learned Knowledge](https://docs.agno.com/learning/stores/learned-knowledge) and the
broader [Learning modes](https://docs.agno.com/learning/learning-modes).

Automatic quarantine, confidence decay, expiry mutation, prompt/policy changes, and
skill rewriting remain experimental and disabled by default in 0.12. Stable behavior is
candidate capture, provenance, consent, scoped CRUD/forget, evaluation, and explicit
host-approved promotion or removal.

## Relationship to workspace files

Workspace files remain useful because they are transparent and versionable:

- `AGENTS.md`: reviewed operating rules;
- `SOUL.md`: reviewed persona/tone;
- `USER.md`: operator-visible user context, with privacy controls;
- `MEMORY.md`: curated index/continuation context;
- `TOOLS.md`: environment-specific operational guidance;
- daily logs: human-readable summaries, not canonical trajectory.

They should not duplicate every Agno store. A target ownership model:

| Information | Canonical owner | Optional projection |
|---|---|---|
| exact run history | RuntimeStore | daily summary |
| current goal/progress | Session Context/snapshot | `progress.md` for human visibility |
| user preference | User Profile/Memory | reviewed `USER.md` |
| entity facts | Entity Memory or domain DB | concise workspace reference |
| reusable insight | Learned Knowledge | reviewed rule in `AGENTS.md`/skill when promoted to behavior |
| decision rationale/outcome | Decision Log | report or changelog |

Conflicts follow authority, freshness, and scope—not “newest text wins.” Deterministic
policy and operator-authored rules outrank learned heuristics.

## Relationship to `.learnings/`

The bundled `self-improving-agent` skill currently writes Markdown entries and can
promote them to workspace files. Keep the human-readable idea, but change its role:

- `.learnings/` becomes an optional projection/queue of typed Learning Candidates;
- entries include source run/artifact IDs, scope, evidence, confidence, risk, and expiry;
- promotion uses the same application policy as Agno store promotion;
- duplicate workspace and Agno writes are avoided;
- runtime events, not prompt wording, trigger eligible candidate creation;
- the skill may inspect, explain, or request promotion, but cannot bypass policy.

## Required evaluation gates

Learning is production-ready only when all of these are automated:

### Store function

- write and retrieve one item for every enabled store/mode;
- prove Learned Knowledge semantic recall through its vector DB;
- verify Session Context updates goal/plan/progress;
- verify Decision Log outcome attachment;
- fail fast when prerequisites are missing.

### Isolation and privacy

- same user/same tenant recall succeeds;
- different user recall fails for user stores;
- different tenant recall always fails;
- different agent namespace recall follows configured sharing;
- deletion removes content from primary, index, cache, summaries, and future recall;
- events and traces do not leak memory content by default.

### Quality

- relevant memory improves or preserves task success versus control;
- irrelevant memory is not injected;
- contradictory memory is surfaced or safely resolved;
- stale memory expires;
- a malicious conversation cannot promote a policy override;
- false-memory and memory-poisoning suites pass.

### Operations

- maintenance APIs are verified for every supported Agno version;
- concurrent writes do not lose or corrupt memories;
- migrations preserve scope and provenance;
- store outage behavior is explicit: fail closed for required audited decisions, degrade
  safely for optional personalization;
- cost/token budgets are measured per store and mode.

See [Harness evaluation](evaluation.md) for the broader release gates.

## Primary references

- [Lilian Weng harness/self-improvement research audit](lilian-weng-harness-audit.md)
- [Evidence-gated harness self-improvement](self-improvement-evaluation.md)
- [Personal and session learning administration](learning-administration.md)
- [Governed learning candidates](learning-candidates.md)
- [Harness Engineering for Self-Improvement](https://lilianweng.github.io/posts/2026-07-04-harness/)
- [Why We Think](https://lilianweng.github.io/posts/2025-05-01-thinking/)
- [Reward Hacking in Reinforcement Learning](https://lilianweng.github.io/posts/2024-11-28-reward-hacking/)
- [Agno Learning Machines](https://docs.agno.com/learning/overview)
- [Agno learning modes](https://docs.agno.com/learning/learning-modes)
- [Agno Learned Knowledge example](https://docs.agno.com/examples/learning/basics/learned-knowledge)
- [Agno institutional learning](https://docs.agno.com/use-cases/deep-research/institutional-learning)
- [Agno release-practice audit](agno-release-practices.md)
- [Agno 2.9.0](https://github.com/agno-agi/agno/releases/tag/v2.9.0)
- [Agno 3.0.1](https://pypi.org/project/agno/3.0.1/)
- [Agno 3.0.0a1](https://github.com/agno-agi/agno/releases/tag/v3.0.0a1)
- [Letta context hierarchy](https://docs.letta.com/guides/core-concepts/memory/context-hierarchy)
- [Letta memory blocks](https://docs.letta.com/guides/core-concepts/memory/memory-blocks)
- [Letta stateful-agent evaluation concepts](https://docs.letta.com/guides/evals/concepts/overview)
