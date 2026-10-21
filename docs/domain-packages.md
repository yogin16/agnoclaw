# Author and embed a domain package

Start with the [short domain quickstart](domain-quickstart.md) and
[function-based example](../examples/clarion_domain_pack/clarion_domain/simple.py).
This guide covers explicit contracts and host adapters as a package grows.

The 0.15 preview provides a Python-first package lifecycle: define, compile, inspect,
evaluate, bind host adapters, and operate an explicitly scoped instance. Start with the
[runnable Clarion-shaped package](../examples/clarion_domain_pack/clarion_domain/__init__.py).
It imports public APIs, runs offline, and includes an installable Python package,
skill resource, pack manifest and [host demonstration](../examples/clarion_domain_pack/demo.py).
The example's finance rules are illustrative; it does not migrate Clarion production.

## Run the example

From an agnoclaw checkout with its development environment installed:

```bash
uv sync --extra dev
uv run python examples/clarion_domain_pack/demo.py
```

The result reports successful output checks, fixture evaluation, a planned reaction,
watcher suppression and a resolved manifest digest. No credentials are needed. The
committed business event in this demo is simulated. The existing
[action example](../examples/domain_harness/ergonomic_demo.py) separately exercises
reviewed writes and replay against real SQLite business/runtime stores.

The package layout is ordinary Python:

```text
clarion_domain_pack/
  pyproject.toml
  agnoclaw-pack.toml
  clarion_domain/__init__.py
  skills/company-metric/SKILL.md
  demo.py
```

`pyproject.toml` declares the library dependency and includes skill bytes in the wheel.
The optional TOML manifest lets the existing trusted pack loader call `register()`.
`register()` returns a `Pack` or `Domain`; the loader compiles it into `LoadedPack.domains`.
Loading declarations does not install capabilities into a running agent. The embedding
host chooses which compiled capabilities to register and which extensions to activate.
Python imports and registration functions are trusted code. Inspection of an exported
JSON manifest, through `inspect_domain_manifest()`, imports no package code.

## Define each contract once

```python
from pydantic import BaseModel, ConfigDict
from agnoclaw.domain import Check, Domain, Procedure, Resources, Schema, SkillBinding

class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_minor: int
    currency: str
    period: str

metric = Schema("company-metric", Metric)
skill = SkillBinding(
    "company-metric", metric, resource="skills/company-metric/SKILL.md"
)

def nonnegative(work):
    return work.content["amount_minor"] >= 0

extract = Procedure(
    "extract-company-metric", metric, skill=skill,
    checks=(Check("metric.nonnegative", nonnegative),),
)
package = Domain(
    "clarion-metrics", (extract,),
    components=(Resources("skills", ("skills/company-metric/SKILL.md",)),),
    aliases={"builtin:company_metric": metric},
)
compiled = package.compile()
```

`Schema` is an authoring alias for `OutputSchema`; it retains the existing output
validation and serialization contract. Versions default to `"1"`, the first version
of that definition. Pass objects directly: the compiler discovers a procedure's schema,
checks, skill binding and capability dependencies. There is no second registration of
each of those objects. `compiled.use(extract)` retains the definition's Python type;
`compiled.procedure("extract-company-metric")` and `compiled.schema("builtin:company_metric")`
support lookup. An unversioned lookup succeeds only when it selects one version.

Aliases bridge names already persisted by a host. They do not rewrite stored schema
references. Clarion can retain its `builtin:` and `ontology:` formats and supply explicit
mappings. Canonical output metadata belongs on `Schema(..., metadata=...)`, not in
model-generated arguments. `SkillBinding.validate_skill(loaded_skill)` checks the
existing skill loader's name and allowed tools; `validate_output()` validates at the
host save boundary. Bind authorization and object-identifying arguments in the host.

## Compile, inspect and evolve

`package.validate()` returns all catalog diagnostics. `package.compile()` raises one
`DomainValidationError` carrying those same diagnostics if invalid. Errors identify
definition paths, conflicts, undeclared skill resources, missing skill bindings and
changed dependencies. These operations do not run checkers, evaluators, model calls,
capability factories or listeners. Custom definition/schema construction remains trusted
Python and can execute its own code.

```python
import json
from agnoclaw.domain import compare_schemas, inspect_domain_manifest

manifest = compiled.manifest()                 # detached JSON-compatible data
serialized = json.dumps(manifest)
inspect_domain_manifest(json.loads(serialized)) # references, pins, cycles, format

metric_v2 = metric.extend({"source_note": (str, "")}, version="2")
compatibility = compare_schemas(metric, metric_v2, direction="read_old")
```

An overlay uses normal Pydantic `(annotation, default/Field)` definitions. Existing
fields require `replace_fields=True`; a changed definition requires a new name/version.
Annotations must be resolved Python types. Tenant YAML should first pass through the
host's permitted field types; the SDK does not evaluate type strings. Pydantic custom
validators and business meanings require fixtures: `compare_schemas` deliberately
returns `unknown` where shape analysis cannot establish compatibility. `read_old`
means the new consumer reads old data; `write_old` means the old consumer reads new data.

Compose packages with `Domain(..., includes=(base,), components=(...))`. Identical
definitions deduplicate; conflicting meanings or callbacks under one reference fail.
An intentional replacement uses `Override(replacement, expected_digest, reason)`.
Consumers pinned to the old definition must be rebound explicitly. An override does
not silently upgrade an in-flight procedure. `new.compile().diff(old.compile())`
reports added, removed, changed and affected definitions, alias changes and package
version changes.

Manifest fingerprints describe declared content and implementation provenance. Python
callable source cannot be reproduced from a digest of metadata: pin the installed
package/build and change checker versions or implementation IDs when behavior changes.

## Model objects, relationships and conflicting evidence

`Entity("company", schema, label_fields=("name",))` connects a business kind to a
schema. `Relation("deal.company", deal, company, max_targets=1)` declares directed
meaning and cardinality. `relation.link(source_snapshot, target_snapshot)` validates
endpoint data and creates an `ObjectLink`. `relation.verify(link, neighbors=..., complete=...)`
returns shape/cardinality checks. A partial view produces `UNKNOWN` for an unproven
cardinality constraint. Hosts still check existence and enforce constraints in their
business transaction; a graph snapshot is not a lock.

`ObjectId` identifies a logical object, `ObjectRef` pins its revision, display fields
provide labels, and `OperationIdentity` describes request retries. `MatchPolicy` scores
candidate entities without merging them. Multiple plausible candidates require review;
comparator errors remain errors. `FieldClaim` retains source evidence and qualifiers.
`ResolutionPolicy` can choose a preferred source while retaining every competing claim;
conflicting top-priority values remain unresolved, and different periods/units are
incomparable. These are independent of `ChangeSet`'s structural three-way edit merge.

## Attach evaluations and decisions

`EvaluationSuite` uses the existing `EvaluationCase` and `evaluate_cases` runner. Attach
the suite to the package and list its subject definitions. `await compiled.evaluate()`
returns `PackEvaluation`, binding results to the exact manifest and an optional workspace
digest. `report.require_current(compiled, workspace_digest=...)` rejects stale evidence.
An empty suite cannot succeed, and errors/unknown results never count as passes.

Use existing `DecisionTable` rules for deterministic choices and `InferencePolicy` for
optional fast/deliberate/review hints. Tables preserve explicit no-match, conflict and
error outcomes. A domain fixture can evaluate those policies or call a host's model
adapter explicitly. This facade performs exact structured regression; advanced paired
model studies and custom graders remain in the existing improvement runners. There is
no implicit model call, automatic training or policy promotion.

`prepared.feedback(context=..., kind=FeedbackKind.CORRECTION, payload=...)` attributes
feedback to the exact prepared product and procedure automatically. The existing
`FeedbackEvent.regression_case()` can turn a host-curated correction into an
`EvaluationCase`; it does not enroll the case or promote a policy automatically.

## Bind calls and workflow plans

`Interface("validate-metric", inputs, outputs).bind(existing_capability)` checks the
capability's input schema and pins its implementation. `binding.execute()` delegates to
`CapabilityExecutor`, validating inputs and transforming/validating outputs before
settlement. Effects require an explicit host `pre_dispatch` guard. Use the existing
`Action` API when preparing checked business changes and scoped human reviews.
Before activation, `compiled.validate_host(registry).require_valid()` diagnoses missing
factories and absent/changed registry bindings without creating capability instances.

`Workflow("validate", (WorkflowStep("check", binding),))` composes sequential calls.
Adjacent output/input shapes must match. Create a plan using `workflow.plan(inputs,
tenant_id=..., request_id=...)`, persist it through the host, then pass it to
`workflow.execute(executor, plan, run_id=..., attempt_id=..., admission=...)`.
Supply per-step guards through `pre_dispatch={step_name: guard}` for effectful calls.
The adapter derives stable operation identities and uses existing ledger replay;
it rejects a plan whose workflow/implementation changed. Reopened-store replay requires
the runtime's persistent result/artifact configuration. The host owns plan persistence,
run lifecycle, scheduling, approvals and recovery of uncertain effects. This is a
sequential adapter, not certification of arbitrary Agno workflow durability or branching.
The interface and complete plan are included in persisted execution identity, so a
new plan under an existing request ID cannot silently change workflow semantics.
Resuming under the same run with a new worker attempt preserves existing steps' original
attempt attribution; it does not reclaim an in-flight step automatically.
See [durability and recovery](domain-durability.md) for plan serialization, current
Agno/Flue lessons and the host adoption gates.

## Activate scoped extensions

```python
async with compiled.extensions(tenant_id=context.tenant_id) as extensions:
    transformed = await extensions.apply(metric, values, context=context)
    prepared = await extract.prepare(
        transformed, target=current_object, run_id=run_id, context=context
    )
    prepared.require_ready()
```

`Hook` declares its schema, mode, implementation ID, ordering and timeout. Policy hooks
must return exactly `True`; transforms return data validated against the declared
schema. Transform before preparing checks/reviews. Hooks do not retroactively approve
already checked work. `Extensions.register()` returns an idempotent closeable handle.
The registry starts no background workers, has bounded registration/concurrency, applies
sequential backpressure, and belongs to one event loop. Use `aclose(cancel=False)` to
drain callbacks or `cancel=True` to cancel them; cancellation propagates.
Synchronous callbacks must be brief. Timeouts bound cooperative async work; they cannot
preempt arbitrary blocking Python. Hosts should move blocking I/O into an owned adapter.

`EventType.event()` creates a validated, scoped event with stable source/event IDs.
`Listener` receives `Delivery(event, identity)`. A durable listener failure propagates
to the caller for retry; `mode="observer"` records failure in `DeliveryResult` while
allowing delivery to proceed. A consumer must atomically combine its receipt and
business write, comparing intent on repeated keys. Handler execution is at least once;
registration does not create a receipt database.

Use `RuntimeListenerExporter(scope, convert)` with the existing `RuntimeOutboxWorker`.
`convert` is a host adapter selecting events and binding authenticated scope. Clarion's
business outbox can instead call `scope.publish(event)` directly and acknowledge only
after success. Runtime and business outboxes retain their respective transaction owners.
A synchronous post-hook is not proof of a committed business event.

`Trigger` validates an event contract and produces a `TriggerRequest` for a compatible
`Workflow` or `Watcher`. Repeated delivery retains its identity; changed intent under
that identity must conflict in the host store. Causation chains prevent the same trigger
from recursively invoking itself and enforce a maximum depth. A trigger does not start
a worker. `Watcher.run()` returns scoped findings, source health, suppression and error
counts, with one accounted outcome per assessment and a bounded finding count. Clarion
decides which findings become product insights and how often detectors run.

## Present interactions and pin workspaces

`Interaction` declares a response schema and an affordance such as a question,
comparison or review. `ChannelAdapter` exposes `ChannelCapabilities` and async
`present(request) -> ChannelReceipt`. `interaction.present()` negotiates support and
checks receipt identity; unsupported channels require an explicit alternate adapter.
`interaction.accept()` rejects changed subjects/revisions and expired requests before
validating responses. Hosts authenticate recipients/responders and deduplicate replies;
an interaction answer or delivery receipt does not authorize a business action.

`Resources.capture(provider, tenant_id=...)` reads explicitly declared paths and hashes
exact bytes. Providers can use `LocalResources` or implement `read(path) -> bytes` for
host-managed storage. The example includes a wheel resource provider. Snapshots are
immutable values: later source changes do not modify a captured snapshot. Overlays need
expected old hashes for replacements. `materialize()` stages content and publishes a
new directory without overwriting an existing snapshot. The host owns the parent path,
active-version pointer, session scratch, credentials, retention and cleanup. Keep the
old snapshot available for pinned runs; compare its digest before resuming.

## Clarion adoption boundary

Start by declaring existing schemas and skills, preserving stored references through
aliases, and running fixture suites against the compiled manifest. Bind actual Clarion
object reads, write capabilities, review queues, channels, workspace providers and
outbox callbacks at the host boundary. Use an opt-in path and compare behavior before
removing the old glue. These constructs do not change Clarion's dependency pin or flags.
The existing production pin and earlier agnoclaw APIs continue to work.
