# Personal and session learning administration

Status: v0.12 preview contract

Last verified: 2026-08-13 against Agno 2.6.4 and 2.9.0

agnoclaw provides a host-only read/replace/forget boundary for the three Agno stores
whose records have one deterministic owner key:

- `user_profile`;
- `user_memory`;
- `session_context`.

The API resolves identity from a trusted `ExecutionContext`, maps it to agnoclaw's
opaque storage keys, calls the underlying Agno database contract, and verifies the
result with a second read. It does not use Agno's high-level store CRUD methods for
administration because those methods intentionally catch backend exceptions and can
make an outage look like “not found” or “delete returned false.”

Institutional Entity Memory, Learned Knowledge, Decision Log, and harness-component
changes remain on the governed candidate path. They are not exposed through this direct
administrator.

## Small API

```python
from agnoclaw import LearningDataStore

record = await harness.read_learning_data(
    LearningDataStore.USER_PROFILE,
    context=trusted_context,
    learning_consent=True,
)

receipt = await harness.replace_learning_data(
    LearningDataStore.USER_PROFILE,
    {"name": "Ada", "preferred_name": "Ada"},
    context=trusted_context,
    operation_id="profile:user-request:2026-08-07",
    learning_consent=True,
)

forget_receipt = await harness.forget_learning_data(
    LearningDataStore.USER_PROFILE,
    context=trusted_context,
    operation_id="forget:user-request:2026-08-07",
    learning_consent=True,
)
```

The relevant store must be enabled by the immutable `LearningPolicy`. Personal stores
still enforce the policy's consent requirement. Session Context requires a stable
trusted session ID. Missing tenant/user/session/consent fails before the database call.

## Read result

`LearningDataRecord` contains:

| Field | Meaning |
|---|---|
| `schema_version` | Versioned serialization contract; currently `1.0`. |
| `store` | Exact `LearningDataStore`. |
| `scope_digest` | Content-free digest of store plus resolved tenant/opaque identity. |
| `present` | Whether the active database returned a record. |
| `content` | Deep-frozen schema content, or `None`. |
| `content_digest` | Canonical SHA-256 of present content, or `None`. |
| `observed_at` | UTC observation time. |

Raw tenant, user, and session identity is never accepted as a method argument and is not
returned in the receipt. Agno's stored schema necessarily contains its opaque
`agnoclaw:*:v1:<digest>` identity key; this is not the caller's raw ID.

An absent result means the direct database read returned no row. A database exception
raises retryable `LEARNING_ADMIN_BACKEND_FAILED` with no raw backend message; it is not
converted to absence.

## Replace semantics

Replace is whole-record, schema-validated administration. It is not a merge and it does
not ask a model to infer fields.

The gateway:

1. reads the exact current record;
2. rejects caller-supplied identity and audit fields;
3. injects the resolved opaque user/session identity;
4. parses with the configured Agno store schema;
5. enforces the built-in store shapes, including typed profile fields, unique
   non-empty memory entries, and string-only plan/progress lists;
6. rejects fields the schema silently drops;
7. enforces a 1 MiB canonical JSON limit;
8. writes through `upsert_learning` with exact identity columns;
9. reads the same identity again and compares the canonical content digest.

A method return alone is never treated as success. If the post-read is absent or has a
different digest, the operation raises retryable
`LEARNING_ADMIN_REPLACE_NOT_VERIFIED` and produces no success receipt.

Use the model-driven Learning Machine path for normal preference/memory extraction.
Use replace for explicit user/operator correction, import, recovery, or administrative
control where whole-record intent is clear.

## Forget semantics

Forget is idempotent active-store deletion:

1. read the exact record and retain only its digest in the receipt;
2. compute the deterministic Agno row ID from the already-opaque scoped identity;
3. call `delete_learning(id=...)`;
4. read the same exact scope again;
5. return a receipt only when the row is absent.

If the database reports `False` because the row was already absent, the post-read still
produces a successful idempotent receipt with `existed_before=False` and
`backend_delete_confirmed=False`. If a row remains visible—even when delete returned
`True`—the operation raises `LEARNING_ADMIN_FORGET_NOT_VERIFIED`.

`LearningMutationReceipt` records the operation ID, action, store, scope digest,
before/after content digests, backend delete confirmation, completion time, and its own
canonical digest. Its serialization also carries `schema_version = 1.0`.

Every current receipt has:

```text
verification_level = point_in_time
```

That means absence was verified in the active Agno database at the recorded time. It is
not proof that:

- another worker cannot recreate the row after verification;
- an in-flight model run was fenced across another process;
- replicas, caches, exports, snapshots, backups, or vector projections were purged;
- a legal retention workflow completed.

Applications must not present the receipt as a full deletion certificate. Service-wide
writer fencing, durable admin mutation idempotency/outbox, replica checks, backup expiry,
and retention-complete proof remain release gates.

## Concurrency and lifecycle

AgentHarness serializes concurrent admin mutations for the same resolved personal or
session key inside one process. Session Context administration also shares the exact
session lane with lifecycle `start()` runs in that harness. Reads are not serialized.

Named-legacy direct `run/arun`, another harness process, another service instance, or
an external writer is not fenced by this preview. Explicit-profile convenience calls
share the lifecycle session lane. Quiesce all other writers around a
privacy-critical forget. The final service contract must use a store-issued mutation
fence or equivalent database transaction boundary before it can return a stronger
verification level.

`replace_learning_data()` and `forget_learning_data()` reject mutations after harness
close. Reads remain allowed for inspection while caller-owned resources are live.

## Stable errors

| Code | Meaning |
|---|---|
| `LEARNING_ADMIN_STORE_DISABLED` | Requested direct store is absent from policy. |
| `LEARNING_ADMIN_STORE_UNAVAILABLE` | Agno failed to materialize the configured store. |
| `LEARNING_ADMIN_DATABASE_REQUIRED` | Store has no database administration boundary. |
| `LEARNING_ADMIN_CRUD_UNAVAILABLE` | Supported Agno CRUD method is missing. |
| `LEARNING_ADMIN_BACKEND_FAILED` | Database call raised; retry may be safe at the application boundary. |
| `LEARNING_ADMIN_RECORD_INVALID` | Database returned an unexpected record shape. |
| `LEARNING_ADMIN_SCHEMA_UNAVAILABLE` | Store has no verifiable schema parser. |
| `LEARNING_ADMIN_CONTENT_INVALID` | Content is empty or fails schema parsing. |
| `LEARNING_ADMIN_CONTENT_IDENTITY_FORBIDDEN` | Caller attempted to set identity/audit fields. |
| `LEARNING_ADMIN_CONTENT_FIELDS_UNSUPPORTED` | Schema would silently discard fields. |
| `LEARNING_ADMIN_CONTENT_TOO_LARGE` | Canonical replacement exceeds 1 MiB. |
| `LEARNING_ADMIN_REPLACE_NOT_VERIFIED` | Post-write content did not match. |
| `LEARNING_ADMIN_FORGET_NOT_VERIFIED` | Post-delete read still found content. |

The usual learning-scope and consent errors can occur before these database-specific
errors.

## Upstream compatibility

The centralized Agno compatibility report exposes `learning_admin_crud`. It probes:

- sync/async get/save/delete on User Profile, User Memory, and Session Context stores;
- sync/async `get_learning`, `upsert_learning`, and `delete_learning` on Agno databases.

The deterministic row IDs and method signatures were inspected on both production
lanes, Agno 2.6.4 and 2.9.0. The contract suite also performs an actual SQLite
replace/read/forget round trip through all three upstream stores on both lanes. CI
exercises the compatibility feature on both lanes. Any future ID/signature change must
update the central adapter and both-path tests; product code does not grow a silent
`hasattr` fallback.

See [Learning and self-improvement](learning.md) for store selection and
[Governed learning candidates](learning-candidates.md) for institutional writes,
promotion, rollback, and reconciliation.
