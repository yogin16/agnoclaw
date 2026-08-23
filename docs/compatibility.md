# Compatibility

Status: provisional 0.12 development matrix

Last verified: 2026-08-18

`agnoclaw` supports behavior, not merely import success. A production lane enters this
table only after the full non-network contract suite passes; providers, databases, MCP,
AgentOS, and optional extras need their own integration evidence before a narrower
feature is called certified.

## Current runtime matrix

The Agno 2.9.0 primary row is the complete schema-v12/spec-1.1 local evidence from
2026-08-18. The legacy row retains complete schema-v11 evidence; the other isolated
rows retain exact schema-v10 evidence and require a current-schema rerun before
release-candidate certification.

The primary line additionally certifies `AgnoFeature.TOOL_BATCH_CHECKPOINT` through two
real-process matrices. Three tool-sequence crashes produce two exact completions, one
reconciliation wait, six provider calls, one post-restart call, three tool effects, and
no duplicates. One atomic approval-wait crash reconstructs from the first-provider
artifact and exact decision/grant, completes the capability/provider sequence once, and
creates no duplicate approval evidence. This applies only to an agnoclaw-materialized
native Agent with a public-factory model classified as run-owned, isolated, and
recreatable, non-streaming `start()`, durable runtime plus Agno databases,
a shared ArtifactStore, and governed registered capabilities. Agno 2.6.4 does not
expose the exact checkpoint/cancel/continue contract and deliberately retains the
conservative recovery path.

The provider-neutral outer-operation restart gate is independently green on both Agno
2.6.4 and 2.9.0 using the public `AgnoModelFactory`. Four real child deaths cover
planned continuation, ambiguous dispatch, committed-result completion, and changed
factory-digest refusal. Across each run, only the planned same-digest case calls after
restart; all four are found by the bounded owner-scoped startup scanner, and changed
identity fails with `RUN_RECOVERY_SPEC_MISMATCH` before dispatch. This
does not imply that Agno 2.6.4 supports the separate native tool-batch continuation.

The real-process learning-reconciliation worker gate is likewise green on both 2.6.4
and 2.9.0 and joins those hosted compatibility rows. It kills a worker inside
observation, denies active-lease theft, reclaims fence 1→2 after database-clock expiry,
and reconciles once with zero promotion redispatch. This is local SQLite maintenance
evidence, not production PostgreSQL failover or a provider-backed learning-benefit
claim.

| Runtime | Level | Evidence on this branch |
|---|---|---|
| Python 3.11 + Agno 2.8.7 | provisional supported | 1,404 passed, 27 skipped, 10 integration tests deselected |
| Python 3.12 + Agno 2.8.7 | provisional supported | 1,404 passed, 27 skipped, 10 integration tests deselected |
| Python 3.13 + Agno 2.6.4 | legacy supported | The retained complete lane passes 1,739 tests with 35 environment/optional-extra skips and 10 integration deselections. On 2026-08-17 the current affected ownership/learning/evaluation/documentation slice additionally passed 214 tests with 2 deselections through a fresh isolated editable environment. The schema-v11/spec-1.1 lane covers declared children, nested bounded/fair lifecycle admission, depth-16 recovery lineage, pre-provisioned result identities, deterministic every-state/all-effect/both-order controls, history-bounded recovery plans, operational budgets, structured result validation, governed synthesis, typed artifact handoff, output replay, recovery/reconciliation, trajectory/outbox, dead-letter administration, governed context/effects, learning, migration, profiles, overflow, improvement evaluation, and service safety contracts. |
| Python 3.13 + Agno 2.9.0 | primary supported | 1,948 passed, 55 environment/service/provider/vector skips, and 17 integration deselections in 175.21 seconds in the warning-clean non-integration lane; development lock is 2.9.0. The full run includes explicit-profile convenience lifecycle routing, schema-v12 scheduling, service migration, observability/privacy, schema-v5 evaluation archive, schema-v6 application/outcome attribution, retryable learning-store recovery, harness-owned model-transport cleanup, the Agno evaluation-subject adapter, governed-corpus evidence, runner-schema-1.2 fresh-process subjects, strict Docker contracts, capability-effect process-crash contracts, deterministic plus opt-in live tool-bearing long-run contracts, cooperative same-host context locking, tool-free summary maintenance, automatic source-bound goal carry, bounded narrative fitting, carried-state search deduplication, and typed continuation/source-token accounting. The retained outer-model, tool-checkpoint, and approval-wait real-process integration files pass 3/3 in 21.03 seconds. The strengthened outer factory file separately passes on both Agno 2.6.4 and 2.9.0 and is wired into each hosted compatibility row; tool/approval continuation remains hosted in the primary Python 3.13 lane. The focused affected compatibility/documentation path passes 99 tests with one intentional upstream-capability skip on isolated Agno 2.6.4; the affected 74-test context lane also passes there. 51/51 focused SQLite learning contracts pass, the live learning ledger passes 11/11 against disposable PostgreSQL 17, the exact Agno 2.9.0 Ollama/LanceDB benefit gate passes in an isolated optional-dependency environment, and separate deterministic plus pinned `qwen2.5:7b` Ollama 100-turn/11-tool context certifications pass. The independent coverage measurement was rerun at the 0.12.0 release-candidate checkpoint: 82.42% on the narrowed core scope (see docs/evaluation.md, "Core coverage gate"). |
| Python 3.14 + Agno 2.8.7 | provisional supported | 1,404 passed, 27 skipped, 10 deselected; no former frontmatter warning |
| Python 3.13 + Agno 3.0.0a1 | preview only | 1,404 passed, 27 skipped, 10 deselected after an explicit CI-only dependency override; never selected by normal installation |

These local results were produced on macOS. The matching Ubuntu CI matrix is a release
gate. Windows support remains unclaimed until shell, path, sandbox, and process suites
run there.

The normal dependency range is `agno>=2.6.4,<2.10`. Agno 2.9.0 was the latest
stable release and 3.0.0a1 the latest prerelease when the official
[PyPI registry](https://pypi.org/project/agno/) and
[GitHub releases](https://github.com/agno-agi/agno/releases) and exact repository tags
were rechecked on 2026-08-17. A passing prerelease suite provides migration warning; it does not make the
preview production-supported.

## Lean core and extras

The core wheel is provider-neutral. Its direct dependency budget is six: Agno,
Pydantic, Pydantic Settings, PyYAML, HTTPX, and SQLAlchemy. SQLAlchemy remains core
because the default Agno SQLite backend imports it; clean-wheel validation constructs a
real default harness, not merely the package import. Install optional surfaces deliberately:

| Need | Extra |
|---|---|
| Claude | `agnoclaw[anthropic]` |
| OpenAI | `agnoclaw[openai]` |
| Gemini | `agnoclaw[google]` |
| Groq | `agnoclaw[groq]` |
| rich HTML extraction and keyless search | `agnoclaw[web]` |
| MCP 2.0 tools | `agnoclaw[mcp]` |
| AgentOS/FastAPI lifecycle server | `agnoclaw[server]` |
| PostgreSQL RuntimeStore and service migration | `agnoclaw[postgres]` |
| cron expressions | `agnoclaw[scheduler]` |
| structured terminal output and operator CLI | `agnoclaw[cli]` |
| OpenTelemetry SDK and OTLP/HTTP export | `agnoclaw[otel]` |
| batteries-included personal assistant | `agnoclaw[full]` |

APScheduler, PathSpec, and `python-frontmatter` were removed from the direct core because
agnoclaw did not use them. Provider and search libraries moved to extras;
the legacy `duckduckgo-search` import was migrated to Agno's current `ddgs` package.
CI installs the built core wheel into a clean environment and enforces explicit source,
wheel, sdist, import-time, imported-module, and dependency-count budgets. The clean
core constructs with a host-supplied Agno `Model`; the default-provider lane installs
`agnoclaw[anthropic]` and constructs `AgentHarness()` directly. A missing common
provider SDK raises stable `MODEL_PROVIDER_DEPENDENCY_MISSING` with its install extra.

The modern-ingress/context-provider checkpoint rebaselined ceilings to 1.922 MB of
uncompressed Python source, a 450 KB wheel, and a 1.175 MB sdist. The next measured
paired-improvement-runner checkpoint uses 1,960,913 source bytes and moves only those
aggregate ceilings to 1.962 MB source, 456 KB wheel, and 1.187 MB sdist. The exact
pre-model checkpoint tranche measures 1,981,742 source bytes, a 459,998-byte wheel, and
a 1,193,491-byte sdist, moving only aggregate ceilings to 1.983 MB, 461 KB, and
1.195 MB. The normalized-trajectory tranche measures 1,998,191 source bytes, a
463,796-byte wheel, and a 1,200,391-byte pre-evidence-page sdist, moving only aggregate
ceilings to 2.000 MB, 465 KB, and 1.204 MB. The generic outbox-delivery tranche measures
2,007,094 source bytes, a 466,092-byte
wheel, and a 1,206,529-byte pre-evidence-page sdist, moving only aggregate ceilings to
2.009 MB, 467 KB, and 1.209 MB. The schema-v8 dead-letter tranche measures 2,019,243
source bytes, a 467,613-byte wheel, and a 1,210,258-byte pre-evidence-page sdist. The
schema-v9 owner-scoped recovery tranche measures 2,033,384 source bytes, a 471,105-byte
wheel, and a 1,217,832-byte pre-evidence-page sdist, moving only aggregate ceilings to
2.034 MB, 472 KB, and 1.220 MB. The isolated operation-reconciliation coordinator then
measures 2,075,413 source bytes, a 478,005-byte wheel, and a 1,229,043-byte
pre-evidence-page sdist, moving only aggregate ceilings to 2.076 MB, 479 KB, and
1.231 MB. Schema-v10 owner-authorized dead-letter administration then measures
2,111,226 source bytes, a 484,200-byte wheel, and a 1,241,584-byte pre-evidence-page
sdist, moving only aggregate ceilings to 2.113 MB, 485 KB, and 1.244 MB. Exact final
artifact sizes and digests belong in
detached build provenance because this documentation is itself included in the sdist.
The authenticated AgentOS/remote lifecycle tranche then measures 2,159,004 source
bytes, a 494,540-byte wheel, and a 1,261,276-byte pre-final-evidence sdist, moving only
aggregate ceilings to 2.161 MB, 496 KB, and 1.264 MB. Server dependencies remain in the
optional extra and the facade/dependency/import caps do not move.
The profile-aware first-party client tranche then measures 2,164,651 source bytes, a
496,258-byte wheel, and a 1,267,938-byte pre-final-evidence sdist, moving only aggregate
ceilings to 2.166 MB, 498 KB, and 1.271 MB. Core dependencies, import limits, and the
facade cap remain unchanged.
The lifecycle presentation tranche measures 2,173,954 source bytes, a 499,229-byte
wheel, and a 1,274,720-byte pre-final-evidence sdist, moving only aggregate ceilings to
2.175 MB, 501 KB, and 1.277 MB. The focused presentation module keeps `agent.py` below
the unchanged facade limit; dependency and import caps do not move.
Artifact-backed provider-text replay then measures 2,200,442 source bytes, a
504,339-byte wheel, and a 1,285,246-byte pre-final-evidence sdist, moving only aggregate
ceilings to 2.202 MB, 506 KB, and 1.288 MB. The dependency, import, and facade caps stay
fixed.
Schema-v11 declared-child lineage/join/cancellation then measures 2,255,866 source
bytes, a 514,676-byte wheel, and a 1,306,698-byte pre-final-evidence sdist, moving only
aggregate ceilings to 2.258 MB, 516 KB, and 1.310 MB. Extracting compatibility trace
helpers reduces `agent.py` to 398,525 bytes/9,942 lines; the dependency, import, and
facade caps remain fixed.
Active-worker child budgets and typed lossless result/artifact handoff then measure
2,286,756 source bytes, a 523,071-byte wheel, and a 1,322,517-byte pre-final-evidence
sdist, moving only aggregate ceilings to 2.288 MB, 525 KB, and 1.326 MB. The facade
remains 398,974 bytes/9,951 measured lines; dependency and import caps do not move.
Declaration-bound child output schemas and governed synthesis then measure 2,297,299
source bytes, a 525,944-byte wheel, and a 1,331,781-byte pre-final-evidence sdist,
moving only aggregate ceilings to 2.299 MB, 528 KB, and 1.336 MB. Focused runtime
extraction keeps the facade at 399,927 bytes/9,967 measured lines; dependency and
import caps do not move.
Full-chain child recovery, terminal-ancestor reaping, PostgreSQL restart proof, and the
current upstream audit then measure 2,336,092 source bytes, a 533,691-byte wheel, and a
1,354,043-byte pre-final-evidence sdist. Only aggregate ceilings move to 2.338 MB,
536 KB, and 1.360 MB. The facade is 399,477 bytes/9,974 measured lines; dependency and
import caps remain fixed.
Pre-provisioned result identities and the opt-in deterministic effect testkit then
measure 2,348,703 source bytes, a 537,066-byte wheel, and a 1,360,374-byte
pre-final-evidence sdist. Only aggregate ceilings move to 2.351 MB, 539 KB, and
1.365 MB; the same dependency, import, and facade caps remain fixed.
History-bounded partial indexes, planner gates, and the recovery benchmark then measure
2,349,391 source bytes, a 537,310-byte wheel, and a 1,367,449-byte pre-final-evidence
sdist. Only the documentation/test-bearing sdist ceiling moves to 1.372 MB; the
2.351 MB source, 539 KB wheel, dependency, import, and facade caps remain fixed.
The finite SQLite/PostgreSQL race matrix and its public database barrier then measure
2,352,358 source bytes, a 537,746-byte wheel, and a 1,373,419-byte pre-final-evidence
sdist. Ceilings move narrowly to 2.355 MB source and 1.378 MB sdist; the 539 KB wheel,
dependency, import, and facade caps remain fixed.
Nested global/tenant/session admission, public admission/store overloads, and the
PostgreSQL load gate then measure 2,367,759 source bytes, a 540,534-byte wheel, and a
1,385,711-byte pre-final-evidence sdist. Aggregate ceilings move narrowly to 2.370 MB,
542 KB, and 1.390 MB. The facade remains below the unchanged 400 KB/10,000-line cap at
399,981 bytes/9,982 lines; dependencies and import caps remain fixed.
The backup and bounded-outage recovery evidence then measures 2,368,847 source bytes,
a 540,757-byte wheel, and a 1,399,684-byte pre-final-evidence sdist. Only the
documentation/test-bearing sdist ceiling moves again, narrowly to 1.402 MB; source,
wheel, facade, dependency, and import caps remain fixed.
The owned fenced-promotion executable, pure destructive-authority contracts, isolated
CI job, and operator/release evidence leave source and wheel at the same 2,368,847 and
540,757 bytes; the pre-final measured sdist is 1,412,832 bytes. Only its ceiling
moves narrowly to 1.415 MB; all runtime/import/dependency/facade caps remain fixed. The
measured pair passes core, sdist, MCP, AgentOS, Anthropic, PostgreSQL dependency checks and
an installed-wheel live fenced-promotion drill.
The synchronous remote-apply/abrupt-loss companion, deterministic exact-sender fault,
immutable offline-image path, pure/CI contracts, and cross-linked operational evidence
leave source and wheel unchanged again; the pre-final sdist is 1,424,586 bytes. Only
the documentation/test-bearing sdist ceiling moves narrowly to 1.427 MB. The six core
dependencies, source, wheel, facade, import-time, and imported-module ceilings do not
move; exact final bytes and hashes remain detached provenance.
The double-rewind round-trip role-rotation probe, pure authority contracts, HA CI lane,
and cross-linked operator/release evidence again leave source and wheel unchanged at
2,368,847 and 540,757 bytes. The pre-final sdist is 1,436,762 bytes, so only its ceiling
moves narrowly to 1.439 MB. Six dependencies, the 2.370 MB source, 542 KB wheel,
400 KB/10,000-line facade, two-second import, and 900-module budgets remain fixed.
The external writer-authority seam and first-party etcd adapter are isolated in
`runtime/postgres_authority*.py` and exported from `agnoclaw.runtime`, so the already
saturated `AgentHarness` facade does not grow. The earlier authority module/store
integration, pure/live probe, CI, and documentation
measure 2,381,112 source bytes, a 543,835-byte wheel, and a 1,451,567-byte pre-final
sdist. Their ceilings move narrowly to 2,383,000, 545,000, and 1,453,000 bytes. Exact
final hashes remain detached provenance because this page is bundled into the sdist. The six-
dependency, 400 KB/10,000-line facade, two-second import, and 900-module budgets do not
move.
The etcd adapter adds no core dependency: it reuses the already direct `httpx`
dependency and leaves TLS/auth/proxy policy on an injected client. Its latest pre-final
source, wheel, and sdist measure 2,394,754, 547,546, and 1,465,038 bytes under narrowly moved
2,400,000/551,000/1,478,000 ceilings. Twine, clean wheel/sdist/PostgreSQL-extra
installs, and installed-wheel live etcd plus PostgreSQL dual-writer drills pass. The
composed PostgreSQL probes also load their exact sibling helpers under Python isolated
mode, preventing checkout path state from weakening installed-artifact evidence.
The separate 400 KB/10,000-line single-module cap did not move. The facade cap makes extraction a hard growth boundary
rather than hiding complexity inside the aggregate package allowance. Core dependencies
remain capped at six; import remains capped at two seconds and 900 modules.
The secure three-voter mTLS/RBAC/quorum tranche's documentation-complete checkpoint
measures 2,403,138 source bytes, a 549,038-byte wheel, and a 1,480,007-byte pre-final
sdist. Only the exceeded aggregate
source and sdist ceilings move narrowly to 2,405,000 and 1,483,000 bytes. The wheel,
dependency, facade, import-time, imported-module, and single-module caps remain fixed;
exact final hashes remain detached because this page ships inside the sdist.
The current exact-observer/durable-worker checkpoint measures 2,893,908 source bytes, a
642,017-byte wheel, and a 1,640,489-byte sdist. Only the aggregate source/wheel/sdist
ceilings move narrowly to 2,896,500/644,000/1,643,000 bytes. Six dependencies, the
400,000-byte/10,000-line facade, two-second import, and 900-module ceilings remain fixed.
Median import is 0.292558 seconds across 890 modules, and `agent.py` is 398,037
bytes/9,927 lines after reconciliation composition was extracted. Both artifacts pass
Twine; the isolated exact core wheel exercises the schema-v4 learning lease without an
optional provider dependency. Final hashes remain detached because this page is bundled.
The subsequent provider-neutral Agno evaluation-subject checkpoint measures 2,903,447
source bytes, a 644,710-byte wheel, and a 1,648,990-byte documentation-complete pre-final
sdist. Only the three aggregate
ceilings move narrowly to 2,906,000/646,000/1,651,000 bytes. Six dependencies, the
398,037-byte/9,927-line largest module, two-second import ceiling, and 900-module cap do
not move; median import is 0.314777 seconds across 891 modules. Both artifacts pass
Twine, package budgets, dependency checks, and isolated installed-artifact smoke with a
real Agno Agent plus host-supplied provider-neutral model. The artifact intentionally
remained 0.11.0 at that checkpoint, before the 0.12 gates completed.
The governed-corpus checkpoint then measures 2,922,144 source bytes, a 648,902-byte
wheel, and a 1,658,151-byte documentation-complete pre-final sdist. Only aggregate ceilings move narrowly to
2,925,000/651,000/1,660,000 bytes. Six dependencies, the 398,037-byte/9,927-line largest
module, two-second import ceiling, and 900-module cap remain fixed; median import is
0.286436 seconds across 892 modules. Twine, dependency checks, and isolated exact
wheel/sdist smoke pass; that smoke constructs the public content-free corpus contract
and confirms governed qualification is the safe default. The artifact remained 0.11.0 at that checkpoint.
The runner-schema-1.2 fresh-process checkpoint measures 2,985,467 source bytes, a
660,358-byte wheel, and a 1,689,407-byte documentation-complete pre-final sdist. Only
aggregate ceilings move narrowly to 2,988,000/662,000/1,693,000 bytes. Six dependencies,
the 398,709-byte/9,947-line largest module, two-second import ceiling, and 900-module cap
remain fixed; median import is 0.295263 seconds across 893 modules. The standard-library
process boundary adds no dependency. Exact isolated wheel/sdist smoke executes the
public protocol in a fresh child; final artifact evidence follows the documentation
rebuild. The artifact remained 0.11.0 at that checkpoint; the release ships as 0.12.0.

Schema-v7 approval integration initially pushed `agent.py` beyond the deliberate cap.
Governed call composition now lives in `capability_runtime.py`, elevated/session
command ownership in `session_commands.py`, and the context domain/session adapter in
`context_management.py`/`context_runtime.py`; profile, Agent, and built-in
materialization now live in the three `runtime/*materialization.py` modules, and typed
provider-overflow policy lives in `context_overflow.py`, and first-party dispatch in
`runtime/tool_ingress.py`. Deterministic request/checkpoint serialization is extracted
to `runtime/checkpoints.py`, and trajectory projection is isolated in
`runtime/trajectory.py`; the facade is now 399,251 bytes and 9,970 measured lines. The single-module
ceiling was not increased, although the
remaining headroom is intentionally treated as an extraction trigger rather than a
growth allowance.

The final 0.12 candidate is larger than those retained incremental checkpoints:
3,272,866 Python-source bytes, a 725,135-byte wheel, a 1,920,793-byte pre-final sdist,
and a 461,620-byte/11,392-line `agent.py`; cold import loads 1,011 modules in 0.343
seconds. Release ceilings are therefore rebaselined with roughly two percent headroom
to 3.34 MB/740 KB/1.96 MB, 470 KB/11,600 lines, and 1,030 modules. The six-dependency and
two-second import caps do not move. This is an explicit late-release tradeoff: facade
and import-graph extraction are tracked for post-0.12 instead of risking the already
verified lifecycle contract during packaging freeze.

## Runtime inspection API

```python
from agnoclaw import AgnoFeature, inspect_agno_compatibility

report = inspect_agno_compatibility()
print(report.version, report.lane)

if report.has(AgnoFeature.EVALUATION_ENVIRONMENTS):
    ...

if report.has(AgnoFeature.MODEL_EVALUATION_SUBJECT):
    ...  # public Agent.arun + RunOutput/RunMetrics adapter

# Raises AGNO_CAPABILITY_UNAVAILABLE with version and resolution context.
report.require(AgnoFeature.FILESYSTEM)
```

`require_supported_agno()` rejects versions outside the production range. Tooling may
pass `allow_preview=True` only in the quarantined certification lane. Version-sensitive
product code belongs behind `agnoclaw.compat`; ad-hoc checks spread through feature
modules are not an accepted compatibility strategy.

## Feature support

| Surface | Current level | Qualification |
|---|---|---|
| Core `run/arun`, raw streaming, tools, policy, hooks, events | contract-tested on 2.6.4, 2.9.0, and 3.0.0a1 preview | Explicit profiles enter lifecycle; raw streaming uses a bounded presentation, sync calls reuse a harness-owned event loop, cross-loop API mixing fails closed, and named legacy retains direct behavior. Model/provider network calls are mocked in the default suite. |
| Durable native tool/approval recovery | conditionally certified on Agno 2.9.0; conservative fallback on 2.6.4 | Exact feature detection requires the native `tool-batch` checkpoint plus cancel/continue signatures. The certified path is non-streaming, run-owned, artifact-backed, and restricted to governed registered capabilities; unsupported surfaces never silently enter it. |
| Model-driven skill activation | Agno-native progressive disclosure on the primary 2.9.0 lane | A behavioral two-provider turn proves the model selects `get_skill_instructions`, receives one trust-filtered exact skill, and continues under its allowed-tool boundary. Community and model/context/schema-changing skills remain explicit-only. |
| First-party CLI/chat/TUI/heartbeat/scheduler routing | profile-routed preview | Explicit quick/durable/service work uses lifecycle start/wait and records runtime identity; only named legacy remains direct. The schema-v12 RuntimeSchedulerBackend adds database-clock SQLite/PostgreSQL occurrence claims, leases/fences, deterministic retries/jitter, misfires, bounded concurrency groups, and same-attempt lifecycle reattachment; JSON remains compatibility-only. Async REPL/TUI and sync chat model work use the same lifecycle plus bounded presentation and artifact text replay. Host-declared capability-only child runs use the lifecycle kernel; explicit profiles omit/reject raw subagents and Agno Team presets. Named-legacy compatibility certification, first-party cross-child artifact reads, restart-safe/pre-spend hard limits, multi-tenant schedule administration, migration cutover, and production scheduler partition/soak proof remain open. |
| Agno LearningMachine core stores | import/config plus exact-name observer contract-tested; one local vector smoke on 2.9.0 | Institutional Learned Knowledge additionally requires vector-backed `Knowledge`. The public `VectorDb.name_exists` contract and first-party content-free reconciliation composition pass on Agno 2.6.4/2.9.0. A real Ollama-embedding/LanceDB smoke now saves through `learned_knowledge_store` and recalls through Agno context; production vector backends and failover/partition/soak still need certification. |
| Agno model-backed evaluation subjects | contract-tested on 2.6.4 and 2.9.0; narrow live benefit proof on 2.9.0 | `AgnoEvaluationSubject` adapts a host-supplied fresh `Agent.arun()` to the paired runner using public content/token/cost fields, opaque per-rollout sessions, and optional owned cleanup. The local `learning_benefit_probe.py` now proves this exact Learned Knowledge configuration beats an identical no-learning control in held-in/out/transfer cases. The independent verifier, frozen gates, and explicit promotion boundary remain agnoclaw-owned; multi-provider/previous-version/general benefit remains uncertified. |
| Harness-owned model transports | warning-clean on live Ollama plus unit/factory ownership contracts | A model materialized from a harness-owned string/default spec is closed for both the base and fresh per-run Agent, including Ollama's currently non-forwarded HTTPX transport. `AgnoModelFactory` gives custom enterprise/replay/test transports the same fresh-per-run ownership in explicit profiles, binds their implementation digest into the harness spec, and rejects shared instances or identity drift before dispatch. A directly injected Agno Model remains caller-owned and quick/legacy-only. `AgnoEvaluationSubject(close_agent=True)` applies explicit ownership to evaluation subjects. `ResourceWarning` and unraisable warnings are release-blocking. |
| Improvement process subjects | local POSIX contract-tested preview | Runner schema 1.2 binds paired execution-contract digests and launches one no-shell child per rollout with empty-by-default environment, a fresh temporary working directory, bounded request/stdout/stderr, redacted errors, mandatory cleanup, and process-group descendant reaping. This is local state/crash/lifetime isolation, not hardened filesystem/network/kernel sandboxing; use the separate strict Docker subject when applicable. VM and Windows process-tree certification remain open. |
| Improvement Docker subjects | Linux Docker contract-tested preview plus live-daemon proof | An immutable exact-platform image, no pull/network/host environment/mounts, read-only non-root execution, bounded no-exec temporary storage, zero capabilities, `no-new-privileges`, built-in seccomp, CPU/memory/PID/file limits, and exact-owner cleanup are bound into paired evidence. The Docker daemon, host kernel, and reviewed image remain trusted; rootless deployment, VM/provider-egress profiles, non-Linux engines, and production soak remain uncertified. |
| Improvement evaluation corpus | provider-neutral contract-tested foundation | Content-free manifests bind exact ordered payload/lineage digests, split exposure, source usage/retention, selection/sampling/sealed-access controls, independent curation, and exact-scope decontamination evidence. The default gate rejects ungoverned reports. Managed registry/ACL enforcement, semantic near-duplicate certification, provider/model training-set proof, and stronger VM/provider-egress isolation remain open. |
| Learning evaluation archive | SQLite/PostgreSQL contract-tested preview with bounded scale gate | Schema-v5 typed content-free columns and a validated reason-code relation avoid canonical-JSON filter scans while preserving canonical evaluation truth. The optional host read model defaults to rejected/inconclusive verdicts and filters exact-owner results by stable reason/evaluator/mechanism/target/safety fields with a descending scope-bound keyset cursor; it omits content, notes, raw metrics, and artifact IDs. PostgreSQL 17 passes live v4→v5 migration, query/filter/pagination, and a 10,000 queried-owner plus 10,000 noisy-neighbor gate at 58.87 ms noisy p99 and 0.974x slowdown. Production cardinality/skew, memory, failover/partition, retention, and additional-index policy remain uncertified. |
| Learning application/outcome attribution | SQLite/PostgreSQL schema-v6 contract-tested preview | `AgentHarness` resolves the promoted Agno target and exact authorized run, distinguishes retrieval from application, and records content-free evidence outside the candidate CAS row. One host/operator outcome can settle an applied record; exact candidate/run/owner linkage, evidence, sign-consistent scoring, one-outcome uniqueness, idempotency, and conservative non-mutating recommendations pass 51/51 focused local tests, 148/148 on both supported Agno lanes, and 11/11 on PostgreSQL 17. One local model-backed no-learning mechanism smoke passes; previous-version/general benefit, production volume/failover/retention, and automatic outcome-processor wiring remain uncertified. |
| Agno 2.8 evaluation environments and FileSystem | capability-detected | The direct Agent subject adapter and agnoclaw outcome-quality gates exist. Agno Environment/Case/Scorer isolation adapters and production certification remain planned. |
| AgentOS | optional 0.12 preview | Install `agnoclaw[server]`; native-compatibility and versioned lifecycle contracts pass on Agno 2.6.4/2.9.0 and retained FastAPI 0.136.3/0.141.1 lanes, including OS-key/auth-state normalization, claims-first identity, scopes, result/reattach, lifecycle/output cursor pages, commands, disconnect semantics, and malformed peers. JWT issuer integration, proxy/load/soak, and production failover certification remain open. |
| SQLite | durable single-node reference | Schema-v12 runtime/operation/artifact/approval/child/outbox/recovery/reconciliation/dead-letter/scheduler conformance and v7→v12 migrations are contract-tested. Scheduler contention/reopen/fencing passes, and a repeatable gate passes 50/50 real ungraceful exits inside run-create, lifecycle-transition, operation-prepare, dispatch, and settlement transactions with rollback, idempotent retry, exact-event, integrity-check, and WAL-checkpoint proof. Host-power-loss/filesystem certification, full backup/restore, and long-duration production soak remain. |
| PostgreSQL | optional live-service development preview | Install `agnoclaw[postgres]`; the runtime/learning matrix, schema-v12 scheduler parity, multi-connection races, read-only inspection, and the complete service-migration development lifecycle pass against disposable PostgreSQL 17. Existing atomic output, child recovery, connection-loss, effect-race, recovery-index, bounded load, restart, promotion, synchronous-loss, role-rotation, writer-authority, secure-etcd, native restore, forced migration-process death, three-database least privilege, and 5,000-row bounded-memory evidence remains. This is not external controller election, durable multi-AZ quorum/partition/rotation, arbitrary-client physical fencing, production scheduler failover/soak, encrypted off-host/artifact/key/PITR recovery, corruption response, or production migration certification. |
| MCP tools | preview on SDK 2.0 / protocol `2026-07-28` | Two-tool deferred discovery/call, stdio, Streamable HTTP, explicit legacy SSE, pagination, structured content, schema-digest drift checks, conservative lifecycle effects, and run-owned quick/legacy clients are contract-tested. OAuth, resources/prompts/subscriptions/apps/tasks/extensions, and real-network certification remain open. |
| Providers | Agno-routed, not uniformly certified | Each advertised provider/model needs auth, streaming, structured output, tool, usage, cancellation, and error fixtures. |

## Upgrade policy

1. Recheck official stable and prerelease versions.
2. Add every harness-relevant upstream delta to
   [Agno release-practice audit](agno-release-practices.md) with adopt/adapt/avoid and a
   contract owner.
3. Run minimum, primary, and preview lanes before changing the lock.
4. Promote the development lock only after minimum and primary lanes pass.
5. Never widen the published stable range to an untested minor or prerelease.
6. At release-candidate freeze, generate the final range from archived evidence and
   test the exact wheel/sdist, not just the source checkout.

## Known compatibility debt

- Full Ruff and mypy with untyped-body checking are blocking CI gates and pass locally;
  the matching hosted Ubuntu execution is still required for release certification.
- Python 3.14 previously triggered deprecated `codecs.open()` calls in
  `python-frontmatter`; T2 replaced it with an owned UTF-8/PyYAML reader and the final
  refreshed Python 3.14 lane proves the warning is absent.
- The T2b process-kill probe certifies Agno 3.0.0a1 PostgreSQL queue reclaim and stale
  attempt fencing, but not exactly-once external effects; stable Agno 2.x exposes no
  certified settled provider-operation resume boundary. [ADR-0001](adr-0001-recovery-ownership.md)
  keeps the Agno 3 queue/run/event surfaces as future adapters and makes agnoclaw's
  operation/effect ledger authoritative until every atomicity, tenancy, cursor,
  settlement, migration, and load gate passes.
- The agnoclaw-owned path now has a separate three-boundary real-process proof on the
  primary Agno 2.9.0 line: `AgentHarness.start()` persists a planned request that a new
  harness continues once, refuses to redispatch an in-flight model request, and
  completes a settled result after reopening the Agno SQLite database, runtime ledger,
  and artifacts. The provider-neutral host model makes this a lifecycle/recovery proof,
  not cloud-provider receipt, billing, cross-version, or multi-host certification.
  Separate primary-line tool-checkpoint and approval gates certify one governed
  two-provider/one-capability sequence and its pre-tool approval wait. They do not
  certify raw/custom/nested/parallel tools, streaming or parser/output-model paths,
  live-provider receipts, power loss, or multi-host behavior.
- The focused landing README, getting-started, CLI, and configuration guides have
  complete documentation-index coverage plus executable local-link, README-budget, and
  private-API gates. The complete top-level API reference is generated with a canonical
  surface digest and CI staleness check. Search, versioned-site generation, and full
  installed-snippet execution remain release gates.
- Async chat and TUI rendering now enters lifecycle execution for every explicit profile and
  reconciles the bounded live display to terminal truth. The live display itself is
  process-local, while extracted text replays from scoped artifacts locally/remotely.
  A killed worker can still lose the one bounded in-memory segment and cannot resume an
  ambiguous provider operation. Named-legacy streams, sync chat, and raw-tool child
  harnesses remain direct compatibility paths; explicit profiles reject the latter two.
  This is broader first-party migration, not universal adapter parity.
