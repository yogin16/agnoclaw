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
from .backends import RuntimeBackend, SandboxMode, normalize_sandbox_mode
from .config import HarnessConfig, get_config
from .packs import (
    LoadedPack,
    PackError,
    PackManifest,
    PackProvides,
    PackTrust,
    PackTrustError,
    inspect_pack,
    install_pack,
    is_pack_trusted,
    list_installed_packs,
    load_pack,
    pack_store_dir,
    remove_pack,
    trust_pack,
)
from .remote import RemoteHarnessClient, RemoteHarnessRun
from .runtime import (
    AgentOSClaimKeys,
    AgentOSContextAdapter,
    AgentOSHarnessAgent,
    AgentOSPermissionApprover,
    AgnoAuthError,
    AgnoConfigError,
    AllowAllPolicyEngine,
    ElevatedCommandRequest,
    ElevatedCommandResult,
    ElevatedSessionMode,
    EventSinkMode,
    ExecutionContext,
    GuardrailViolation,
    HarnessError,
    InMemoryEventSink,
    InMemorySchedulerBackend,
    InteractivePermissionApprover,
    JsonSchedulerBackend,
    LifecycleHook,
    LifecycleHookRequest,
    PermissionController,
    PermissionMode,
    PlanExitSignal,
    PlanQuestionSignal,
    PolicyAction,
    PolicyDecision,
    RuntimeGuardrails,
    SchedulerBackend,
    SchedulerJob,
    SchedulerRunRecord,
    as_agentos_agent,
    create_agentos_app,
    scheduler_store_path,
)
from .skills import AutoApproveSkillInstallApprover, InteractiveSkillInstallApprover
from .tools import (
    BrowserBackend,
    CommandExecutor,
    LocalCommandExecutor,
    LocalPlaywrightBrowserBackend,
    LocalWorkspaceAdapter,
    PlanSignalToolkit,
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
    "AgentOSHarnessAgent",
    "AgentOSPermissionApprover",
    "AgentHarness",
    "AutoApproveSkillInstallApprover",
    "BrowserBackend",
    "EventSinkMode",
    "ElevatedCommandRequest",
    "ElevatedCommandResult",
    "ElevatedSessionMode",
    "ExecutionContext",
    "GuardrailViolation",
    "HarnessAgent",  # backward compat
    "HarnessConfig",
    "HarnessError",
    "InMemoryEventSink",
    "InMemorySchedulerBackend",
    "InteractivePermissionApprover",
    "InteractiveSkillInstallApprover",
    "CommandExecutor",
    "LocalCommandExecutor",
    "LocalPlaywrightBrowserBackend",
    "LocalWorkspaceAdapter",
    "JsonSchedulerBackend",
    "LifecycleHook",
    "LifecycleHookRequest",
    "LoadedPack",
    "PackError",
    "PackManifest",
    "PackProvides",
    "PackTrust",
    "PackTrustError",
    "PlanExitSignal",
    "PlanQuestionSignal",
    "PermissionController",
    "PermissionMode",
    "PlanSignalToolkit",
    "PolicyAction",
    "PolicyDecision",
    "RuntimeBackend",
    "RuntimeGuardrails",
    "SandboxMode",
    "SchedulerBackend",
    "SchedulerJob",
    "SchedulerRunRecord",
    "scheduler_store_path",
    "RemoteHarnessClient",
    "RemoteHarnessRun",
    "as_agentos_agent",
    "create_agentos_app",
    "get_config",
    "normalize_sandbox_mode",
    "SubagentDefinition",
    "WorkspaceAdapter",
    "Workspace",
    "inspect_pack",
    "install_pack",
    "is_pack_trusted",
    "list_installed_packs",
    "load_pack",
    "pack_store_dir",
    "remove_pack",
    "trust_pack",
]

__version__ = "0.7.5"
