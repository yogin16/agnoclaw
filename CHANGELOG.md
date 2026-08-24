# Changelog

This project follows Keep a Changelog structure.

## Unreleased

No changes yet.

## [0.12.0] - 2026-08-20

### Added

- Offline `agnoclaw doctor`, `agnoclaw explain ERROR_CODE`, and redacted mode-0600
  `agnoclaw support-bundle` commands, with JSON output, stable exits, and root
  `--no-color`/`NO_COLOR` behavior for support and automation workflows.
- An exact-wheel clean-room release gate that runs the public quick/durable/learning/
  migration journey and four-crash process-restart probe in a read-only container with
  operating-system networking disabled.
- Public security/support/conduct policies and structured bug/feature templates with a
  private vulnerability-reporting path.

- Public `AgnoModelFactory` support for custom enterprise, replay, and deterministic
  model transports in explicit profiles. Its canonical implementation digest and
  declared provider/model identity enter the harness spec while credentials remain in
  the host callable. Construction and every run receive a distinct owned Agno `Model`;
  invalid results, identity drift, shared instances, and competing provider/cache/effort
  authority fail before provider dispatch with typed `MODEL_FACTORY_*` errors.
- An executable provider-free public API journey now drives the top-level quick,
  durable session/reopen, governed candidate evaluation/promotion, and local
  migration/rollback grammar in one disposable run. It records content-free cleanup,
  transport ownership, reopen, learning, and migration evidence; rejects private API
  imports and nonempty operator roots. The release workflow reruns it from the exact
  wheel alongside the bounded process-restart proof with OS networking denied.
- `AgentHarness.session(context=...)` now accepts one trusted `ExecutionContext`, merges
  request metadata immutably, and rejects conflicting user/session/workspace arguments
  with `SESSION_SCOPE_CONFLICT`, matching the published getting-started example.
- Public `run()` and `arun()` are now result-shape-preserving adapters over
  `start()` plus `wait()` for the explicit `quick`, `durable`, and `service`
  profiles. The lifecycle worker binds a task-local run identity before entering the
  one direct Agno execution boundary, so the adapter neither recurses nor dispatches
  twice. Async raw streaming uses a bounded detachable live presentation; consumer
  loss never cancels the logical run. Synchronous calls use one reusable harness-owned
  event-loop coordinator, so blocking results and raw streams retain the same lifecycle
  authority without creating a loop per call. A closed or slow sync presentation
  detaches without cancelling its run, terminal run failures still reach the iterator,
  and cross-loop API mixing fails with `HARNESS_EVENT_LOOP_OWNERSHIP_CONFLICT`. A
  compatibility-resource gate is now
  acquired before model intent/dispatch, preventing local single-flight contention
  from being mislabeled as an ambiguous provider effect. Only the named `legacy`
  profile retains direct execution during the 0.12 migration window.
- Raw orchestration is now a named-legacy-only surface. Explicit profiles omit the
  text-returning `spawn_subagent` default tool, reject configured named raw subagents
  with `RAW_SUBAGENT_LIFECYCLE_UNSUPPORTED`, reject raw Agno Team presets with
  `TEAM_LIFECYCLE_UNSUPPORTED`, and reject fork/command-dispatch skills before run
  creation with `SKILL_LIFECYCLE_DISPATCH_UNSUPPORTED`. The supported replacement is
  the existing `DeclaredChildTemplate`/declared-child lifecycle, preserving lineage,
  authority, budgets, joins, recovery, and independent handles instead of silently
  wrapping an unrelated child stack.
- Direct `underlying_agent` access now fails closed with
  `UNDERLYING_AGENT_PROFILE_UNSUPPORTED` in `durable` and `service`; the explicitly
  unsafe, deprecated escape hatch remains only for `quick`/`legacy` migration and is
  still scheduled for removal in 1.0.
- The AgentOS facade now reads the narrow harness `storage` accessor and refuses
  post-construction database replacement with `AGENTOS_STORAGE_REBIND_UNSUPPORTED`,
  instead of reaching through and mutating the private Agno agent.
- A deterministic generated Python API reference now covers every top-level
  `agnoclaw.__all__` export with its runtime signature, implementation owner, and a
  canonical public-surface digest. Generation fails for missing public callable
  docstrings or invalid/duplicate exports, removes process-specific addresses, and a
  CI/documentation contract rejects stale or incomplete output.
- The owned two-node PostgreSQL promotion probe now certifies the learning ledger on
  the same measured-lag/fence/promotion boundary as the runtime store: exact evaluated
  candidate replay, typed bounded no-writer failure, existing/fresh pool recovery,
  post-promotion CAS mutation, contiguous learning events, connection bounds, and
  exact cleanup.
- Per-provider durable model-call boundaries plus exact Agno 2.9 tool-batch and
  registered-capability approval recovery for the certified non-streaming lifecycle
  path. The three-scenario tool-checkpoint process-kill gate resumes after a committed
  tool result, blocks an ambiguous second provider call for reconciliation, and
  completes from a settled second-provider artifact: three real crashes, six provider
  calls, one post-restart call, three tool effects, two completions, one reconciliation
  wait, zero duplicate provider calls/effects, and intact databases. A separate child
  dies immediately after the atomic `waiting_for_approval` request commit; a new host
  settles and idempotently replays the exact decision, reconstructs from the first
  provider artifact, executes the registered capability once, calls the second
  provider once, and completes with one request, one approved record, one tool effect,
  zero duplicates, and intact databases. This certification requires the Agno 2.9
  `tool-batch` checkpoint capability, an agnoclaw-materialized native Agent with a
  public-factory model whose runtime manifest is run-owned/isolated/recreatable,
  durable runtime and Agno databases, a shared ArtifactStore, non-streaming `start()`,
  and governed registered capabilities. Raw/custom tools, parser/output-model or
  persisted-stream paths, cloud receipts, power loss, multi-host recovery, and
  production soak remain outside it; Agno 2.6.4 keeps the conservative legacy path.
- A real-process full `AgentHarness.start()`/Agno model-stack restart gate. The
  boundary is now factory-backed, and four disposable children die after a planned
  model intent but before dispatch, during the entered provider call, after durable
  result/operation settlement but before run completion, and at a second planned
  boundary reopened with a changed factory implementation digest. A new harness
  reopens the Agno session database, runtime ledger, and artifact store; the retained
  gate proves one exact safe continuation, one mandatory reconciliation wait, one
  completion from the durable result, and one pre-dispatch
  `RUN_RECOVERY_SPEC_MISMATCH`. It records three total provider calls, only one
  post-restart call, zero duplicates, zero blind ambiguous redispatches, thirteen
  unique transports, five-of-five graceful recovery closures, and all databases
  intact. Every crash is discovered through bounded owner-scoped
  `recover_pending_runs()` startup scanning rather than direct known-ID recovery. This
  certifies the outer single model-operation lifecycle with a
  provider-neutral public `AgnoModelFactory`; the narrower
  Agno-2.9 tool/approval continuation envelope is certified separately above, while
  arbitrary raw/nested/parallel tool graphs, live-provider receipts, power loss,
  multi-host recovery, and soak remain open.
- A real-process capability-effect crash gate around `OperationGateway`. A child dies
  immediately before provider dispatch and immediately after a separate provider ledger
  commits but before gateway settlement, for all four effect classes. The retained
  eight-scenario run proves safe retry for read-only/idempotent work, provider-key
  deduplication, mandatory reconciliation for compensatable/non-repeatable ambiguity,
  terminal replay without dispatch, both-ledger integrity, zero duplicate external
  effects, and zero blind ambiguous redispatches. This is deliberately not represented
  as live-provider, arbitrary Agno model/tool continuation, power-loss, multi-host, or
  soak evidence.
- A fail-closed operation-kind certification contract. Every `OperationKind` must map
  to at least one real-process crash probe whose integration oracle emits and asserts
  the exact kind; adding an unproved operation category now fails the ordinary suite.
  The capability crash probe is also wired into hosted primary CI instead of remaining
  a locally retained integration.
- A real-process learning-reconciliation worker restart gate. A child dies inside the
  read-only observer while holding an owner-scoped database-clock lease; premature
  takeover is denied, the replacement advances fence 1→2 after expiry, observes once,
  commits one evidence-bound reconciliation with zero promotion redispatch, releases
  cleanly, and preserves ledger integrity. Hosted primary CI retains the oracle.
- A bounded public `ContextContinuationRecord` for compaction-critical goal, plan,
  progress, decision, approval, open-question, test, file, and citation state. Reviewed
  manual compaction persists every structured entry as a stable, exact-scope,
  independently searchable/rehydratable invariant with record/field provenance while
  keeping the narrative summary. The live checkpoint uses a token-efficient priority
  index and leaves full item IDs in the content-free manifest/search results, preventing
  identifier overhead from defeating the 70% release gate. Ambiguous summary plus
  continuation input fails before maintenance; every field and aggregate are bounded;
  checkpoint savings use source-message tokens so derived entries cannot inflate the
  pre-compaction baseline. Automatic compaction source-binds the first real user goal,
  carries the latest record with source-item/record provenance, and lets an explicit
  reviewed record supersede it. Identical carried structured values collapse to their
  newest search result without deleting prior immutable evidence. Internal summary
  synthesis is tool-free and typed as harness context; tool-shaped or empty output is
  rejected for a deterministic transcript fallback. Automatic replacement bounds only
  the narrative when needed to meet its 70% hysteresis target; checkpoint identifiers,
  latest intent, and retained invariants are never clipped, and an oversized required
  checkpoint still fails closed. Automatic extraction/merge for the remaining fields
  and model-backed fidelity remain open.
- An opt-in cooperative same-host context reader/writer fence. Direct runs and open
  streams hold shared exact-scope POSIX file locks; manual/automatic replacement holds
  the exclusive lock through pre-save validation; overflow recovery converts its sole
  reader before archiving. Contention fails retryably before model dispatch or session
  mutation, ownership loss fails closed, lock files and public errors contain only the
  tenant/user/session scope digest, and the canonical lock-directory digest is bound
  into the harness spec. Unit, harness, competing-overflow, and real child-process
  contracts pass. This does not claim multi-host, network-filesystem, PostgreSQL
  advisory-lock, or non-cooperating Agno-writer fencing.
- A deterministic long-run continuity gate over the real public harness. It completes
  100 synthetic Agno turns in a disposable SQLite session, executes 11/11 declared
  side-effect-free tools through Agno's native function-call loop and agnoclaw hooks,
  triggers 13 automatic archive-first compactions plus one final boundary,
  closes/reopens at two scheduled turns and once for final verification, checksum-loads
  every archived segment, retrieves and selectively rehydrates exact head/middle/tail
  input and tool-result facts, proves the content-free manifest chain and exactly-once
  persisted injection, and finishes with bounded live context. Tool markers originate
  only in tool responses; the probe performs no network calls and keeps stdout
  content-free and finishes at 1,253/1,800 live tokens. It exposed and fixed automatic
  context preflight on a brand-new Agno
  session; only Agno's exact missing-session result maps to zero history, while real
  storage errors propagate. Its opt-in live mode requires explicit local/remote
  acknowledgement and exact model inventory/digest resolution. A pinned Agno 2.9.0
  `qwen2.5:7b` Ollama run passes 100 provider turns, 11/11 native tools, 14
  compactions, three reopens, typed canonical recovery, all artifact checks, and
  1,038/1,800 final tokens; `qwen3:0.6b` fails closed after skipping a mandated call.
  Cloud-provider breadth, external/irreversible effects, abrupt death during model/tool
  work, multi-host behavior, semantic drift, and hours-long certification remain
  separate gates.
- An opt-in, model-backed Agno Learned Knowledge benefit gate. Six synthetic held-in,
  held-out, and alias-transfer cases run as balanced fresh-Agent pairs through the
  governed improvement runner. The candidate receives actual vector-backed Agno
  `<relevant_learnings>` context with model save/search tools disabled; the control has
  no learning, and both share exact prompts, instructions, deterministic decoding, and
  an objective token verifier. The probe binds resolved Ollama model/embedder content
  digests, refuses undeclared remote egress and non-empty evidence directories, reserves
  stdout for content-free JSON, documents an isolated optional-dependency environment,
  and requires positive benefit with no loss in every slice. The frozen Agno 2.9.0
  `qwen3:0.6b`/`nomic-embed-text` configuration passes three
  consecutive runs with 6/6 wins and `+1.0` mean delta per slice. This is one local
  mechanism smoke; previous-version, multi-provider, production-vector, and long-soak
  benefit certification remain open.
- A repeatable real-process SQLite crash-recovery gate. It deliberately calls
  `os._exit()` inside disposable run-create, lifecycle-transition, operation-prepare,
  dispatch, and settlement transactions, then reopens the database and requires the
  partial mutation to be invisible, duplicate retry to converge on exactly one event,
  `integrity_check` to pass, and the WAL to checkpoint. The retained run passes 50/50
  ungraceful exits; host-power loss, filesystem durability, provider-side effect truth,
  and production-duration soak are not inferred.
- A strict Linux Docker subject for paired improvement evaluation. It accepts only an
  immutable full image ID or repository digest and exact platform; refuses image-declared
  writable volumes; overrides entrypoint/healthcheck; injects no host environment,
  network, or mounts; runs non-root with a read-only root and bounded no-exec `/tmp`;
  drops all capabilities; enables `no-new-privileges` and Docker's built-in seccomp;
  bounds CPU, memory/swap, PIDs, open files, core dumps, streams, and wall time; and
  verifies the exact owner label before forced cleanup. Isolation and exact-command
  digests enter runner-1.2 evidence. Five adversarial contracts plus a real immutable-
  image Docker probe pass; Docker daemon/kernel/image trust, VM isolation, provider
  egress/credential brokering, and production soak are explicitly not claimed.
- A closed-loop learning-effectiveness foundation over SQLite and PostgreSQL schema v6.
  `AgentHarness.observe_learning_application()` distinguishes retrieval from actual
  application, resolves the exact promoted target, reauthorizes the source run, and
  stores content-free immutable evidence outside the candidate CAS row. One independent
  host/operator outcome may settle an applied record with exact candidate/run linkage,
  sign-consistent scoring, immutable evidence, and idempotent conflict detection.
  Bounded owner-scoped reads and `summarize_learning_effectiveness()` return
  insufficient/retain/review/quarantine recommendations only after configured outcome
  and independent-run minimums. Recommendations never mutate confidence, quarantine,
  roll back Agno knowledge, or promote a replacement. SQLite passes the focused
  adversarial contract and a disposable PostgreSQL 17 lane passes 11/11; model-backed
  no-learning/previous-version benefit certification remains open.
- A content-free observability and owner-authorized inspection foundation. Runtime
  outbox batches now project through a schema-v1 allowlist with domain-separated
  HMAC identifiers, registered event cardinality, bounded token/cost measurements,
  flat structured logs, and low-cardinality counters. The optional `otel` extra adds
  the OpenTelemetry SDK and OTLP/HTTP exporter without expanding the six-dependency
  core. `RuntimeRunInspector` and `agnoclaw inspect run` expose a bounded recovery
  view only after exact owner/scope authorization; raw identities, content, targets,
  arguments, reasons, artifact addresses, and driver errors remain absent. SQLite and
  PostgreSQL stores now support current-schema, database-enforced read-only operation
  for inspection, with typed mutation refusal. CI installs the exact built wheel with
  the `otel` extra and runs the real SDK/log/metric/OTLP import smoke. Live GenAI
  spans/trace linkage, exporter-health SLOs, multi-destination receipts, support
  bundles/UI, Collector chaos/soak, and production retention/TLS/RBAC/key-rotation
  certification remain open.
- A first-party exact-state Agno Learned Knowledge reconciler. Candidate-derived Agno
  titles now include 128 bits of the immutable candidate digest and remain bounded with
  their full ledger reference. `AgnoLearnedKnowledgeReconciliationObserver` uses the
  public exact `VectorDb.name_exists` contract—not semantic similarity—stages a scoped,
  content-free evidence artifact, and lets the existing CAS coordinator settle an
  ambiguous promotion/rollback without replay. `AgentHarness` selects it automatically
  when the first-party promotion adapter is active; custom promotion backends still
  require an explicit matching observer. The capability contract and focused behavior
  pass on both Agno 2.6.4 and 2.9.0. Durable sweep leases/checkpoints and first-party
  observer scheduling remain open.
- Pre-ledger package evidence after the observability/inspection foundation measures
  2,856,507 source bytes, a 634,289-byte wheel, and a 1,626,749-byte sdist. Only the
  three aggregate ceilings move narrowly to 2,859,000/636,000/1,630,000 bytes. The six
  core dependencies, 400 KB/10,000-line single-module cap, two-second import ceiling,
  and 900-imported-module ceiling remain fixed; the largest module stays at 399,896
  bytes/9,984 lines, median import is 0.317701 seconds, and 888 modules load. Both
  artifacts pass Twine. Exact final sdist bytes and hashes remain detached because
  bundled release evidence changes the sdist.
- A schema-v12 durable scheduler over SQLite and PostgreSQL RuntimeStores. Deterministic
  occurrence, attempt, and lifecycle idempotency identities combine with atomic claims,
  expiring leases, fencing tokens, bounded polling, crash reattachment, dead-letter
  history, database-authoritative clocks, timezone-aware cron, stable jitter, bounded
  exponential retry, misfire policy, and cross-worker concurrency keys. The embedded
  worker uses the existing lifecycle rather than a second execution engine, awaits
  owned tasks before closing resources, and preserves ambiguous outcomes for same-
  attempt reconciliation instead of manufacturing failure. CLI CRUD/inspection/manual
  trigger and worker commands default to a RuntimeStore path; legacy JSON scheduling is
  explicitly compatibility-only. Scheduled learning is off unless both the immutable
  worker profile and per-job consent allow it, and the safe CLI preset requires trusted
  tenant, user, and session scope.
- Pre-release package evidence for the durable scheduler measures 2,523,099 source
  bytes, a 567,776-byte wheel, and a 1,512,177-byte sdist. Only those three aggregate
  ceilings move narrowly to 2,525,000, 570,000, and 1,515,000 bytes. The six-dependency,
  400 KB/10,000-line single-module, two-second import, and 900-imported-module limits do
  not move; the measured import is 0.298456 seconds across 878 modules.
- Pre-ledger package evidence after the local migration checkpoint measures 2,586,737
  source bytes, a 581,268-byte wheel, and a 1,532,206-byte sdist. Only the three
  aggregate ceilings move narrowly from 2,525,000/570,000/1,515,000 to
  2,590,000/583,000/1,535,000 bytes. The six-dependency, 400 KB/10,000-line
  single-module, two-second import, and 900-imported-module limits remain fixed; the
  facade is 399,896 bytes/9,984 lines and median import is 0.278250 seconds across 880
  modules. Both artifacts pass Twine; exact final sdist bytes and hashes remain
  detached because bundled release documentation changes the sdist.
- Pre-ledger package evidence after the read-only PostgreSQL migration scanner measures
  2,647,513 source bytes, a 595,044-byte wheel, and a 1,554,802-byte sdist. Only the
  aggregate ceilings move narrowly from 2,590,000/583,000/1,535,000 to
  2,650,000/597,000/1,560,000 bytes. The six-dependency, 400 KB/10,000-line
  single-module, two-second import, and 900-imported-module limits remain fixed; the
  facade stays at 399,896 bytes/9,984 lines and the measured cold import uses 883
  modules. Both artifacts pass Twine; exact final sdist bytes and hashes remain detached
  because bundled release documentation changes the sdist.
- Optional external writer-authority admission for `PostgresRuntimeStore`. A deployment
  adapter supplies a fresh linearizable authority ID, exact PostgreSQL `cluster_name`,
  monotonically fenced generation, and conservative relative lease TTL. Each store
  access verifies the named server is writable, installs PostgreSQL 17
  `transaction_timeout` inside the lease safety margin, and revalidates the same grant
  immediately before commit. Provider outage/timeout, invalid or short grants,
  stale/conflicting generations, standby or server mismatch, commit-boundary change,
  and transaction expiry fail closed as retryable, content-free
  `POSTGRES_WRITER_AUTHORITY_DENIED` reasons. Default behavior is unchanged when no
  provider is injected.
- First-party `EtcdPostgresWriterAuthority` over etcd's v3 JSON gRPC gateway. It pins
  an expected cluster ID, performs two linearizable exact-key reads around live lease
  inspection, requires one dedicated attached key, treats `mod_revision` as the fence,
  subtracts elapsed/uncertainty from relative TTL, bounds response sizes, rejects
  redirects and unsafe endpoints, and never creates, renews, transfers, watches, or
  caches authority. Operators inject a hardened `httpx.Client` for production mTLS,
  CA, proxy, and connection policy. `EtcdGatewayCredentials` supplies exact-origin,
  bounded JSON-gateway token exchange and caching inside the authority deadline, with
  one conditional 401 refresh; authentication content never enters public errors.
- An owned three-member etcd security/quorum gate using the immutable official 3.6.14
  image. It generates unique client/server and peer certificates, mounts no CA private
  key, enforces client and peer certificate authentication plus TLS 1.2, enables RBAC,
  and proves exact-key controller/reader privileges and three 403/code-7 negative
  cases. One stopped voter remains available; stopped majority fails closed as
  `etcd_timeout` in 1.003 seconds; recovery through another endpoint advances the fence
  from 3 to 4. The first run passes in 7.328 seconds and removes all four owned Docker
  resources plus its certificate workspace. This certifies the local regression—not
  external controller election, durable multi-AZ topology, true network partition,
  endpoint/key/certificate rotation, watchdog/STONITH, backup, or production RPO/RTO.
- Pre-artifact-certification package evidence for the secure quorum tranche measures 2,403,138 source
  bytes, a 549,038-byte wheel, and a 1,480,007-byte sdist. Only the exceeded aggregate
  source/sdist ceilings move narrowly to 2,405,000 and 1,483,000 bytes; the 551,000-byte
  wheel, six dependencies, 400 KB/10,000-line module, two-second import, and 900-module
  limits do not move. Exact final artifact sizes and hashes remain detached provenance
  because bundled documentation changes the sdist.
- An owned two-writable-timeline PostgreSQL 17 adversarial gate. It deliberately
  promotes a standby while the old primary remains writable, proves independent
  unguarded RuntimeStore commits diverge, then proves the optional guard denies the
  stale writer, both writers during authority outage, and an over-lease transaction;
  commits only on the named writer; and rolls back an injected mutation when the
  authority generation changes immediately before commit. Five earlier
  hardened PostgreSQL 17.10 runs pass with stale denial in 0.001–0.003 seconds,
  authority-outage denial in 0.001–0.002 seconds, server timeout in 0.506–0.512
  seconds, one connection per pool, and six-of-six cleanup. A newer gate replaces the
  in-process double with official etcd 3.6.14: standalone revision/revocation/natural-
  expiry/loss proof passes, and the dual-writer gate stops/restarts the exact etcd
  process, returns 0.002–0.004-second authority denials, rolls back commit-boundary
  revision change, aborts at 0.516 seconds, and cleans seven of seven resources. This is
  Agnoclaw-client containment, not external controller election, durable production
  quorum/partition, arbitrary-client watchdog/STONITH, host-pause, or RPO/RTO
  certification. The separate local security/quorum gate covers mTLS/RBAC/member-loss
  regression without broadening that claim.
- Package evidence for writer authority and the first-party etcd adapter measures
  2,394,754 source bytes, a 547,546-byte wheel, and a 1,465,038-byte latest pre-final sdist.
  Aggregate ceilings move narrowly to 2,400,000/551,000/1,478,000. Six dependencies,
  the 400 KB/10,000-line facade, two-second import, and 900-module budgets remain fixed.
  CI and publish verification now also install the exact built wheel with its
  PostgreSQL extra and import/construct the public authority grant surface before any
  artifact can be published. Composed PostgreSQL probes resolve only their exact sibling
  helpers under Python isolated mode, so installed-wheel drills cannot accidentally rely
  on checkout path injection.
- An owned PostgreSQL 17 round-trip recovery gate that promotes twice and rewinds/
  rejoins both former writers. It enables data checksums, requires
  `full_page_writes`, pins an exact checkpoint/replay boundary before each cutover,
  proves both bounded no-writer intervals, keeps existing/fresh read-write pools under
  a two-connection cap, and compares exact final state plus seven contiguous events.
  The first old writer is abruptly killed; the second is cleanly stopped and must pass
  `pg_rewind --no-ensure-shutdown`. The helper avoids `pg_rewind -R`, uses a mode-0600
  temporary password file, persists exactly one password-free `primary_conninfo`, and
  treats any failed rewind target as requiring a fresh base backup. Five hardened local
  drills pass with zero observed acknowledged loss and eight-of-eight cleanup. CI runs
  the pure authority contracts and live gate. This closes only the exact local two-node
  double-rewind regression; automatic election/external fencing, split brain/quorum,
  multiple faults, production endpoint/rejoin automation, large-data/archive recovery,
  and production RPO/RTO remain open.
- Package evidence for the role-rotation gate leaves source and wheel unchanged at
  2,368,847 and 540,757 bytes. Its probe, tests, CI wiring, and documentation increase
  only the pre-final sdist to 1,436,762 bytes, so only that ceiling moves narrowly from
  1,427,000 to 1,439,000. Dependency, source, wheel, facade, and import budgets stay
  fixed.
- A synchronous PostgreSQL acknowledgement/abrupt-loss gate on the same owned topology.
  The primary is configured for one exact `FIRST 1` standby and `remote_apply`; the
  probe disconnects only the standby's UUID-named replication network, starts a real
  RuntimeStore commit, and fails if PostgreSQL acknowledges it during the bounded
  partition. Network reattachment must restore `streaming/sync` and only then release
  the commit after replay. The probe commits and verifies an exact run/lease/event
  manifest, kills the old primary with `SIGKILL`, proves a bounded no-writer interval,
  promotes the fenced standby, and requires the existing/fresh pools to recover with
  zero observed acknowledged state or event loss. Five completed local PostgreSQL
  17.10 drills pass, retain the two-connection cap, and remove all six resources each.
  The tag path retries registry refresh within the overall timeout; an explicit
  immutable digest path supports fail-closed offline repetition without trusting a
  mutable cached tag. This proves one remote-apply/two-node/single-
  fault regression path; the separate round-trip gate now covers local rewind/rejoin.
  Automatic election/external fencing, network-partition/physical-fence injection,
  simultaneous/multiple failures, production sync-latency availability, and production-managed
  rejoin/rotation remain open.
- Package evidence for that gate leaves source and wheel unchanged at 2,368,847 and
  540,757 bytes. Its pure tests, CI wiring, probe, and documentation increase only the
  pre-final sdist to 1,424,586 bytes, so only that ceiling moves narrowly from
  1,415,000 to 1,427,000; dependency, source, wheel, facade, and import budgets stay
  fixed.
- An owned two-node PostgreSQL 17 fenced-promotion gate. The probe creates only
  UUID-named/labeled primary, standby, base-backup helper, volumes, and network after
  explicit `--allow-topology-create`; resolves the image content digest and server
  major; keeps both published ports loopback-only; verifies strict hot-standby write
  rejection; deliberately pauses replay to measure positive lag; and refuses promotion
  unless the last acknowledged WAL LSN is replayed and the old primary is stopped. A
  no-writer read must fail within the bounded pool window before promotion. The same
  `target_session_attrs=read-write` pool and a fresh pool must then select the promoted
  writer with acknowledged state, fence, event order, and the two-connection cap
  intact. Cleanup attempts every exact owned resource without masking the primary
  failure, and the old primary is never restarted. Five consecutive local passes
  succeed; CI owns a separate topology job. This is fenced asynchronous promotion
  regression evidence, not automatic failover, synchronous/unplanned-loss RPO,
  split-brain certification, old-primary rewind/rejoin, or production RTO proof.
- Bounded, observable lifecycle admission. Exact-session lanes now rotate ready work
  across tenants, cap global/tenant/session process-local waiters, expire admission
  after a configurable finite interval, clean cancellation races, and expose
  content-free current/peak/
  outcome counters through `runtime_admission_stats()`. Queue-full and timeout outcomes
  use public retryable `RUNTIME_ADMISSION_OVERLOADED`; store-pool saturation's existing
  `RUNTIME_STORE_OVERLOADED` is now exported from the runtime facade. A safe loopback-
  test-only PostgreSQL benchmark gates exact-owner isolation, p50/p95/p99 get/recovery
  latency under a noisy neighbor, hard 2+2 pool saturation, cleanup, and the tenant
  fairness oracle. Cross-process weighted fairness and production-scale SLO/DR proof
  remain open.
- A destructive-target, loopback-test-only PostgreSQL recovery rehearsal. The probe
  requires an exact running container plus explicit `--allow-target-reset`, refuses
  non-test/non-restore databases, captures a native custom-format dump, restores into
  an isolated database, and compares a content-minimized manifest of every
  `runtime_*` row, column, index, constraint, and sequence. It then verifies ordered
  event identity and restored start-idempotency replay before removing the exact dump,
  target database, and source marker. Every cleanup is attempted without masking the
  primary failure. The CI service lane runs the rehearsal; production encryption,
  off-host retention, artifact/key restore, PITR/replica promotion, corruption drills,
  and measured production RPO/RTO remain open.
- Conservative PostgreSQL outage semantics. Every pooled libpq connection now receives
  the configured connect timeout. Capacity/acquisition timeout remains retryable
  `RUNTIME_STORE_OVERLOADED`, while a connection lost after acquisition becomes public
  content-free `RUNTIME_STORE_CONNECTION_LOST`, is not blindly retryable, and requires
  an authoritative re-read/reconciliation decision. The hardened loopback restart
  probe validates the exact running container and published port, deliberately stops
  it, requires a bounded typed failure, heals it even on primary failure, proves
  existing/fresh pool recovery, acknowledged-state/event/fence continuity, and a
  two-connection bound, then removes its exact marker. The CI service lane runs this
  last. The separate owned topology gate now covers measured lag and planned fenced
  promotion; network partition/split-brain behavior, deployment controller/external
  fencing, unplanned-loss RPO, old-primary rejoin, production memory/connection
  budgets, and timed production RTO remain open.
- Canonical pre-dispatch operation result slots. Every `OperationIntent` provisions a
  content-independent future result identity; SQLite/PostgreSQL planned, artifact, and
  settlement evidence carry it; successful settlement normalizes legacy omissions and
  rejects an explicit mismatch or conflicting artifact fulfillment. The opt-in
  `agnoclaw.testing` module adds exact-occurrence store crash scripts, finite-timeout
  database barriers, and a single-loop deterministic effect driver with pre-dispatch,
  before-effect, and after-effect gates. One shared SQLite/PostgreSQL conformance matrix
  now reopens every operation state, drives all four effect classes at every external
  boundary, retries transaction crashes exactly, and proves cancellation-before-
  dispatch-commit versus success-before-cancellation ordering.
- History-bounded recovery discovery. SQLite and PostgreSQL replace broad recovery
  indexes with partial indexes for executable runs, reconciliation waits, dispatchable
  operations, reconcilable operations, and existing child lineage. SQLite query-plan
  contracts reject ledger scans and temporary sorts; a safe temporary-database
  benchmark keeps 10,000-versus-1,000 terminal-history p95 ratios below 2.0; live
  PostgreSQL `EXPLAIN (ANALYZE, BUFFERS)` remains indexed over 10,000 terminal rows.
- A capability-only declared child-run preview on the existing lifecycle kernel.
  `HarnessRun.child()/children()/child_results()` create independently controllable
  children and deterministic typed outcome collections with explicit pending/success/
  failure partitions. Large synthesis inputs become complete operation-result artifact
  pointers rather than hidden truncation, and `read_child_artifact()` pages only a
  direct child's owner-authorized bytes. Children otherwise retain
  deterministic delegation identity, exact parent/root/depth lineage, isolated child
  sessions, inherited owner/context, subset-only budget/capability/learning grants,
  and optional parent step/tool-call links. Schema v11 atomically commits child and
  parent creation events, terminal settlement, join enforcement, recursive descendant
  cancellation, approval cancellation, transition idempotency, and outbox evidence in
  SQLite and PostgreSQL. Parent completion supports `all_success` and `collect`; active
  child workers observe propagated cancellation after lease renewal. Raw/default child
  tools fail lifecycle admission, and the text-returning `spawn_subagent` remains only
  on the named-legacy profile. A lease-owned child worker enforces its granted wall duration;
  successful model settlement records stable Agno token/duration/cost evidence and a
  reported token/cost overage fails before child completion. Missing provider metrics
  are marked unverified, and evidence-extraction failure cannot strand a successful
  provider effect. Child-spec schema `1.1` adds digest-bound bounded object result
  schemas while retaining `1.0` reads. Normalized output is graph-bounded and validated
  after successful operation/budget settlement but before completion, emits
  content-free evidence, and is revalidated during known-success recovery without
  rewriting the settled provider operation. `synthesize_children()` snapshots terminal
  direct children, defaults to all-success, preserves lossless artifact pointers,
  bounds and injection-frames untrusted evidence, reattaches while the same delegation
  is pending, and executes through another ordinary declared child.
  `DeclaredChildTemplate` binds host-owned child harness, purpose, budget, capability,
  join, learning, result-schema, scope, persistence, and inline-result policy into a
  model-visible idempotent/reconcilable capability; the model supplies only task and
  stable delegation identity. The same template can be registered at AgentOS for
  owner/scoped remote child start, independently controllable handles, bounded child
  lists, and typed inline-or-artifact result collections. Arbitrary capabilities cannot
  gain model-visible child authority by relabeling their kind. Startup recovery now
  re-reads under its fenced claim, certifies the complete persisted child ancestry,
  restores the exact declaration before a certified pre-model continuation, preserves
  its model timeout and output/budget checks, and cancels without dispatch beneath a
  terminal ancestor. Depth-16 lineage/cancellation/reaping passes on SQLite and
  PostgreSQL. Persisted admission-to-terminal deadlines, provider-preflight hard
  ceilings/receipts, a first-party governed cross-child artifact reader, and
  multi-worker production failover-control-plane proof remain open. All 33 current PostgreSQL-backed
  cases pass against disposable PostgreSQL 17, including in-transaction backend loss,
  pool reconnection, and a separate whole-database container restart drill.

- An authenticated version-`1.0` AgentOS lifecycle transport and evolved
  `RemoteHarnessClient`. New remote code can start or reattach a run and use the local
  `status/wait/events/output/cancel/command` grammar. Registered declared-child
  templates add `child/children/child_results` without accepting remote policy; result polling is independent of bounded
  gap-free cursor pages, and caller timeout/disconnect never implies cancellation.
  Claims remain authoritative, exact `agents:read`/`agents:run` scopes are enforced,
  open anonymous AgentOS fails closed, protected context/claim fields and oversized or
  unknown inputs are rejected, owner failures are hidden, and every handler/auth/query
  error is a safe versioned envelope. Native AgentOS `arun` stays compatibility-only.
  The same 51 transport/adapter contracts pass on Agno 2.6.4 and 2.8.7; exact wheel
  server-extra smoke is now a CI and publish gate.

- Profile-aware first-party process routing without a second run abstraction.
  Non-interactive CLI, heartbeat, and local scheduler work enters lifecycle
  `start()/wait()` under explicit `quick`/`durable`/`service`; async REPL/TUI model work
  now enters the same governed model operation and receives raw display events through
  a bounded process-local attachment. Slow displays detach without blocking or
  cancelling the run, interactive worker cancellation is explicit, and connected
  clients reconcile to authoritative terminal content. Sync chat uses a reusable
  harness-owned lifecycle coordinator; only named legacy retains direct streaming.
  Scheduler history relates each local
  occurrence to its `runtime_run_id`, records waiter cancellation as `detached` without
  cancelling the run, and stores safe failure codes instead of raw exceptions.
  CLI-owned and isolated scheduler harnesses now close on success, failure, and
  cancellation, including same-loop async shutdown and explicit detach supervision.
  The 100 affected CLI/heartbeat/routing contracts, including 18 new identity,
  cancellation, and ownership cases, pass on Agno 2.6.4 and 2.8.7. The 81-test
  presentation/client slice adds 11 slow-consumer, terminal-reconciliation,
  lifecycle-operation, and interactive-cancellation cases on both supported lanes.

- Artifact-backed provider-text replay on the existing lifecycle run facade.
  `start(..., persist_output=True)` and authenticated lifecycle HTTP starts (true by
  default, with an explicit structured-output opt-out) drain
  Agno streaming inside the already-governed model operation. Text batches at 8,192
  characters or 32 deltas, stages under the exact run owner, and atomically commits a
  content-free `run.output.segment` event, artifact reference, sequence, and outbox row
  in SQLite/PostgreSQL. `HarnessRun.output()` and `RemoteHarnessRun.output()` verify and
  cursor-page segments; remote pages cap at 50. Normal completion/cancellation flushes
  consumed text, slow displays cannot block persistence, injected rollback and
  artifact/protocol drift fail closed, and the terminal result remains authoritative.
  Abrupt process loss may lose one partial segment and does not make an ambiguous
  provider operation resumable. Segmented mode rejects structured output instead of
  silently changing its terminal type.

- Authoritative normalized lifecycle trajectories. Every emitted lifecycle
  `HarnessEvent` is projected before observer notification into an owner-bound,
  idempotent, terminal-fenced `trajectory.*` event whose sequence and outbox row commit
  atomically in SQLite and PostgreSQL. Raw prompts, response/error text, arguments,
  authority metadata, and unsafe identifiers are deterministically digested under
  depth/node/collection/32 KiB projection bounds; the public store input additionally
  rejects timezone-naive timestamps and payloads over 64 KiB. Immediate `EventSink`
  callbacks remain compatibility notifications and cannot override committed lifecycle
  truth; named-legacy direct `run/arun` fail-closed behavior is unchanged.
- A public async-first `RuntimeOutboxWorker` over both runtime stores. It leases bounded
  ordered batches, enforces a delivery deadline below the lease, acknowledges only
  after complete exporter success, and exact-token defers the whole batch with bounded
  exponential retry after failure or timeout. Cancellation releases work immediately;
  safe batch reports never persist exporter exception text. Delivery is explicitly at
  least once and consumers deduplicate by `event_id`. Schema v8 adds configurable
  bounded attempts, one-at-a-time poison isolation, safe-coded exact-token quarantine,
  bounded dead-letter quarantine, and exact-timestamp CAS requeue on
  SQLite/PostgreSQL. Schema v10 replaces unaudited raw listing/requeue with the small
  async `RuntimeDeadLetterAdmin`: authoritative identity and exact scopes, same-tenant
  owner targeting, owner-bound opaque cursors, content-minimized inspection history,
  and atomic mutation-idempotent replay audit. It has no global owner enumeration and
  cancellation cannot hide an in-flight commit. A packaged operator UI, independent
  audit anchoring/export, tenant fairness, multi-destination state, and concrete
  OTEL/client adapters remain open.
- A first-party preview `ImprovementEvaluationRunner` for frozen harness-change
  experiments. It verifies the exact scoped hypothesis/cluster/judge artifacts before
  execution, creates and closes a fresh async baseline/candidate resource per rollout,
  alternates execution order, stages one content-addressed evidence artifact per pair,
  enforces rollout/token/wall/cost budgets, records subject timeout/error as negative
  evidence, invalidates lifecycle/evaluator failure, safely normalizes diagnostics,
  binds the installed runner implementation digest, and emits three paired 95%
  confidence records. The pure gate now applies confidence lower bounds and retains
  causal-cluster evidence. This is resource-lifecycle isolation, not OS/process
  sandboxing, and adds no edit or promotion authority.
- A factory-based `context_provider_capability()` for explicitly attested read-only
  Agno provider queries. The stable single-tool surface is run-owned and routes through
  scopes, policy/approval, leases, operation settlement/replay, redaction, artifacts,
  and spill; it propagates the active Agno `RunContext`, bounds typed answers, marks
  content untrusted, emits query events, and closes on every dispatch outcome. Raw
  provider tools remain direct-only; writes and unclassified nested MCP tools require
  explicit effect/reconciliation capabilities.
- Opt-in artifact-first automatic context compaction: 90% proactive and 97%
  deterministic emergency triggers, a 70% hysteresis release boundary, session-local
  run/stream maintenance admission, scoped summaries, and fail-closed prerequisites.
- One fenced reactive retry for Agno-classified context overflow. It retries only the
  exact non-streaming model invocation, holds sole-session maintenance through retry,
  refuses after observed tool activity, and terminates after one attempt.
- Opt-in lossless spill for governed registered-capability and lifecycle first-party
  tool results. Oversized results
  settle to their authoritative scoped artifact before the model receives a bounded
  head/tail envelope; the internal read-only `read_spilled_output` capability pages
  verified content within the owning run or trusted session and denies cross-session
  access. Compaction classifies spill envelopes and failures as deterministic
  invariants, retains their stable item IDs, and exposes a bounded recent artifact
  index with searchable/rehydratable provenance. Plugin/pack registered capabilities
  plus configured MCP calls join the governed first-party path; direct compatibility,
  context-provider, caller-supplied raw MCP, and outer-model output remain outside this
  slice.
- Explicit versioned replay semantics for every currently constructed first-party tool,
  included in the immutable harness-spec digest. During `start()` only, the native Agno
  entrypoint is wrapped at dispatch: exact intent commits first, the active run lease is
  renewed at the no-effect boundary, sync work is observed through completion, after-tool
  policy/redaction precedes artifact commit, duplicate call IDs replay the committed
  value, and non-repeatable cancellation is classified as ambiguous. Direct
  `run()`/`arun()` compatibility is unchanged; newly added undeclared built-ins fail
  construction instead of acquiring guessed effects.
- One extension-ingress contract: plugins expose `PluginManifest.capabilities`, packs
  register `[provides].capabilities`, and both flow through the existing immutable
  `CapabilitySpec` registry. Raw plugin/pack/context-provider functions and empty mutable
  toolkits such as caller-supplied pre-discovery MCP are retained only for named-legacy
  direct `run/arun` compatibility; `start()` and explicit-profile convenience calls
  reject them with tool/source evidence before creating a run. Empty dynamic toolkits
  can no longer become invisible during construction.
- Stable MCP Python SDK 2.0 / protocol `2026-07-28` tool ingress. One fixed
  `search_mcp_tools`/`call_mcp_tool` surface keeps remote schemas out of the base prompt,
  bounds servers/pagination/schema/catalog/search/arguments/results/JSON, refreshes and
  digest-binds selection on the same client before call,
  preserves structured/public multi-content results, withholds private `_meta`, treats
  annotations as untrusted, distinguishes connection/discovery/call/close errors, and
  routes configured calls through conservative first-party lifecycle effects. Eligible
  quick/legacy runs own and close their async clients; Streamable HTTP disables
  redirects, stdio and explicit legacy SSE remain available, and the MCP extra is
  certified against an in-memory v2 server.
- Rebaselined source from 1.885 MB to 1.922 MB and, from exact build evidence, wheel/
  sdist from 440/1,150 KB to 450/1,175 KB for governed extension inventory and the
  replacement of the obsolete dynamic SSE wrapper with a bounded async MCP 2.0 client,
  followed by the measured run-owned context-provider query adapter, its executable
  contracts, and public guide. The scoped paired improvement runner/statistical gate
  then moves only the aggregate ceilings to 1.962 MB source, 456 KB wheel, and 1.187 MB
  sdist. Exact request-checkpoint continuation then measures 1,981,742 source bytes, a
  459,998-byte wheel, and a 1,193,491-byte sdist and narrowly moves those aggregate
  ceilings to 1.983 MB, 461 KB, and 1.195 MB. The facade, single-module, import, and
  core-dependency ceilings remain fixed. The authoritative trajectory tranche measures
  1,998,191 source bytes, a 463,796-byte wheel, and a 1,200,391-byte pre-evidence-page
  sdist and moves only those aggregate ceilings to 2.000 MB, 465 KB, and 1.204 MB; the facade remains
  below its unchanged 400,000-byte/10,000-line cap.
  The measured generic outbox-worker tranche then uses 2,007,094 source bytes, a
  466,092-byte wheel, and a 1,206,529-byte pre-evidence-page sdist, moving only those
  ceilings to 2.009 MB, 467 KB, and 1.209 MB. Core dependencies, facade, single-module,
  import-time, and imported-module caps remain unchanged.
  Schema-v8 poison isolation/quarantine and both v7→v8 migration contracts then measure
  2,019,243 source bytes, a 467,613-byte wheel, and a 1,210,258-byte pre-evidence-page
  sdist, moving only aggregate ceilings to 2.021 MB, 469 KB, and 1.214 MB.
  Owner-scoped, age-gated, store-clock startup recovery then measures 2,033,384 source
  bytes, a 471,105-byte wheel, and a 1,217,832-byte pre-evidence-page sdist, moving only those
  aggregate ceilings to 2.034 MB, 472 KB, and 1.220 MB. Dependency, import, and
  400,000-byte/10,000-line facade caps remain unchanged.
  The bounded operation-reconciliation coordinator then measures 2,075,413 source
  bytes, a 478,005-byte wheel, and a 1,229,043-byte pre-evidence-page sdist, moving only
  aggregate ceilings to 2.076 MB, 479 KB, and 1.231 MB. Dependency, import, and facade
  caps remain unchanged; the facade measures 399,251 bytes and 9,970 lines.
  Schema-v10 owner-authorized dead-letter administration then measures 2,111,226 source
  bytes, a 484,200-byte wheel, and a 1,241,584-byte pre-evidence-page sdist, moving only
  aggregate ceilings to 2.113 MB, 485 KB, and 1.244 MB. Core dependencies, the
  2-second/900-module import budgets, and the unchanged 400,000-byte/10,000-line facade
  cap do not move.
  The authenticated AgentOS/remote lifecycle tranche then measures 2,159,004 source
  bytes, a 494,540-byte wheel, and a 1,261,276-byte pre-final-evidence sdist and narrowly
  moves only the aggregate ceilings to 2.161 MB, 496 KB, and 1.264 MB. Server packages
  remain optional; the core dependency, import, and unchanged facade caps do not move.
  Profile-aware first-party routing then measures 2,164,651 source bytes, a 496,258-byte
  wheel, and a 1,267,938-byte pre-final-evidence sdist, moving only aggregate ceilings
  to 2.166 MB, 498 KB, and 1.271 MB. The six core dependencies, 2-second/900-module
  import limits, and 400,000-byte/10,000-line facade cap remain unchanged.
  Lifecycle-governed interactive presentation then measures 2,173,954 source bytes, a
  499,229-byte wheel, and a 1,274,720-byte pre-final-evidence sdist, moving only
  aggregate ceilings to 2.175 MB, 501 KB, and 1.277 MB. Presentation execution lives in
  a focused internal module, so the unchanged dependency/import/facade caps still hold.
  Artifact-backed provider-text replay then measures 2,200,442 source bytes, a
  504,339-byte wheel, and a 1,285,246-byte pre-final-evidence sdist, moving only
  aggregate ceilings to 2.202 MB, 506 KB, and 1.288 MB. The six-dependency,
  2-second/900-module import, and 400,000-byte/10,000-line facade caps do not move.
  Schema-v11 declared-child lineage/join/cancellation then measures 2,255,866 source
  bytes, a 514,676-byte wheel, and a 1,306,698-byte pre-final-evidence sdist, moving
  only aggregate ceilings to 2.258 MB, 516 KB, and 1.310 MB. Extracting compatibility
  trace helpers reduces the facade to 398,525 bytes/9,942 lines; fixed dependency,
  import, and facade ceilings do not move.
  Active-worker child deadlines, stable Agno settlement evidence, post-settlement
  token/cost enforcement, and cancellation-safe shared supervision then measure
  2,270,873 source bytes, a 519,130-byte wheel, and a 1,316,322-byte pre-final-evidence
  sdist, moving only aggregate ceilings to 2.272 MB, 521 KB, and 1.320 MB. Cohesive
  concurrency/reconciliation modules keep the facade at 398,974 bytes/9,951 measured
  lines; dependency, import, and facade ceilings remain unchanged.
  Typed partial-failure collection and lossless direct-child result/artifact handoff
  then measure 2,286,756 source bytes, a 523,071-byte wheel, and a 1,322,517-byte
  pre-final-evidence sdist, moving only aggregate ceilings to 2.288 MB, 525 KB, and
  1.326 MB. The six-dependency, import, and unchanged facade caps still hold.
  Declaration-bound child output schemas and governed synthesis then measure 2,297,299
  source bytes, a 525,944-byte wheel, and a 1,331,781-byte pre-final-evidence sdist,
  moving only aggregate ceilings to 2.299 MB, 528 KB, and 1.336 MB. The façade remains
  399,927 bytes/9,967 measured lines; dependency and import caps do not move.
  Full-chain child recovery, terminal-ancestor reaping, PostgreSQL backend-loss and
  whole-database restart proof, and the current Pi/Agno/Weng audit then measure
  2,336,092 source bytes, a 533,691-byte wheel, and a 1,354,043-byte
  pre-final-evidence sdist. Only aggregate ceilings move narrowly to 2.338 MB, 536 KB,
  and 1.360 MB; the six-dependency, 2-second/900-module import, and unchanged
  400,000-byte/10,000-line façade limits continue to pass.
Pre-provisioned result identities and the opt-in deterministic effect testkit then
measure 2,348,703 source bytes, a 537,066-byte wheel, and a 1,360,374-byte
pre-final-evidence sdist. Only aggregate ceilings move to 2.351 MB, 539 KB, and
1.365 MB; dependency, import, and façade limits remain fixed. History-bounded partial
indexes, planner contracts, and their benchmark then measure 2,349,391 source bytes, a
537,310-byte wheel, and a 1,367,449-byte pre-final-evidence sdist. Only the sdist
ceiling moves to 1.372 MB; source, wheel, dependency, import, and façade limits remain
fixed. The finite SQLite/PostgreSQL race matrix and public database barrier then measure
2,352,358 source bytes, a 537,746-byte wheel, and a 1,373,419-byte pre-final-evidence
sdist. Source and sdist ceilings move narrowly to 2.355 MB and 1.378 MB; the 539 KB
wheel, dependency, import, and façade limits remain fixed.
Nested global/tenant/session admission, retryable public overloads, content-free
metrics, and the PostgreSQL load gate then measure 2,367,759 source bytes, a
540,534-byte wheel, and a 1,385,711-byte pre-final-evidence sdist. Aggregate ceilings
move narrowly to 2.370 MB, 542 KB, and 1.390 MB; the six-dependency, import, and
unchanged 400 KB/10,000-line façade limits remain fixed.

- Central `agnoclaw.compat` inspection for Agno versions and version-sensitive
  capabilities, with stable `AGNO_VERSION_UNSUPPORTED` and
  `AGNO_CAPABILITY_UNAVAILABLE` errors.
- Stable `MODEL_PROVIDER_DEPENDENCY_MISSING` preflight errors with exact provider-extra
  install guidance.
- Python 3.11-3.14 and Agno 2.6.4/2.8.7 CI lanes plus a quarantined Agno 3 prerelease
  lane.
- Public `learning_knowledge=` injection for AgentHarness and learning-enabled team
  presets.
- Trusted per-run permission preapproval fields on `ExecutionContext`.
- Named provider, web, and scheduler extras plus a clean-wheel packaging/budget gate.
- A reproducible real-process Agno recovery probe and ADR-0001, which assign canonical
  operation/effect settlement to agnoclaw while retaining Agno 3's queue as a future
  evidence-gated adapter.
- A normative reconciliation of Lilian Weng's July 2026 harness/self-improvement
  research with itemized context, component/change manifests, immutable evaluators,
  causal failure mining, transfer/diversity, and Pareto acceptance gates.
- Frozen T10a security-foundation contracts for source-classified admission identity,
  data handling, policy evidence, opaque tenant keys, safe diagnostics, and threat
  ownership.
- The first isolated-kernel foundation: a deep-frozen deterministic private harness
  spec, explicit resource materializer classifications, defensive config copying, and
  one provenance-preserving admission envelope for `run/arun` and internal tool hooks.
- A certified non-streaming fast path that materializes a distinct Agno Agent for each
  run. Factory-backed registered capabilities, compression/summary managers, immutable
  dependency/session seeds, and the Agent itself have simultaneous overlap proofs;
  unsupported mutable extensions retain a typed single-flight fallback.
- Public `quick`, `durable`, and `service` runtime profiles plus the `legacy`
  compatibility profile. Explicit profiles apply safe defaults, preserve caller
  overrides, and fail before workspace/model effects when required durability resources
  are absent or incompatible.
- A schema-`0.12a2` complete runtime-spec digest over identity, profile, settings, and
  all resource guarantees, plus a public content-minimized `runtime_manifest()` that
  exposes classification evidence without settings bodies, filesystem paths, or live
  objects.
- A run-owned factory and reverse-order release scope for the supported quick/legacy
  host-local built-in suite, including configured MCP clients. Every eligible call gets
  fresh default tools and a fresh Agent. Lifecycle calls route these declared
  file/shell/web/child/MCP effects through operation settlement while retaining
  conservative single-flight admission.
  Child harnesses now always close.
- Versioned run states and typed `Pause`, `Resume`, `Respond`, `Steer`, and `Fork`
  commands backed by a pure reducer and in-memory conformance reference.
- Preview `AgentHarness.start/get_run`, `HarnessSession.start`, and `HarnessRun`
  lifecycle APIs with independent wait/status/cursor-events/cancel/command behavior.
- One public `RuntimeStore` boundary and transactional `SQLiteRuntimeStore` for atomic
  state/event/terminal/outbox commits, start/transition idempotency, exact ownership,
  cursor resume, and expiring outbox leases.
- First-party preview `PostgresRuntimeStore` with bounded Psycopg pooling, database-
  clock leases, row-lock/CAS concurrency, advisory migration/idempotency arbitration,
  `SKIP LOCKED` outbox claims, and a real PostgreSQL CI service gate.
- Schema-v3 event-retention watermarks with explicit cursor expiry, terminal-event and
  idempotency-evidence preservation, and undelivered-outbox pruning protection.
- Cancellation-safe exact same-session lanes plus a bounded process-global lifecycle
  execution limit.
- Explicit `drain`, `detach`, and `cancel` close policies with supervised timeout,
  admission closure, caller-owned-store preservation, and deferred resource release.
- Schema-v4 transactional operation ledgers in SQLite and PostgreSQL with immutable
  intent, mutation idempotency, revisions, dispatch attempts, fencing, safe settlement,
  recovery listing, and operation-linked events/outbox rows.
- An async-first `OperationGateway` with intent-before-dispatch, bounded result replay,
  cancellation-safe database commit boundaries, explicit safe retry/reconciliation
  decisions, and no implicit in-flight work stealing.
- One immutable `CapabilitySpec` for trust, lifetime, concurrency, recovery, effects,
  scopes, schemas, implementation digests, idempotency support, and live factories.
- Bounded fail-closed runtime validation for registered capability object schemas and
  effective arguments, including local references, object/array/scalar assertions,
  combinators, conditional schemas, cycle/depth/node/byte limits, and content-free
  failures. Unsupported assertion keywords and external references fail construction
  rather than relying on provider-side hints.
- An admission-bound `CapabilityExecutor` over the registry and `OperationGateway`:
  exact run-owner/scope reauthorization, bounded digest-only arguments, protected
  metadata, run/session/process materialization, cancellation-safe serialization,
  artifact replay without redispatch, idle-only closure, idempotency-key enforcement,
  and authorized no-dispatch recovery.
- `AgentHarness(capabilities=[...])` now owns the normal registered-capability path:
  exact versions become provider-safe Agno functions, async permission/policy and a
  bounded constraint grammar run before intent, custom policies require stable
  versions, custom approvers require stable approval versions, required scopes fail
  before extension callbacks, policy-bound effective arguments are guardrailed again,
  content-minimized policy/lease evidence enters the operation, the active run/session
  claim is renewed at the gateway's no-effect pre-dispatch boundary, and after-call
  redaction runs before result settlement/artifact persistence. Exact replay does not
  rematerialize or redispatch. Named-legacy direct `run/arun` fails closed for generated
  capabilities; explicit-profile convenience calls now join the lifecycle coordinator.
  Arbitrary legacy `tools=` are normalized but are not claimed as per-tool durable
  operations.
- Schema-v7 durable approval-before-effect for registered capabilities: an exact,
  content-minimized request commits atomically with `waiting_for_approval`; callback or
  trusted-host decisions bind the request digest/nonce; approved calls receive
  tenant/principal/session/run, capability/digest, effect, argument, policy, authority,
  issuer, expiry, and nonce-bound grants; and lease renewal plus grant reauthorization
  run at the final no-effect boundary. Raw lifecycle responses, late decisions, policy
  or argument drift, and owner/authority mismatch fail closed. Cancellation/failure/
  expiry tombstones the pending request without materializing the capability. Public
  host APIs list and settle authority-matched requests while process-restart
  reconstruction of a suspended model stack remains explicitly out of scope.
- Governed capability-call composition moved from the public `AgentHarness` facade into
  a focused internal runtime module, keeping policy → approval → lease → effect ordering
  unchanged while restoring the facade below its 400 KB architecture ceiling.
- Raw caller `tools=` now normalize into deterministic opaque `CapabilitySpec`
  inventory with per-Toolkit function expansion, bounded names, explicit duplicate
  precedence/shadowing, and a 1,000-tool bound. Compatibility `run/arun` retains
  serialized live-only behavior;
  lifecycle `start()` rejects the opaque surface before run creation and points callers
  to explicit `capabilities=` instead of guessing effects, trust, or recovery.
- Lifecycle `start()` model execution now crosses the operation gateway and records a
  deterministic non-repeatable model operation before provider dispatch.
- Schema-v5 atomic run/session execution leases with exact claim tokens, heartbeat
  renewal, release, expiry reclaim, and monotonically increasing fences in both SQLite
  and PostgreSQL; lifecycle workers cancel themselves on ownership loss.
- Explicit `recover_run()` classification that never blindly replays a stranded model
  call and identifies unknown effects, missing checkpoints, and missing result artifacts.
- Bounded `recover_pending_runs()` startup sweeps with exact tenant/user store filters,
  owner-bound keyset cursors, conservative eligible states, capped claim concurrency,
  live-lease skips, ordered safe-coded outcomes, and cancellation propagation in both
  SQLite and PostgreSQL.
- Bounded `reconcile_pending_operations()` sweeps for ambiguous external operations.
  Exact-owner/database-age keyset discovery, owner-bound cursors, a digested read-only
  observer, exact operation revision/digest binding, 1–16 scoped physical evidence
  artifacts, and store CAS/fencing are required before `succeeded`, `failed`, or
  `effect_absent` can settle. The worker never redispatches; `continued` resumes a
  settlement left behind by cancellation/process loss without re-observing. In-flight
  store commits finish before cancellation propagates, and safe outcomes omit raw
  observer errors. Both first-party stores share the contract without a schema bump.
- Schema-v9 store-authoritative recovery timestamps and v8→v9 migrations prevent a
  skewed caller clock from making a newly queued run look abandoned; PostgreSQL uses
  `CURRENT_TIMESTAMP` and SQLite stamps inside its single-node store authority.
- Exact pre-model request continuation: lifecycle execution settles a content-addressed
  `run_request_checkpoint` before model intent, and `recover_run()` relaunches only when
  dispatch is absent/planned and the owner/session, full frozen authority context,
  harness spec, request digest, checkpoint operation/artifact, and planned model intent
  all match. Dispatching/unknown work parks at `waiting_for_reconciliation` and is never
  replayed without independent evidence.
- A copy-on-write `CapabilityRegistry` with immutable version conflicts, explicit
  defaults, scope-filtered lazy search, bounded prompts, count/schema budgets, and a
  1,000-capability scale contract.
- Public `ArtifactStore`, `ArtifactScope`, `ArtifactReference`, and
  `LocalArtifactStore` contracts with scoped content addresses, plaintext/stored
  checksums, bounded JSON/page sizes, atomic filesystem publication, optional
  tenant-bound `KeyProvider` sealing, and grace-period garbage collection.
- Schema-v6 scoped artifact metadata and operation-result relationships in both runtime
  stores. Staged bytes, `artifact.committed`, operation settlement, mutation evidence,
  and outbox rows now follow one atomic authorization contract.
- Artifact-backed `recover_run()` completion for a known successful model operation,
  including integrity verification and no provider redispatch after process loss.
- Preview truthful context management: deterministic exact-or-estimated budget
  recommendations; stable scoped item/segment/checkpoint identities; artifact-first
  full-session archival; actual manual Agno-history replacement that preserves the
  caller's pre-maintenance latest intent; content-free manifests and authoritative GC
  keys; bounded exact-scope lexical search; and selective provenance-bearing
  rehydration with explicit untrusted-data framing. `summarize_session()` is now the
  separately named summary-only operation. Automatic replacement is opt-in, requires a
  positive context budget plus artifact store, and keeps live-provider/drift proof as a
  release gate rather than silently claiming it. Manifest load recomputes item/segment/
  checkpoint/digest/token chains, failed writes do not mutate the live Agno cache, and
  cancellation racing the final database write reports that the operation committed.
- Immutable `LearningPolicy`, `LearningStorePolicy`, run-resolved `LearningScope`, and
  `LearningProfile` constructors for personal, session, combined, and institutional
  intent. Trusted run identity maps to deterministic opaque Agno storage keys; personal
  consent, stable user/session/tenant requirements, retention metadata, and per-store
  update budgets fail closed before model dispatch.
- Per-run LearningMachine materialization for explicit policies, public Session Context,
  and safe learning-scope provenance on run events. Institutional stores are recall-only
  on the direct Agno path and cannot grant the proposing model save authority.
- Artifact-backed governed learning candidates with a transactional SQLite ledger,
  exact tenant/namespace authorization, source-run verification, immutable supersession
  edits, evidence/control exports, CAS/idempotent evaluation, quarantine, tombstones,
  and authoritative live-artifact keys.
- First-party bounded-pool `PostgresLearningLedger` with advisory migration locking,
  exact null-safe scope filtering, row-lock/revision CAS, transactional mutation rollback,
  overload errors, and real PostgreSQL 17 conformance coverage.
- Learning-ledger schema v3 canonical events and transactional outbox rows for every
  candidate revision, with bounded monotonic reads, expiring leases, exact
  acknowledgements, SQLite write serialization, and PostgreSQL database-clock
  `SKIP LOCKED` claims.
- Learning-ledger schema v4 owner-scoped reconciliation maintenance: a dedicated
  SQLite/PostgreSQL worker claims a database-clock lease, renews slow read-only
  observations, checkpoints only completed pages, preserves its cursor across restart,
  and uses a monotonic takeover fence. Lease credentials are excluded from public
  serialization and reprs; `AgentHarness.build_learning_reconciliation_worker()` wires
  the same first-party Agno exact-name observer without exposing private factories.
  A real PostgreSQL 17 test uses two independent pools and proves a second process lane
  cannot steal a one-second lease while a 1.25-second observer is heartbeat-renewed.
  A spawned worker is also terminated without cleanup; another pool remains blocked
  until database-clock expiry, then reclaims with a strictly higher fence.
- Learning-ledger schema v5 evaluation-archive projection: SQLite/PostgreSQL atomically
  maintain content-free owner/target/mechanism/verdict/evaluator/safety fields plus a
  validated stable reason-code relation and bounded owner/filter/order indexes beside
  canonical immutable evaluation JSON. The idempotent v4→v5 migration backfills typed
  fields and scans reasons in bounded 1,000-row batches under the existing migration
  lock; corrupt canonical rows fail closed and roll back. SQLite query-plan coverage
  rejects temporary sorting, live PostgreSQL 17 migration/query coverage passes 10/10,
  and the 10,000 queried-owner plus 10,000 noisy-neighbor benchmark passes at 56.64 ms
  p95, 58.87 ms p99, and 0.974x slowdown with exact cleanup. These are bounded
  development gates, not production-volume/failover certification.
- The exact observer/worker package checkpoint narrowly rebaselines only aggregate
  source/wheel/sdist ceilings to 2,896,500/644,000/1,643,000 bytes after measuring
  2,893,908/642,017/1,640,489. The fixed six-dependency, 400,000-byte/10,000-line,
  two-second, and 900-module ceilings remain unchanged; the current largest module is
  398,037 bytes/9,927 lines and median core import is 0.292558 seconds/890 modules.
- The provider-neutral Agno evaluation-subject checkpoint measures 2,903,447 source
  bytes, a 644,710-byte wheel, and a 1,648,990-byte documentation-complete pre-final
  sdist; only aggregate ceilings move
  narrowly to 2,906,000/646,000/1,651,000 bytes. Six dependencies, the
  398,037-byte/9,927-line largest module, two-second import ceiling, and 900-module cap
  remain fixed; median import is 0.314777 seconds across 891 modules. Both artifacts
  pass Twine and exact isolated wheel/sdist smoke with a real Agno Agent and host model.
- The governed-corpus checkpoint measures 2,922,144 source bytes, a 648,902-byte wheel,
  and a 1,658,151-byte documentation-complete pre-final sdist; only aggregate ceilings move narrowly to
  2,925,000/651,000/1,660,000 bytes. Six dependencies, the 398,037-byte/9,927-line
  largest module, two-second import ceiling, and 900-module cap remain fixed; median
  import is 0.286436 seconds across 892 modules. Twine, package budgets, dependency
  checks, and exact wheel/sdist corpus smoke pass.
- The documentation-complete negative-evaluation archive checkpoint measures 2,942,596
  source bytes, a 652,208-byte wheel, and a 1,666,719-byte pre-final sdist; only the
  aggregate ceilings move narrowly to 2,945,000/654,000/1,670,000 bytes. Six core
  dependencies, the 400,000-byte/10,000-line largest-module limits, two-second import
  ceiling, and 900-module cap remain fixed. The current largest module is 398,709
  bytes/9,947 lines, and the measured import uses 892 modules. Twine, package budgets,
  and exact installed wheel/sdist smoke pass.
- The schema-v5 archive-scale checkpoint measures 2,955,869 source bytes, a
  653,953-byte wheel, and a 1,676,493-byte documentation-complete pre-final sdist;
  aggregate ceilings move narrowly to 2,958,000/656,000/1,680,000 bytes. Six core
  dependencies, the 398,709-byte/9,947-line largest-module limits, two-second import
  ceiling, and 892-module import surface remain fixed. Twine and package budgets pass;
  exact wheel/sdist smoke confirms schema v5 and negative-only defaults.
- The runner-schema-1.2 fresh-process checkpoint measures 2,985,467 source bytes, a
  660,358-byte wheel, and a 1,689,407-byte documentation-complete pre-final sdist;
  aggregate ceilings move narrowly to 2,988,000/662,000/1,693,000 bytes. The boundary
  remains standard-library-only: six dependencies, the 398,709-byte/9,947-line largest
  module, two-second import ceiling, and 900-module cap stay fixed; the measured import
  is 0.295263 seconds across 893 modules. Exact installed wheel/sdist process execution
  joins Twine and package-budget gates after the documentation rebuild.
- The refreshed warning-clean slim primary lane passes 1,948 tests with 55 expected
  service/credential/provider/vector skips and 17 integration deselections in 173.28
  seconds. The outer-model, tool-checkpoint, and approval-wait restart integration gates
  pass separately as 3/3 in 21.03 seconds. The current affected
  ownership/learning/evaluation/documentation lane passes
  214/214 on isolated Agno 2.6.4 and is covered by the primary suite; the retained
  earlier 148-test cross-version lane remains green. The live PostgreSQL 17 learning
  ledger passes 11/11, and optional live model/vector proof now runs in an isolated
  environment rather than mutating normal QA dependencies.
- Evidence-backed promotion/rollback reconciliation records and host APIs. Ambiguous
  effects can settle present/absent only from a digested reconciler, immutable evidence,
  and explicit actor; reconciliation is itself CAS/idempotent, evented, and exported.
- Restart-safe reconciliation discovery on both learning ledgers and `AgentHarness`,
  with oldest-first keyset pagination, exact tenant/namespace authorization,
  scope-bound serializable cursors, and typed promotion/rollback work items. Discovery
  is read-only and never retries or guesses an external effect.
- A bounded host-observer reconciliation coordinator and
  `observe_learning_reconciliation_page()`. Observations bind to an exact candidate
  digest/revision; evidence bytes, purpose, tenant, and user scope are verified before
  ledger CAS; batch cursors must remain bound to the exact owner query; concurrent
  workers converge without replaying promotion/rollback; and per-item reports redact
  raw observer failures.
- Host-only candidate APIs on `AgentHarness` plus intent-first reviewed promotion and
  rollback. Ambiguous backend outcomes enter fenced unknown states and are never blindly
  replayed; the default reversible Agno adapter supports uniquely named Learned
  Knowledge and fails closed for unsafe Entity Memory/Decision Log paths.
- Preview immutable self-improvement contracts for the seven harness component classes,
  verifier-grounded failure clusters, falsifiable bounded change hypotheses, frozen
  evaluator/model/permission/budget controls, paired held-in/held-out/transfer evidence,
  deterministic safety/privacy/cost/latency/novelty/diversity gates, auditable model
  judges, and non-scalarized five-objective Pareto selection. Gate decisions convert
  directly to scoped ledger evaluations but cannot edit or promote a candidate.
- A provider-neutral `AgnoEvaluationSubject` and
  `agno_evaluation_subject_factory()` bridge host-supplied fresh Agno Agents into the
  paired improvement runner on the supported 2.6.4/2.9.0 lanes. It uses opaque
  per-rollout sessions, projects only JSON-like public content and bounded token/cost
  metrics, optionally closes owned Agents, and leaves cases, verification, safety and
  privacy gates, judging, ledger mutation, and promotion outside the adapter.
- Runner-schema-1.2 paired process subjects. `process_evaluation_subject_factory()`
  launches one absolute no-shell command per rollout with an empty-by-default
  environment, fresh temporary working directory, bounded request/stdout/stderr,
  redacted error handling, and mandatory cleanup. POSIX uses a fresh process group and
  adversarial cancellation proves both the worker and its spawned descendant are reaped.
  Baseline/candidate command-contract digests bind argv while one required-equal
  isolation digest binds hashed environment values, working-directory mode, limits,
  termination grace, and process-group policy into the report, runner digest, and every
  case artifact; asymmetric or unequal isolation fails before model work. This is local
  process/state/fault isolation, not hardened filesystem/network/kernel sandbox or
  Windows process-tree certification.
- A content-free `EvaluationCorpusManifest` with ordered payload/lineage digests,
  development-versus-sealed split exposure, source usage/retention provenance,
  selection/sampling/sealed-access controls, independent curator authority, and a
  separately staged decontamination report. Exact duplicates, cross-split lineages,
  case drift, proposer-curator conflicts, malformed provenance, and known/unresolved
  overlap fail before subject construction. Corpus evidence enters runner/case/gate/
  candidate digests, and the default gate rejects ungoverned qualification with an
  explicit legacy-only opt-out.
- An optional content-free evaluation archive over the canonical learning ledger.
  `AgentHarness.query_learning_evaluation_archive()` defaults to rejected and
  inconclusive results, applies exact-owner SQLite/PostgreSQL filters for stable gate
  reason, evaluator, mechanism, target, and safety result, and paginates with an
  owner-bound descending keyset cursor. It retains only safe identifiers, verdict/state,
  validated reason/control digests, and evidence counts—never candidate content, notes,
  raw metrics, control metrics, or artifact IDs. Custom ledgers remain compatible until
  they opt into `EvaluationArchiveLedger`; the live PostgreSQL 17 contract passes.
- `AgentHarness.record_learning_candidate_evaluation()` for submitting a prebuilt
  immutable evaluation without manually unpacking a gate decision.
- Host-only personal/session `read_learning_data()`, `replace_learning_data()`, and
  `forget_learning_data()` APIs. They derive opaque identity from trusted context, call
  Agno's direct database CRUD contract so backend exceptions stay distinguishable from
  absence, validate versioned replacement schemas/size/store shapes, serialize
  same-scope mutations, and return canonical post-read-verified receipts explicitly
  labeled point-in-time. Real SQLite round trips cover all three stores on both supported
  Agno lanes.
- A focused 250-line landing README plus public-API getting-started, CLI, and
  configuration guides. Executable documentation gates now check local links, complete
  index coverage, the README size budget, and private-Agent reach-through in code
  blocks.
- Public `inspect_migration_012()` and `agnoclaw migrate 0.12 check` read-only
  preflight. It fingerprints bounded legacy Agno SQLite learning/scheduler JSON sources,
  reports content-free scope/owner/collision/timezone/misfire/fence evidence, requires
  explicit institutional map-or-quarantine decisions, emits schema-v1 deterministic
  JSON, and uses exit `3` for semantic blockers. The check remains mutation-disabled.
- A certified local SQLite/JSON `agnoclaw migrate 0.12
  plan/apply/verify/cutover/rollback` workflow. Content-free digest-bound plans,
  source-checksum revalidation, explicit writer-stop confirmation, enforced learning/
  scheduler source fences, native/atomic mode-0600 backups, idempotent identity-safe
  imports, institutional quarantine, schema-v12 schedule conversion/history archival,
  exact target verification, WAL/SHM-aware drift refusal, and resumable multi-target
  rollback are backed by stable JSON/stdout/stderr and semantic exit contracts.
  PostgreSQL/service-scale, artifacts/keys, online reverse migration, and production
  RPO/RTO are explicitly not claimed.
- The first PostgreSQL/service migration control-plane slice: uppercase credential
  references instead of serialized DSNs, strict schema identifiers, bounded sensitive
  schedule maps whose prompts collapse to digest/count/source-set evidence, immutable
  restore-tested backup receipts, content-free endpoint/table evidence, explicit
  writer-fence and rollback boundaries, mode-0600 atomic plan files, and tamper-evident
  reads. A streamed PostgreSQL scanner now groups identical endpoints, enforces
  repeatable-read/read-only transactions and timeouts, hashes rows in bounded batches,
  checks learning ownership/scope decisions plus exact schedule IDs/in-flight state,
  and emits no DSNs, prompts, row contents, or raw driver errors. The sole public plan
  factory rejects blockers and binds exact database references and scope decisions to
  the scan; secret-shaped receipt/fence values and forged schedule-map digests fail
  closed. Schedule-map schema 1.1 now digest-binds the complete executable schedule
  contract instead of inferring cron, isolation, consent, or retry behavior. A public
  bounded-memory transformation preview repeats the exact-plan scan, streams a fresh
  read-only snapshot, recomputes source evidence, rekeys personal learning, quarantines
  institutional learning, compiles deterministic durable jobs, archives completed
  history, and detects collisions through a mode-0600 disk-backed identity registry.
  Its report contains only counts and digests. Provenance-owned lifecycle control now
  rebinds every endpoint, takes target-schema advisory locks, compiles the same source
  snapshot before writing, and records monotonic resumable checkpoints. Apply classifies
  rows as inserted or preexisting-identical; independent verification detects source,
  endpoint, owned-row, provenance, and unowned-target drift; cutover records an external
  deployment receipt without changing routing; reverse-order rollback removes only exact
  inserted rows and refuses post-cutover drift. Automation-safe
  `agnoclaw migrate 0.12 service check|plan|preview|apply|verify|cutover|rollback`
  commands keep
  data on stdout and diagnostics on stderr, use semantic `3`/`4`/`75`/`78` exits,
  expose credential environment-variable names rather than DSN flags, rescan before
  planning, require explicit overwrite, reject symbolic-link destinations, expose
  dry-run for mutations, and distinguish plan-time `apply_available: false` from a
  successful transform preview. All 27 pass against disposable PostgreSQL 17: the
  one-row matrix includes forced process-death resume during apply and rollback,
  deterministic preview, drift refusal, repeat apply/rollback, and zero executable
  target rows; a second matrix streams 5,000 learning rows across three independent
  databases using isolated least-privilege roles under a 64 MiB preview-memory cap.
  The lane is wired into CI. Complete checkpoint-kill, deployment-fence/rogue-writer,
  credential-rotation/TLS, native-backup/production-volume, and production-certification
  matrices remain open and are specified in the service runbook. The prior CLI-only
  checkpoint's exact built wheel with
  `postgres,cli` extras passed Twine and installed-command smoke; its pre-ledger
  package evidence was 2,661,461 source bytes, a 597,926-byte wheel, and a
  1,562,508-byte sdist under 2,665,000/600,000/1,568,000-byte aggregate ceilings.
- Pre-ledger package evidence after the deterministic service transformation preview
  measures 2,699,215 source bytes, a 606,122-byte wheel, and a 1,573,731-byte sdist.
  Only the three aggregate ceilings move narrowly to
  2,702,000/608,000/1,580,000 bytes. The six-dependency, 400 KB/10,000-line
  single-module, two-second import, and 900-imported-module limits remain fixed; the
  facade is still 399,896 bytes/9,984 lines and median import is 0.286981 seconds
  across 884 modules. Both artifacts pass Twine, and the exact-wheel lane now installs
  `postgres,cli,scheduler` before invoking service `check` and `preview` help. Exact
  final sdist bytes and hashes remain detached because bundled release documentation
  changes the sdist.
- Pre-ledger package evidence after the full PostgreSQL migration development lifecycle
  measures 2,809,620 source bytes, a 623,345-byte wheel, and a 1,597,745-byte sdist.
  Only the three aggregate ceilings move narrowly to
  2,812,000/625,000/1,605,000 bytes. The six-dependency, 400 KB/10,000-line
  single-module, two-second import, and 900-imported-module limits remain fixed; the
  facade is still 399,896 bytes/9,984 lines and median import is 0.332042 seconds
  across 885 modules. Both artifacts pass Twine, and the exact-wheel lane installs
  `postgres,cli,scheduler` before invoking service lifecycle help. Exact final sdist
  bytes and hashes remain detached because bundled release documentation changes the
  sdist.

### Changed

- The production Agno dependency is now bounded to `>=2.6.4,<2.10` and the
  development lock is promoted to 2.9.0 after a warning-clean 1,844-pass primary
  suite. The release audit adopts identity-aware run-only Studio dispatch and strict
  fail-loud rehydration, while retaining stronger agnoclaw guarantees: lifecycle tools
  reject Agno result caching, capability selection is digest-bound, and caller identity
  comes from trusted admission. Agno 2.9.0's cross-user cache and call-time MCP
  `tool_name` fixes are treated as security contracts, not just upstream notes.
- Full Ruff (`E`, `F`, `I`, `UP`, `B`) and mypy with untyped-body checking now pass
  across the repository and are blocking CI gates; typing stubs remain development-only
  and do not expand the six-dependency core.

- The earlier development baseline was promoted to Agno 2.8.7 after both 2.6.4 and
  2.8.7 contract suites passed; the current 2.9.0 promotion above supersedes that
  checkpoint without erasing its retained evidence.
- Supported Python metadata expands from 3.13-only to 3.11-3.14, provisionally pending
  CI/package certification.
- The provider-neutral core now has six direct dependencies. Unused APScheduler and
  PathSpec declarations were removed; Anthropic, Beautiful Soup, and DDGS moved to
  explicit extras. SQLAlchemy remains core because the default Agno SQLite backend
  imports it. The legacy `duckduckgo-search` adapter now uses current `ddgs`.
- SKILL.md frontmatter now uses an owned UTF-8/PyYAML reader, removing the deprecated
  `python-frontmatter` package and its Python 3.14 warning.
- Effect-safe model/capability calls may overlap through isolated per-run Agents.
  Supported host-local built-ins also receive run-owned Agents/tools but remain
  single-flight with custom/streaming and other unsettled-effect surfaces; overlap falls
  back to `HARNESS_RUN_IN_PROGRESS`.
- Lifecycle workers now clean ephemeral control maps after settlement. Async code must
  use `aclose()`; sync `close()` fails clearly inside an event loop and cannot detach
  work onto a temporary loop.
- Harness shutdown now closes host-local command executors and other default resources
  it creates while preserving injected database/runtime/backend/browser ownership.
  Background commands close parent log handles immediately and run in owned POSIX
  process groups so shutdown terminates and reaps descendants, not only their shell.
- Durable/service `agnoclaw run` now prints final settled content after lifecycle
  completion instead of exposing a provider token stream. Durable/service async
  chat/TUI keeps live token UX through a bounded lifecycle presentation attachment;
  extracted text is now artifact-backed and replayable, while raw event/tool UI replay
  and legacy sync chat remain explicit future/compatibility boundaries.
- Elevated/session command routing and executor ownership moved from the public harness
  facade into a focused internal module, restoring meaningful headroom below the
  unchanged 400 KB/10,000-line architecture ceiling.
- Session-context orchestration moved into `context_runtime.py` and its immutable
  archive domain into `context_management.py`; `agent.py` remains below the unchanged
  facade ceiling after adding real manual replacement/search/rehydration.
- Resource, unraisable-exception, deprecation, and future-compatibility warnings are
  blocking across the normal Python, Agno compatibility, PostgreSQL, and publish suites.
  CI and publishing now validate both wheel and sdist metadata/budgets/clean installs,
  and PyPI receives the exact archived artifacts that passed the release gate.
- Session enumeration requires exact tenant and user ownership instead of accepting
  unowned storage rows.
- Sandbox/artifact admin operations require an authorized session-bound harness
  context.
- Legacy learning flags remain compatible but now emit a removal-floor warning; mixing
  them with `learning=LearningPolicy` fails with `LEARNING_CONFIGURATION_CONFLICT`.
- Candidate deletion is an audit tombstone: content reads stop immediately and physical
  artifact removal is delegated to grace-period garbage collection. Promoted candidates
  must be rolled back before deletion.
- Configuration precedence is now explicit Python values, environment, project TOML,
  user TOML, then defaults. Environment overlays are field-specific and do not erase
  unrelated nested TOML settings.
- Package budgets are frozen against the final 0.12 surface at 3.34 MB of Python
  source, a 740 KB wheel, a 1.96 MB sdist, 470 KB/11,600 lines for one module, and
  1,030 imported modules, each with roughly two percent headroom over the candidate.
  The six direct dependencies and two-second import ceiling remain unchanged. The
  enlarged `AgentHarness` facade and import graph are explicit post-0.12 extraction
  debt; future growth must stay inside these new regression ceilings.

### Fixed

- Harness-created Agno models now have explicit transport ownership. Base and fresh
  per-run model transports close on every exit path, and owned evaluation subjects do
  the same. Public provider close methods are preferred; Ollama's currently hidden
  HTTPX client is handled by a narrow fallback. Caller-injected Model objects remain
  caller-owned. A real Ollama run now passes with `ResourceWarning` promoted to error.
- `create_agentos_app()` now uses Agno's current `mcp_server` constructor when
  available, avoiding the deprecated `enable_mcp_server` warning while retaining the
  older supported fallback. The `server` extra now includes the multipart parser that
  AgentOS's native form routes require. Verified OS-key requests are normalized across
  Agno 2.6/2.8 only after an exact constant-time comparison; configured-but-
  unauthenticated middleware state can no longer pass the lifecycle gate.

- Session SDK helpers now preserve tenant, org, team, roles, scopes, request ID, trace
  ID, and workspace identity.
- Explicit `run/arun` user/session values can no longer conflict with trusted
  `ExecutionContext` identity.
- Client metadata named `claims` or `agentos_claims` can no longer grant AgentOS
  identity.
- Permission approvals no longer become harness-global grants or leak to later runs.
- Skill command dispatch no longer calls arbitrary plain callables outside the governed
  Agno Function hook/policy/permission/event path.
- Institutional Agno learning no longer constructs a silently unusable Learned
  Knowledge store without vector-backed `Knowledge`; it raises
  `LEARNING_KNOWLEDGE_REQUIRED` before a model call.
- Invalid learning modes no longer silently fall back to Agentic, and the nonexistent
  periodic `LearningMachine.optimize_memories()` maintenance path has been removed.
- Agno Learned Knowledge promotion no longer treats an unconfirmed `False` save result
  as success, and Entity Memory merge updates are no longer mislabeled reversible.
- Session changes clear session scratch to prevent temporal cross-session leakage.
- Lifecycle failures now persist an allowlisted safe diagnostic and debug reference,
  never the raw exception message.
- `get_config()` now makes root and nested environment variables override project/user
  TOML as documented; previously constructor-loaded TOML could silently win.
- Cancellation before provider dispatch is recorded as cancellation, while a generic
  provider failure or cancellation after non-repeatable dispatch truthfully parks at
  `waiting_for_reconciliation`. Repeated cancellation cannot erase the ambiguity;
  `wait()` raises typed `RUN_RECONCILIATION_REQUIRED`.
- PostgreSQL runtime outbox availability now uses the database clock, eliminating a
  host/database clock-skew window where freshly committed events could appear not ready.
- Host-local background executors no longer retain parent-side log files, abandon live
  `Popen` objects at harness shutdown, or reject a new task because a finished task was
  not reclaimed at the configured capacity.

### Security

- Identity conflicts fail closed with `IDENTITY_CLAIM_CONFLICT` or
  `IDENTITY_CONTEXT_CONFLICT`.
- Session and artifact administration reauthorizes the exact namespace it reads,
  snapshots, resets, or downloads.

### Migration notes

- Code that overlaps stateful/custom tools or streams on one harness must still use a
  harness per worker. Handle `HARNESS_RUN_IN_PROGRESS` as retryable. Model-only
  non-streaming calls are now independently materialized.
- `start/get_run` and lifecycle persistence are preview. Model operation settlement is
  implemented along with store-issued leases, successful-result artifacts, an exact
  explicit pre-model request continuation point, and a narrowly certified Agno 2.9
  registered-capability tool/approval continuation path. Arbitrary raw, nested,
  parallel, streaming, parser/output-model, and extension tool continuation,
  autonomous deployment scheduling, certified provider observers, general artifact
  retention/deletion, and universal effect ingress are not certified;
  bounded exact-owner startup and operation-reconciliation scanning are implemented;
  keep existing production
  job ownership until those recovery gates pass.
- Code using `enable_learning=True` must pass
  `learning_knowledge=Knowledge(vector_db=...)`; use
  `enable_learning=False, enable_user_memory=True` for personal stores only.
- Storage adapters used by `list_sessions()` must return tenant and user ownership.
  Legacy unowned rows are intentionally invisible.
- See [0.12 migration guide](docs/migration-0.12.md) for the evolving preflight and
  rollback contract.
- Run `agnoclaw migrate 0.12 check --learning-db PATH --schedules PATH --json` before
  planning local persisted-data changes. A clear report is source-decision evidence
  only; follow the separately confirmed local SQLite/JSON runbook for
  plan/apply/verify/cutover/rollback. PostgreSQL deployments use the distinct
  `service check|plan|preview|apply|verify|cutover|rollback` runbook; that lifecycle is
  implemented for development but remains unavailable for production until its
  certification matrix passes.
