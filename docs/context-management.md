# Context accounting, compaction, search, and rehydration

Maturity: **preview**

The v0.12 development branch treats the model window as a bounded cache. Before either
manual or opt-in automatic compaction replaces Agno history, agnoclaw writes the
complete pre-replacement `AgentSession` trajectory and an itemized message segment to
the session's scoped `ArtifactStore` namespace. Automatic compaction is a preview,
in-process controller; it is off by default while adversarial/model-backed drift,
distributed fencing, cloud-provider breadth, and production-duration certification
remain open. One pinned local Ollama tool-bearing overflow configuration now passes the
same 100-turn/reopen/recovery gate described below.

## Configure a recoverable context archive

```python
from agnoclaw import AgentHarness, LocalArtifactStore

artifacts = LocalArtifactStore("./.agnoclaw/artifacts")

harness = AgentHarness(
    "anthropic:claude-sonnet-4-6",
    session_id="incident-42",
    user_id="operator-7",
    tenant_id="tenant-a",
    artifact_store=artifacts,
    enable_session_summary=True,
    max_context_tokens=120_000,
    auto_compact_context=True,
)
```

An `artifact_store` is mandatory for real replacement. Without it,
`compact_session()` raises `CONTEXT_ARTIFACT_STORE_REQUIRED`; use
`summarize_session()` when summary-only behavior is intended. Automatic compaction
also requires a positive `max_context_tokens`; constructor validation fails before
agent work begins when either prerequisite is absent.

## Inspect the budget

```python
budget = harness.inspect_context_budget()
if budget is not None:
    print(budget.used_tokens, budget.max_tokens, budget.action, budget.exact)
```

The model's tokenizer is used when its `count_tokens()` method returns a valid integer.
Otherwise agnoclaw returns a deterministic UTF-8 estimate with `exact=False`. The
recommendation thresholds are:

| Utilization | Action |
|---:|---|
| below 80% | `none` |
| 80% | `prepare` |
| 90% | `compact` |
| 97% | `emergency` |

With `auto_compact_context=False`, these are read-only recommendations and a warning
never means that compaction ran. With the explicit opt-in:

- below 90%, the preflight remains read-only;
- at 90%, agnoclaw runs the governed memory-preservation prompt, produces a scoped
  summary, archives the full source trajectory, and replaces history only if the new
  live checkpoint measures at or below 70%;
- at 97%, it avoids additional model calls, creates a deterministic bounded emergency
  summary from recent messages, archives the same full trajectory, and applies the
  same 70% release condition.

The separate prepare, compact, emergency, and release boundaries provide hysteresis:
a successful replacement cannot immediately retrigger at the next call. An oversized
replacement fails before the Agno session row is changed. When only the narrative
summary would cross the automatic 70% release boundary, agnoclaw deterministically
bounds that narrative and points to the exact scoped archive; required checkpoint IDs,
latest intent, and retained invariant index are never clipped. If those required bytes
alone cannot fit, replacement still fails closed.

Automatic preflight runs before the caller's model request. Async applications should
use `arun()`. Calling sync `run()` at a compaction boundary while already inside an
event loop fails with `CONTEXT_AUTOMATION_ASYNC_REQUIRED`; sync callers outside an
event loop are supported.

## Reactive provider overflow

The same opt-in also handles Agno's typed `ContextWindowExceededError` (including a
`ModelProviderError` or error `RunOutput` that Agno's own classifier maps to it). The
harness uses the deterministic emergency checkpoint, then retries the exact failed
model invocation once. It does not rerun pre-hooks, policy, prompt construction, or the
whole harness call. The prompt, run ID, metadata, tool view, and trusted context remain
the same; the archived/replaced session history is the intended difference.

Recovery is deliberately refused when:

- automatic compaction is disabled or its archive/budget prerequisites are absent;
- the request is streaming;
- any tool call was observed in the current model loop;
- another same-session run is active; or
- the one retry also exceeds the context window.

The maintenance fence remains held through the retry. This prevents another direct
same-session call from entering between replacement and the retried provider request.
A second overflow raises `CONTEXT_OVERFLOW_RETRY_EXHAUSTED`; it never loops. Generic
message exceptions are not guessed to be overflows—exception recovery requires Agno's
typed provider hierarchy, while error outputs use Agno's own published classifier.

## Session maintenance and concurrency

Every direct run, including an open sync or async stream, owns a session-activity lease
until completion or explicit close. Manual and automatic replacement acquire a
session-local maintenance lease first. A same-session collision fails retryably rather
than racing history replacement; unrelated sessions remain independent at this layer.
Internal preservation and summary calls carry maintenance ownership and still pass
through the normal governed run path. They are also typed in protected, task-local run
metadata before archival, so quoted transcript text and memory-maintenance prompts do
not become new `user_intent` evidence. Summary synthesis receives an empty tool surface
and disables Agno history injection; any tool-shaped or empty summary result is rejected
in favor of the bounded deterministic transcript fallback. This prevents quoted prior
tool instructions from becoming live effects while retaining the attempted internal run
in the immutable trajectory for audit.

The in-memory coordinator remains the zero-configuration process-local layer. For
multiple cooperating processes on one POSIX host, inject one provider rooted at the
same trusted directory everywhere:

```python
from agnoclaw import AgentHarness, LocalFileContextLockProvider

harness = AgentHarness(
    model,
    context_lock_provider=LocalFileContextLockProvider(
        "/var/lock/agnoclaw/context"
    ),
    # session_id, user_id, tenant_id, artifact_store, and context settings omitted
)
```

Normal runs take a non-blocking shared `flock`; manual/automatic replacement takes an
exclusive lock; reactive overflow converts its sole shared lock to exclusive before
archival. A competing reader or writer fails with
`CONTEXT_CROSS_PROCESS_LOCK_UNAVAILABLE` before a session write. Replacement validates
the still-open exclusive descriptor immediately before `asave_session`; lost ownership
fails with `CONTEXT_CROSS_PROCESS_LOCK_LOST`. Lock filenames contain only the exact
tenant/user/session scope digest. The canonical directory digest enters the harness
spec so deployments can detect differently configured workers without disclosing the
path. Descriptors and locks release on normal close and process death.

This is cooperative same-host fencing. Every writer must participate, and the
directory must be local storage with proven POSIX `flock` behavior. It is not a
multi-host, NFS, database-CAS, or arbitrary-client fence. A future distributed adapter
must prove that its ownership survives through the actual Agno session save boundary;
a short-lived lease check next to a blind upsert is insufficient.

## Summary-only versus replacement

```python
# Does not remove or replace any run history.
summary = await harness.summarize_session(session_id="incident-42")

# Flushes memory through the governed run path, archives the full trajectory,
# checks that replacement is smaller, then commits one compacted live run.
checkpoint = await harness.compact_session()
print(checkpoint.segment_id, checkpoint.saved_tokens)
```

Hosts may supply an already reviewed summary:

```python
checkpoint = await harness.compact_session(
    summary="The deployment is paused; rollback approval is still pending."
)
```

For unattended work, prefer a reviewed typed continuation instead of asking one prose
summary to carry every invariant:

```python
from agnoclaw import ContextContinuationRecord

continuation = ContextContinuationRecord(
    summary="The deployment remains paused while the canary is investigated.",
    goal="Restore the canary without widening impact.",
    plan=("Inspect the failed shard.", "Re-run the bounded canary."),
    progress=("Rollback artifacts have been verified.",),
    decisions=("Keep traffic at 1% until the shard passes.",),
    approvals=("Operator approved read-only production diagnostics.",),
    open_questions=("Did shard 7 receive configuration generation 42?",),
    tests=("tests/test_canary.py: 18 passed",),
    files=("deploy/canary.yaml", "src/canary/health.py"),
    citations=("incident://canary/2026-08-17",),
)

checkpoint = await harness.compact_session(continuation=continuation)
```

Each structured entry has a stable item ID, exact kind, continuation-record
provenance, and `invariant=True`. It remains independently searchable and selectively
rehydratable even when the prose summary omits it. The live checkpoint contains a
token-efficient priority index: goal first; then approvals, decisions, open questions,
and plans; then failures, artifacts, citations, tests, and files; then progress. Full
item IDs stay in the content-free manifest and search results instead of consuming the
model window. That index is independently bounded to 16 entries and 4,000 characters,
selecting higher-priority and then newer evidence before clipping. Records are bounded
to 64 entries per field, 256 total entries, 16 KiB per entry, and a 64 KiB summary.
Supplying both `summary=` and `continuation=` fails before maintenance begins with
`CONTEXT_CONTINUATION_CONFLICT`.

The manifest records both total archived-item tokens and source-message tokens.
Checkpoint savings and the replacement quality gate use only source-message tokens;
derived continuation entries can never inflate the “before” measurement and make an
oversized live checkpoint appear safe.

This explicit path does not pretend an arbitrary model summary is structured. On the
first automatic compaction, agnoclaw source-binds the exact first real user message as
the active goal, recording its ordinal, content digest, and normalization. Later
compactions carry the latest structured continuation forward with source-item and
source-record provenance. An explicit reviewed `ContextContinuationRecord` supersedes
that active record; prior immutable segments remain auditable. The harness does not yet
infer plan, progress, decisions, approvals, tests, files, citations, or open questions
from arbitrary model prose. Model-generated extraction/merge and adversarial fidelity
evaluation remain separate gates.

The live replacement contains:

- the current harness system prompt, regenerated normally rather than copied from stale
  history;
- the original caller's latest user intent, captured before agnoclaw adds its internal
  memory-flush or summary turns;
- the compacted summary;
- deterministic stable IDs for the latest caller intent, recognized spilled-output
  artifact references, tool failures, and any supplied typed continuation entries;
- a bounded priority index for structured continuation, artifact, and failure state
  whose exact full source records remain searchable;
- stable checkpoint, segment, artifact, and retained-item identifiers.

The complete prior session, including tool messages and internal maintenance turns,
remains in the trajectory artifact. The session database stores a bounded content-free
manifest rather than duplicating archived content.

Replacement fails before changing the session when the artifact store is absent, the
session scope cannot be proven, the source is empty, the summary is empty, the manifest
is inconsistent, or the replacement would not reduce the measured context. Staged
bytes left by a failed database write are unreferenced and may be removed after the

## Search archived trajectory

```python
hits = await harness.search_session_context(
    "rollback approval",
    session_id="incident-42",
    limit=10,
)

for hit in hits:
    print(hit.item_id, hit.kind, hit.score, hit.source, hit.excerpt)
```

The current search implementation is deterministic bounded lexical retrieval. It
loads and integrity-checks the exact scoped artifacts, ranks term overlap, supports
item-kind filtering at the lower-level `ArtifactContextArchive` API, and reports
`source=trajectory`. Identical structured values carried through repeated immutable
compactions collapse to their newest searchable instance, so active state cannot crowd
original source evidence out of a bounded result set; the older records remain in their
artifacts. It is intentionally not described as semantic search.

Tenant, user, and session filters cannot override constructor-trusted identity. Stored
user ownership must be present, and a tenant-scoped request must find matching tenant
evidence in the session or its run metadata. Conflicting or missing ownership fails
closed.

## Selective rehydration

```python
selected = await harness.rehydrate_session_context(
    [hits[0].item_id],
    max_tokens=2_000,
)

print(selected.sources)  # trajectory + artifact
print(selected.items[0].provenance)
```

Reading does not alter the live model context. Set `inject=True` only when the host
wants the exact selected items appended as a new live history run:

```python
selected = await harness.rehydrate_session_context(
    [hits[0].item_id],
    max_tokens=2_000,
    inject=True,
)
```

Injected bytes are framed as untrusted historical data, not executable instructions.
Current system, policy, permission, and tool rules remain authoritative. Selection is
bounded to 100 items and the configured token limit.

## Recover spilled governed output

With `max_inline_output_chars` configured, a governed registered capability or a
first-party tool invoked inside `start()` returns a
bounded `agnoclaw.spilled_output` envelope instead of reinserting a large result. The
envelope names its authoritative artifact, checksum, character count, safe head/tail
preview, and the internal `read_spilled_output` capability. Following `next_offset`
reconstructs the exact deterministic rendering in bounded pages.

The reader accepts only committed capability results visible to the exact tenant/user
and either the producing active run or its same trusted session. This is why a
compacted later turn can recover the source while a different session cannot use the
identifier. Compaction recognizes the envelope even when Agno stores it as JSON text,
marks it as an invariant `artifact_reference`, retains its item ID, and records a
bounded artifact/checksum/read-tool index. Search and selective rehydration preserve
the complete envelope and provenance; they do not bypass reader authorization.

For first-party tools, post-tool policy and redaction run before the result artifact is
committed. Repeated Agno call IDs replay that committed result without redispatching the
tool. Host/plugin/pack registered capabilities and configured MCP calls share this
contract. Raw custom/plugin/pack/context-provider and caller-supplied MCP tools fail
lifecycle admission; direct `run()`/`arun()` and outer model output stay on the
compatibility path. See
[Durable artifacts](artifacts.md#model-context-spill).

## Artifact retention and garbage collection

Context artifacts are owned by the content-free session manifest. Before calling
`LocalArtifactStore.garbage_collect()`, include context keys alongside runtime-result
and learning-candidate keys:

```python
context_keys = await harness.context_artifact_storage_keys()
live_keys = [*runtime_keys, *learning_keys, *context_keys]
await artifacts.garbage_collect(live_keys, grace_seconds=86_400)
```

Omitting manifest keys can delete live archived context after the grace period. A
service-wide retention worker must enumerate every authorized session manifest, not
only the currently active harness.

## Error reference

| Code | Meaning |
|---|---|
| `CONTEXT_ARTIFACT_STORE_REQUIRED` | Replacement, search, or rehydration was requested without recoverable byte storage. |
| `CONTEXT_SESSION_REQUIRED` / `CONTEXT_SESSION_NOT_FOUND` | No exact active session can be resolved. |
| `CONTEXT_IDENTITY_CONFLICT` | A requested tenant/user filter conflicts with trusted harness identity. |
| `CONTEXT_SESSION_SCOPE_UNPROVEN` | Stored user or tenant ownership is missing for a scoped operation. |
| `CONTEXT_SCOPE_MISMATCH` / `CONTEXT_SESSION_SCOPE_CONFLICT` | Stored or archived ownership contradicts the requested exact scope. |
| `CONTEXT_BUDGET_REQUIRED` | Automatic compaction was enabled without a positive model-context budget. |
| `CONTEXT_SESSION_BUSY` | Replacement would race any live direct/durable run in the same session. |
| `CONTEXT_MAINTENANCE_IN_PROGRESS` | A caller attempted same-session work while replacement already owned maintenance. |
| `CONTEXT_AUTOMATION_ASYNC_REQUIRED` | Sync automatic compaction was reached inside an event loop; use `arun()`. |
| `CONTEXT_WINDOW_EXCEEDED` | The provider reported a typed overflow while automatic recovery was disabled. |
| `CONTEXT_OVERFLOW_STREAM_UNSAFE` | A streaming overflow cannot be replayed safely. |
| `CONTEXT_OVERFLOW_RETRY_UNSAFE` | A tool call was already observed, so replay could duplicate an effect. |
| `CONTEXT_OVERFLOW_RETRY_EXHAUSTED` | Archive-first compaction succeeded, but the single retry also overflowed. |
| `CONTEXT_COMPACTION_QUALITY_FAILED` | Source, summary, identity chain, manifest, token savings, or artifact evidence failed validation. |
| `CONTEXT_CONTINUATION_CONFLICT` | Both prose `summary=` and typed `continuation=` were supplied; choose one. |
| `CONTEXT_MANIFEST_CONFLICT` / `CONTEXT_MANIFEST_LIMIT` | The evidence chain is non-contiguous, inconsistent, stale, or over its bound. |
| `CONTEXT_SEARCH_BOUND_EXCEEDED` | Search would exceed its configured segment bound. |
| `CONTEXT_ITEM_NOT_FOUND` | A selected stable item is absent from the exact scoped archive. |
| `CONTEXT_REHYDRATION_BUDGET_EXCEEDED` | Selected evidence exceeds the bounded reinsertion budget. |
| `CONTEXT_CROSS_PROCESS_LOCK_UNAVAILABLE` | A cooperating process owns conflicting shared/exclusive session context activity; retry later. |
| `CONTEXT_CROSS_PROCESS_LOCK_LOST` | The active shared/exclusive ownership could not be proven at the write boundary; do not continue blindly. |
| `CONTEXT_FILE_LOCK_UNSUPPORTED` | The local provider was selected on a platform without POSIX `flock`. |
| `CONTEXT_COMPACTION_COMMITTED_AFTER_CANCELLATION` | Cancellation raced the final row write; replacement committed and must not be repeated blindly. |
| `CONTEXT_REHYDRATION_COMMITTED_AFTER_CANCELLATION` | Cancellation raced live reinsertion; the selected items committed. |

## Current certification boundary

Implemented and tested:

- deterministic fallback token accounting and typed budget recommendations;
- stable identity-scoped item, segment, and checkpoint IDs;
- artifact-first full-trajectory archival and checksum verification;
- actual manual Agno history replacement;
- separation of internal maintenance prompts from unresolved caller intent;
- bounded content-free manifests and authoritative GC-key exposure;
- exact-scope lexical search and selective provenance-bearing rehydration;
- explicit untrusted-data framing for live reinsertion;
- opt-in 90% proactive compaction with a 70% release boundary;
- deterministic no-extra-model-call emergency compaction at 97%;
- session-local admission covering direct runs and open streams;
- scoped summary generation for explicitly targeted sessions;
- one exact-invocation reactive retry with tool/stream/attempt fences.
- opt-in lossless registered-capability and lifecycle first-party-tool spill with
  bounded same-session paging;
- deterministic artifact-reference/failure extraction, retained IDs, bounded live
  index, and search/rehydration provenance.
- a public bounded `ContextContinuationRecord` for goal, plan, progress, decisions,
  approvals, open questions, tests, files, and citations; every entry is an immutable
  searchable invariant and the live priority index omits token-heavy stable IDs;
- opt-in same-host cooperative reader/writer fencing with content-free exact-scope
  filenames/spec identity, fail-fast contention, sole-reader overflow upgrade,
  pre-save validation, idempotent release, and a real child-process exclusion proof;
- a disposable deterministic 100-turn Agno/SQLite gate with 13 threshold-triggered
  compactions plus one final archive boundary, two scheduled close/reopen boundaries,
  one final verification reopen, 11/11 side-effect-free Agno-native function calls,
  exact head/middle/tail input and tool-result retrieval, bounded selective
  rehydration, content-free manifest checks, and integrity reads of all 14 artifacts.

Run the long-session gate explicitly; its 100 real Agno turns make it a certification
test rather than part of the fast unit loop:

```bash
uv run python scripts/long_run_continuity_probe.py \
  --turns 100 \
  --restart-turns 30,70 \
  --max-context-tokens 1800 \
  --tool-every 10
```

The retained 2026-08-17 run passed with 10 contiguous compactions (nine automatic),
three verified database reopens, 11 exact-once tool calls, three input plus three
tool-result retrieval/rehydration checks, one persisted injection, and 909/1,800 final
live tokens. The three tool-result markers exist only in actual tool responses. The
gate also exposed and fixed the new-session preflight boundary: Agno 2.x's plain
`Session not found` result now means zero existing history, while unrelated database
errors still propagate.

The opt-in live mode also passes on Agno 2.9.0 with the exact local
`qwen2.5:7b` Ollama digest documented in [Harness evaluation](evaluation.md): 100
provider-backed turns, 11/11 native tool calls, 14 compactions, three reopens, typed
canonical input/tool-result recovery, 14 artifact-integrity checks, exactly-once
injection, and 1,038/1,800 final live tokens. Run it with `--provider ollama
--allow-live-model`; non-loopback origins additionally require
`--allow-remote-ollama`. A `qwen3:0.6b` attempt failed closed when the model skipped a
mandated turn-10 tool call, demonstrating that model adherence is part of the gate.

The 2026-08-18 post-change certification reran both gates after tool-free typed
summary maintenance, automatic goal carry, carried-state search deduplication, and
required-checkpoint-preserving narrative fitting landed. The deterministic gate passed
in 85.52 seconds and the pinned live gate passed in 924.54 seconds; the live oracle
again required all 100 turns, 11/11 exact tool calls, three verified reopens, repeated
compaction, bounded final context, and exact head/middle/tail recovery.

The fresh release-closeout deterministic run after provider/tool-checkpoint recovery
changes also passes: 100 turns, 141 deterministic model calls including maintenance,
13 automatic compactions plus the final archive, 14 artifact-integrity loads, three
verified reopens, 11/11 exact-once native tool calls, all six input/tool-result
retrieval and rehydration checks, exactly-once persisted injection, and a bounded
1,253/1,800-token final context.

Still open for final 0.12 certification:

- live credentialed/cloud overflow and tool-selection certification across advertised
  providers beyond the retained local Ollama configuration;
- automatically extract and fidelity-check plan/progress/decision/approval/test/file/
  citation/open-question fields instead of requiring a reviewed manual record; exact
  first-user goal capture, carry-forward provenance, and explicit supersession already
  exist;
- adversarial/model-backed summary and invariant-retention evaluation across hours-long
  sessions and multiple providers;
- extend implemented governed spill to direct compatibility tools, governed
  context-provider/raw-MCP adapters, and outer-model results;
- multi-host/database-backed manifest CAS and session-save fencing, plus proof against
  non-cooperating Agno writers;
- remote artifact/index adapters, semantic or hybrid search, retention automation, and
  service-wide deletion proof.

Do not wrap the preview API in a second unattended compaction loop. The built-in
controller is useful for host-local long sessions and now has deterministic repeated
compaction/reopen, one pinned live local-provider run, and opt-in same-host process
fencing evidence, but cloud-provider breadth, multi-host save-boundary fencing, and
model-backed drift-quality certification remain release-gated T7/T12 deliverables.
