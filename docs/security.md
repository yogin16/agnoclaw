# Security foundation and threat model

Status: T10a contract plus registered-capability enforcement, durable exact-approval,
and P0 extension/egress/CI supply-chain hardening; broader integration continues in
T3-T13

Last reviewed: 2026-08-19

## Purpose and boundary

This document freezes the identity, authorization-evidence, data-classification,
encryption-key, and diagnostic contracts that durable runtime schemas depend on. It
does not claim that the current legacy runner already enforces every target boundary.
T1 contains the known identity/approval bypasses; T3 routes every run through the frozen
admission envelope; T5-T6 persist policy/effect records; and T10b completes safe-profile,
approval, sandbox, deletion, audit, and adversarial certification.

The foundation deliberately contains no authentication server, policy language, secret
vault, or cryptographic implementation. Hosts supply those systems behind small typed
interfaces. `agnoclaw` owns consistent resolution, provenance, non-weakenable defaults,
and enforcement points.

## One admission envelope

Every future execution source resolves exactly one immutable `AdmissionEnvelope` before
policy, storage, prompt construction, Agno invocation, or telemetry.

| Source | Can supply identifiers | Can supply roles/scopes | Trust |
|---|---|---|---|
| Authenticated protocol claims | yes | yes | authoritative |
| Host-created trusted context | yes | yes | authoritative |
| Parent runtime for a child run | inherited bounded subset | inherited bounded subset | authoritative |
| Direct caller argument | may fill absent local identity | no | non-authoritative |
| Request path/body | may confirm/fill an allowed absent field | no | non-authoritative |
| Client metadata | no authority | no | data only |

Any two non-empty values for the same canonical identity field must agree. A lower-trust
value never overrides a higher-trust value, and a conflict fails with
`IDENTITY_CLAIM_CONFLICT` without echoing either value. `service` requires tenant
identity from an authoritative source. Roles and scopes never come from ordinary
arguments, path/body, model text, tool arguments, or client metadata.

Canonical identity fields are tenant, org, team, user, session, and workspace. The
future coordinator appends run/attempt/operation identity; these are runtime-issued,
not caller authority. Roles/scopes are exact authority sets, not silently unioned across
disagreeing sources. Additional host grants use the separately bound approval/grant
contract rather than smuggling privilege through identity or metadata.

Declared children use a store-authoritative relation, not client metadata. They inherit
the exact tenant/user and full trusted context through `internal_parent`, receive a
distinct session, and may only reduce ancestor budget, capability, and learning grants.
Depth/fan-out bounds and unique parent/delegation identity are checked inside child
creation. Parent settlement events contain IDs/digests rather than task/result text.
Raw/default child tool surfaces fail before admission. Explicit profiles omit the
text-returning `spawn_subagent` tool and reject named raw subagents/Agno Team presets;
named legacy retains them without any durable representation. See
[Declared child runs](child-runs.md).

`AuthorizationGrant` binds tenant, principal, session and optional exact run; exact
capability IDs and manifest digests; effect categories; argument digest; policy and
authority digests; issuer and issuance time; expiry; and nonce. It is run- or
session-scoped and can never become a harness-global allowlist. The approval contract
introduced in runtime schema v7 remains part of current schema v12; schema v8 adds safe-coded
dead-letter quarantine, schema v10 adds exact-owner replay administration, and schema
v11 adds exact-owner child lineage with subset-only capability/budget/learning grants,
and v12 adds host-owned scheduler jobs/attempts without changing grant scope.
Registered capabilities enforce
grant replay/expiry. Session
scope is represented, but current live coordination consumes the
grant for the pending call rather than treating it as a reusable approval cache.

Admission metadata is deep-frozen JSON-like data. Live clients, callbacks, credentials,
models, stores, and arbitrary Python objects stay in trusted materializer registries and
cannot become durable admission data. Client and trusted metadata remain separate. A
full envelope digest supports evidence comparison. A separate authority digest excludes
request IDs, trace IDs, and client metadata, letting operation intent bind principal,
provenance, and trusted metadata without making harmless retry/telemetry churn an
idempotency conflict. Neither digest persists identity or metadata values. The explicit
capability executor requires this envelope in durable/service profiles and reauthorizes
the exact tenant/user run owner before capability resolution or intent creation.

## Data classification

Classification follows content wherever it is projected. An adapter may strengthen a
classification but cannot weaken these defaults silently.

| Class | Model access | Persistence | Telemetry |
|---|---|---|---|
| `public` | allowed | plaintext allowed | content allowed when exporter is enabled |
| `internal` | allowed | plaintext allowed | metadata only by default |
| `confidential` | explicit policy | encryption required | metadata only |
| `restricted` | denied by default | encryption required | forbidden |
| `credential` | denied; use a broker/handle | reference only, never plaintext | forbidden |

Business labels such as PII, financial, health, source code, legal, customer content,
and untrusted external content are `DataLabel.categories`; they do not replace the
sensitivity class. Tenant and retention policy travel with a label. Prompt, artifact,
learning, event, error, cache, trace, export, and deletion paths must consult the same
label rather than infer sensitivity from field names at the final exporter.

## Policy evidence

Runtime policy can still return the existing `PolicyDecision` to preserve compatibility.
Durable profiles persist only a safe `PolicyDecisionRecord` containing:

- decision ID and checkpoint;
- allow/deny/constrained/redacted action and stable reason code;
- policy version;
- digests of the evaluated input and principal;
- optional constraint digest and redaction count.

Raw prompts, tool arguments, policy messages, constraints, identities, and redacted
values are not copied into the decision record. Their access-controlled artifacts are
linked separately when retention policy permits. Re-evaluation uses current policy at
reconstruction/fork and never treats an old allow decision as a transferable grant.

Required checkpoints are admission, prompt send, capability discovery/load, model and
tool operation intent, effect dispatch/settlement, artifact access, learning
read/write/promotion, child spawn, and reconstruction/fork.

## Approval before effect

A registered-capability request that requires approval enters a durable control-plane
protocol:

```text
policy + effective arguments
  -> atomic waiting state + immutable approval request
  -> callback or trusted host decision
  -> exact grant
  -> lease renewal + grant reauthorization
  -> operation intent and external dispatch
```

The request contains digests rather than raw arguments and binds the authority that
created it. A decision binds its request digest and nonce. Database uniqueness,
revision CAS, owner checks, and the run's exact pending request prevent duplicate or
late settlement. A raw lifecycle response cannot grant authority. Leaving the wait
atomically records `approval.cancelled`; expiration records a terminal denial. Policy,
arguments, capability semantics, effect, authority, scope, or time drift at the final
boundary rejects dispatch before materialization.

The API that lists and decides requests accepts trusted `ExecutionContext` and must
remain outside model tools and self-improvement edit authority. Durable evidence allows
another process to decide a live request; it does not yet reconstruct a suspended
Agno/Python continuation after worker death.

The runtime request checkpoint is a different, earlier safe point. It captures the full
frozen authority context and canonical request before model dispatch, binds them to the
harness-spec and request digests, and is recoverable only under the exact durable owner
and session. It grants no new authority and cannot resume after external dispatch.
Because it contains request/context data rather than only digests, its ArtifactStore is
inside the application's encryption, access-control, retention, deletion, backup, and
restore boundary.

## Key-provider contract

`KeyProvider` is an opaque tenant-bound envelope-encryption seam:

- `seal` accepts plaintext plus tenant, purpose, and authenticated associated data;
- `unseal` requires the same associated data;
- `destroy` supports crypto-shredding where the configured retention contract allows;
- the runtime stores `KeyReference` and `SealedContent`, never raw key material.

Key purposes distinguish runtime content, artifacts, learning, and audit. Providers own
key generation, rotation, access control, audit, HSM/KMS integration, and deletion
assurance. T5 store adapters must bind tenant/object/version/classification in associated
data and prove ciphertext/metadata swapping fails.

## Safe diagnostics

Errors have two views:

1. the in-process `HarnessError`, which remains a compatibility object and may be
   available only to authorized local code; and
2. `SafeDiagnostic`, the only representation permitted in durable events, remote
   responses, telemetry, support bundles, and default logs.

Creating `SafeDiagnostic` requires an explicit safe message and explicitly supplied
safe details. It never copies an exception message or its details automatically.
Details use a small scalar allowlist; secret/token,
prompt/input/output, authorization, cookie, credential, arbitrary nested content, and
unknown fields are dropped. A random access-controlled debug reference may point an
authorized operator to raw evidence without embedding it in the diagnostic.

Sanitizing at an exporter is too late: data that entered the ledger, event, or exception
message has already leaked to downstream consumers. T3-T6 must construct the safe view
at the failure boundary.

## Telemetry and inspection boundary

The durable outbox is privileged evidence and can contain metadata that must not be
sent wholesale to a general telemetry backend. `RuntimeTelemetryBatchExporter` applies
a second strict boundary for this narrower purpose: registered event/enumeration fields
and bounded numeric measurements are allowlisted; IDs are domain-separated HMACs;
unknown event names collapse to one token; prompt/output/argument/target/metadata/
reason/error/artifact content is never copied. There is no content opt-in in telemetry
schema `1.0`, and HMACs never become metric labels.

Run inspection is separately authorized. It requires trusted `ExecutionContext`, a
non-empty user, `runtime:run:inspect`, and the exact tenant/user owner. Not-found and
wrong-owner are deliberately indistinguishable. Its output reports presence/count/
known-enum state only and cannot authorize recovery. The CLI accepts only environment
names for HMAC/DSN material and opens current-schema SQLite/PostgreSQL stores with
database-enforced read-only settings. Database RBAC, TLS, Collector policy, HMAC key
custody/rotation, retention, and regional routing remain deployment obligations. See
[Observability and safe run inspection](observability.md).

## MCP boundary

Configured MCP servers expose only bounded discovery and a schema-digest-bound call.
Remote descriptions, schemas, annotations, results, resource links, and errors are
untrusted input. MCP effect annotations never weaken the local effect class: lifecycle
calls are non-repeatable unless a future trusted server-policy registration proves a
narrower contract. Private result `_meta` is withheld before model/artifact projection,
and transport failures retain only server/tool identifiers and exception type.

Remote configuration applies HTTPS, host allow/deny, legacy-IP rejection, and
DNS-resolved private-address guardrails before client construction; Streamable HTTP
does not follow redirects. The MCP SDK transport does not currently expose an
exact-address pinned connection backend, so these admission checks do not replace
routing-aware egress containment. Service workers need an outbound proxy/firewall or
sandbox restricted to intended endpoints.
Stdio servers are locally executed supply-chain code and require provenance, sandbox,
filesystem, environment, and secret controls.

Static headers are a compatibility bridge, not an OAuth implementation. Interactive
OAuth, issuer/CIMD validation, token refresh, enterprise identity assertions,
extensions, and hostile-server certification remain T9/T10b gates. See [MCP client and
governed tool ingress](mcp.md).

## Outbound network boundary

`RuntimeGuardrails` is the shared URL admission policy. Network-enabled requests must
use HTTPS, match the configured host allow/deny lists, reject credentials in URLs,
reject localhost and private/reserved/link-local/multicast/unspecified addresses, and
reject ambiguous legacy IPv4 forms. Hostnames are resolved before admission; every
resolved address must be allowed.

The concrete containment varies by backend:

- `web_fetch` validates every redirect and connects to the exact admitted IP through
  the pinned HTTP transport;
- the default Playwright browser intercepts every request, including click-triggered
  navigation and redirects; custom browser backends receive the network policy and
  must enforce an equivalent boundary;
- ClawHub metadata, redirect, and archive requests use the same exact-address pinned
  transport with bounded redirects;
- search-provider SDKs receive the allowed-host configuration but may resolve and
  connect internally, so deploy an egress proxy/firewall when DNS rebinding or routing
  compromise is in scope; and
- configured MCP HTTP transport has the limitation described above.

The Bash tool is a host command runner, not a network sandbox. Network policy cannot
contain processes it launches. Use the Docker/VM sandbox profile or an external egress
control when executing untrusted code.

## Skill, pack, and workspace execution boundary

ClawHub content is downloaded into a dedicated community quarantine, validated under
strict archive limits, installed atomically, and recorded with immutable provenance.
Installation and inspection are parse-only. Community metadata is excluded from the
automatic model catalog, inline execution remains blocked, and restart discovery does
not elevate trust.

Packs cannot self-declare trust: Python registration entries always require host trust,
untrusted skill directories register as community content, and trust is stored outside
the payload against the exact canonical identity and digest. A content change revokes
that trust. Workspace command hooks are disabled by default, require an embedding-host
opt-in, run with `shell=False`, and receive a minimal explicitly allowlisted
environment. These controls reduce the execution surface but do not sandbox reviewed
local Python or hook programs.

## CI and dependency supply chain

Repository workflows default to read-only contents, grant OIDC only to the isolated
PyPI publish job, and grant tag-write permission only to the final tagging job. Every
GitHub Action is pinned to a reviewed full commit SHA; the `uv` executable is pinned;
release artifacts are transferred within one workflow run; and Dependabot tracks
GitHub Actions and Python dependency changes. CI exports the frozen all-extras lock and
runs `pip-audit` in strict, no-dependency mode. The current lock has zero known
vulnerabilities according to that audit.

Repository files cannot enforce hosted GitHub settings. Maintainers must separately
enable a protected `main` ruleset with required CI and code-owner reviews and protect
the `pypi` environment with required reviewers and a main-only deployment policy.

## Threat model

| Threat | Boundary | Required control and proof owner |
|---|---|---|
| Cross-tenant run/session/artifact/learning access | host identity → admission/store/cache | authoritative tenant binding, scoped keys/queries, negative tests; T3/T5/T8/T10b |
| Identity or scope substitution | body/path/metadata/model → authority | one admission resolver, conflict failure, hidden trusted bindings; the lifecycle HTTP edge rejects anonymous mode, requires AgentOS read/run scopes, strips claim-shaped metadata, and reauthorizes every run; T3/T10b |
| Lifecycle protocol confusion or cursor injection | remote client/proxy → run control | exact protocol/kind/run identity, strict fields and bounded bodies/pages/IDs, gap-free run-bound cursors, no redirects, safe errors; T4b/T10b |
| Prompt/tool-result injection | untrusted content → model decision/effect | label content untrusted, policy after selection and before dispatch, allowlists; T6/T9/T10b |
| Malicious skill, pack, package, hook, or MCP server | supply chain/network → runtime | community quarantine, archive bounds, content-bound host trust, hook opt-in/minimal environment, dependency/action pinning, schema limits, sandbox, auth scopes; T9/T10b |
| Secret exfiltration | environment/files/network → model/tool/telemetry | broker handles, credential classification, redaction, egress policy, audit; T9/T10b |
| Approval replay or confused deputy | human decision → operation | tenant/principal/run/arguments/policy/issuer/expiry/nonce binding and one settlement; T6/T10b |
| Unknown or duplicated external effect | worker/process → external system | intent before dispatch, idempotency/reconciliation, fencing, explicit unknown state; T5/T6 |
| Dual PostgreSQL writers after failover | control plane/endpoint → RuntimeStore | first-party etcd adapter pins cluster ID, brackets dedicated live-lease inspection with linearizable reads, derives a monotonic `mod_revision` fence, bounds/parses responses, rejects redirects/unsafe endpoints, and revalidates before commit; endpoint-bound credentials add bounded token exchange and one 401 refresh. An owned three-voter gate proves TLS 1.2 client/peer mTLS, exact-key RBAC, one-member survival, majority-loss denial, alternate-endpoint recovery, and fence advance. Controller-owned transfer, durable multi-AZ quorum/partition/rotation certification, and watchdog/STONITH for arbitrary clients and paused hosts remain deployment controls; T5b/T10b/T12 |
| Forged reconciliation verdict/evidence | observer/operator → operation ledger | exact owner/revision/digest, observer digest, scoped physical artifacts, CAS/fence, safe diagnostics; T6/T10b |
| Learning or harness poisoning | trajectory/content → future context/behavior | immutable evidence, scoped candidates, held-out/safety gates, rollback, evaluator isolation; T8/T10/T12 |
| Evaluator/reward tampering | proposer → verifier/model/config/budget/runs | read-only control plane, digests, independent/human audit; T10/T12 |
| Evaluation contamination or evidence leakage | shared subject state/raw case output → scores/artifacts | fresh owned resources, balanced order, exact scoped/encrypted artifacts, first-party empty-environment process boundary, or strict immutable/no-network Docker subject with resource policy and exact-owner cleanup; external VM/egress/retention policy where required; T10/T12 |
| Privileged replay/fork | persisted history → new execution | reauthorize current caller and current policy; new run/effect IDs; T4/T6/T10b |
| Resource exhaustion/noisy neighbor | model/tenant → workers/stores | bounded admission, budgets, fairness, backpressure, circuit breakers; T5/T11/T12 |
| Telemetry or support-bundle leakage | runtime → operators/exporters | registered-event allowlist, content-free HMAC projection, low-cardinality metrics, exact-owner/scope inspection, database read-only mode, `SafeDiagnostic`; support-bundle/production Collector certification remains T10b/T13 |
| Store tampering/corruption | storage/admin → recovery | checksums, transactions, AAD, backup/restore, corruption state; T5/T10b/T12 |

Trusted in-process Python is not sandboxed from the host process. Sandboxed local and
remote MCP capability classes must state the backend-specific containment guarantee.
No prompt instruction is described as a deterministic security boundary.

The strict Docker evaluation profile injects no host environment, network, or mounts;
uses an immutable exact-platform image with no declared volume; runs non-root on a
read-only root with bounded temporary storage; drops all capabilities; and enables
`no-new-privileges`, Docker's built-in seccomp profile, and CPU/memory/PID/file limits.
It verifies an exact owner label before forced cleanup. Treat the Docker daemon, host
kernel, and reviewed image as trusted infrastructure, prefer rootless Docker, and use a
separate credential broker/egress proxy or VM when provider access is required. It is
not a general untrusted-code or host-compromise boundary.

## Contract status and next gates

Implemented and tested in T10a:

- immutable source-classified identity assertions and conflict-safe resolution;
- authoritative service-tenant requirement and non-authoritative role/scope rejection;
- deep-frozen, JSON-like client/trusted admission metadata separation;
- complete classification-to-handling table;
- safe policy evidence, key-provider, sealed-content, and diagnostic types;
- sensitive/structured diagnostic dropping; and
- this threat/ownership matrix.

Not yet claimed:

- SDK `run/arun` and explicit/AgentHarness capability execution consume
  `AdmissionEnvelope`; registered model-callable specs additionally enforce current
  run ownership, scopes, async permission/policy, versioned decision evidence,
  pre-dispatch lease renewal, and pre-persistence result policy. Lifecycle first-party
  tools now add explicit effect declarations, policy-version evidence, lease renewal,
  and pre-persistence result policy at the same operation authority. Plugin/pack
  capabilities use the registered path. Configured MCP tools use bounded first-party
  discovery/call and conservative settlement. Explicitly attested read-only provider
  query factories now use the registered path and mark bounded answers untrusted;
  raw tools/providers and caller-supplied MCP fail lifecycle admission. T3/T9b still
  have to add effectful/provider-tool context, richer authenticated remote,
  administrative, and scheduled adapters;
- model/start, registered capability, and lifecycle first-party-tool effects persist,
  including policy evidence inside nested intent metadata. Registered-capability approval requests,
  decisions, exact grants, expiry, cancellation tombstones, and final-boundary
  reauthorization are also persisted. Lifecycle observer signals commit as bounded,
  content-minimized `trajectory.*` events before compatibility notification; standalone
  general policy-decision records, native/direct event ingress, destination adapters,
  independent dead-letter audit anchoring/operator UI, exporter isolation, and
  encryption remain T6/T10b work;
- safe-default profile migration, approval coverage for every legacy/elevated/remote
  ingress, process-restart approval continuation, complete backend-independent
  sandbox/egress containment, hosted GitHub/PyPI protection enforcement, deletion,
  audit export, and adversarial certification (T10b);
- live span correlation, exporter-health SLOs, production Collector/backpressure/key-
  rotation certification, remote diagnostic renderer, and support bundle remain T13.
  Content-free durable logs/metrics plus owner-authorized read-only run inspection are
  implemented; see [Observability](observability.md).

## Related decisions and research

- [ADR-0001: recovery and external-effect ownership](adr-0001-recovery-ownership.md)
- [Harness architecture](architecture.md)
- [Observability and safe run inspection](observability.md)
- [Policy and guardrails](embedding/policy-and-guardrails.md)
- [Lilian Weng harness/self-improvement audit](lilian-weng-harness-audit.md)
