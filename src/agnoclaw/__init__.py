"""
agnoclaw — a hackable, model-agnostic agent harness built on Agno.

Quick start:
    from agnoclaw import AgentHarness

    agent = AgentHarness()
    agent.print_response("Summarize today's AI news")

With model string (provider:model_id):
    agent = AgentHarness("anthropic:claude-sonnet-4-6")
    agent = AgentHarness("openai:gpt-4o")
    agent = AgentHarness("ollama:qwen3:8b")    # local, no API key

Multi-agent:
    from agnoclaw.teams import research_team

    team = research_team()
    team.print_response("Research the state of fusion energy in 2026", stream=True)

With skills:
    agent = AgentHarness()
    agent.print_response("Research quantum computing", skill="deep-research")
"""

from .agent import AgentHarness, HarnessAgent  # HarnessAgent = backward compat alias
from .config import HarnessConfig, get_config
from .runtime import (
    AllowAllPolicyEngine,
    AgentOSClaimKeys,
    AgentOSContextAdapter,
    EventSinkMode,
    ExecutionContext,
    GuardrailViolation,
    HarnessError,
    InMemoryEventSink,
    PermissionController,
    PermissionMode,
    PolicyAction,
    PolicyDecision,
    RuntimeGuardrails,
)
from .tools.tasks import SubagentDefinition
from .workspace import Workspace

__all__ = [
    "AllowAllPolicyEngine",
    "AgentOSClaimKeys",
    "AgentOSContextAdapter",
    "AgentHarness",
    "EventSinkMode",
    "ExecutionContext",
    "GuardrailViolation",
    "HarnessAgent",  # backward compat
    "HarnessConfig",
    "HarnessError",
    "InMemoryEventSink",
    "PermissionController",
    "PermissionMode",
    "PolicyAction",
    "PolicyDecision",
    "RuntimeGuardrails",
    "get_config",
    "SubagentDefinition",
    "Workspace",
]

__version__ = "0.1.0"
