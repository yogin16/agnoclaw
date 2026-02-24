# AgentOS Adapter (Compatibility Profile)

`agnoclaw` does not require AgentOS, but v0.2 includes an adapter to map AgentOS/JWT claims into harness execution context.

## Adapter API

- `AgentOSContextAdapter`
- `AgentOSClaimKeys` (override claim-key mapping)

## Basic usage

```python
from agnoclaw import AgentHarness
from agnoclaw.runtime import AgentOSContextAdapter

claims = {
    "sub": "user-123",
    "sid": "session-abc",
    "tenant_id": "tenant-a",
    "org_id": "org-a",
    "team_id": "team-a",
    "roles": ["employee", "developer"],
    "scopes": ["agents.run"],
    "x_request_id": "req-100",
    "trace_id": "trace-100",
}

harness = AgentHarness(workspace_dir="/srv/workspace")
adapter = AgentOSContextAdapter()

ctx = adapter.to_execution_context(
    claims,
    workspace_id="/srv/workspace",
)

result = harness.run("Prepare standup summary", context=ctx)
print(result.content)
```

## What gets mapped

Default mapping:
- `user_id`: `user_id` or `sub`
- `session_id`: `session_id` or `sid`
- `tenant_id`: `tenant_id` or `tenant`
- `org_id`: `org_id`, `organization_id`, or `org`
- `team_id`: `team_id` or `team`
- `roles`: `roles` or `role`
- `scopes`: `scopes`, `scope`, or `permissions`
- `request_id`: `request_id` or `x_request_id`
- `trace_id`: `trace_id`, `x_trace_id`, or `traceparent`

## Custom claim mapping

```python
from agnoclaw.runtime import AgentOSClaimKeys, AgentOSContextAdapter

keys = AgentOSClaimKeys(
    user_id=("uid", "sub"),
    org_id=("org",),
    scopes=("permissions",),
)
adapter = AgentOSContextAdapter(claim_keys=keys)
```

## Integration guidance

- Use adapter output as the single source of runtime identity context.
- Keep JWT validation and signature verification in the API gateway/platform layer.
- Treat harness policy as additional restriction; it should not weaken upstream AgentOS authorization.
