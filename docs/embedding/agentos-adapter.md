# AgentOS and remote lifecycle adapter

Status: 0.12 development preview; versioned lifecycle transport is implemented and
contract-tested on Agno 2.6.4 and 2.9.0

Last verified: 2026-08-13

AgentOS is an optional hosting edge. It supplies HTTP, verified authentication state,
and its native compatibility APIs; agnoclaw remains the authority for run identity,
ownership, state, events, effects, recovery, and learning policy. Install the complete
edge explicitly:

```bash
pip install "agnoclaw[server]"
```

The extra includes FastAPI, Uvicorn, and the multipart parser required by AgentOS's
native form routes. The core wheel does not import FastAPI.

## Choose the right API

`create_agentos_app()` exposes two deliberately different run surfaces:

| Surface | Intended use | Lifecycle control |
|---|---|---|
| `/agnoclaw/v1/...` with `RemoteHarnessClient.start/get_run` | New durable clients | Typed status, result, lifecycle/output cursors, cancel, and commands |
| AgentOS `/agents/{agent_id}/runs` with `RemoteHarnessClient.arun` | Existing AgentOS compatibility clients | Completed response or native raw stream only |

Do not start new controllable integrations with `arun()`. Explicit-profile servers now
execute that completed-response/raw-stream request through lifecycle, but the wire
wrapper does not expose its lifecycle handle. A compatibility `RemoteHarnessRun` may
contain a provider run ID, but it cannot acquire lifecycle control retroactively and
raises `RUN_CONTROL_UNAVAILABLE` for unsupported controls.

## Host setup

An open, anonymous AgentOS is intentionally rejected by `/agnoclaw/v1`. Configure an
OS security key, JWT authorization middleware, or verified AgentOS service accounts.
This security-key example is small enough for local deployment; use a secret manager
and TLS in production:

```python
import os

from agno.os.settings import AgnoAPISettings
from agnoclaw import AgentHarness, HarnessConfig, create_agentos_app

harness = AgentHarness(
    config=HarnessConfig.service(),
    workspace_dir="/srv/agnoclaw/workspace",
    runtime_store=runtime_store,
    artifact_store=artifact_store,
    include_default_tools=False,
)

settings = AgnoAPISettings(
    os_security_key=os.environ["AGNO_OS_SECURITY_KEY"],
)
app = create_agentos_app(
    [harness],
    settings=settings,
    # Built from host-owned child/model/budget/schema policy; see child-runs.md.
    child_templates={
        harness.name: {delegate_research.name: delegate_research},
    },
    telemetry=False,
)
```

Configure the Agno/runtime database on `AgentHarness` before exporting it. The adapter
uses the narrow `storage` accessor and will not mutate the harness's private Agno
agent. If AgentOS attempts to replace that database after construction, the adapter
fails with `AGENTOS_STORAGE_REBIND_UNSUPPORTED`; an invisible storage swap would break
the runtime manifest and recovery authority.

`child_templates` is an optional host registry keyed by the exact exported AgentOS
agent/harness ID. Each value is a `DeclaredChildTemplate`; see
[Declared child runs](../child-runs.md). It is deliberately separate from request
data, so a remote caller cannot serialize a new model, tool grant, budget, learning
policy, or result schema into the service. Every replica must deploy the same template
catalog and template digests.

`include_agnoclaw_lifecycle=True` is the default. Set it to `False` only for a native
AgentOS compatibility deployment. `include_agnoclaw_admin` remains a separate,
optional diagnostics surface and is not required by the lifecycle client.

For JWT or service-account operation, verified request state must include
`authenticated=True`. Lifecycle reads require `agents:read`; starts, cancellation, and
commands require `agents:run`. A verified OS security key is an intentionally unscoped
root credential. Agnoclaw normalizes the verified-key behavior difference between Agno
2.6 and 2.8 without treating an open request as authenticated.

## Remote client

The remote grammar matches the local `HarnessRun` grammar:

```python
import os

from agnoclaw import RemoteHarnessClient, Steer
from agnoclaw.runtime import encode_event_cursor

async with RemoteHarnessClient(
    "https://agents.example.com",
    api_key=os.environ["AGNO_OS_SECURITY_KEY"],
) as client:
    run = await client.start(
        "Investigate incident 42",
        idempotency_key="incident-42-v1",
        session_id="incident-42",
        user_id="alice",
        metadata={"case_id": "42"},
        learning_consent=False,
    )

    snapshot = await run.status()
    await run.command(Steer("Prioritize database evidence"))

    last_cursor = None
    async for event in run.events():
        last_cursor = encode_event_cursor(
            run_id=event.run_id,
            sequence=event.sequence,
        )

    result = await run.wait(timeout=60)

    # A later process can reattach using the same authorized identity.
    reattached = await client.get_run(run.run_id, user_id="alice")
    output_cursor = None
    async for segment in reattached.output():
        render(segment.content)
        output_cursor = segment.cursor
    assert await reattached.wait() == result

    # Long-running delegation returns its independently controllable child handle.
    child = await reattached.child(
        "delegate_research",
        "Collect primary-source incident evidence.",
        delegation_id="incident-42-research-v1",
    )
    direct_children = await reattached.children()
    child_results = await reattached.child_results(
        max_inline_result_chars=8_000,
    )
    child_result = await child.wait()
```

`wait()` polls independently of event consumption. Timing out or cancelling a caller
task never sends run cancellation. `cancel()` is the only cancellation request.
Likewise, an HTTP disconnect has no run-cancellation meaning.

Use `events(after=last_cursor)` after a disconnect. Each page is checked for exact run
identity, monotonically gap-free sequence, bounded cardinality, and a cursor that
matches the last delivered sequence.
`output(after=output_cursor)` independently replays owner-authorized provider text from
artifact-backed segments of at most 8,192 characters/32 deltas and pages of at most 50.
Lifecycle HTTP starts default `persist_output=True`; set it false for structured-output
runs, whose typed terminal result remains authoritative. The stream excludes raw
provider/tool event objects, and the final result remains authoritative.
For either stream, `follow=False` reads one finite snapshot, `follow=None` follows an
active run through terminal state, and `follow=True` can continue
until its timeout and may yield local `RunHeartbeat` values. The server exposes bounded
pages rather than holding one fragile SSE connection; the client supplies the follow
loop.

The client accepts only an HTTP(S) origin with no credentials, path, query, or fragment,
does not follow redirects, and uses safe unreserved path identifiers. Put credentials
in `api_key` or a preconfigured injected `httpx.AsyncClient`, never in the URL.

## Versioned routes

Every successful or handler-generated error response has
`protocol_version: "1.0"` and a typed `kind`.

| Method and route | Scope | Response kind |
|---|---|---|
| `POST /agnoclaw/v1/harnesses/{id}/runs` | `agents:run` | `run` (`202`) |
| `GET /agnoclaw/v1/harnesses/{id}/runs/{run_id}` | `agents:read` | `run` |
| `POST .../{run_id}/children` | `agents:run` plus template scopes | `child` (`202`) |
| `GET .../{run_id}/children?limit=` | `agents:read` | `children` |
| `GET .../{run_id}/child-results?limit=&artifact_limit=&max_inline_result_chars=` | `agents:read` | `child_results` |
| `GET .../{run_id}/result` | `agents:read` | `result` |
| `GET .../{run_id}/events?after=&limit=` | `agents:read` | `events` |
| `GET .../{run_id}/output?after=&limit=` | `agents:read` | `output` |
| `POST .../{run_id}/cancel` | `agents:run` | `run` |
| `POST .../{run_id}/commands` | `agents:run` | `run` |

The start body is strict: protocol version, message, optional idempotency/session/user
IDs, metadata, explicit `learning_consent`, `persist_output`, and an options object.
Unknown fields fail.
Options cannot replace `context`, identity, idempotency, metadata, consent, or stream
semantics. Metadata cannot inject `_agnoclaw_context`, `agentos`, `agentos_claims`, or
`claims`.

The child-start body is narrower: protocol version, registered template name, task,
stable delegation ID, and optional user ID. Owner claims override body identity. The
parent must still be running. Repeating the exact declaration reattaches through the
child lifecycle idempotency contract; changing the task under the same delegation ID
conflicts. Unknown templates return the non-enumerating `CHILD_TEMPLATE_NOT_FOUND`.
Template-specific scopes are checked before dispatch.

Current admission limits are:

- 1 MiB total request body;
- 1,000,000 UTF-8 bytes for the message;
- 64 KiB canonical metadata;
- 512 characters for identity values and path IDs;
- 64 direct children per page, 100 artifact handoffs per child-result request, and a
  256–1,000,000-character reviewed inline-result bound;
- 100 lifecycle events or 50 output segments per page, 8,192 characters per output
  segment, and a 2,048-character cursor.

Malformed JSON, protocol versions, limits, commands, and protected fields return a
versioned `LIFECYCLE_REQUEST_INVALID` error. Upstream AgentOS security-key failures are
also normalized into the lifecycle error envelope. Unexpected server failures return
only `LIFECYCLE_INTERNAL_ERROR`; raw exception and database details are not exposed.

## Identity and ownership

Authenticated claims are authoritative. Default claim mapping is:

- `user_id`: `user_id` or `sub`;
- `session_id`: `session_id` or `sid`;
- `tenant_id`: `tenant_id` or `tenant`;
- `org_id`: `org_id`, `organization_id`, or `org`;
- `team_id`: `team_id` or `team`;
- roles: `roles` or `role`;
- scopes: `scopes`, `scope`, or `permissions`;
- request/trace IDs: their direct or `x_`/`traceparent` spellings.

When a body or query user/session conflicts with an authenticated claim, admission
fails with `IDENTITY_CLAIM_CONFLICT`. Client metadata is never treated as a claim
channel. Reattachment reauthorizes the exact store owner tuple and hides a missing or
unauthorized run behind the same `RUN_NOT_FOUND` response.

For a trusted in-process integration, the lower-level adapter remains public:

```python
from agnoclaw.runtime import AgentOSClaimKeys, AgentOSContextAdapter

adapter = AgentOSContextAdapter(
    AgentOSClaimKeys(user_id=("uid", "sub"), scopes=("permissions",))
)
context = adapter.to_execution_context(
    verified_claims,
    workspace_id="/srv/agnoclaw/workspace",
)
```

Call it only with claims already verified by middleware or a trusted gateway. Harness
policy is an additional restriction and must never weaken AgentOS authorization.

## Deployment and failure semantics

- All replicas that must reattach the same runs need the same `RuntimeStore`, artifact
  store, tenant policy, key material, compatible harness specification, and declared
  child-template catalog.
- The lifecycle HTTP layer is not a queue and does not make unsupported model/tool
  stacks restartable. It exposes the exact recovery and reconciliation truth already
  documented by the local runtime.
- A `waiting_for_reconciliation` result raises
  `RunReconciliationRequiredError`. Failed/cancelled/expired terminal results raise a
  redacted `RunWaitError`. HTTP errors raise `RemoteHarnessError`; malformed successful
  peer data raises `LifecycleProtocolError`.
- Reverse proxies must preserve `Authorization`, reject oversized request lines/bodies,
  disable cache storage for owner-scoped responses, and use timeouts that do not imply
  application cancellation.
- Native AgentOS scheduler, sessions, traces, and approvals remain compatibility or
  platform surfaces. They do not bypass the agnoclaw run/effect ledger.

The route, auth, identity, reattachment, result, event cursor, command, disconnect,
declared-child start/list/result, template hiding/scope, malformed-peer, and
compatibility contracts run against both supported Agno lines in CI. Production
PostgreSQL failover/partition, proxy, load, and long-duration recovery soak evidence
remains a release gate rather than an implied guarantee.
