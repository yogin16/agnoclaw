# Policy and Guardrails

The runtime governance layer enforces two layers:

1. Policy engine decisions (`ALLOW`, `DENY`, `ALLOW_WITH_REDACTION`, `ALLOW_WITH_CONSTRAINTS`)
2. Runtime guardrails (path/network checks before tool execution)

Registered capabilities add a third enforcement primitive: a durable, exact
approval/grant ledger between policy and external effect.

Those layers sit above the tool backend. If you inject a custom `CommandExecutor` or
`WorkspaceAdapter`, policy and guardrail checks still run before the built-in tool
implementation delegates to that backend.

The current configuration default is `permission_mode="bypass"`. That is convenient for
local development but is not a safe service default. Embedded services should choose a
permission mode explicitly, require an approver where needed, fail closed on policy
evaluation, and test every legacy custom tool whose effects are not recognized by the
built-in name-based classifier. Registered `CapabilitySpec` entries use their declared
kind and effect instead of name inference. Existing entries supplied through `tools=`
remain on the compatibility classifier; see [Capabilities](../capabilities.md).

## Policy checkpoints

Current checkpoints:

- `before_run`
- `before_skill_load`
- `before_prompt_send`
- `before_tool_call`
- `after_tool_call`

## Example policy engine

```python
from agnoclaw.runtime import PolicyAction, PolicyDecision, RedactionRule

class EnterprisePolicy:
    # Required for registered capabilities whose decisions enter durable evidence.
    policy_version = "enterprise-policy-2026-08"

    def before_run(self, run_input, context):
        if "secret" in run_input.message.lower() and "security" not in context.roles:
            return PolicyDecision.deny(
                reason_code="RUN_SENSITIVE_BLOCKED",
                message="Sensitive requests require security role",
            )
        return PolicyDecision.allow()

    def before_prompt_send(self, prompt, context):
        return PolicyDecision.allow()

    def before_skill_load(self, request, context):
        return PolicyDecision.allow()

    def before_tool_call(self, request, context):
        if request.tool_name == "bash" and "security" not in context.roles:
            return PolicyDecision.deny(
                reason_code="BASH_ROLE_REQUIRED",
                message="bash requires security role",
            )
        return PolicyDecision.allow()

    def after_tool_call(self, result, context):
        if isinstance(result.output, str) and "apikey" in result.output.lower():
            return PolicyDecision(
                action=PolicyAction.ALLOW_WITH_REDACTION,
                reason_code="REDACT_TOOL_OUTPUT",
                redactions=(RedactionRule(target="apikey"),),
            )
        return PolicyDecision.allow()
```

## Registered capability governance

`AgentHarness(capabilities=[...])` gives registered model-callable capabilities a
stricter path than legacy raw tools. The harness performs, in order:

1. active-run and exact tenant/user reauthorization;
2. declared-scope, guardrail, permission classification, and `before_tool_call` policy
   checks;
3. fail-closed application of permitted constraints, a second guardrail pass, and
   bounded validation of effective arguments against the frozen input schema;
4. atomic `waiting_for_approval` plus exact durable request when approval is required;
5. durable callback or trusted-host decision and least-privilege grant;
6. store-lease renewal plus grant reauthorization at the last no-effect pre-dispatch
   point;
7. operation intent and dispatch-fence persistence;
8. invocation; then
9. `after_tool_call` policy and redaction before result settlement or artifact storage.

Every custom policy engine in this path must expose a stable, deployment-controlled
`policy_version`; every custom permission approver must similarly expose an
`approval_version`. Changing code or configuration that can affect decisions must
change the corresponding value. Durable evidence binds a digest of all engine
identities/versions, permission mode, approver identity/version, preapprovals,
capability version, authority, inputs, constraints, and decision reason. It does not
store raw lease tokens or capability arguments in operation metadata.

The supported pre-call constraint grammar is intentionally small:

- `arguments`: authoritative argument bindings added or replaced by policy;
- `max_timeout_seconds`: may only tighten a caller timeout; and
- `require_idempotency_key`: rejects a call that lacks a provider key.

Unknown or malformed constraints fail closed. Non-empty result constraints also fail
until an explicit output adapter exists; `ALLOW_WITH_REDACTION` is supported and runs
before persistence.

A permission approver may be synchronous or asynchronous:

```python
class ServiceApprover:
    approval_version = "service-approval-policy-2026-08"

    async def approve(self, request, context) -> bool:
        decision = await approval_service.check(
            tenant_id=context.tenant_id,
            run_id=request.run_id,
            capability=request.tool_name,
            category=request.category,
        )
        return decision.allowed
```

The capability path awaits this method directly and never bridges an active event loop
through a synchronous callback. A required request is already durable before the
callback starts, and the callback result is persisted before continuation.

Without a callback, the trusted embedding may list and settle the pending request:

```python
from agnoclaw.runtime import ApprovalState

pending = harness.capability_approvals(
    run_id,
    context=trusted_context,
    states=(ApprovalState.PENDING,),
)
harness.decide_capability_approval(
    pending[0].request.request_id,
    run_id=run_id,
    context=trusted_context,
    approved=True,
    issuer="change-management:operator-42",
    reason_code="TICKET_APPROVED",
)
```

Those methods are control-plane APIs and must never be exposed to model tools or accept
identity from model/user payloads. They reauthorize exact owner and authority. The
request and grant bind capability/version digest, effect, effective-argument digest,
policy version, tenant/principal/session/run, expiry, and nonce. A policy deployment or
argument mutation after approval fails before materialization. Raw lifecycle
`Respond` cannot substitute for a settled decision.

The wait and decision survive process visibility, and another process sharing the
store can settle a live worker's request. Arbitrary process-restart continuation of
the suspended Agno/model stack is not yet implemented; see
[Capabilities](../capabilities.md#durable-approval-before-effect).

## Guardrail configuration

Key config fields:

- `guardrails_enabled`
- `path_guardrails_enabled`
- `path_allowed_roots`
- `path_blocked_roots`
- `network_enabled`
- `network_enforce_https`
- `network_allowed_hosts`
- `network_blocked_hosts`
- `network_block_private_hosts`
- `network_block_in_bash`

### Example environment config

```bash
export AGNOCLAW_GUARDRAILS_ENABLED=true
export AGNOCLAW_PATH_GUARDRAILS_ENABLED=true
export AGNOCLAW_PATH_ALLOWED_ROOTS='["/srv/workspace","/tmp"]'

export AGNOCLAW_NETWORK_ENABLED=true
export AGNOCLAW_NETWORK_ENFORCE_HTTPS=true
export AGNOCLAW_NETWORK_ALLOWED_HOSTS='["docs.agno.com","api.company.com"]'
export AGNOCLAW_NETWORK_BLOCK_PRIVATE_HOSTS=true
```

## Elevated command execution

`AgentHarness.run_elevated_command()` and `AgentHarness.arun_elevated_command()`
provide an explicit host-local execution path for operations that should not run
inside the normal sandbox/backend plane.

Elevated commands require:

- a non-empty human-readable reason
- guardrail preflight
- `before_tool_call` policy approval for `tool_name="bash.elevated"`
- a configured permission approver that approves category `elevated_exec`

Example:

```python
from agnoclaw import AgentHarness, InteractivePermissionApprover

harness = AgentHarness(
    permission_mode="default",
    permission_approver=InteractivePermissionApprover(),
)

result = harness.run_elevated_command(
    "launchctl list | grep my-service",
    reason="Inspect host service state outside the sandbox",
)
```

In `agnoclaw chat`, `/elevated <command>` runs the same path and installs an
`InteractivePermissionApprover` automatically when no approver is configured.
`/elevated on|ask|full|off` sets a session-wide elevated mode for later bash
tool calls. `ask` and `on` still prompt; `full` skips the permission prompt but
keeps guardrail checks, policy checks, and audit events.

Elevated commands emit:

- `elevated.command.requested`
- `elevated.command.approved`
- `elevated.command.approval_skipped`
- `elevated.command.rejected`
- `elevated.command.started`
- `elevated.command.completed`
- `elevated.command.failed`

## Error behavior

- Guardrail deny: `HarnessError(code="GUARDRAIL_DENIED", category="guardrail")`
- Policy deny: `HarnessError(code="POLICY_DENIED", category="policy")`
- Elevated approval missing/rejected: `HarnessError(category="elevated")`
- Policy evaluation failure: `HarnessError(code="POLICY_EVALUATION_FAILED", category="policy")` unless `policy_fail_open=true`
- Auth/config model failures: `AgnoAuthError` / `AgnoConfigError` (raised for non-recoverable provider setup issues)

## Event behavior

These events are emitted for observability:
- `policy.decision`
- `tool.call.started`
- `tool.call.completed`
- `tool.call.failed`
- `step_started`
- `step_completed`
- `response_chunk`
- `thinking`
- `guardrail.violation`

Event sinks may receive sensitive arguments, result summaries, or metadata. Apply
default secret/DLP redaction before exporting events and define retention by tenant.
Registered capability result redaction protects operation settlement; it does not make
an arbitrary custom event sink safe. The planned native event/redaction contract is
described in [Harness evaluation](../evaluation.md).
