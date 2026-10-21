# A domain pack in ordinary Python

Start with a model and a function. `Pack` registers the function and derives its
input/output contracts; `Model` supplies strict Pydantic defaults.

```python
from agnoclaw.domain import Model, Pack

pack = Pack("clarion")

class Metric(Model):
    amount_minor: int
    currency: str

@pack.procedure
def read_metric(text: str) -> Metric:
    return Metric.model_validate_json(text)

# Inside your async handler or test:
metric = await read_metric('{"amount_minor": 12000, "currency": "USD"}')
assert metric.amount_minor == 12000
```

That is enough for a callable, registered, validated procedure. Arguments and results
are checked strictly, including returned model instances. Its Python signature and
return type remain visible to editors and type checkers. The decorated value is a real
async function and supports normal Agno tool registration. Both synchronous and async
function bodies are supported; decorated calls always use `await`. Keep blocking work
off the event loop; a timeout cannot interrupt synchronous Python code.

The example parses JSON so it runs without credentials. Your function can instead call
your configured Agno agent or existing extraction service and return `Metric`. The
pack neither selects a model nor creates a second agent runtime.

Run the [complete short example](../examples/clarion_domain_pack/clarion_domain/simple.py):

```bash
uv run python examples/clarion_domain_pack/clarion_domain/simple.py
```

## Add a rule or a regression example

Declare these alongside the function, **before its first call**:

```python
@read_metric.check
def nonnegative(metric: Metric) -> bool:
    return metric.amount_minor >= 0

read_metric.example(
    '{"amount_minor": 12000, "currency": "USD"}',
    expected=Metric(amount_minor=12000, currency="USD"),
)

# In an async test:
await pack.test()
```

Checks receive the typed model. Failed, unknown, errored or timed-out checks prevent a
successful return. Use normal Pydantic `Field` constraints and validators for model
validation; use checks for additional domain rules. Async checks also work.
`pack.test()` runs examples through the same function, validation and checks, reports
failures by procedure/case, and raises on a failed case. Normal pytest tests calling
`await read_metric(...)` work too. Examples execute your function, so supply offline
implementations or controlled fixtures when it otherwise calls an external service.

Registration finishes automatically on first call, test, compile or host binding.
Add definitions and examples during module setup, then use the pack concurrently.

## Bind a Clarion request once

A host that needs object-bound verification receipts supplies its authenticated context,
existing run ID and selected object revision around the calls:

```python
async with pack.run(company_revision, context=context, run_id=run_id) as run:
    metric = await read_metric(source_text)
    receipt = run.last
```

Here `company_revision` is an existing `ObjectRef` or `ObjectSnapshot`. Supply
`evidence=...` once on `pack.run` when the request has source references. Inside an
active governed Agnoclaw tool, `pack.run(company_revision)` inherits the authoritative
context/run ID; conflicting explicit identities are rejected. Context is local to the
request, including concurrent tenant requests and nested scopes.

`run.results` contains immutable `PreparedWork` receipts for completed verification;
`run.last` is the most recent. A failing domain check leaves its receipt available for
inspection and raises. Validation failures and cancelled function calls do not create
success receipts. Hooks registered for that output run within this scope. A direct
call requiring those hooks is rejected before the function executes; test such hooks
with an explicit host fixture instead of an unscoped `.example()`.

An ordinary function call does **not** acquire automatic durability or permission to
write business data. Use [actions and reviewed execution](domain-authoring.md) for
effects and [pinned workflow plans](domain-durability.md) for supported replay. The
host still owns authorization, commits, evidence access and durable receipt storage.

## Grow the package when needed

Keep functions in normal modules and import `pack` where the host needs it. No directory
manifest is required for Python embedding. The trusted directory loader also accepts
a `register()` function returning this `Pack`.

- Compose packages with `Pack("app", includes=(shared_pack,))`.
- Keep an existing schema name with `pack.alias("builtin:company_metric", Metric)`.
- Add existing entity, relation, hook, skill or workflow definitions with `pack.add(...)`.
- Inspect the resolved catalog or compare versions with `pack.compile()`; procedures
  expose `.inputs`, `.output`, and `.definition` for explicit integrations.

Names default to `pack_name.function_name` and `pack_name.ModelName`. Contract versions
inherit `Pack(..., version="1")`. Usually you do not type IDs or version references at
call sites. Before distributing a pack, choose stable names and version its behavior:
`@pack.procedure(name="clarion.read_metric", version="2")` pins a procedure name across
a Python refactor. Model schemas inherit the pack version. Pin the installed package
build as well; a manifest is not a snapshot of arbitrary Python code.

The [package guide](domain-packages.md) covers explicit ontology, skills, workflows and
host adapters. The older `Domain`, `Procedure`, `Check`, `Schema` and `*Spec` APIs remain
available; function-first authoring compiles onto those existing contracts.
