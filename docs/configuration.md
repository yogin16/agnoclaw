# Configuration reference

Use explicit Python values for embedded applications and TOML/environment settings for
CLI or operator-managed deployments.

## Precedence

The effective order is:

1. explicit `HarnessConfig(...)` values supplied by Python code;
2. environment variables when using `get_config()`;
3. project `.agnoclaw.toml` in the current working directory;
4. user `~/.agnoclaw/config.toml`;
5. code defaults.

Only environment fields that are actually present override TOML. An environment value
for one nested setting does not reset unrelated nested values to defaults.

`get_config()` is process-cached. Tests or applications that intentionally change the
environment or working directory after first load must call `get_config.cache_clear()`
before loading again.

## Start from the annotated template

```bash
cp .agnoclaw.toml.example .agnoclaw.toml
```

Minimal project configuration:

```toml
default_provider = "anthropic"
default_model = "claude-sonnet-4-6"
profile = "legacy" # 0.12 preview compatibility default; choose explicitly for new code
workspace_dir = "~/.agnoclaw/workspace"
permission_mode = "default"
permission_require_approver = true
permission_durable_approvals = true
permission_approval_ttl_seconds = 900

[storage]
backend = "sqlite"
sqlite_path = "~/.agnoclaw/sessions.db"

[heartbeat]
enabled = false
interval_minutes = 30
```

## Environment names

Root fields use the `AGNOCLAW_` prefix. Nested root fields use a double underscore:

```bash
AGNOCLAW_DEFAULT_PROVIDER=anthropic
AGNOCLAW_DEFAULT_MODEL=claude-sonnet-4-6
AGNOCLAW_PROFILE=durable
AGNOCLAW_PERMISSION_MODE=default
AGNOCLAW_PERMISSION_REQUIRE_APPROVER=true
AGNOCLAW_PERMISSION_DURABLE_APPROVALS=true
AGNOCLAW_PERMISSION_APPROVAL_TTL_SECONDS=900
AGNOCLAW_PERMISSION_APPROVAL_POLL_INTERVAL_SECONDS=0.25
AGNOCLAW_STORAGE__BACKEND=postgres
AGNOCLAW_STORAGE__POSTGRES_URL=postgresql://user:pass@host/database
AGNOCLAW_HEARTBEAT__ENABLED=true
AGNOCLAW_HEARTBEAT__INTERVAL_MINUTES=30
```

The existing child-setting aliases `AGNOCLAW_STORAGE_*` and `AGNOCLAW_HB_*` are also
accepted. Prefer the nested root form in new deployments so the relationship to the
TOML sections is obvious. When both forms set the same field, the nested root form is
authoritative.

Provider credentials such as `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and
`GOOGLE_API_KEY` are consumed by their provider SDKs rather than by `HarnessConfig`.

## Runtime profiles

Profiles select runtime semantics; `session_id` independently selects continuity.
The 0.12 preview keeps `legacy` as the no-argument compatibility default so an upgrade
does not silently change permissions or persistence. New code should select one of the
three target profiles explicitly:

| Profile | Preset | Additional construction contract |
|---|---|---|
| `quick` | In-memory Agno session DB, learning/context automation off, fail-closed events, plugins off, approval required for non-read effects | No durable-store requirement; intended for short work |
| `durable` | SQLite-oriented, fail-closed policy/approval defaults, plugins off | Explicit `RuntimeStore` and `ArtifactStore` are mandatory |
| `service` | PostgreSQL-oriented durable defaults | `PostgresRuntimeStore`, `ArtifactStore`, and injected/configured Agno PostgreSQL storage are mandatory |
| `legacy` | Pre-0.12 compatibility values | Opaque resources are serialized and no durable/service guarantee is implied |

All three explicit profiles route `run/arun` through the lifecycle kernel. They omit
the raw `spawn_subagent` default tool and reject `subagents={...}`, raw Agno Team
presets, and specialized fork/command-dispatch skills. Use declared child templates or
registered capabilities; only `legacy` retains those raw migration surfaces.

The TOML `storage.postgres_url` selects connectivity; it does not create an HA control
plane. Service deployments that can fail over should inject a
`EtcdPostgresWriterAuthority` or another `PostgresWriterAuthorityProvider` into
`PostgresRuntimeStore`. The adapter/client cannot be serialized safely as a URL or
token in `.agnoclaw.toml`: it is trusted host code with cluster-ID pinning and a fresh
linearizable control-plane read. Supply production CA/mTLS/auth/proxy policy through an
injected `httpx.Client`; never put credentials in the endpoint URL. See
[PostgreSQL RuntimeStore operations](postgresql-runtime-store.md) for the exact grant,
TTL, `cluster_name`, transaction-deadline, commit-revalidation, and watchdog/STONITH
contract.

Use named data presets or the equivalent constructor argument:

```python
from agnoclaw import AgentHarness, HarnessConfig

short = AgentHarness(model, config=HarnessConfig.quick())
# Equivalent when no conflicting config.profile was supplied:
short = AgentHarness(model, profile="quick")
```

`HarnessConfig.local_safe(profile="quick" | "durable")` strengthens sandbox,
permission, guardrail, private-network, and plugin defaults without creating another
runtime profile. Explicit host overrides remain authoritative. Supplying conflicting
values through `AgentHarness(profile=...)` and `config.profile` raises
`RUNTIME_PROFILE_CONFLICT` rather than choosing one silently.

Inspect the compiled, content-minimized evidence without reaching into private Agno
state:

```python
manifest = harness.runtime_manifest()
print(manifest.profile, manifest.spec_digest)
for resource in manifest.resources:
    print(resource.resource_id, resource.trust, resource.lifetime, resource.recovery)
```

The `0.12a2` digest binds the profile, serializable settings, and every resource's
trust/lifetime/concurrency/recovery declaration. The public manifest intentionally
omits settings bodies, workspace paths, credentials, and live resource objects.

Durable preview construction separates serializable configuration from live stores:

```python
from agnoclaw import AgentHarness, HarnessConfig, LocalArtifactStore, SQLiteRuntimeStore

harness = AgentHarness(
    model,
    config=HarnessConfig.durable(),
    include_default_tools=False,
    runtime_store=SQLiteRuntimeStore("./runtime.db"),
    artifact_store=LocalArtifactStore("./artifacts"),
)
```

An explicit profile also drives live-resource classification. Durable/service reject
opaque models, caller tools, dependencies, or session-state values unless they have a
certified factory/immutable/host-managed contract. Registered `CapabilitySpec`
functions and Agno compression/summary managers are rebuilt per isolated run. The
supported host-local built-ins, including the configured deferred MCP surface, are also
rebuilt and closed per run, but their external effects remain single-flight. Explicit
profiles omit raw subagents and reject named-subagent configuration; background shell,
browser, media, notebook, custom-backend, pack, plugin, raw/effectful context-provider,
and streaming surfaces remain on their documented preview boundary. The factory-based read-query provider
adapter is governed through `capabilities=`. Accordingly, explicit
durable/service examples use `include_default_tools=False` and registered capabilities.
Plugin/pack `CapabilitySpec` registrations are governed identically; raw extension and
caller-supplied mutable MCP toolkits are compatibility-only and fail lifecycle
admission before a run is created.

## Model and optional dependencies

The core package has no provider SDK. Match the selected provider with an installed
extra. A directly prebuilt Agno model is quick/legacy-only; explicit profiles use the
factory contract above:

```bash
pip install "agnoclaw[anthropic]"
pip install "agnoclaw[openai]"
pip install "agnoclaw[google]"
pip install "agnoclaw[local]"
pip install "agnoclaw[mcp]"
```

Unsupported or missing capabilities fail with a stable compatibility error before a
model call. See [Compatibility](compatibility.md).

## Runtime lifecycle

| Field | Default | Contract |
|---|---:|---|
| `runtime_max_concurrency` | `16` | Process-level bound for lifecycle workers |
| `runtime_max_waiting` | `1024` | Hard bound across lifecycle workers waiting for a session lane or execution slot |
| `runtime_max_waiting_per_tenant` | `256` | Per-tenant subset of the global waiting bound |
| `runtime_max_waiting_per_session` | `32` | Per-session subset of the tenant waiting bound |
| `runtime_admission_timeout_seconds` | `30.0` | Finite wait to begin execution; `None` retains the queue bound without a time bound |
| `runtime_close_policy` | `drain` | `drain`, `detach`, or `cancel` ownership on close |
| `runtime_close_timeout_seconds` | unset | Optional bounded close wait |
| `runtime_operation_result_cache_size` | `128` | Process-local exact replay cache; `0` disables it |
| `runtime_lease_seconds` | `30` | Store-issued run/session lease duration |
| `runtime_lease_renew_interval_seconds` | `10` | Must be shorter than the lease |
| `permission_durable_approvals` | `true` | Persist registered-capability waits, decisions, and exact grants |
| `permission_approval_ttl_seconds` | `900` | Exact request/grant lifetime; allowed range 1 second–7 days |
| `permission_approval_poll_interval_seconds` | `0.25` | Live worker polling interval; greater than 0 and at most 5 seconds |

Cross-process durability also requires the appropriate `RuntimeStore`; known-success
result recovery requires an `ArtifactStore`. Configuration values alone do not upgrade
a compatibility path into a certified durable path.

Ready sessions enter capacity in process-local tenant round-robin order. Global,
per-tenant, and per-session ceilings prevent one noisy scope from consuming every
waiter. Queue overflow or timeout produces public retryable
`RUNTIME_ADMISSION_OVERLOADED` evidence; a run
that already persisted before its worker hit that boundary settles explicitly rather
than disappearing. Use a new idempotency key when intentionally submitting replacement
work. Export `harness.runtime_admission_stats()` to observe active/waiting tenants,
peaks, admissions, fair-queue turns, rejections, timeouts, cancellations, and lane
cleanup without exposing tenant or session identifiers. This is not a distributed
tenant scheduler; the service-wide fairness gate remains separate.

## Tools and network

The built-in tool defaults are convenient for local use:

```toml
enable_bash = true
enable_web_search = true
enable_web_fetch = true
enable_background_bash_tools = false
bash_timeout_seconds = 120
```

Service deployments should define a runtime backend, explicit path roots, network
allowlists, policy, and approval handling. Important controls include:

```toml
guardrails_enabled = true
path_guardrails_enabled = true
path_allowed_roots = ["/srv/agent/workspace"]
network_enabled = true
network_enforce_https = true
network_block_private_hosts = true
network_block_in_bash = true
policy_fail_open = false
permission_mode = "default"
permission_require_approver = true
permission_durable_approvals = true
permission_approval_ttl_seconds = 900
permission_approval_poll_interval_seconds = 0.25
```

Guardrails are defense in depth, not a sandbox. Use a sandboxed/custom runtime backend
for untrusted code. See [Runtime backends](embedding/workspace-backends.md) and [Policy
and guardrails](embedding/policy-and-guardrails.md).

### Custom Agno model factories

A caller-supplied live `Model` is opaque mutable state. It remains supported for
serialized `quick`/`legacy` compatibility, but `durable` and `service` require an
explicit fresh-model factory so concurrent runs never share provider clients, retry
state, or monkey-patched gateways:

```python
from agnoclaw import AgentHarness, AgnoModelFactory, HarnessConfig


def build_enterprise_model():
    # Construct and return one new Agno Model using credentials from the host's
    # secret manager. Never return a cached singleton.
    return EnterpriseAgnoModel(id="enterprise-chat")


model = AgnoModelFactory(
    model_id="enterprise-chat",
    provider="enterprise",
    implementation_digest="sha256:<64 lowercase hex characters>",
    factory=build_enterprise_model,
)
harness = AgentHarness(
    model,
    config=HarnessConfig.durable(),
    runtime_store=runtime_store,
    artifact_store=artifact_store,
)
```

`model_id`, optional `provider`, and the canonical implementation digest are safe
manifest identity; the callable and its credentials are never serialized. The factory
owns provider-specific cache/effort configuration, must return a matching Agno `Model`,
and must return a distinct instance on every call. agnoclaw closes the construction-time
model and each run-owned model. Invalid output, identity drift, a reused singleton, or
competing `provider=`/cache/effort authority fails before provider dispatch with a typed
`MODEL_FACTORY_*` error.

For a non-streaming outer lifecycle operation, the same factory declaration is
restart-safe when the host reconstructs it with the identical implementation digest:
planned work may resume, ambiguous dispatch waits for reconciliation, and a committed
result completes without redispatch. A changed digest fails before provider dispatch
with `RUN_RECOVERY_SPEC_MISMATCH`. The narrower Agno 2.9 native tool/approval
checkpoint certification also exercises a factory-created model, but only for its
exact governed non-streaming envelope; see
[Operations and recovery](operations-and-recovery.md#model-transport-ownership).

### MCP servers

MCP requires the optional `mcp>=2,<3` SDK. Each entry has a unique `name` and exactly
one `command` (stdio) or `url` (remote). Remote URLs default to
`transport = "streamable_http"`; `transport = "sse"` is an explicit legacy escape
hatch.

```toml
network_enabled = true
network_enforce_https = true
network_block_private_hosts = true
network_allowed_hosts = ["mcp.example.com"]

[[mcp_servers]]
name = "catalog"
url = "https://mcp.example.com/mcp"
transport = "streamable_http"
```

The model receives only `search_mcp_tools` and `call_mcp_tool`, not every remote
schema. Calls refresh discovery and require the exact returned schema digest. Static
`headers` and stdio `env` mappings are accepted; resolve secrets in trusted Python host
code instead of committing them to TOML. This preview does not implement an OAuth
client. See [MCP client and governed tool ingress](mcp.md) for async ownership,
security, result, error, and parity contracts.

Durable approvals apply to explicitly registered model-callable `CapabilitySpec`
entries on `start()` today. Disabling `permission_durable_approvals` restores the
legacy in-memory callback path; it is a compatibility escape hatch, not a service
hardening option. Approval TTL bounds both the pending question and a resulting grant.
The poll interval changes live-worker observation latency, not database truth. See
[Capabilities](capabilities.md#durable-approval-before-effect).

## Learning

Prefer the Python `learning=LearningProfile...` API because it expresses store intent,
scope prerequisites, consent, write path, promotion policy, and budgets. The legacy
booleans remain compatibility inputs and cannot express the full governed contract.

```python
from agnoclaw import AgentHarness, LearningProfile

harness = AgentHarness(
    learning=LearningProfile.personal_and_session(
        user_profile="always",
        user_memory="agentic",
        session_context="always",
        max_updates_per_run=5,
    )
)
```

Institutional Learned Knowledge additionally requires an Agno `Knowledge` object with a
vector database. See [Learning](learning.md).

## Context controls

`enable_compression` and `enable_session_summary` expose Agno's native mechanisms.
`max_context_tokens` adds harness accounting at 80/90/97%, while
`auto_compact_context=true` opts into agnoclaw's artifact-first preflight replacement
at 90% and deterministic emergency replacement at 97%. Automatic mode requires both a
positive budget and an injected `artifact_store`, releases below 70%, and is off by
default. Manual `compact_session()` and summary-only `summarize_session()` remain
available. The same opt-in performs one exact, non-streaming model-invocation retry for
Agno-classified context overflow, but refuses after observed tool activity. Live-
provider overflow proof, multi-host fencing, and adversarial/model-backed drift
certification remain open. A host may inject `LocalFileContextLockProvider` to make
ordinary runs shared readers and replacement an exclusive writer across cooperating
POSIX processes on one machine; read [Context management](context-management.md)
before unattended long runs.

The provider is live host code, not a TOML secret or path setting:

```python
from agnoclaw import AgentHarness, LocalFileContextLockProvider

harness = AgentHarness(
    model,
    context_lock_provider=LocalFileContextLockProvider(
        "/var/lock/agnoclaw/context"
    ),
)
```

Every process that may touch the same Agno session must use the same trusted lock
directory. Its canonical-path digest enters the immutable harness spec; the raw path
does not enter the public manifest. This adapter does not coordinate different hosts
or network filesystems with unproven `flock` semantics.

`max_inline_output_chars` separately opts lifecycle runs into lossless model-context
spill for explicitly registered, governed capability results. Values range from 1,024
to 1,000,000 characters. The harness requires `artifact_store`, returns small values
unchanged, and replaces larger values with a bounded preview plus the internal
read-only `read_spilled_output` pager. Pages remain within the exact tenant/user and
session authority. This setting does not yet cover raw `tools=`, built-ins, media,
model results, or child runs; see [Durable artifacts](artifacts.md#model-context-spill).

| Field / environment variable | Default | Contract |
|---|---:|---|
| `max_context_tokens` / `AGNOCLAW_MAX_CONTEXT_TOKENS` | unset | Positive provider context budget used for exact-tokenizer or deterministic-fallback accounting. |
| `auto_compact_context` / `AGNOCLAW_AUTO_COMPACT_CONTEXT` | `false` | Archive-first automatic preflight at 90%; requires `max_context_tokens` and `artifact_store`. |
| `max_inline_output_chars` / `AGNOCLAW_MAX_INLINE_OUTPUT_CHARS` | unset | Spill larger governed registered-capability and lifecycle first-party-tool results to verified artifacts; requires `artifact_store` and `start()`. |

## Observability and inspection

Runtime telemetry is configured in host code rather than `HarnessConfig`: the host
owns its OpenTelemetry providers/exporters, HMAC key source, TLS, endpoints, and
shutdown. Install `agnoclaw[otel]` only in the telemetry worker. The core package keeps
the content-free projection and sink protocol dependency-free.

For CLI inspection, provide the HMAC key and database credential through environment
variables and pass their names (`--identifier-key-env` and
`--postgres-credential-env`). Do not place either secret value in process arguments or
TOML. Grant the inspecting identity `runtime:run:inspect`, exact run ownership, and a
database role with read-only privileges. See
[Observability and safe run inspection](observability.md) for the full setup, privacy,
cardinality, exit-code, and delivery contract.

## Safe service baseline

```python
from agnoclaw.config import HarnessConfig

config = HarnessConfig.service(
    storage={"backend": "postgres", "postgres_url": secret_postgres_url},
    permission_approval_ttl_seconds=900,
)
```

Pass a `PostgresRuntimeStore`, `ArtifactStore`, and either an injected Agno PostgreSQL
DB or the configured URL to `AgentHarness`. This is a preview starting posture, not a
complete tenant policy or service certification. The host must still resolve trusted
identity, authorize resources, redact telemetry, retain/delete data, manage keys, and
select a suitable execution backend.
Failover-capable deployments must additionally wire the writer-authority adapter, a
controller-owned dedicated lease key, verified client/peer TLS, least-privilege etcd
RBAC, a durable odd-member quorum, and infrastructure watchdog/STONITH. For the JSON
gateway, inject `EtcdGatewayCredentials` bound to the exact adapter origin; certificate
Common Name identity is not a gateway RBAC mechanism. See
[PostgreSQL runtime store](postgresql-runtime-store.md) for the current `SSLContext`
example, token failover rules, and the secure three-member regression command. The
profile does not invent these controls.
