# Building a domain harness

The opt-in `agnoclaw.domain` authoring API composes the contracts introduced in 0.14.
Use `OutputSchema`, `Check`, `Procedure`, `Domain`, and `Action` for new integrations.
Existing `OutputSpec`, `ProcedureSpec`, `ActionSpec`, and `DomainPack` constructors and
serialized records remain supported. This is still a preview API.

## Start with one checked output

```python
from pydantic import BaseModel, ConfigDict, Field
from agnoclaw.domain import Check, Domain, OutputSchema, Procedure, WorkProduct

class Invoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subtotal_cents: int = Field(ge=0)
    tax_cents: int = Field(ge=0)
    total_cents: int = Field(ge=0)

def amounts_balance(product: WorkProduct) -> bool:
    value = product.content
    return value["subtotal_cents"] + value["tax_cents"] == value["total_cents"]

invoice = Procedure(
    "reconcile-invoice",
    OutputSchema("invoice-result", Invoice),
    checks=(Check("invoice.arithmetic", amounts_balance),),
)
accounting = Domain("accounting", (invoice,))
```

From an async host handler, supply the authenticated context, selected object revision,
and run ID:

```python
from agnoclaw.domain import ObjectId

prepared = await invoice.prepare(
    {"subtotal_cents": 1000, "tax_cents": 80, "total_cents": 1080},
    target=ObjectId(context.tenant_id, "invoice", "inv-42").at("revision-7"),
    run_id=run_id,
    context=context,
)
product = prepared.require_ready()
```

`prepare()` validates the schema, constructs an immutable work product, runs the actual
checks, and binds their results to that exact product. `require_ready()` rejects failed,
unknown, or errored checks. It establishes completion of these checks; business writes
still need an authorized action. Arithmetic alone does not validate invoice legitimacy,
tax rules, matching purchase orders, or source accuracy.

Copy [the starter definitions](../examples/domain_harness/authoring.py), or run the full
offline example with real SQLite business and runtime stores:

```bash
uv run python examples/domain_harness/ergonomic_demo.py
uv run pytest tests/test_domain_authoring.py tests/test_domain_identity_changes.py tests/test_domain_example.py
```

## What does `invoice.arithmetic@1` mean?

It means the check named `invoice.arithmetic`, contract version `1`. The version is
chosen by its developer. It is independent of the agnoclaw release, model version,
invoice revision, and number of executions. New definitions default to version `"1"`.
Pass `version="2"` when you change a check's meaning or a schema's contract.

Previously, authors declared a required string and separately constructed a matching
`CheckResult`. Now a `Check` owns the callable and its reference, and `Check.run()` binds
the subject digest. Procedures accept check objects and derive their required references.
Logs and stored reports still show explicit versions for reproducibility:

```python
arithmetic = invoice.checks[0]
assert str(arithmetic.ref) == "invoice.arithmetic@1"
assert accounting.procedure(invoice.ref) is invoice
print(accounting.describe())  # JSON-compatible inspection; no execution
```

An unversioned name resolves only when there is one matching procedure. With multiple
versions, use its `ContractRef` or an explicit `name@version` string; there is no implicit
"latest" selection. `ContractRef` rejects whitespace and the reserved `@` delimiter
inside a name or version.

Check callbacks return `bool` or `CheckOutcome(status, reason, evidence_digests=...)`.
Use `UNKNOWN` for insufficient evidence. Arbitrary truthy objects are errors, and
exceptions become sanitized `ERROR` results. Async checks have a cooperative timeout;
external cancellation propagates. Sync callbacks must be short and pure: their CPU
work cannot be interrupted by an async timeout. Checks run sequentially so host adapters
can share a database session safely. Optional `implementation_id` records build
provenance, but does not replace a semantic version bump. Python code is not
automatically fingerprinted or sandboxed.

## Names distinguish definitions from results

| Authoring object | Purpose | Result or existing contract |
|---|---|---|
| `OutputSchema` | Pydantic shape and strict validation | `WorkProduct`; compatible with `OutputSpec` |
| `Check` | Versioned checker implementation | `CheckResult`, `VerificationReport` |
| `Procedure` | Output definition plus executable checks | `PreparedWork`; `.spec` exports `ProcedureSpec` |
| `Action` | Governed capability plus action checks | `PreparedAction`, containing an `ActionProposal`; `.spec` exports `ActionSpec` |
| `Domain` | Conflict-checked collection of procedures | `.pack` exports an existing `DomainPack` |

`Domain.describe()` exposes declarations and registered checks without calling them.
Identical procedure declarations are deduplicated. Reusing a reference for conflicting
outputs, procedures, or checker implementations raises `DOMAIN_DEFINITION_CONFLICT`.
Within one composition, the same check reference must use the same callable and the
same output reference must use the same model class; schema equality alone cannot
establish equivalence of custom validators.

Existing pack registration functions can return `accounting.pack`. That exports
declarations; it does not install callable checks, register mutations, or start a
workflow. Keep the executable `Domain` in the host that calls its procedures. Agno
continues to own model execution. There is no additional agent loop or workflow engine.

## Object identity, versions, and deduplication

| Identity | Example | What it identifies |
|---|---|---|
| `ObjectId` | tenant `acme`, kind `invoice`, ID `inv-42` | One stable host-owned entity |
| `ObjectRef` | that ID at `revision-7` | One revision of the entity |
| `ContractRef` | `invoice.arithmetic@1` | One version of a definition |
| Record `digest` | `sha256:...` | Exact serialized content, including its bindings |
| Host request ID | a UUID retained across retries | One intended business operation |
| `OperationIdentity.key` | hash of tenant, namespace, request ID | Lookup key for the operation's receipt |
| Runtime operation/attempt IDs | host runtime identifiers | Execution and recovery history |

Use business primary keys or stable IDs from the authoritative source. If separate
systems both call something an invoice, namespace `kind` appropriately or map their IDs
in the host. Similar names or identical content do not establish that two records are
the same entity. The library does not perform fuzzy entity resolution.

`ObjectId.at(revision)` creates a reference; `ObjectRef.identity` recovers the stable ID.
Revisions are opaque host values, not automatically incremented counters. Existing
`ObjectRef` fields and serialized digests are unchanged.

`OperationIdentity(tenant_id, namespace, request_id, intent_digest)` classifies a lookup:

| Existing receipt | `compare(existing)` |
|---|---|
| None | `NEW` |
| Same key and intent | `DUPLICATE` |
| Same key, different intent | `CONFLICT` |
| Different key | `DOMAIN_DEDUPE_SCOPE_MISMATCH` |

The key deliberately excludes the intent digest: changing the payload under the same
request must expose a conflict, rather than silently create another operation. A host
must atomically claim/store that key with its business mutation and receipt; an in-memory
comparison cannot reserve a key or guarantee exactly-once external effects. Retention,
receipt lookup, transaction isolation, and reconciliation remain host policies.

## Links and evidence

```python
from agnoclaw.domain import ObjectLink

link = ObjectLink(document_revision, "supports", invoice_revision)
```

Links are directed and bind exact source and target revisions. `link.key` identifies the
logical edge across revisions; `link.digest` includes endpoint revisions and metadata.
Both endpoints must belong to one tenant. Hosts own relation names, cardinality, storage,
and authorization. This gives a stable key for a relationship without hiding changes to
its contents. Multiple events between the same objects need their own event IDs; do not
deduplicate them solely by their edge key.

A link asserts a relationship. It does not prove a claim, grant access, or locate a
quote. Use `EvidenceRef` and the source/quote checks for supporting passages, plus domain
checks for meaning. A document can be linked to an invoice while its cited passage fails
to support a particular amount.

## Concurrent edits

`ChangeSet(base_snapshot, proposed_document).rebase(current_snapshot)` performs a
three-way comparison of complete JSON objects:

- Disjoint nested edits combine; identical concurrent edits deduplicate.
- Overlapping edits produce `ChangeConflict` records with JSON pointers and all three
  values. The preview keeps the current value until explicitly resolved.
- Missing keys differ from explicit `null`. Deletion versus modification conflicts.
- Arrays and scalars are atomic; array ordering and entity matching are domain policy.
- Another object/tenant is rejected. Different contents under the same revision are
  rejected as `DOMAIN_REVISION_CONFLICT`.

Use `resolution.clean` for UI branching and `resolution.require_clean()` before taking
the merged document forward. A clean merge is only a candidate: revalidate, rerun checks,
and request fresh review against `resolution.current_target`. The host must still compare
that expected revision atomically with its write. The helper neither persists data nor
reuses an approval issued for an older proposal.

## Reviewed dispatch with fewer manual bindings

Create an `Action(capability, checks=(Check(...),))`, then call
`await action.prepare(product, arguments, context=context)`. This checks the exact
proposal. `prepared.request_review(...)` creates the existing scoped, expiring
`ReviewRequest`; `prepared.review_key` is a queue lookup key, never an authorization token.

After the host has stored an authenticated decision:

```python
result = await action.execute(
    executor, pending,
    run_id=run_id, attempt_id=attempt_id, operation_id=operation_id,
    admission=admission,
    load_current=business_service.current_reference,  # ObjectId -> ObjectRef
    load_review=review_service.for_prepared_action,   # PreparedAction -> decision/None
    idempotency_key=pending.operation_identity(request_id).key,
)
```

Both loaders may be sync or async. Dispatch derives its context from the same admission
as the existing executor, uses only the prepared arguments, pins the actual resolved
capability digest, and checks the current object and authoritative review before a new
side effect. Policy or implementation drift requires preparation and review again.
Settled runtime replay retains the executor's existing replay semantics.

The host must bind object-identifying arguments before preparation and enforce object
access, expected revision, and idempotency in its committed mutation. A runtime
`idempotency_key` does not automatically propagate into business service arguments or
create a business receipt. Keep the exact prepared record across retries; regenerating
a product with another run ID changes the reviewed intent. Persist review/proposal
records under host authority; constructing these trusted in-process records from
untrusted client JSON does not authenticate them.

For check failures, inspect `prepared.report.results`. Common errors are
`DOMAIN_STALE_OBJECT`, `DOMAIN_ACTION_POLICY_CHANGED`, `CAPABILITY_DIGEST_MISMATCH`,
`DOMAIN_IDEMPOTENCY_CONFLICT`, and `DOMAIN_CHANGE_CONFLICT`. Respond by reloading,
repreparing, or explicitly resolving the conflict; do not blindly retry a changed intent.

## Clarion adoption and next gates

Clarion's existing default-off evidence and typed-output adapters can keep their 0.14
`OutputSpec` calls. `OutputSchema` accepts the same model and retains the same output
fingerprint. New extraction journeys can replace manual report assembly with a
`Procedure`, attach stable source/deal references through `ObjectId` and `ObjectLink`,
and render `ChangeConflict` records in their proposal UI. `Action` can remove repeated
proposal/review/pre-dispatch glue once its authoritative loaders and mutation are wired.

Clarion keeps its canonical schema registry, source permissions, review UI, transaction
service, and business receipts. This iteration does not change its production 0.11.0
pin or certify a complete runtime upgrade. Adoption should proceed through:

1. Run existing adapter contracts against this additive API, then migrate one default-off
   procedure with representative extraction fixtures.
2. Integrate a real mutation with authenticated review lookup and atomic revision/receipt
   checks, including stale edits, retries, and implementation changes.
3. Measure a second independent domain's setup effort before adding reusable storage/UI
   adapters or freezing the public authoring API.

Deterministic decisions and evaluated fast-model routing remain optional existing
modules. Connect them as host-owned checks/capabilities only when measured accuracy,
abstention, latency, and cost justify the route. This ergonomics layer introduces no
model calls, autonomous promotion, or new learned decision runtime.

## Design sources

This design separates occurrence identity from content and business entity identity,
informed by the [CloudEvents identity rules](https://github.com/cloudevents/spec/blob/main/cloudevents/spec.md).
Its reused-request conflict rule follows the pattern documented by
[Stripe idempotency](https://docs.stripe.com/api/idempotent_requests).
Expected-revision writes follow the lost-update prevention principle in
[HTTP If-Match](https://www.rfc-editor.org/rfc/rfc9110.html#name-if-match).
These are design influences, not claims of protocol conformance, a graph database,
distributed exactly-once delivery, or a general CRDT implementation.
