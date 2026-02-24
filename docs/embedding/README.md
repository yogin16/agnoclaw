# Embedding agnoclaw v0.2

This folder documents how to embed the harness core inside another service.

## What embedding gives you

- Stable `run/arun` harness API.
- Typed execution context (`ExecutionContext`).
- Policy checkpoints across run and tool boundaries.
- Structured events via pluggable event sinks.
- Runtime path/network guardrails.
- Optional AgentOS claim-to-context adapter.

## Minimal embedding pattern

```python
from agnoclaw import AgentHarness
from agnoclaw.runtime import ExecutionContext

harness = AgentHarness(workspace_dir="/srv/agent/workspace")

ctx = ExecutionContext.create(
    user_id="u-123",
    session_id="s-123",
    workspace_id="/srv/agent/workspace",
    tenant_id="tenant-a",
    org_id="org-a",
    roles=["employee"],
    scopes=["agents.run"],
    request_id="req-123",
    trace_id="trace-123",
)

result = harness.run("Summarize open pull requests", context=ctx)
print(result.content)
```

## Runtime behavior to rely on

- Harness emits lifecycle events for run/prompt/model/policy/tool checkpoints.
- Policy denials return typed `HarnessError` values.
- Tool calls are checked by guardrails before execution.
- Run metadata includes normalized execution context for downstream tooling.

## Related docs

- [Policy and Guardrails](./policy-and-guardrails.md)
- [AgentOS Adapter](./agentos-adapter.md)
- [v0.2 Spec](../../spec/v0.2-harness-core.md)
