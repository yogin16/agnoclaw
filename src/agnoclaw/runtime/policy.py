"""Policy decision contract for harness lifecycle checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Protocol, runtime_checkable

from .hooks import PromptEnvelope, RunInput, SkillLoadRequest, ToolCallRequest, ToolCallResult


class PolicyAction(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    ALLOW_WITH_REDACTION = "ALLOW_WITH_REDACTION"
    ALLOW_WITH_CONSTRAINTS = "ALLOW_WITH_CONSTRAINTS"


@dataclass(frozen=True)
class RedactionRule:
    target: str
    replacement: str = "[REDACTED]"


@dataclass(frozen=True)
class PolicyDecision:
    action: PolicyAction
    reason_code: str
    message: str = ""
    constraints: dict[str, Any] = field(default_factory=dict)
    redactions: tuple[RedactionRule, ...] = ()

    @classmethod
    def allow(cls) -> "PolicyDecision":
        return cls(action=PolicyAction.ALLOW, reason_code="ALLOW_DEFAULT")

    @classmethod
    def deny(cls, *, reason_code: str, message: str) -> "PolicyDecision":
        return cls(action=PolicyAction.DENY, reason_code=reason_code, message=message)


def apply_redactions(text: str, redactions: tuple[RedactionRule, ...]) -> str:
    """Apply redaction rules to text."""
    updated = text
    for rule in redactions:
        if not rule.target:
            continue
        updated = updated.replace(rule.target, rule.replacement)
    return updated


@runtime_checkable
class PolicyEngine(Protocol):
    """Policy checks over run lifecycle checkpoints."""

    def before_run(self, run_input: RunInput, context) -> PolicyDecision | Awaitable[PolicyDecision]:
        ...

    def before_prompt_send(
        self,
        prompt: PromptEnvelope,
        context,
    ) -> PolicyDecision | Awaitable[PolicyDecision]:
        ...

    def before_skill_load(
        self,
        request: SkillLoadRequest,
        context,
    ) -> PolicyDecision | Awaitable[PolicyDecision]:
        ...

    def before_tool_call(
        self,
        request: ToolCallRequest,
        context,
    ) -> PolicyDecision | Awaitable[PolicyDecision]:
        ...

    def after_tool_call(
        self,
        result: ToolCallResult,
        context,
    ) -> PolicyDecision | Awaitable[PolicyDecision]:
        ...


class AllowAllPolicyEngine:
    """Default policy behavior for local/standalone mode."""

    def before_run(self, run_input: RunInput, context) -> PolicyDecision:
        del run_input, context
        return PolicyDecision.allow()

    def before_prompt_send(self, prompt: PromptEnvelope, context) -> PolicyDecision:
        del prompt, context
        return PolicyDecision.allow()

    def before_skill_load(self, request: SkillLoadRequest, context) -> PolicyDecision:
        del request, context
        return PolicyDecision.allow()

    def before_tool_call(self, request: ToolCallRequest, context) -> PolicyDecision:
        del request, context
        return PolicyDecision.allow()

    def after_tool_call(self, result: ToolCallResult, context) -> PolicyDecision:
        del result, context
        return PolicyDecision.allow()
