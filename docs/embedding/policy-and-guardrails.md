# Policy and Guardrails

`agnoclaw` v0.2 enforces two layers:

1. Policy engine decisions (`ALLOW`, `DENY`, `ALLOW_WITH_REDACTION`, `ALLOW_WITH_CONSTRAINTS`)
2. Runtime guardrails (path/network checks before tool execution)

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

## Error behavior

- Guardrail deny: `HarnessError(code="GUARDRAIL_DENIED", category="guardrail")`
- Policy deny: `HarnessError(code="POLICY_DENIED", category="policy")`
- Policy evaluation failure: `HarnessError(code="POLICY_EVALUATION_FAILED", category="policy")` unless `policy_fail_open=true`

## Event behavior

These events are emitted for observability:
- `policy.decision`
- `tool.call.started`
- `tool.call.completed`
- `tool.call.failed`
- `guardrail.violation`
