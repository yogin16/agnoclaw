# agnoclaw documentation

Use this page to find the right document without reading the repository in release
order.

## Start here

- [Project README](../README.md) — installation, quick start, major public surfaces,
  and current cautions.
- [Getting started](getting-started.md) — first model call, trusted context, lifecycle
  run, session continuation, learning, and cleanup using public APIs only.
- [CLI reference](cli.md) — command groups, common options, and automation/durability
  boundaries, including the full PostgreSQL `service` migration lifecycle contract.
- [Support](../SUPPORT.md) — diagnostics, safe issue reports, and help channels.
- [Security policy](../SECURITY.md) — private reporting and supported response scope.
- [Community conduct](../CODE_OF_CONDUCT.md) — participation expectations.
- [Configuration reference](configuration.md) — exact precedence, TOML/environment
  mapping, runtime controls, and a safe service baseline.
- [Generated Python API reference](reference/api.md) — every supported top-level
  export, its runtime signature, implementation owner, and the public-surface digest.
- [Vision](vision.md) — product boundary and design principles.
- [World-class harness strategy](world-class-harness.md) — 2026 primary-source research,
  product decision, competitive lessons, and phased roadmap.
- [Agno release-practice audit](agno-release-practices.md) — every stable Agno release
  from 2.6.0 through 2.9.0, the 3.0 alpha architecture, and agnoclaw's tested
  adopt/adapt/avoid decisions.
- [Pi 0.84 harness audit](pi-harness-audit.md) — released lane/session/operation/storage
  contracts, scaffold maturity, unreleased v3 signals, and agnoclaw reconciliation.
- [Lilian Weng research audit](lilian-weng-harness-audit.md) — line-by-line
  reconciliation of the July 2026 harness/self-improvement research with 0.12 tasks and
  release gates.
- [Compatibility](compatibility.md) — certified/provisional Python and Agno lanes,
  capability inspection, optional surfaces, upgrade policy, and known debt.
- [Harness gap analysis](harness-gap-analysis.md) — implementation-aligned current
  capability and limitation matrix.

## Understand the design

- [Harness architecture](architecture.md) — target kernel, state machine, events,
  context, capabilities, policy, durability, and invariants.
- [Run lifecycle and RuntimeStore](runtime-lifecycle.md) — implemented `start/get_run`,
  `HarnessRun`, state/command semantics, SQLite authority, cursors, bounded startup
  recovery, and the current continuation boundary.
- [Durable scheduling](durable-scheduling.md) — schema-v12 jobs/attempts, database-clock
  claims, leases/fences, retries, misfires, concurrency groups, lifecycle recovery,
  learning consent, CLI/Python setup, and remaining service limits.
- [Declared child runs](child-runs.md) — durable lineage, authority/budget grants,
  join/cancellation semantics, declaration-bound structured results, typed artifact
  handoff, governed synthesis, learning behavior, examples, and preview limits.
- [PostgreSQL RuntimeStore operations](postgresql-runtime-store.md) — service-store
  transactions, pooling, first-party etcd writer authority, HA drills, retention,
  operations, and open production gates.
- [Durable event export](event-export.md) — first-party leased outbox pump, at-least-once
  batch semantics, retry/cancellation behavior, owner-authorized dead-letter
  administration/audit, security, and current exporter limits.
- [Observability and safe run inspection](observability.md) — content-free durable
  OpenTelemetry logs/metrics, HMAC linkage, owner-scoped inspection, CLI/SDK setup,
  privacy/cardinality rules, exit codes, and remaining trace/production gates.
- [Operations, effects, and recovery](operations-and-recovery.md) — effect classes,
  intent-before-dispatch, fencing, cancellation truth, recovery decisions, the
  certified Agno 2.9 tool/approval restart envelope, and remaining T6 boundaries.
- [Durable artifacts](artifacts.md) — scoped content addressing, atomic result
  references, integrity, paging, encryption, garbage collection, and restart recovery.
- [Context management](context-management.md) — deterministic token accounting,
  artifact-first manual replacement, scoped trajectory search, selective rehydration,
  retention ownership, and the automatic-compaction certification boundary.
- [Agno context providers](context-providers.md) — direct compatibility versus
  run-owned governed query ingress, lifecycle, trust, bounds, and effect limits.
- [Capability descriptors and lazy discovery](capabilities.md) — the one immutable
  capability contract, bounded discovery, explicit execution, AgentHarness-owned
  governed Agno binding, durable exact approvals, and the remaining legacy-ingress
  boundary.
- [MCP client and governed tool ingress](mcp.md) — the two-tool deferred surface,
  MCP 2.0 transport/configuration, schema-drift binding, effect/lifecycle behavior,
  security boundary, errors, and explicit parity limits.
- [Security foundation and threat model](security.md) — admission trust, data
  classification, policy evidence, key-provider, safe-diagnostic, and threat contracts.
- [ADR-0001: recovery ownership](adr-0001-recovery-ownership.md) — real-process Agno
  2.x/3-preview evidence and the operation/effect-ledger decision.
- [Learning and self-improvement](learning.md) — current validated learning behavior,
  remaining limits, store selection, target profiles, scope, promotion, and evaluation.
- [Governed learning candidates](learning-candidates.md) — capture, evaluation,
  promotion/rollback, unknown-effect discovery, evidence-bound reconciliation, and the
  dedicated SQLite/PostgreSQL leased/fenced/checkpointed maintenance worker.
- [Personal/session learning administration](learning-administration.md) — exact scoped
  read/replace/forget, post-operation verification, receipts, errors, concurrency, and
  point-in-time deletion limits.
- [Evidence-gated harness self-improvement](self-improvement-evaluation.md) — immutable
  component/diagnosis/hypothesis records, a scoped paired runner, confidence-aware
  held-in/out/transfer gates, judge audit, Pareto governance, ledger handoff, and
  explicit non-claims.
- [Workspace files](workspace.md) — human-readable instructions and curated context.
- [Skills reference](skills.md) — SKILL.md fields, trust, tool scoping, and current
  activation semantics.

## Embed and operate

- [Embedding overview](embedding/README.md) — current service shape and limitations.
- [Policy and guardrails](embedding/policy-and-guardrails.md) — enforcement checkpoints,
  durable host approval, configuration, elevation, errors, and event cautions.
- [Runtime backends](embedding/workspace-backends.md) — host, sandbox, and custom
  execution planes.
- [AgentOS and remote lifecycle adapter](embedding/agentos-adapter.md) — authenticated
  hosting, versioned routes, local/remote API parity, claims/scopes, limits, errors, and
  the native AgentOS compatibility boundary.

## Verify quality

- [Harness evaluation and release gates](evaluation.md) — maturity labels, contracts,
  chaos/soak/security tests, benchmarks, CI lanes, and documentation gates.
- [Provider-free public API journey](public-api-journey.md) — executable quick,
  durable/reopen, governed-learning, and local-migration proof plus exact limitations.
- [PE risk embedded test plan](embedding/pe-risk-testing-plan.md) — a domain-specific
  library simulation and integration runbook.

## Plan the next release

- [agnoclaw 0.12.0 release plan](releases/v0.12.0-plan.md) — reviewed scope,
  architecture, implementation graph, change management, evidence gates, and release
  checklist for the World-Class Harness Runtime.
- [0.12 implementation progress](releases/v0.12.0-progress.md) — live workstream and
  verification evidence.
- [0.12 migration guide](migration-0.12.md) — source/config changes and the current
  migration boundary.
- [0.12 migration preflight](migration-preflight.md) — implemented read-only learning/
  schedule inventory, scope/fence decisions, report schema, blockers, and limits.
- [Local 0.12 migration runbook](migration-apply-0.12.md) — digest-bound plan, verified
  backup/apply/verify/cutover, crash-resumable rollback, failure handling, and limits.
- [PostgreSQL/service migration runbook](migration-service-0.12.md) — credential
  references, scan/plan/preview, idempotent provenance-owned apply, independent verify,
  cutover receipts, reverse-order rollback, explicit schedule maps and backup receipts,
  and the remaining production certification gates.
- [Changelog](../CHANGELOG.md) — implemented unreleased behavior and migration notes.

## Specifications and historical direction

- [v0.2 harness core spec](../spec/v0.2-harness-core.md) — historical runtime-governance
  contract that introduced the current policy/event layer.
- [v0.8 SDK/server/packs direction](../spec/v0.8-harness-sdk-server-packs.md) — historical
  rationale for AgentOS export, packs, and SDK ergonomics.

The current roadmap is [World-class harness strategy](world-class-harness.md); older
specifications remain useful decision records but do not override it.

## Documentation conventions

- Current behavior and target behavior must be labeled separately.
- Public capability claims use the maturity definitions in
  [Harness evaluation](evaluation.md).
- Examples should use public APIs. A private `_agent` workaround belongs in a known
  limitation, not a recommended integration.
- Security, scope, concurrency, retention, failure, and version behavior are part of the
  contract, not optional footnotes.
- Research claims link to primary sources and record the review date.
- Documentation changes ship with the code or behavior they describe.
