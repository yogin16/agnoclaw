# Durable artifacts

Status: 0.12 development preview; local content store, atomic operation-result and
provider-output-segment references, governed capability/tool spill, and direct-child
lossless result handoff implemented;
complete universal output routing and retention automation remain in progress

Last verified: 2026-08-12

`ArtifactStore` is agnoclaw's one external-byte boundary. The `RuntimeStore` remains
authoritative for ownership and liveness; the artifact store owns immutable bytes,
integrity, bounded reads, and optional host-managed encryption.

## Use it with lifecycle runs

```python
from agnoclaw import AgentHarness, LocalArtifactStore, SQLiteRuntimeStore

runtime = SQLiteRuntimeStore("/var/lib/my-agent/runtime.db")
artifacts = LocalArtifactStore("/var/lib/my-agent/artifacts")
harness = AgentHarness(
    model=model,
    runtime_store=runtime,
    artifact_store=artifacts,
)
```

For `start()` runs, the outer model result is normalized to finite JSON and staged
before operation settlement. Runtime schema v10 (using the artifact relationships added
in v6) then atomically commits:

1. the scoped artifact reference;
2. its operation-result relationship;
3. `artifact.committed` and `operation.settled` events and outbox rows; and
4. the successful operation settlement containing the exact artifact ID.

The run's terminal transition follows. If the process dies after operation settlement
but before terminal completion, a new harness configured with the same stores can call
`recover_run()`. It verifies and loads the retained result and completes the run
without dispatching the model again.

Before model intent, lifecycle execution also commits a `run_request_checkpoint`
artifact containing the canonical message, full frozen execution context/admission
envelope, lifecycle keyword arguments, request digest, and harness-spec digest. Explicit
`recover_run()` may use it only when model dispatch is absent or still planned and the
operation/artifact/owner/session/spec/digest chain matches exactly. These bytes can be
sensitive: production deployments must provide tenant-appropriate encryption, access
control, retention, deletion, backup, and restore for request checkpoints and their
ledger references together.

Without `artifact_store=`, the compatibility result-reference behavior remains. A
known successful operation then cannot be reconstructed after process loss and fails
closed with `RUN_RECOVERY_RESULT_UNAVAILABLE`; it is never replayed implicitly.

### Provider-text segments

For reconnectable presentation, call `start(..., persist_output=True)`. Authenticated
AgentOS lifecycle starts default this to true, and explicit-profile async REPL/TUI
calls combine it with a bounded process-local live display. The provider stream remains
inside the model `OperationGateway`; this option does not create a second execution
path.

Each non-empty `RunContent` delta joins a buffer that flushes at 8,192 characters or 32
deltas. The artifact payload carries its schema, run ID, segment sequence, text, and
delta count. The runtime event carries only schema, sequence, artifact ID, character
count, and delta count. `append_runtime_event(..., artifact_reference=...)` commits the
scoped reference, event, monotonic run sequence, and outbox row in one transaction.
An injected failure after reference insertion rolls everything back; staged bytes are
unreferenced and safe for normal artifact garbage collection.

```python
run = await harness.start("Draft the report", persist_output=True)

cursor = None
async for segment in run.output(after=cursor):
    render(segment.content)
    cursor = segment.cursor
```

Loading a segment reauthorizes its run owner and verifies purpose, content address,
stored/plaintext checksums, event-to-artifact binding, content length, and gap-free
segment order. The authenticated HTTP adapter decrypts/loads on the owning service and
returns at most 50 verified segments per page; it does not expose an arbitrary artifact
read endpoint.

Normal completion and lifecycle cancellation flush consumed buffered text. A hard
process failure can lose the one in-flight segment (at most either bound) and can leave
the provider operation ambiguous. Persisted text is useful for
display recovery and evidence; it cannot authorize provider redispatch or reconstruct
the suspended Agno stack. Terminal `wait()` content remains authoritative.
The segmented contract currently accepts plain text only. Use
`RemoteHarnessClient.start(..., persist_output=False)` for structured-output runs and
consume their authoritative terminal result; `RUN_OUTPUT_TEXT_REQUIRED` prevents a
silent text/typed-result semantics change.

PostgreSQL synchronous replication protects the ledger reference, not the external
artifact body. The owned `remote_apply` abrupt-loss drill proves acknowledged runtime
metadata survives its tested database failure path; it does not copy, fence, restore,
or validate `LocalArtifactStore` bytes or their key generations. Production RPO is the
intersection of the database, artifact-object, and key-provider guarantees. Replicate
and rehearse all three as one recovery set before claiming durable output recovery.

## Address and scope

Each `ArtifactReference` binds:

- exact tenant, user, and run ownership;
- plaintext and stored-byte SHA-256 checksums;
- plaintext and stored sizes;
- media type, encoding, purpose, and schema version;
- an opaque storage key;
- optional envelope-key metadata; and
- non-identity reference evidence such as operation metadata and staging time.

The opaque ID is content-addressed within its exact scope and immutable byte/protection
descriptor. Re-staging identical scoped content reuses the same object. Observation
time and per-operation metadata do not create false identity conflicts. A different
tenant/run scope produces a different address even for identical content.

`RuntimeStore.get_artifact()` applies the owning run's exact tenant/user check and hides
unauthorized IDs as `ARTIFACT_NOT_FOUND`. Possession of an artifact ID is not
authorization. Applications should resolve the ledger reference under authenticated
`RunOwner` context before passing it to the byte store.

## Integrity and bounded I/O

`LocalArtifactStore` defaults to:

- 16 MiB maximum plaintext JSON artifact size;
- bounded encrypted/stored size with limited envelope overhead; and
- 1 MiB maximum page size.

Both stored bytes and decoded plaintext are checked for exact size and SHA-256 on every
read. Missing committed bytes, symlinks/non-regular objects, truncation, tampering,
failed decryption, or invalid JSON raise `ARTIFACT_CORRUPT`; no path returns an empty or
partial value as if it were valid. An already-existing content address is verified
before it can be committed to the ledger.

Paged access is explicit:

```python
references = await harness.list_artifacts(run_id, context=trusted_context)
chunk = await harness.read_artifact(
    references[0].artifact_id,
    offset=0,
    limit=64 * 1024,
    context=trusted_context,
)
while chunk.next_offset is not None:
    chunk = await harness.read_artifact(
        chunk.artifact_id,
        offset=chunk.next_offset,
        limit=64 * 1024,
        context=trusted_context,
    )
```

Both harness methods reauthorize the current exact tenant/user owner; list results are
bounded to 1–1,000 records and byte pages cannot exceed the store's configured limit.

The current operation-result loader uses `load_json()` and therefore remains bounded by
the configured whole-artifact maximum. General binary/media streaming is an open T7
adapter, not silently encoded into model context.

## Model-context spill

Set `max_inline_output_chars` to opt registered lifecycle capabilities and first-party
tools invoked through `start()` into lossless spill before their result is returned to
the model:

```python
harness = AgentHarness(
    capabilities=[report_capability],
    runtime_store=runtime,
    artifact_store=artifacts,
    max_inline_output_chars=8_192,
)

run = await harness.start("Generate and inspect the report")
result = await run.wait()
```

Small results preserve their original Python value. A larger result has already passed
after-call policy/redaction and atomically settled to its authoritative
`operation_result` artifact before the model sees this bounded shape:

```json
{
  "type": "agnoclaw.spilled_output",
  "id": "artifact:v1:...",
  "artifact": {
    "artifact_id": "artifact:v1:...",
    "checksum": "sha256:...",
    "media_type": "application/json",
    "size_bytes": 5002
  },
  "rendered_chars": 5000,
  "preview": "bounded head … bounded tail",
  "read": {
    "tool": "read_spilled_output",
    "artifact_id": "artifact:v1:...",
    "offset": 0
  }
}
```

The harness automatically registers `read_spilled_output` as a built-in, run-lifetime,
read-only governed capability. It returns deterministic character pages with
`content`, `next_offset`, `complete`, `total_chars`, artifact ID, and checksum. A
requested oversized page is clamped to half the configured inline bound (and never
below 256 characters), so reading cannot immediately recreate the original context
spike. The reader result itself is not recursively spilled.

Access is authorized through the RuntimeStore, exact tenant/user owner, committed
capability-result metadata, and either the owning active run or the same trusted
session. This lets a later turn recover an earlier result after compaction without
letting another session reuse a leaked artifact ID. A constructor capability named
`read_spilled_output` conflicts with the reserved reader and fails closed.

Compaction recognizes these envelopes, classifies them as invariant
`artifact_reference` items, retains their stable item IDs, and adds a bounded recent
artifact/checksum/reader index to the live checkpoint. Full envelopes remain in the
searchable trajectory and selective rehydration retains their provenance. The index is
historical data, never executable authority.

This slice applies to specs supplied by the host, plugin, or pack through the unified
capability registry and to functions currently constructed by Agnoclaw's first-party
tool builder when invoked through lifecycle `start()`. Each first-party effect is
explicitly declared; policy/redaction executes before result settlement and exact
repeated call IDs replay without redispatch. Configured MCP calls are first-party tools;
factory-governed read-only provider queries join through registered capabilities. Raw
custom/plugin/pack/provider and caller-supplied MCP tools are rejected by lifecycle
admission; direct compatibility results and outer-model results remain outside it.

## Atomic staging and garbage collection

Local writes use a private staging directory, restrictive permissions, file `fsync`,
an atomic hard-link publication into a canonical object path, and directory `fsync`.
They never overwrite an existing content address.

External bytes cannot share a database transaction, so the crash contract is:

```text
stage immutable bytes -> commit authorized ledger reference -> eligible for reads
```

A crash before ledger commit can leave an unreferenced object or staging temporary.
`garbage_collect()` deletes only objects absent from the authoritative union of runtime,
learning-candidate, and context-manifest live keys and older than a caller-selected
grace period. Its scan and deletion count are bounded. Production automation must use a
grace period longer than the maximum in-flight settlement and replication delay.

Do not invent liveness by scanning events or terminal JSON. Runtime operation results
come from `RuntimeStore.list_artifact_storage_keys()`, learning content comes from the
authorized learning ledger, and archived context comes from each content-free
`ContextManifest.artifact_storage_keys` value. Omitting any authority can delete live
objects after the grace period.

## Encryption and revocation seam

Pass a host-owned `KeyProvider` to `LocalArtifactStore` to seal bodies with
`KeyPurpose.ARTIFACT`:

```python
artifacts = LocalArtifactStore(
    "/var/lib/my-agent/artifacts",
    key_provider=tenant_key_provider,
)
```

Encrypted artifacts require an authoritative tenant scope. Only opaque key ID/version,
algorithm, nonce, and AAD digest enter the ledger reference; raw key material and
ciphertext do not. Reads reconstruct the sealed envelope and call the provider's
`unseal()` method with deterministic authenticated scope metadata.

The `KeyProvider.destroy()` contract is the revocation seam. Transactional deletion
tombstones, key destruction orchestration, backup expiry, and propagation into
checkpoints/search/learning projections remain T10b/T14 release gates. Until those
land, do not claim verified legal deletion merely because a local object was removed.

## Ownership and deployment

An injected runtime or artifact store is caller-owned. Drain all workers before closing
or replacing it. The local implementation is for a single shared filesystem; a
multi-replica service needs an ArtifactStore adapter whose object consistency,
encryption, lifecycle, quotas, and disaster recovery pass the same conformance suite.
PostgreSQL stores metadata and authorization only—it does not place artifact bodies in
the database.

Back up the runtime ledger, artifact objects, and the matching key generations as one
recovery set. Restoring only one component converts successful settlements into typed
corruption.

`scripts/postgres_backup_restore_probe.py` deliberately verifies only the PostgreSQL
runtime ledger. Its successful exact manifest is necessary store evidence, but it is
not artifact/key-generation recovery proof and must never be reported as complete
service disaster-recovery certification.

## Current boundary

Implemented now:

- public `ArtifactStore`, `ArtifactReference`, `ArtifactScope`, and
  `LocalArtifactStore` contracts;
- authorized bounded `AgentHarness.list_artifacts()` and `read_artifact()` access;
- scoped deterministic addresses and exact authorization in both runtime stores;
- pre-dispatch canonical operation-result slots, staged JSON results, and atomic
  SQLite/PostgreSQL slot/artifact references with mismatched fulfillment rejection;
- process-restart result recovery without external redispatch;
- typed direct-child result collections that replace oversized synthesis values with
  exact `operation_result` links and allow scope-checked parent paging without exposing
  storage keys or protection metadata;
- opt-in bounded model envelopes and same-session paging for governed registered-
  capability and lifecycle first-party-tool results, with cross-session denial and
  `output.spilled` events;
- artifact-first manual context replacement, content-free context manifests/GC keys,
  deterministic artifact/failure invariants, scoped lexical trajectory search, and
  selective rehydration;
- integrity, size/page, encryption-seam, rollback, GC, and cross-owner tests.

Still required before the complete T7/T10/T14 claim:

- extend automatic spill to direct compatibility and raw/effectful provider/MCP tools,
  and outer model output;
- authorized deletion/tombstone APIs and richer download/media adapters;
- retention quotas, expiry watermarks, key-destruction and backup-expiry automation;
- remote object-store adapter plus retry/consistency/partition conformance;
- live-provider proof for automatic/reactive compaction and governed-output
  spill, plus universal output routing, remote context indexing, cross-process fencing,
  and repeated-drift certification;
- malware/content-type validation where deployment policy requires it; and
- long-run capacity, corruption-repair, and disaster-recovery certification.

## Errors

| Code | Meaning |
|---|---|
| `ARTIFACT_TOO_LARGE` | Plaintext or protected bytes exceed the configured bound. |
| `ARTIFACT_RANGE_INVALID` | Offset/page size is invalid or unbounded. |
| `ARTIFACT_NOT_FOUND` | Reference is absent, deleted, or invisible to the exact owner. |
| `ARTIFACT_CORRUPT` | Committed bytes, protection, checksum, size, or JSON are invalid. |
| `ARTIFACT_TENANT_REQUIRED` | An encrypted write lacks authoritative tenant scope. |
| `ARTIFACT_STORE_REQUIRED` | Artifact access was requested without an injected byte store. |
| `ARTIFACT_SCOPE_MISMATCH` | Staged scope differs from the authoritative run owner. |
| `ARTIFACT_SETTLEMENT_MISMATCH` | Result reference and successful operation settlement differ. |
| `ARTIFACT_IDEMPOTENCY_CONFLICT` | One address was presented with different immutable byte metadata. |
| `OUTPUT_SPILL_ARTIFACT_STORE_REQUIRED` | Spill was enabled without recoverable artifact bytes. |
| `OUTPUT_SPILL_LIMIT_INVALID` | A direct constructor bound is outside 1,024–1,000,000 or is not an integer. |
| `OUTPUT_SPILL_TOOL_CONFLICT` | A caller capability used the reserved paging name. |
| `OUTPUT_SPILL_REFERENCE_REQUIRED` | A governed result lacks an authoritative committed artifact. |
| `OUTPUT_SPILL_ACTIVE_RUN_REQUIRED` | Paging was called outside an executing lifecycle run. |
| `OUTPUT_SPILL_SCOPE_MISMATCH` | The reference is not a capability result owned by the active run/trusted session. |
| `OUTPUT_SPILL_RANGE_INVALID` | The requested character page is invalid. |
| `OUTPUT_SPILL_SERIALIZATION_INVALID` | A result cannot be deterministically rendered as finite JSON-like output. |

## Verification evidence

The spill/config/invariant contracts plus first-party-ingress and output-segment
contracts join the prior artifact/operation/recovery/lifecycle gate. The refreshed
primary suite passes 1,609 tests with 34 environment/service skips and 10 integration
deselections; the fresh isolated minimum-Agno suite passes 1,608 tests with 35
optional-environment skips and the same 10 deselections. A disposable
PostgreSQL 17 service passes all 33 current PostgreSQL-backed runtime/learning/matrix
cases (36 with the paired SQLite matrix cases), including atomic
provider-output commit and declared-child lineage/join/cancellation/spec-1.1 persistence.
The prior exact `wheel[postgres]` completed a real schema-v10
inspect/requeue/audit round trip.
These are transaction and local-integrity results; remote object-store chaos, legal
deletion, and production disaster recovery remain open release gates.
