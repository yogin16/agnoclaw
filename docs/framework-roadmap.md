# Embeddable framework and domain-pack roadmap

Design direction, 2026-10-19. This extends the [domain research](domain-harness-research.md)
and [0.15 authoring guide](domain-authoring.md). Unless marked implemented, the
capabilities below are proposed work, not available APIs or release commitments.

## Product direction

agnoclaw should let a developer build, inspect, test, version, embed, and operate a
domain pack. A pack joins schemas, object relationships, evidence rules, skills,
procedures, actions, evaluations, interaction requirements, and workspace resources.
Clarion should increasingly supply its domain definitions and host adapters through
that framework. The opportunity is broader than shortening `ProcedureSpec` constructors.

The primary interface is a Python library. File formats, CLI commands, editors and
services should call the same public APIs. A small application must be able to use
one schema or one evaluation without configuring an entire platform. Importing or
inspecting a pack must not start models, listeners, workers, or infrastructure.

The practical boundary is **shared mechanics plus application-owned semantics**.
agnoclaw can own reference resolution, validation, compatibility diagnostics,
evaluation execution, callback lifetimes and event delivery contracts. Clarion owns
its financial meanings, source permissions, business transactions, canonical entities,
deployment credentials and product experience. Extraction succeeds when Clarion can
delete glue code while retaining those responsibilities explicitly.

## One authoring lifecycle

```mermaid
flowchart LR
    A[Define pack] --> B[Resolve and validate]
    B --> C[Inspect plan and changes]
    C --> D[Evaluate fixtures and adapters]
    D --> E[Bind host services]
    E --> F[Activate a pinned version]
    F --> G[Observe and collect feedback]
    G --> H[Review an upgrade]
    H --> C
```

Resolution should produce an immutable manifest of exact schema, skill, checker,
capability, workflow and workspace versions. Inspection explains where each definition
came from, which override won, what is unresolved, and what an upgrade would invalidate.
Live runs keep their pinned definitions until an explicit, supported migration.
Python callables remain trusted host code; a manifest cannot reproduce arbitrary code
without the corresponding pinned package/build.

## Capability families and existing foundations

These are cohesive responsibilities, not a requirement to create a Python package
for every row. Extend the existing modules before inventing parallel registries.

| Family | Developer experience to provide | Existing foundation / remaining work |
|---|---|---|
| Pack catalog and composition | Discover, resolve, inspect, validate, diff, activate and retire packs; actionable errors with origin paths | `packs.py`, `Domain`, `DomainPack` exist; dependency resolution, resolved manifests and cross-definition diagnostics are next |
| Schemas and ontology | Register shapes and entity/relation definitions; resolve references; validate overlays; preview compatibility and migrations | `OutputSchema`, `ObjectId`, `ObjectLink` exist; schema catalog, relation constraints and schema evolution are proposed |
| Skills and procedures | Bind skill metadata to input/output schemas, checks, capabilities and applicability; derive tool schemas from those bindings | Skill loader/registry and `Procedure` exist; shared typed extension metadata and cross-reference validation remain |
| Changes and conflicts | Distinguish definition conflicts, data edits, duplicate requests, and contradictory claims; render explicit resolution choices | 0.15 handles definition conflicts, JSON rebasing and request identity; semantic claim resolution stays a separate policy |
| Evaluations | Run pure checks, replay fixtures, grade outputs/trajectories, test adapters, compare policies/models, measure cost and latency | `domain.evaluate_cases`, corpus and improvement runners exist; a common authoring/inspection facade should reuse them |
| Workflows | Compose typed steps, branch on results, await reviews, expose progress, cancel and resume with explicit effect semantics | Existing lifecycle, child runs, scheduler and Agno workflows; a certified domain workflow adapter is proposed |
| Interaction and channels | Declare questions, choices, review/diff requests, evidence views and artifacts; expose unsupported affordances clearly | Domain review contracts and lifecycle input requirements exist; transport-neutral affordances and adapter conformance remain |
| Protocol adapters | Versioned ingress/egress envelopes, schema validation, capability negotiation and compatibility checks | Existing lifecycle HTTP protocol and governed MCP; extend through adapters rather than replace these protocols |
| Hooks and middleware | Typed interception points with declared ordering, mutation rights, failure policy and cleanup | Existing pre/post/lifecycle hooks and capability pre-dispatch; pack-scoped composition/inspection need work |
| Events and observers | Subscribe to appropriate committed facts, replay by cursor, deduplicate, apply backpressure and inspect delivery failures | Runtime events, transactional outbox and reconciliation observers exist; domain adapters and consumer tooling remain |
| Watchers and triggers | Evaluate source changes or scheduled work, report suppression/health, deduplicate triggers and request governed runs | Scheduler/capability substrate exists; reusable detector contracts and host trigger adapters are proposed |
| Workspaces | Pin immutable pack resources, layer controlled overrides, isolate session writes, preview refreshes and manage cleanup | Local `Workspace` and pack content trust exist; snapshot/provider contracts and staged materialization remain |

## What can come upstream from Clarion

The following assessment comes from reading the local Clarion implementation. Paths
are relative to its repository, mostly under `packages/server/src/clarion/`.
These are extraction candidates, not claims that the entire modules are portable.

| Clarion evidence | Extractable mechanism | Retain in Clarion / adaptation needed |
|---|---|---|
| `domain/schema_ref.py`, `services/ontology_overlay.py`, `_overlay_loaders.py` | Typed references, explicit schema sources, bounded field definitions, parsing, duplicate diagnostics and compilation cache | `deal/company/contact/artifact` vocabulary, finance field meanings, tenant storage; bridge existing reference strings without rewriting persisted data |
| `services/_overlay_presentation.py` | Validating presentation hints against actual schema fields | React components, product layout and user permissions; presentation hints cannot become write authority |
| `agent/skill_registry.py`, `services/skill_frontmatter.py`, `_skill_validation.py` | Strict metadata, output reference resolution, canonical metadata binding and schema/tool consistency checks | Finance artifact names, title policy and composer scopes; extend existing skill metadata compatibly |
| `services/workspace.py`, `_workspace_sync.py` | Source providers, immutable base snapshots, session overlays, refresh and pinned-resume behavior | S3 settings, tenant/deal provisioning, application retention and access rules |
| `channels/contracts.py`, `channels/protocol/v1.py`, `channels/registry.py` | Typed affordances, adapter capability negotiation, ingress/delivery separation and conformance manifests | Clarion-specific navigation/actions, provider authentication, routing and account mapping |
| `watchers/base.py`, `watchers/registry.py` | Detector registration, assessed/suppressed counts, source health, quality versions, reaction bindings and noise-budget reporting | Investment detectors, `InsightService`, SQLAlchemy sessions, entity scopes and promotion policy |
| `services/event_bus.py` | Subscription declarations, consumer diagnostics and replay/idempotency test contracts | Business outbox transaction, ORM, tenant partitioning and worker deployment; adapt this bus rather than copy it into the runtime |
| `scripts/eval_intelligence_corpus.py`, `scripts/eval_document_classifier.py` | Dataset validation, protected cases, slices, abstention/precision/recall reports and explicit rollout gates | Corpus contents, materiality rules, financial graders and deployment thresholds |

Two details argue for careful adaptation. Clarion's workspace freshness digest uses
file size/mtime or object-store metadata; that is a refresh signal, not proof of exact
content. agnoclaw's pack trust already hashes file bytes. Preserve both concepts.
Similarly, Clarion's event bus joins the caller's business transaction; emitting a
runtime event cannot make an unrelated SQL transaction atomic.

Do not move services wholesale and then replace their imports with `Any`. Extract a
small contract, supply an adapter in Clarion, prove behavior against the old path, and
only then remove duplicated implementation. A second domain must exercise the same
contract before application-specific assumptions become a stable SDK promise.

## Schemas and ontology need separate layers

An ontology describes entity kinds, relationship meanings, cardinality, units and
constraints. A schema validates a representation of one of those concepts. A business
database stores instances. They should compose without forcing one ORM or graph store.

Start with Pydantic/JSON Schema and an explicit local schema catalog. Resolve references
from pinned packages/providers; a schema URI must not imply automatic network access.
Keep logical schema identity, authored version, source provenance and exact digest
separate. [JSON Schema's reference model](https://json-schema.org/understanding-json-schema/structuring)
supports identifying reusable schemas independently of their retrieval location.

Schema evolution needs producer/consumer direction: accepting older data and producing
data older consumers can read are different compatibility questions. Adding a required
field, narrowing an enum, changing units, or altering a custom validator needs explicit
impact analysis. Some changes are mechanically classifiable; others must return
"unknown—domain review required." Offer dry-run migrations and host adapters rather
than silently rewrite stored data. A rollback of definitions does not necessarily
reverse an irreversible data migration.

Tenant/project overlays need declared precedence and provenance. Reject ambiguous
duplicates by default, distinguish additive extensions from replacements, and show a
diff before applying a replacement. An in-flight proposal or review must not silently
follow a newly resolved schema. Contradictory evidence is not a JSON merge conflict
and cannot be resolved by a generic last-writer-wins rule.

Graph constraint standards such as [SHACL](https://www.w3.org/TR/shacl/) are useful
reference points for relation validation. Full RDF/OWL/SHACL execution should remain an
optional adapter if adopters need it, not a prerequisite for a Python invoice pack.

## Protocols, hooks, listeners and observers

An interface needs both a typed signature and operational semantics. Do not combine
every extension into one untyped `on_event` callback.

| Extension | What it may do | Required contract |
|---|---|---|
| Pre-action policy hook | Validate, reject, or produce an explicit revised proposal | Deterministic ordering; bounded duration; failure closes the protected boundary; any mutation invalidates affected checks/reviews |
| Transform middleware | Transform a declared input/output shape | Typed before/after contract; explicit trust and mutation rights; changes included in digests |
| Runtime observer | Consume telemetry/progress for display and diagnostics | Best-effort versus durable mode explicit; bounded buffering; no implicit business authority |
| Durable event listener | React to committed facts and request further governed work | At-least-once delivery, consumer identity, deduplication, cursor/checkpoint, partition ordering, retries and dead-letter handling |
| Reconciliation observer | Read external state to determine an uncertain effect's outcome | Read-only observation, versioned evidence, no automatic redispatch of the original mutation |
| Watcher/detector | Evaluate a schedule or source change and return findings/proposals | Trigger identity, source freshness/health, assessed/suppressed/errored distinction, noise limits, existing scheduler/worker ownership |
| Interaction/channel adapter | Authenticate ingress or present/submit a typed affordance | Version/capability negotiation; explicit fallback; expired/stale input rejection; host identity binding; delivery receipts |

Listener handlers that perform effects must enter governed capabilities or host business
transactions with idempotency. A listener must not turn at-least-once events into repeated
payments. Ordering is usually scoped to an object/run/partition; do not promise a global
order across independent business and runtime stores.

Registrations should be scoped to a host/harness instance, not import-time mutable global
state. Return a subscription/registration handle with deterministic close/unsubscribe,
drain/cancel behavior and bounded cleanup. Define whether an exception fails the caller,
retries the consumer, or is recorded as observer failure. Shutdown must not orphan async
tasks. Include correlation, causation, tenant, schema version and effect references,
with redaction and payload-size limits at the boundary.

Reuse `runtime/hooks.py`, `runtime/events.py`, `RuntimeOutboxWorker`, lifecycle cursors,
and reconciliation observers. Business-event adapters attach to the host's own outbox.
[CloudEvents](https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md) supplies
an event identity reference point; [AsyncAPI](https://www.asyncapi.com/docs/reference/specification/v3.0.0)
offers ways to describe messages, operations and channels. Neither provides delivery
guarantees by declaring a document. Compatibility adapters require conformance tests;
this plan does not claim conformance today.

## Evaluations are part of the pack's everyday DX

Provide one discoverable facade over existing runners, with small independent entry
points. The current domain `EvaluationCase` and top-level `EvaluationCase` already serve
different purposes; bridge them deliberately rather than add a third incompatible
runner/record family. Preserve existing imports while improving discovery.

Support distinct suites for schema/contract validation, deterministic business rules,
structured extraction, evidence support, workflow outcomes and trajectories, protocol
conformance, recovery/fault injection, watcher signal quality, and model-routing policies.
Each suite should name its dataset, subject/build, schema/pack/workspace fingerprints,
grader/version, source snapshot, budgets and deterministic seed where applicable.

Expose errors and abstentions separately from incorrect outputs. Report slices, protected
cases, uncertainty, latency, cost and review burden; an aggregate pass rate can hide a
critical regression. Keep human judgments and model graders attributed. Offline suites
remain usable without credentials; live-model suites are explicit and budgeted. Model
routing, learned predictors and deterministic decision tables can be evaluated through
the same subject adapters without requiring a particular model family.

Corrections can propose new fixtures. Admission to a held-out corpus and promotion of
policy changes remain explicit operations. Reusing the governed corpus/improvement
machinery is appropriate when promotion is required; a developer running a unit fixture
should not need to configure a learning ledger.

## Workflow and workspace composition

A workflow should connect declared inputs/outputs and explicit actions, reviews and
failure routes. Its definition belongs in a pack; its execution belongs to an existing
runtime. [Agno workflows](https://docs.agno.com/workflows/overview) are a candidate adapter,
alongside explicit host orchestration and existing child runs. Do not infer durable
step recovery simply because a whole workflow can be called as one capability.

Certify one adapter against pause/resume, cancellation, effect uncertainty, retries,
child ownership and definition drift before promising general workflow portability.
Preview a workflow's required capabilities, effects, budgets and human interactions
before activation. A workflow definition is not a grant of permission to execute it.

Workspace providers should expose source manifests and materialize pinned snapshots.
Keep pack resources separate from session scratch, user overrides, curated memory,
secrets and model-generated files. Use content hashes for integrity and explicit
freshness tokens for refresh; stage writes before atomic activation. Reject path escape,
undeclared overwrites and unknown snapshot substitutions. Host adapters supply local/S3
or other storage and define cleanup/retention. Existing `Workspace` callers keep working.

## Ordered implementation slices

| Slice | Concrete deliverable | Acceptance gate |
|---|---|---|
| 0 — 0.15 foundation | Executable authoring, stable IDs/links, explicit merge/dedup semantics, reviewed dispatch | Implemented preview; old APIs and Clarion adapters retained |
| 1 — Pack catalog and validation | One resolved manifest joining existing skills, output schemas, procedures and capability declarations; aggregated diagnostics with source paths; read-only inspection and dependency/override diff | Clarion typed-output pack and an invoice pack validate offline; unresolved/ambiguous refs and changed definitions fail; inspection executes no pack code |
| 2 — Schema and workspace lifecycle | Schema provider/overlay contract, bounded compatibility report, pinned workspace snapshot/provider and staged activation | Two adopters exercise same contracts; unchanged 0.14 records; no cross-tenant override; resume refuses unavailable/mismatched pinned content |
| 3 — Pack evaluation facade | Fixtures and conformance suites attached to exact resolved manifests; adapters over current deterministic/paired runners | Both packs run from Python and pytest; offline by default; changed schema/check/workspace invalidates stale evidence; failures identify the relevant definition |
| 4 — Interaction and extension contracts | Typed affordances plus scoped hook/listener/observer registration, lifecycle handles and adapter conformance fixtures | Clarion channel adapter plus a local test adapter; retries, duplicate delivery, ordering, slow consumers, cancellation, cleanup and stale reviews tested |
| 5 — Workflow and watcher integration | One certified workflow adapter and reusable detector/result contract using existing scheduler/capabilities | Clarion one real action and watcher, plus non-finance equivalents; crash/replay cannot duplicate effects or promote findings without host policy |
| 6 — Stable ecosystem | Versioned pack format/migrations, maintained adapter compatibility matrix, optional storage/UI/provider packages | Independent adoption and measured maintenance benefit; framework usable without a hosted service |

Each slice should reduce a measurable burden: time to first checked domain output,
duplicate binding code, number of registration sites, clarity of a failed evaluation,
upgrade-preview accuracy, or adapter conformance effort. Start with catalog/validation
and evaluation contracts; these make subsequent ontology, interface and workflow work
inspectable. Avoid promising all extension points before their lifecycle semantics are
tested through a real adopter.
