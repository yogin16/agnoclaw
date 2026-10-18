# Domain harness SDK preview

The opt-in `agnoclaw.domain` package provides composable contracts for domain work.
It adds no dependencies, model calls, business database, workflow engine, or required
server. The existing capability executor owns execution; host services own business
authorization, persistence, and transactions. These APIs are a preview, not a stable
transport protocol or a certification of domain accuracy.

For the market evidence, industry differences and platform sequencing, see
[domain harness research and roadmap](domain-harness-research.md).

## Run the complete example

```bash
uv run python examples/domain_harness/demo.py
uv run pytest tests/test_domain_core.py tests/test_domain_workflows.py tests/test_domain_example.py
```

The example uses temporary SQLite databases and fixture content. It locates evidence,
assembles context, validates and persists an extraction, records a scoped human review,
commits a revision-checked mutation, and replays the settled operation. A second invoice
procedure uses integer-cent arithmetic. A curated correction becomes an offline
regression fixture. No provider calls, credentials, or Clarion services are involved.

The preview adds approximately 51 KB of source without eager top-level imports. Its
wheel budget is explicitly adjusted from 771,000 to 794,000 bytes; dependency, source,
sdist, import-time and import-count limits remain unchanged.

## Modules and ownership

| Module | Implemented preview | Host responsibility |
|---|---|---|
| `objects` | `ObjectRef`, immutable `ObjectSnapshot` | Load authorized objects, supply revisions, retain the business schema/ORM |
| `evidence` | `TextSource`, `EvidenceRef`, conservative quote location | Authorize/load source renditions; supply extractor versions; assess source authority |
| `outputs` | `OutputSpec`, immutable `WorkProduct`, `bind_output_capability`, `bind_output_schema` | Choose schemas and trusted metadata; persist/commit products |
| `verification` | Versioned, exact-subject checks and reports | Implement domain checkers and required-check policy |
| `actions` | `ActionSpec`, immutable proposals, pre-dispatch validation | Attach checks to execution, authenticate review lookup, enforce RBAC and atomic writes |
| `interaction` | Scoped, expiring requests and exact-proposal review decisions | Durable review queue, reviewer authentication, UI and cancellation/revocation |
| `procedures` | Typed skill/output/check/action declarations, `DomainPack` | Activate skills and enforce these declarations on the selected journey |
| `context` | Sequential contributors, expiry, required/optional handling, token budgets | Select/read sources; provide a tokenizer when exact provider accounting is needed |
| `feedback` | Corrections, preferences, outcomes, explicit regression conversion | Authenticate actors, persist events, curate corrections and learning candidates |
| `decisions` | Unique-hit deterministic rules; optional inference routing policy | Supply pure rules; select/run models; evaluate and approve routing policies |
| `evaluation` | Bounded, exact structured-result regression runner | Representative fixtures, domain graders, held-out quality/cost/latency evaluation |

Records export detached JSON via `to_dict()` and bind exact content via `digest`.
They are in-process trusted-host contracts. Constructing a record from client data is
not authentication. Load authoritative reviews and sources through application services.
Tenant matching is an additional consistency check; object-level RBAC remains with the
host. A local application can choose an explicit local tenant namespace.

## Typed work products

```python
from pydantic import BaseModel, ConfigDict
from agnoclaw.domain import ObjectRef, OutputSpec
from agnoclaw.runtime import ExecutionContext

class ExtractedFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revenue_usd: int

context = ExecutionContext.create(
    tenant_id="firm-1", user_id="analyst-1", session_id="session-1", workspace_id=None,
)
output = OutputSpec(
    "deal-fields", "1", ExtractedFields,
    metadata={"artifact_type": "deal_field_extraction"},
)
product = output.build(
    {"revenue_usd": 120},
    target=ObjectRef("firm-1", "deal", "deal-1", "revision-7"),
    run_id="run-1", context=context,
)
```

The model supplies content. Host code binds the target, run, schema, canonical metadata,
and authorized evidence references. Models must declare `extra="forbid"`; configure
nested models similarly when their extra fields must be rejected. Validation uses strict
Pydantic JSON semantics, including JSON representations of dates and UUIDs. Schema
digests detect structural drift; host versions must also identify validator/implementation
changes that do not change JSON Schema.

Aliases are retained in saved JSON. Input and serialization schemas must match;
models that exclude fields or use different serialization aliases are rejected at
registration. This keeps the advertised contract and the work-product shape aligned.
Custom validators and serializers remain trusted host code and need their own tests.

For hosts with an existing save service, `bind_output_schema(base_schema, output)`
returns a detached tool schema with its `content` argument narrowed. It preserves
other arguments and root definitions. Call `output.validate_content(content)` at
the save boundary as well: advertising a tool schema alone cannot validate a write.
The host can capture the schema from its own Agno `Function.parameters`, without
inspecting a private harness agent. This adapter does not assume storage ownership.

`bind_output_capability(base_spec, output, target=..., run_id=..., context=..., evidence=...)`
creates an ordinary `CapabilitySpec` whose model-visible argument is only `content`.
It preserves the base effect, recovery, concurrency and required-scope declarations.
The base run-owned factory returns a callable accepting one `WorkProduct`. Sync execution,
async results and resource cleanup retain executor ownership. Nested JSON Schema references
are relocated under the content property; unsupported assertions still fail in the
existing capability schema validator.

For `AgentHarness`, omit `run_id` and `context`: the adapter resolves both from the
public active tool context, including the run ID allocated by `start()`. Outside that
ingress, a missing context fails. Low-level executor integrations can bind both explicitly;
an explicit binding conflicting with an active run fails. Bind the intended target,
revision and sources rather than caching a work-item binding across unrelated work.
The writer must
commit before returning and implement the effect/recovery semantics it declares.
For idempotent storage, a product digest can be the host write key. Register the returned
spec with the existing harness/capability registry. This validates persisted work products;
it is separate from Agno's final assistant-response schema.

## Evidence and verification

Quote location collapses whitespace and expands known typographic ligatures, retaining
Unicode character offsets into the original text rendition. It abstains on multiple
matches, never guesses OCR digits, and will not select half an expanded ligature.
Evidence includes source identity/revision, rendition digest and extractor version.
Source documents and their text renditions are host-owned; a PDF extraction adapter can
retain page/bounding-box metadata outside this minimal text contract.

`check_quote()` checks location and integrity only. A valid quote is not proof that a
claim is supported, that a financial period matches, or that a source is authoritative.
Use separate domain checks for those questions. Checks return `pass`, `fail`, `unknown`
or `error`; failures in `run_check()` produce a content-free error result. Reports reject
mixed subject digests and duplicate check references. Required checks are explicit and
versioned; an empty set cannot accidentally pass.

## Reviewed actions and transaction boundaries

1. `ActionSpec.propose()` validates arguments against the existing capability schema.
   Its proposal binds target revision, capability implementation, output, and evidence.
2. Run checks against the **proposal digest**, not only the earlier output digest.
3. Create a `ReviewRequest` binding the proposal, report and target. `decide()` requires
   a host-authenticated user and the configured review scope.
4. Load the authoritative decision and current object, then run `ActionSpec.preflight()`
   **inside the registered business capability**, or through `CapabilityExecutor.execute`
   using its `pre_dispatch` callback, as the example demonstrates.
5. The business mutation must recheck the expected revision atomically with its write,
   enforce authorization and idempotency, and commit before returning.

The SDK cannot infer which capability argument identifies an object. Host adapters
must bind tenant, entity and expected-revision arguments from the selected `ObjectRef`
and reject conflicting model-supplied values. Checking a proposal's target alone does
not establish that arbitrary capability arguments address that target. Clarion's
existing trusted argument bindings remain the appropriate integration boundary.

`DomainPack.capabilities()` exposes declarations; it does not install review enforcement.
Register only host implementations that enforce the selected action contract. A
`ProcedureSpec` is also a declaration, not an automatically scheduled workflow.
The demo uses the public low-level executor with explicit admission. Applications using
`AgentHarness` retain its policy/permission layer and attach domain checks in their
registered capability implementations; this SDK does not replace that layer.

A successful inference, passing report, review record, and committed business decision
are distinct events. Review records must come from a trusted host queue, not model/tool
arguments. A changed proposal, report, capability, revision, or expired review fails
preflight. Host revision checks close the race between preflight and mutation. Local
database receipts demonstrate one transaction boundary; they do not prove exactly-once
external writes or distributed transactions. Uncertain external effects still use the
runtime's reconciliation mechanism.

## Decision models and System 1 / System 2

`DecisionTable` provides host-authored Python predicates with unique-hit semantics.
Zero matches abstain, overlapping matches conflict, predicate errors produce `error`.
Inputs and implementation identity are recorded. No expression strings are evaluated.
Rules must be pure; this is not a sandbox or a DMN/FEEL interpreter.

`InferencePolicy` is an optional routing utility. "Fast" means a domain-evaluated,
lower-latency inference route, not a hard-coded model family or provider capability.
It defaults to deliberate inference. Enabling the fast route is an explicit host choice
after evaluation. Failed/missing checks or ambiguous evidence escalate; unresolved work
after deliberate inference requests review. Passing checks can complete inference while
domain policy still requires human review. Reports must identify the current subject.

The host supplies attempted tiers, selects existing `AgnoModelFactory` instances and
runs the chosen tier. There are no implicit model calls, extra provider dependencies,
automatic mid-run model swaps, or new authorization bypasses. Self-reported model
confidence is not an acceptance input. The utility does not itself enforce token/dollar
budgets or prove that a fast route is cheaper; measure those in domain evaluations.

ML scoring models, external DMN engines, optimization solvers and calibrated classifiers
are candidates for later adapters. Add an adapter when a real domain needs it and its
versioning, uncertainty and evaluation requirements are demonstrated. Clarion's immutable
business Decisions should remain in Clarion; these recommendations do not replace them.

A learned classifier can initially be a read-only registered tool capability: the host
loads a pinned model/feature schema, returns a typed prediction, and keeps policy and
mutation separate. Document triage, candidate ranking and anomaly detection are plausible
uses. Prediction calibration and distribution drift need domain evaluation; a numeric
score is not permission. This is an extension pattern, not a bundled trained model.
Sequential learned policies such as Decision Transformers would additionally need task
trajectories, an explicit reward/outcome definition and held-out policy evaluation before
an adapter is justified. They are distinct from deterministic business decision tables.

## Context, feedback and packs

`assemble_context()` loads contributors sequentially, supporting hosts that share one
database session. Required sources precede optional sources, whose supplied order is
their priority. The token budget includes the rendered JSON envelope; optional omissions
report `missing`, `error`, `stale`, or `budget`. Required omissions fail. Cross-tenant
sources fail even when optional. Coroutine cancellation propagates. Contributor timeouts
require cooperative async implementations; blocking work belongs in host-owned workers.
JSON encoding is a transport representation, not prompt-injection isolation. Insert these
sources as untrusted context and retain existing runtime tool/policy controls.

`FeedbackEvent.capture()` attributes feedback to an exact work product, procedure digest
and authenticated actor. `regression_case()` is an explicit host action for curated
corrections. Preferences and business outcomes cannot silently become corrected truth.
The host persists records, applies retention policy, and can stage reviewed evidence into
the existing learning-candidate machinery. This preview neither trains nor promotes
shared knowledge automatically.

Existing pack manifests support trusted Python domain registrations:

```toml
[provides]
domains = ["my_domain:register"]
```

The factory returns a `DomainPack` or list of packs. `inspect_pack()` remains read-only;
`load_pack()` requires either explicit per-call host trust (`trusted=True`) or a
stored trust record matching the installed pack's exact content before executing
registration code. Explicit per-call trust does not perform that recorded-content
check. Read packs through `loaded.domains`. Loading does not register actions, choose
models, execute procedures, or change host policy automatically. The example manifest
and two procedures are under `examples/domain_harness/`.

## Evidence and next iterations

The contract tests cover immutable content, strict output validation, nested schemas,
OCR locations, source/version mismatches, wrong-subject checks, scoped/expired/stale
reviews, capability drift, pre-dispatch rejection, idempotent runtime replay, context
budgets/timeouts/cancellation, rule abstention, bounded routing, feedback and pack trust.
The runnable example uses real SQLite business and runtime authorities. These are offline
contract proofs, not real-model quality, production recovery, or Clarion adoption claims.

Release verification uses the locked Agno 3.0.6 / Python 3.14.1 offline suite, Ruff,
mypy, generated API checks, wheel/sdist builds, installed-wheel domain imports and
package/import budgets. The release PR records the final results. The original local
Agno 2.6.4 environment passed focused domain tests but lacked declared development
dependencies for a broad run; it is not the full-suite release environment.

| Iteration | Status | Next acceptance gate |
|---|---|---|
| Objects/evidence/outputs/checks | Implemented preview | Evaluate representative Clarion OCR and extraction fixtures, including period/currency confusion |
| Actions/reviews/procedures | Implemented preview | Integrate one real Clarion capability with authoritative review lookup and transaction checks |
| Context/feedback/packs | Implemented preview | Replace one persona contributor and one feedback path without changing host ownership |
| Decisions/inference routing | Rules and routing policy implemented; model execution remains host-owned | Compare deliberate-only against fast-then-escalate on held-out domain data and review/cost/latency metrics |
| Clarion migration | Opt-in evidence/review and typed artifact-input adapters implemented in the Clarion checkout; production pin remains 0.11.0 | Broaden source coverage, certify a supported runtime version pair, then integrate action preflight |
| Stable SDK | Planned | Independent adopter, transport/storage adapters where needed, compatibility policy and measured DX |
| Hosted platform | Deferred | Repeated operational demand from independent adopters |

Clarion remains a design partner, not a dependency. Its canonical skill/output registry,
save-tool schema adaptation, OCR gate bypass, versioned sheet proposals and schema-driven
presentation motivated these contracts. Its models, authorization, KnowledgeService,
review UI, and business transactions remain application-owned. A subsequent local
Clarion integration now uses this SDK for stored-document evidence, exact-proposal
verification metadata and mandatory human review. Four real Postgres tests cover
source selection, no generated-artifact self-citation, host commit/rollback and
human approval. A separate typed-output flag shares strict dilution/exit-waterfall
input schemas between tool advertisement and the canonical save service. Its tool
schema comes from host-owned public definitions; canonical schema references and
source links stay with Clarion. Clarion's `docs/domain-evidence-preview.md` describes its flags,
source override and remaining gates. Its production settings and 0.11.0 pin are
unchanged; this evidence adapter does not certify a complete runtime migration.

## Research ledger — 2026-10-18

- **Evidence:** W3C's [Web Annotation selectors](https://www.w3.org/TR/selectors-states/)
  describe quote and position selectors and the brittleness of positions when sources
  change. We combine original positions with source/rendition identity. This is an
  inspired internal contract, not a claim of W3C serialization conformance.
- **Decisions:** OMG's [Decision Model and Notation](https://www.omg.org/dmn/) separates
  business decisions and rules from surrounding processes. We adopt explicit inputs,
  implementation versions, unique-hit rules and abstention; full DMN support is deferred.
- **Reasoning:** [Agno reasoning documentation](https://docs.agno.com/reasoning/overview)
  distinguishes native reasoning, explicit tools and separate reasoning stages. Existing
  Agno/model-factory integration remains the execution layer; domain routing adds policy.
- **Routing:** Anthropic's [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
  discusses task routing and simpler composable patterns. Our design inference is to
  expose optional, evaluated tiers without adding a second agent loop.
- **Context:** Anthropic's [context engineering guidance](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
  treats context as a finite resource. We reuse agnoclaw token accounting and expose
  source selection, expiry and omissions instead of hiding unbounded prompt concatenation.
- **Evaluation:** Anthropic's [agent evaluation guidance](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
  distinguishes outcomes from execution traces. Contract fixtures establish mechanics;
  real domain success and model-routing benefits need separately measured outcomes.
- **Learned predictors:** [ONNX Runtime's Python examples](https://onnxruntime.ai/docs/get-started/with-python.html)
  show inference for exported neural and traditional ML models. Our design inference
  is to use an optional host capability first, rather than add a mandatory ML runtime.
- **Learned policies:** [Decision Transformer](https://arxiv.org/abs/2106.01345) models
  sequential decision making through trajectory sequences. This is adjacent research,
  not evidence that Clarion needs reinforcement learning or a new default agent loop.
