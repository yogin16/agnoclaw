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
from .backends import RuntimeBackend
from .config import HarnessConfig, get_config
from .runtime import (
    AgentOSClaimKeys,
    AgentOSContextAdapter,
    AgnoAuthError,
    AgnoConfigError,
    AllowAllPolicyEngine,
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
from .skills import AutoApproveSkillInstallApprover, InteractiveSkillInstallApprover
from .tools import (
    BrowserBackend,
    CommandExecutor,
    LocalCommandExecutor,
    LocalPlaywrightBrowserBackend,
    LocalWorkspaceAdapter,
    WorkspaceAdapter,
)
from .tools.tasks import SubagentDefinition
from .workspace import Workspace

__all__ = [
    "AllowAllPolicyEngine",
    "AgnoAuthError",
    "AgnoConfigError",
    "AgentOSClaimKeys",
    "AgentOSContextAdapter",
    "AgentHarness",
    "AutoApproveSkillInstallApprover",
    "BrowserBackend",
    "EventSinkMode",
    "ExecutionContext",
    "GuardrailViolation",
    "HarnessAgent",  # backward compat
    "HarnessConfig",
    "HarnessError",
    "InMemoryEventSink",
    "InteractiveSkillInstallApprover",
    "CommandExecutor",
    "LocalCommandExecutor",
    "LocalPlaywrightBrowserBackend",
    "LocalWorkspaceAdapter",
    "PermissionController",
    "PermissionMode",
    "PolicyAction",
    "PolicyDecision",
    "RuntimeBackend",
    "RuntimeGuardrails",
    "get_config",
    "SubagentDefinition",
    "WorkspaceAdapter",
    "Workspace",
]

__version__ = "0.7.2"
