"""The embeddable Agno harness facade; detailed usage lives in the public docs."""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import functools
import hashlib
import importlib.util
import inspect
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import warnings
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Never, cast
from uuid import uuid4

from agno.agent import Agent
from agno.context.provider import ContextProvider
from agno.exceptions import AgentRunException
from agno.run.agent import RunOutput, RunOutputEvent
from agno.tools.function import Function, FunctionCall
from agno.tools.toolkit import Toolkit

if TYPE_CHECKING:
    from agno.models.base import Model

from .backends import RuntimeBackend, SandboxMode, normalize_sandbox_mode
from .capabilities import (
    CapabilityCatalogEntry,
    CapabilityKind,
    CapabilityRegistry,
    CapabilitySpec,
)
from .capability_adapters import (
    AgnoCapabilityBinding,
    build_agno_capability_bindings,
)
from .capability_approval import DurableApprovalCoordinator
from .capability_execution import (
    CapabilityExecution,
    CapabilityExecutor,
    CapabilityInvoker,
)
from .capability_governance import capability_digest, capability_policy_version
from .capability_runtime import execute_harness_capability
from .commands import Fork, Pause, Respond, Resume, RunCommand, Steer
from .compat import AgnoFeature, inspect_agno_compatibility
from .config import HarnessConfig, RuntimeProfile, get_config
from .context_automation import ContextAutomationCoordinator
from .context_locking import ContextLockLease, ContextLockMode, ContextLockProvider
from .context_management import ArtifactContextArchive, ContextScope
from .context_overflow import (
    context_overflow_error,
    is_context_overflow_exception,
    is_context_overflow_signal,
)
from .context_runtime import _ContextManagementMixin
from .learning import LearningPolicy, LearningScope
from .learning_admin import (
    LearningAdminGateway,
    LearningDataRecord,
    LearningDataStore,
    LearningMutationReceipt,
)
from .learning_candidates import (
    AgnoLearningPromotionAdapter,
    CandidateAction,
    CandidateAuthor,
    CandidateEvaluation,
    CandidateReconciliation,
    CandidateRecord,
    CandidateRisk,
    CandidateState,
    EvaluationArchivePage,
    EvaluationArchiveQuery,
    EvaluationVerdict,
    LearningApplication,
    LearningApplicationKind,
    LearningCandidate,
    LearningCandidateExport,
    LearningEffectivenessPolicy,
    LearningEffectivenessSummary,
    LearningEvent,
    LearningGateway,
    LearningLedger,
    LearningOutcome,
    LearningOutcomeKind,
    LearningOwner,
    LearningPromotionAdapter,
    LearningTarget,
    PromotionActor,
    ReconciliationKind,
    ReconciliationVerdict,
)
from .learning_reconciliation_runtime import _LearningReconciliationMixin
from .legacy_tools import (
    LegacyToolBinding,
    normalize_legacy_tools,
    require_no_extension_tools_for_lifecycle,
    require_no_legacy_tools_for_durable,
)
from .model_factory import AgnoModelFactory
from .models.ownership import OwnedAgnoModelResource
from .output_spill import (
    READ_SPILLED_OUTPUT,
    model_output,
    output_page,
    read_capability,
)
from .prompts.system import SystemPromptBuilder
from .runtime import (
    AdmissionEnvelope,
    AgnoAuthError,
    AgnoConfigError,
    AllowAllPolicyEngine,
    ApprovalRecord,
    ApprovalState,
    ArtifactChunk,
    ArtifactReference,
    ArtifactStore,
    AsyncSessionLanes,
    DeclaredChildTemplate,
    EffectClass,
    ElevatedCommandRequest,
    ElevatedCommandResult,
    ElevatedSessionMode,
    EventSink,
    EventSinkMode,
    ExecutionContext,
    GrantScope,
    HarnessError,
    HarnessRun,
    IdentityAssertion,
    IdentitySource,
    LifecycleHook,
    LifecycleHookRequest,
    LifecycleTransition,
    ModelProviderDependencyError,
    NullEventSink,
    OperationDispatchDeferredError,
    OperationGateway,
    OperationIntent,
    OperationKind,
    OperationNotFoundError,
    OperationReconciliationBatch,
    OperationReconciliationObserver,
    OperationState,
    PermissionApprover,
    PermissionController,
    PermissionMode,
    PermissionRequest,
    PlanExitSignal,
    PlanQuestionSignal,
    PolicyAction,
    PolicyDecision,
    PolicyEngine,
    PostRunHook,
    PreRunHook,
    PromptEnvelope,
    RunInput,
    RunLeaseClaim,
    RunOwner,
    RunResultEnvelope,
    RunSnapshot,
    RunState,
    RuntimeClosePolicy,
    RuntimeGuardrails,
    RuntimeLeaseLostError,
    RuntimeLeaseUnavailableError,
    RuntimeRecoveryBatch,
    RuntimeStore,
    SafeDiagnostic,
    SkillLoadRequest,
    SQLiteRuntimeStore,
    TerminalRecord,
    ToolCallRequest,
    ToolCallResult,
    TransitionKind,
    apply_redactions,
    build_event,
    command_decision,
    normalize_elevated_session_mode,
    normalize_permission_mode,
    recover_pending_runs,
    thaw_data,
)
from .runtime import (
    reconcile_pending_operations as reconcile_runtime_operations,
)
from .runtime.agent_materialization import materialize_run_agent
from .runtime.builtin_materialization import (
    BuiltinToolBundle,
    builtin_tool_settings,
    can_materialize_host_builtin_tools,
    materialize_host_builtin_tools,
)
from .runtime.checkpoints import (
    RuntimeRequestCheckpoint,
    load_runtime_request_checkpoint,
    persist_runtime_request_checkpoint,
    persisted_result_value,
    runtime_request_digest,
    validate_recoverable_model_intent,
)
from .runtime.child_output import enforce_child_result_contracts
from .runtime.children import (
    ChildRunSpec,
    build_subagent_execution_context,
    trace_payload_from_context,
)
from .runtime.concurrency import drain_thread_call
from .runtime.materialization import (
    callable_resource_type,
    materialize_factory_value,
    resolve_runtime_profile,
    run_factory_materializer,
    validate_profile_resources,
)
from .runtime.model_gateway import has_valid_tool_batch_checkpoint
from .runtime.presentation import (
    LiveRunPresentation,
    RunPresentationPublisher,
    execute_presented_model_request,
    validate_output_persistence,
)
from .runtime.reconciliation import (
    model_operation_has_unknown_effects,
    wait_for_model_operation_reconciliation,
)
from .runtime.recovery import inspect_child_recovery
from .runtime.spec import (
    HarnessRuntimeManifest,
    ResourceConcurrency,
    ResourceLifetime,
    ResourceRecovery,
    classify_resource,
    compile_harness_spec,
    host_managed_resource,
)
from .runtime.sync_bridge import SyncLifecycleCoordinator
from .runtime.tool_ingress import (
    activate_builtin_ingress,
    builtin_effect,
    declare_builtin_effects,
    restore_builtin_ingress,
    toolkit_functions,
)
from .runtime.trajectory import project_runtime_event_async, project_runtime_event_sync
from .runtime.usage import (
    agno_result_settlement_evidence,
    supervise_child_deadline,
)
from .session_commands import _ElevatedSessionCommandExecutor
from .skills.agno import AgnoClawSkills
from .skills.backends import SkillInstallApprover
from .skills.registry import ModelSkillActivation, SkillRegistry
from .tools import PlanSignalToolkit, get_default_tools
from .tools.backends import (
    CommandExecutor,
    LocalCommandExecutor,
)
from .workspace import Workspace

_AGNO_AGENT_TYPE = Agent
logger = logging.getLogger("agnoclaw.agent")
_LEARNING_PROMPT_MAX_BYTES = 64 * 1024

_ERROR_MESSAGE_LIMIT = 500
_RESULT_PREVIEW_LIMIT = 240
_RESULT_REF_KEYS = ("id", "name", "title", "type", "version", "filename")
_ASSISTANT_STREAM_EVENTS = frozenset({"RunContent"})
_DURABLE_MODEL_LOOP_MODE = "provider-checkpoint-v1"


def _is_durable_model_loop_intent(operation: Any) -> bool:
    metadata = thaw_data(operation.intent.metadata)
    return bool(
        operation.intent.effect_class is EffectClass.READ_ONLY
        and isinstance(metadata, dict)
        and metadata.get("orchestration_mode") == _DURABLE_MODEL_LOOP_MODE
    )
_TOOL_LIFECYCLE_EVENT_TYPES = frozenset(
    {"tool.call.started", "tool.call.completed", "tool.call.failed"}
)
_CURRENT_TOOL_RUNTIME: ContextVar[dict[str, Any] | None] = ContextVar(
    "agnoclaw_current_tool_runtime",
    default=None,
)
_CURRENT_RUN_CONTEXT: ContextVar[Any | None] = ContextVar(
    "agnoclaw_current_run_context",
    default=None,
)

_PROVIDER_ALIASES: dict[str, str] = {
    "bedrock": "aws-bedrock",
    "aws": "aws-bedrock",
    "grok": "xai",
}

_KNOWN_PROVIDERS: set[str] = {
    "aimlapi",
    "anthropic",
    "aws-bedrock",
    "azure-ai-foundry",
    "azure-openai",
    "cerebras",
    "cohere",
    "cometapi",
    "dashscope",
    "deepinfra",
    "deepseek",
    "fireworks",
    "google",
    "groq",
    "huggingface",
    "ibm",
    "internlm",
    "langdb",
    "litellm",
    "llama-cpp",
    "lmstudio",
    "meta",
    "mistral",
    "moonshot",
    "nebius",
    "neosantara",
    "nexus",
    "nvidia",
    "ollama",
    "openai",
    "openrouter",
    "perplexity",
    "portkey",
    "requesty",
    "sambanova",
    "siliconflow",
    "together",
    "vercel",
    "vertexai-claude",
    "vllm",
    "xai",
}

# Provider SDKs represented by first-party extras.
_PROVIDER_DEPENDENCIES: dict[str, tuple[str, str]] = {
    "anthropic": ("anthropic", "agnoclaw[anthropic]"),
    "openai": ("openai", "agnoclaw[openai]"),
    "google": ("google.genai", "agnoclaw[google]"),
    "groq": ("groq", "agnoclaw[groq]"),
    "ollama": ("ollama", "agnoclaw[local]"),
    "deepseek": ("openai", "agnoclaw[openai]"),
    "xai": ("openai", "agnoclaw[openai]"),
}


def _merge_result_ref_keys(extra: list[str] | None) -> tuple[str, ...]:
    """Generic identity keys with consumer-configured keys appended (order-preserving,
    de-duplicated). Non-string / blank entries are ignored."""
    keys = list(_RESULT_REF_KEYS)
    for key in extra or []:
        if isinstance(key, str) and key.strip() and key not in keys:
            keys.append(key)
    return tuple(keys)


def get_current_tool_runtime() -> dict[str, Any] | None:
    """Return the currently executing tool runtime context, if any."""
    runtime = _CURRENT_TOOL_RUNTIME.get()
    if runtime is None:
        return None
    return dict(runtime)


def get_current_run_context() -> Any | None:
    """Return the active Agno run context during custom tool dispatch, else None."""
    return _CURRENT_RUN_CONTEXT.get()


def get_current_dependencies() -> dict[str, Any] | None:
    """Return ``dependencies`` from the active run context, if any.

    Convenience over :func:`get_current_run_context`; returns the active run's
    ``dependencies`` mapping (a copy) or ``None`` when no run context is active or
    no dependencies were set for the run.
    """
    run_context = _CURRENT_RUN_CONTEXT.get()
    dependencies = getattr(run_context, "dependencies", None)
    if isinstance(dependencies, dict):
        return dict(dependencies)
    return None


def _run_output_status_value(value: Any) -> str | None:
    status = getattr(value, "status", None)
    if status is None:
        return None
    raw = getattr(status, "value", status)
    if raw is None:
        return None
    text = str(raw).strip().lower()
    return text or None


def _run_output_is_error(value: Any) -> bool:
    return _run_output_status_value(value) == "error"


def _resolve_model(
    model: str | Model | None, provider: str | None, config: HarnessConfig
) -> str | Model:
    if model is not None and not isinstance(model, str):
        # Pre-built Agno Model instance — Agno's Agent accepts it as-is.
        return model

    model_str = model or config.default_model
    prov = provider or config.default_provider

    # If the model string looks like "x:y", it may be either:
    #   1) provider:model_id (e.g. openai:gpt-4o), or
    #   2) a model_id that itself contains ":" (e.g. qwen3:0.6b for Ollama).
    if ":" in model_str:
        left, right = model_str.split(":", 1)
        normalized_left = _PROVIDER_ALIASES.get(left.lower(), left.lower())

        # Explicit provider prefix wins when recognized.
        if normalized_left in _KNOWN_PROVIDERS:
            return f"{normalized_left}:{right}"

        # Unknown prefix: treat entire model_str as model_id and use provider arg/default.
        p = _PROVIDER_ALIASES.get(prov.lower(), prov.lower())
        return f"{p}:{model_str}"

    # Separate model_id + provider
    p = prov.lower()
    p = _PROVIDER_ALIASES.get(p, p)
    return f"{p}:{model_str}"


def _require_model_provider_dependency(model: str | Model) -> None:
    if not isinstance(model, str) or ":" not in model:
        return
    provider = model.split(":", 1)[0].strip().lower()
    requirement = _PROVIDER_DEPENDENCIES.get(provider)
    if requirement is None:
        return
    package, install_extra = requirement
    try:
        available = importlib.util.find_spec(package) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        available = False
    if not available:
        raise ModelProviderDependencyError(
            provider=provider,
            package=package,
            install_extra=install_extra,
        )


def _make_db(config: HarnessConfig):
    if config.storage.backend == "postgres":
        if not config.storage.postgres_url:
            raise ValueError(
                "AGNOCLAW_STORAGE__POSTGRES_URL is required when storage backend is 'postgres'"
            )
        from agno.db.postgres import PostgresDb

        return PostgresDb(
            db_url=config.storage.postgres_url,
            session_table=config.storage.session_table,
            memory_table=config.storage.memory_table,
        )
    else:
        from agno.db.sqlite import SqliteDb

        from .migration_fence import assert_migration_store_writable

        db_path = Path(config.storage.sqlite_path).expanduser()
        if str(db_path) != ":memory:":
            assert_migration_store_writable(
                db_path,
                code="LEARNING_STORE_FENCED",
                category="learning",
                store_name="SQLite learning/session",
            )
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return SqliteDb(
            db_file=str(db_path),
            session_table=config.storage.session_table,
            memory_table=config.storage.memory_table,
        )


class HarnessSession:
    """Per-session SDK facade over AgentHarness.run/arun."""

    def __init__(
        self,
        harness: AgentHarness,
        *,
        user_id: str | None = None,
        workspace_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        context: ExecutionContext | None = None,
    ) -> None:
        self.harness = harness
        self._base_context = context or harness._build_execution_context(
            user_id=user_id or harness.user_id,
            session_id=session_id or harness.session_id,
            workspace_id=workspace_id or str(harness.workspace.path),
            metadata=metadata,
        )
        self.user_id = self._base_context.user_id
        self.workspace_id = self._base_context.workspace_id
        self.session_id = self._base_context.session_id
        self.metadata = dict(self._base_context.metadata)

    def _context(self, metadata: dict[str, Any] | None = None) -> ExecutionContext:
        return self._base_context.with_metadata(metadata)

    async def send(self, message: str, *, stream: bool = False, **kwargs) -> HarnessRun:
        """Send a message through this session and return a run wrapper."""
        metadata = kwargs.pop("metadata", None)
        result = await self.harness.arun(
            message,
            stream=stream,
            context=self._context(metadata),
            **kwargs,
        )
        if stream:
            return HarnessRun(stream=result)  # type: ignore[arg-type]
        return HarnessRun(result=result)

    async def start(self, message: str, **kwargs: Any) -> HarnessRun:
        """Start controllable work in this session without waiting for completion."""
        metadata = kwargs.pop("metadata", None)
        return await self.harness.start(
            message,
            context=self._context(metadata),
            **kwargs,
        )


class _RunGateLease:
    """Idempotent lease over one harness's single-flight run gate."""

    __slots__ = (
        "_context_lock",
        "_gate",
        "_on_release",
        "_released",
        "_state_lock",
    )

    def __init__(
        self,
        gate: threading.Lock | None,
        on_release: Callable[[], None] | None = None,
        context_lock: ContextLockLease | None = None,
    ) -> None:
        self._gate = gate
        self._on_release = on_release
        self._context_lock = context_lock
        self._released = False
        self._state_lock = threading.Lock()

    @property
    def context_lock(self) -> ContextLockLease | None:
        return self._context_lock

    def upgrade_context_lock(self) -> ContextLockLease | None:
        with self._state_lock:
            if self._released:
                raise HarnessError(
                    code="CONTEXT_CROSS_PROCESS_LOCK_LOST",
                    category="context",
                    message="The active run no longer owns its session context lock.",
                    retryable=False,
                )
            if self._context_lock is not None:
                self._context_lock.upgrade()
            return self._context_lock

    def release(self) -> None:
        with self._state_lock:
            if self._released:
                return
            self._released = True
            try:
                if self._context_lock is not None:
                    self._context_lock.release()
            finally:
                try:
                    if self._on_release is not None:
                        self._on_release()
                finally:
                    if self._gate is not None:
                        self._gate.release()


class _LeasedIterator(Iterator[Any]):
    """Iterator that releases a run lease on exhaustion, close, error, or GC."""

    def __init__(self, iterator: Iterator[Any], lease: _RunGateLease) -> None:
        self._iterator = iterator
        self._lease = lease

    def __iter__(self) -> _LeasedIterator:
        return self

    def __next__(self) -> Any:
        try:
            return next(self._iterator)
        except BaseException:
            self._lease.release()
            raise

    def send(self, value: Any) -> Any:
        try:
            return self._iterator.send(value)  # type: ignore[attr-defined]
        except BaseException:
            self._lease.release()
            raise

    def throw(self, *args: Any) -> Any:
        try:
            return self._iterator.throw(*args)  # type: ignore[attr-defined]
        except BaseException:
            self._lease.release()
            raise

    def close(self) -> None:
        try:
            close = getattr(self._iterator, "close", None)
            if callable(close):
                close()
        finally:
            self._lease.release()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class _LeasedAsyncIterator(AsyncIterator[Any]):
    """Async iterator with the same single-flight lifetime guarantees."""

    def __init__(self, iterator: AsyncIterator[Any], lease: _RunGateLease) -> None:
        self._iterator = iterator
        self._lease = lease

    def __aiter__(self) -> _LeasedAsyncIterator:
        return self

    async def __anext__(self) -> Any:
        try:
            return await self._iterator.__anext__()
        except BaseException:
            self._lease.release()
            raise

    async def asend(self, value: Any) -> Any:
        try:
            return await self._iterator.asend(value)  # type: ignore[attr-defined]
        except BaseException:
            self._lease.release()
            raise

    async def athrow(self, *args: Any) -> Any:
        try:
            return await self._iterator.athrow(*args)  # type: ignore[attr-defined]
        except BaseException:
            self._lease.release()
            raise

    async def aclose(self) -> None:
        try:
            close = getattr(self._iterator, "aclose", None)
            if callable(close):
                await close()
        finally:
            self._lease.release()

    def __del__(self) -> None:
        self._lease.release()


class _ToolScope:
    """Reversible per-run tool visibility, schema, and argument bindings."""

    __slots__ = (
        "_agent",
        "_original_tools",
        "_param_backups",
        "_entrypoint_backups",
        "_restored",
    )

    def __init__(self, agent: Any, original_tools: Any) -> None:
        self._agent = agent
        self._original_tools = original_tools
        # list of (function_object, original_parameters_dict)
        self._param_backups: list[tuple[Any, dict[str, Any]]] = []
        # list of (function_object, original_entrypoint, original_skip_flag)
        self._entrypoint_backups: list[tuple[Any, Any, Any]] = []
        self._restored = False

    def record_param_override(self, function: Any, original_parameters: dict[str, Any]) -> None:
        self._param_backups.append((function, original_parameters))

    def record_entrypoint_binding(
        self, function: Any, original_entrypoint: Any, original_skip: Any
    ) -> None:
        self._entrypoint_backups.append((function, original_entrypoint, original_skip))

    def restore(self) -> None:
        if self._restored:
            return
        self._restored = True
        self._agent.tools = self._original_tools
        # Restore in reverse so repeated overrides of the same object unwind cleanly.
        for function, original_entrypoint, original_skip in reversed(self._entrypoint_backups):
            function.entrypoint = original_entrypoint
            function.skip_entrypoint_processing = original_skip
        for function, original_parameters in reversed(self._param_backups):
            function.parameters = original_parameters


class AgentHarness(_LearningReconciliationMixin, _ContextManagementMixin):
    """Embeddable Agno runtime for short, long, and controllable agent work.

    It composes trusted identity, profiles, model/tool execution, workspace and skills,
    governed capabilities, lifecycle stores, artifacts, context, and learning behind
    one facade. Constructor options intentionally remain explicit; use the named
    :class:`HarnessConfig` profiles and the configuration, embedding, context, learning,
    capability, and lifecycle guides for their prerequisites and guarantees.
    """

    @property
    def _agent(self) -> Agent:
        """Return the run-local Agent when one is active, otherwise the base Agent."""
        active = getattr(self, "_active_agent", None)
        if active is not None:
            run_agent = active.get()
            if run_agent is not None:
                return run_agent
        return self._base_agent

    @_agent.setter
    def _agent(self, value: Agent) -> None:
        self._base_agent = value

    def runtime_manifest(self) -> HarnessRuntimeManifest:
        """Return content-minimized profile and live-resource guarantee evidence."""
        return self._spec.public_manifest()

    def runtime_admission_stats(self) -> dict[str, int | float | None]:
        """Return content-free process admission counters for metrics and health checks."""
        return dict(self._session_lanes.admission_stats)

    @property
    def _prompt_session_id(self) -> str | None:
        active = getattr(self, "_active_agent", None)
        if active is not None and active.get() is not None:
            return self._active_prompt_session.get()
        return self._base_prompt_session_id

    @_prompt_session_id.setter
    def _prompt_session_id(self, value: str | None) -> None:
        active = getattr(self, "_active_agent", None)
        if active is not None and active.get() is not None:
            self._active_prompt_session.set(value)
            return
        self._base_prompt_session_id = value

    @classmethod
    async def create(cls, *args, **kwargs) -> AgentHarness:
        """Async constructor that also runs async provider setup hooks."""
        harness = cls(*args, **kwargs)
        await harness.asetup_context_providers()
        return harness

    def __init__(
        self,
        model: str | Model | AgnoModelFactory | None = None,
        *,
        provider: str | None = None,
        profile: str | RuntimeProfile | None = None,
        cache_prompts: bool | None = None,
        effort: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        workspace_dir: str | Path | None = None,
        allow_workspace_hooks: bool = False,
        workspace_hook_env_allowlist: list[str] | tuple[str, ...] | None = None,
        sandbox_dir: str | Path | None = None,
        sandbox_mode: str | SandboxMode | None = None,
        include_default_tools: bool = True,
        tools: list | None = None,
        capabilities: list | tuple | None = None,
        capability_invoker: CapabilityInvoker | None = None,
        instructions: str | None = None,
        config: HarnessConfig | None = None,
        db=None,
        name: str = "agnoclaw",
        agent_id: str | None = None,
        debug: bool = False,
        # Subagents
        subagents: dict | None = None,
        # Memory options
        enable_user_memory: bool = False,
        enable_learning: bool | None = None,
        learning_mode: str | None = None,
        learning_namespace: str | None = None,
        learning_knowledge: Any | None = None,
        enable_session_context: bool | None = None,
        learning: LearningPolicy | None = None,
        # Context management
        enable_compression: bool | None = None,
        compress_token_limit: int | None = None,
        enable_session_summary: bool | None = None,
        num_history_runs: int | None = None,
        num_history_messages: int | None = None,
        max_tool_calls_from_history: int | None = None,
        max_context_tokens: int | None = None,
        auto_compact_context: bool | None = None,
        context_lock_provider: ContextLockProvider | None = None,
        max_inline_output_chars: int | None = None,
        # Structured output / response parsing
        output_schema: type | dict[str, Any] | None = None,
        parser_model: Any | None = None,
        parser_model_prompt: str | None = None,
        output_model: Any | None = None,
        output_model_prompt: str | None = None,
        parse_response: bool = True,
        structured_outputs: bool | None = None,
        use_json_mode: bool = False,
        # v0.2 runtime contracts
        event_sink: EventSink | None = None,
        event_sink_mode: str | None = None,
        policy_engine: PolicyEngine | None = None,
        policy_fail_open: bool | None = None,
        permission_mode: str | None = None,
        permission_approver: PermissionApprover | None = None,
        permission_require_approver: bool | None = None,
        permission_preapproved_tools: list[str] | tuple[str, ...] | None = None,
        permission_preapproved_categories: list[str] | tuple[str, ...] | None = None,
        backend: RuntimeBackend | None = None,
        runtime_store: RuntimeStore | None = None,
        artifact_store: ArtifactStore | None = None,
        learning_ledger: LearningLedger | None = None,
        learning_promotion_adapter: LearningPromotionAdapter | None = None,
        skill_install_approver: SkillInstallApprover | None = None,
        context_providers: list[ContextProvider] | tuple[ContextProvider, ...] | None = None,
        dependencies: dict[str, Any] | None = None,
        add_dependencies_to_context: bool = False,
        session_state: dict[str, Any] | None = None,
        add_session_state_to_context: bool = False,
        packs: list[str | Path] | tuple[str | Path, ...] | None = None,
        trusted_packs: bool = False,
        pre_run_hooks: list[PreRunHook] | None = None,
        post_run_hooks: list[PostRunHook] | None = None,
        tenant_id: str | None = None,
        org_id: str | None = None,
        team_id: str | None = None,
        roles: list[str] | tuple[str, ...] | None = None,
        scopes: list[str] | tuple[str, ...] | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        context_metadata: dict[str, Any] | None = None,
        # Skills directories — list of (path, trust_level) tuples
        skills_dirs: list[tuple[str | Path, str]] | None = None,
        # Session lifecycle callbacks
        on_compaction: Callable[[str], Awaitable[None]] | None = None,
        on_session_end: Callable[..., Awaitable[None] | None] | None = None,
        # Event enrichment — merged into every HarnessEvent's metadata
        session_metadata: dict[str, Any] | None = None,
        # Legacy compat — use model + provider instead
        model_id: str | None = None,
        extra_tools: list | None = None,
        extra_instructions: str | None = None,
    ):
        # Isolate this harness from later caller/global config mutation.
        source_config, resolved_profile = resolve_runtime_profile(
            config or get_config(),
            profile,
        )
        self.config = (
            source_config.model_copy(deep=True)
            if isinstance(source_config, HarnessConfig)
            else copy.deepcopy(source_config)
        )
        self.profile = resolved_profile
        if subagents and self.profile is not RuntimeProfile.LEGACY:
            raise HarnessError(
                code="RAW_SUBAGENT_LIFECYCLE_UNSUPPORTED",
                category="lifecycle",
                message=(
                    "Named raw subagents do not provide durable child lineage. "
                    "Use DeclaredChildTemplate/declared child runs, or select "
                    "HarnessConfig.legacy() for the temporary compatibility surface."
                ),
                retryable=False,
                details={
                    "profile": self.profile.value,
                    "replacement": "declared_child_runs",
                },
            )
        self._result_ref_keys = _merge_result_ref_keys(
            getattr(self.config, "result_ref_keys", None)
        )
        self.name = name
        self._agent_id = agent_id or name
        self.user_id = user_id
        self.session_id = session_id
        self._roles = tuple(roles or ())
        self._scopes = tuple(scopes or ())
        self._tenant_id = tenant_id
        self._org_id = org_id
        self._team_id = team_id
        self._request_id = request_id
        self._trace_id = trace_id
        self._context_metadata = copy.deepcopy(context_metadata or {})
        self._allow_workspace_hooks = bool(allow_workspace_hooks)
        hook_env_names = tuple(workspace_hook_env_allowlist or ())
        invalid_hook_env_names = [
            name
            for name in hook_env_names
            if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
        ]
        if invalid_hook_env_names:
            raise ValueError(
                "workspace_hook_env_allowlist contains invalid environment variable names"
            )
        self._workspace_hook_env_allowlist = tuple(dict.fromkeys(hook_env_names))
        self._on_compaction = on_compaction
        self._on_session_end = on_session_end
        self._session_metadata = copy.deepcopy(session_metadata or {})
        self._closed = False
        self._active_agent: ContextVar[Agent | None] = ContextVar(
            f"agnoclaw_active_agent_{id(self)}", default=None
        )
        self._active_prompt_session: ContextVar[str | None] = ContextVar(
            f"agnoclaw_prompt_session_{id(self)}", default=None
        )
        self._base_prompt_session_id = None
        self._active_runtime_run_id: ContextVar[str | None] = ContextVar(
            f"agnoclaw_runtime_run_id_{id(self)}", default=None
        )
        self._active_durable_model_loop: ContextVar[bool] = ContextVar(
            f"agnoclaw_durable_model_loop_{id(self)}", default=False
        )
        self._prepared_run_model: ContextVar[
            tuple[Model, OwnedAgnoModelResource] | None
        ] = ContextVar(f"agnoclaw_prepared_run_model_{id(self)}", default=None)
        self._active_runtime_checkpoint_resume: ContextVar[bool] = ContextVar(
            f"agnoclaw_runtime_checkpoint_resume_{id(self)}", default=False
        )
        self._active_runtime_run_gate_owned: ContextVar[bool] = ContextVar(
            f"agnoclaw_runtime_run_gate_owned_{id(self)}", default=False
        )
        # Harness-authored maintenance turns need an unforgeable, task-local label.
        # The label is projected into protected Agno run metadata so archival can
        # distinguish quoted history and maintenance prompts from new user intent.
        self._internal_run_kind: ContextVar[str | None] = ContextVar(
            f"agnoclaw_internal_run_kind_{id(self)}", default=None
        )
        self._runtime_store: RuntimeStore | None = runtime_store
        self._owns_runtime_store = runtime_store is None
        self._artifact_store = artifact_store
        self._context_archive = (
            ArtifactContextArchive(artifact_store) if artifact_store is not None else None
        )
        if context_lock_provider is not None and not isinstance(
            context_lock_provider,
            ContextLockProvider,
        ):
            raise TypeError("context_lock_provider must implement ContextLockProvider")
        if context_lock_provider is not None and not re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            context_lock_provider.identity_digest,
        ):
            raise ValueError(
                "context_lock_provider.identity_digest must be a canonical sha256 digest"
            )
        self._context_lock_provider = context_lock_provider
        self._max_context_tokens = (
            max_context_tokens if max_context_tokens is not None else self.config.max_context_tokens
        )
        self._auto_compact_context = (
            auto_compact_context
            if auto_compact_context is not None
            else self.config.auto_compact_context
        )
        self._max_inline_output_chars = (
            max_inline_output_chars
            if max_inline_output_chars is not None
            else self.config.max_inline_output_chars
        )
        if self._max_inline_output_chars is not None and not (
            isinstance(self._max_inline_output_chars, int)
            and not isinstance(self._max_inline_output_chars, bool)
            and 1_024 <= self._max_inline_output_chars <= 1_000_000
        ):
            raise HarnessError(
                code="OUTPUT_SPILL_LIMIT_INVALID",
                category="configuration",
                message="max_inline_output_chars must be between 1024 and 1000000.",
                retryable=False,
            )
        if self._auto_compact_context and self._max_context_tokens is None:
            raise HarnessError(
                code="CONTEXT_BUDGET_REQUIRED",
                category="configuration",
                message="Automatic context compaction requires max_context_tokens.",
                retryable=False,
            )
        if self._auto_compact_context and self._context_archive is None:
            raise HarnessError(
                code="CONTEXT_ARTIFACT_STORE_REQUIRED",
                category="configuration",
                message="Automatic context compaction requires artifact_store.",
                retryable=False,
            )
        if self._max_inline_output_chars is not None and artifact_store is None:
            raise HarnessError(
                code="OUTPUT_SPILL_ARTIFACT_STORE_REQUIRED",
                category="configuration",
                message="Output spill requires artifact_store.",
                retryable=False,
            )
        self._context_automation = ContextAutomationCoordinator()
        self._context_maintenance_depth: ContextVar[int] = ContextVar(
            f"agnoclaw_context_maintenance_{id(self)}", default=0
        )
        validate_profile_resources(
            profile=self.profile,
            config=self.config,
            runtime_store=runtime_store,
            artifact_store=artifact_store,
            agno_db=db,
        )
        self._learning_ledger = learning_ledger
        self._learning_gateway: LearningGateway | None = None
        self._runtime_worker_id = f"worker_{uuid4().hex}"
        self._operation_gateway: OperationGateway | None = None
        self._capability_operation_gateway: OperationGateway | None = None
        self._capability_registry = CapabilityRegistry()
        for capability in capabilities or ():
            if isinstance(capability, DeclaredChildTemplate):
                capability = capability.capability(self)
            self._capability_registry.register(capability)
        if self._max_inline_output_chars is not None:
            if any(
                spec.name == READ_SPILLED_OUTPUT for spec in self._capability_registry.snapshot()
            ):
                raise HarnessError(
                    code="OUTPUT_SPILL_TOOL_CONFLICT",
                    category="configuration",
                    message=f"'{READ_SPILLED_OUTPUT}' is reserved for output spill paging.",
                    retryable=False,
                )
            self._capability_registry.register(read_capability(lambda: self._read_spilled_output))
        self._capability_invoker = capability_invoker
        self._capability_executor: CapabilityExecutor | None = None
        self._capability_approval_coordinator: DurableApprovalCoordinator | None = None
        self._capability_bindings: tuple[AgnoCapabilityBinding, ...] = ()
        self._capability_tool_map: dict[str, AgnoCapabilityBinding] = {}
        self._active_runtime_claim: ContextVar[RunLeaseClaim | None] = ContextVar(
            f"agnoclaw_runtime_claim_{id(self)}", default=None
        )
        self._active_runtime_context: ContextVar[ExecutionContext | None] = ContextVar(
            f"agnoclaw_runtime_context_{id(self)}", default=None
        )
        self._live_runs: dict[str, asyncio.Task[Any]] = {}
        self._run_results: dict[str, Any] = {}
        self._run_requests: dict[str, dict[str, Any]] = {}
        self._run_resume_events: dict[str, asyncio.Event] = {}
        self._runtime_supervisor_failures: dict[str, BaseException] = {}
        self._shutdown_task: asyncio.Task[None] | None = None
        self._shutdown_policy: RuntimeClosePolicy | None = None
        self._resources_closed = False
        self._sync_lifecycle_lock = threading.Lock()
        self._sync_lifecycle_coordinator: SyncLifecycleCoordinator | None = None
        self._session_lanes = AsyncSessionLanes(
            max_concurrency=self.config.runtime_max_concurrency,
            max_waiting=self.config.runtime_max_waiting,
            max_waiting_per_tenant=self.config.runtime_max_waiting_per_tenant,
            max_waiting_per_session=self.config.runtime_max_waiting_per_session,
            admission_timeout_seconds=self.config.runtime_admission_timeout_seconds,
        )
        raw_lease_seconds = getattr(self.config, "runtime_lease_seconds", 30)
        raw_lease_interval = getattr(
            self.config,
            "runtime_lease_renew_interval_seconds",
            10.0,
        )
        self._runtime_lease_seconds = (
            int(raw_lease_seconds)
            if isinstance(raw_lease_seconds, (int, float))
            and not isinstance(raw_lease_seconds, bool)
            else 30
        )
        self._runtime_lease_interval_seconds = (
            float(raw_lease_interval)
            if isinstance(raw_lease_interval, (int, float))
            and not isinstance(raw_lease_interval, bool)
            else 10.0
        )
        if self._runtime_lease_interval_seconds >= self._runtime_lease_seconds:
            raise ValueError(
                "runtime_lease_renew_interval_seconds must be shorter than runtime_lease_seconds"
            )
        self._run_gate = threading.Lock()
        self._active_skill_lock = threading.Lock()
        self._active_skills: dict[str, ModelSkillActivation] = {}
        self._owning_loop: asyncio.AbstractEventLoop | None = None
        self._context_providers: list[ContextProvider] = list(context_providers or [])
        self._context_provider_tool_map: dict[str, dict[str, str]] = {}
        self._context_providers_setup = False
        self._dependencies = dict(dependencies or {})
        self._add_dependencies_to_context = add_dependencies_to_context
        self._session_state = dict(session_state or {})
        self._add_session_state_to_context = add_session_state_to_context
        self._loaded_packs: list[Any] = []

        # Runtime extension contracts
        self._event_sink: EventSink = event_sink or NullEventSink()
        mode_value = event_sink_mode or self.config.event_sink_mode
        try:
            self._event_sink_mode = EventSinkMode(mode_value)
        except ValueError:
            raise ValueError(
                f"Invalid event_sink_mode={mode_value!r}. "
                f"Use '{EventSinkMode.BEST_EFFORT.value}' or '{EventSinkMode.FAIL_CLOSED.value}'."
            ) from None
        self._policy_engine: PolicyEngine = policy_engine or AllowAllPolicyEngine()
        self._policy_engines: list[tuple[str, PolicyEngine]] = [("harness", self._policy_engine)]
        self._policy_fail_open = (
            policy_fail_open if policy_fail_open is not None else self.config.policy_fail_open
        )
        self._pre_run_hooks: list[PreRunHook] = list(pre_run_hooks or [])
        self._post_run_hooks: list[PostRunHook] = list(post_run_hooks or [])
        self._lifecycle_hooks: dict[str, list[LifecycleHook]] = {}
        permission_mode_value = permission_mode or self.config.permission_mode
        require_approver = (
            permission_require_approver
            if permission_require_approver is not None
            else self.config.permission_require_approver
        )
        self._permission_controller = PermissionController(
            mode=permission_mode_value,
            approver=permission_approver,
            require_approver=require_approver,
            preapproved_tools=tuple(
                permission_preapproved_tools or self.config.permission_preapproved_tools
            ),
            preapproved_categories=tuple(
                permission_preapproved_categories or self.config.permission_preapproved_categories
            ),
        )
        self._plan_mode_restore_permission_mode: PermissionMode | None = None
        self._elevated_session_mode = ElevatedSessionMode.OFF

        # Legacy compat: model_id / extra_tools / extra_instructions
        if model_id is not None:
            warnings.warn(
                "model_id is deprecated, use model instead",
                DeprecationWarning,
                stacklevel=2,
            )
        if extra_tools is not None:
            warnings.warn(
                "extra_tools is deprecated, use tools instead",
                DeprecationWarning,
                stacklevel=2,
            )
        if extra_instructions is not None:
            warnings.warn(
                "extra_instructions is deprecated, use instructions instead",
                DeprecationWarning,
                stacklevel=2,
            )
        _model: str | Model | None = model_id if model is None else cast(Any, model)
        self._model_factory = model if isinstance(model, AgnoModelFactory) else None
        if self._model_factory is not None:
            if provider is not None:
                raise HarnessError(
                    code="MODEL_FACTORY_PROVIDER_CONFLICT",
                    category="configuration",
                    message=(
                        "Declare provider identity on AgnoModelFactory; do not also pass provider=."
                    ),
                    retryable=False,
                )
            if cache_prompts not in (None, False) or effort is not None:
                raise HarnessError(
                    code="MODEL_FACTORY_OPTION_CONFLICT",
                    category="configuration",
                    message=(
                        "A model factory owns provider-specific cache/effort configuration."
                    ),
                    retryable=False,
                )
            _model = self._model_factory.create()
        self._owns_model_transport = (
            model is None or isinstance(model, (str, AgnoModelFactory))
        )
        _tools = tools or extra_tools
        self._legacy_tool_bindings: tuple[LegacyToolBinding, ...] = normalize_legacy_tools(
            tuple(_tools or ())
        )
        _instructions = instructions or extra_instructions

        # Resolve model → Agno-native "provider:model_id" string
        self._model = _resolve_model(_model, provider, self.config)
        _require_model_provider_dependency(self._model)

        # Route "provider:id" specs through the provider adapter registry.
        # Callers state intent only (cache_prompts / effort); each adapter
        # applies its provider's best practice, and providers without an
        # adapter (or without applicable levers) keep the plain spec
        # string. Pre-built model instances pass through untouched.
        _cache_prompts = cache_prompts if cache_prompts is not None else self.config.cache_prompts
        _effort = effort if effort is not None else self.config.model_effort
        if isinstance(self._model, str) and (_cache_prompts or _effort):
            from .models import materialize_model

            self._model = materialize_model(
                self._model, cache_prompts=_cache_prompts, effort=_effort
            )

        # Workspace (with hierarchical parent chain)
        _ws_dir = workspace_dir or self.config.workspace_dir
        self.workspace = Workspace(
            _ws_dir,
            global_dir=self.config.global_workspace_dir,
            project_dir=self.config.project_workspace_dir,
        )
        self.workspace.initialize()
        self._workspace_hook_specs = (
            self.workspace.hook_specs() if self._allow_workspace_hooks else []
        )
        self._load_workspace_lifecycle_hooks()
        self.sandbox_dir = self._resolve_sandbox_dir(sandbox_dir, session_id=session_id)
        effective_backend = backend or RuntimeBackend()
        self._builtin_tool_settings = builtin_tool_settings(
            enabled=include_default_tools,
            config=self.config,
            backend=effective_backend,
            subagents=subagents,
        )
        self._builtin_tool_factory_enabled = bool(
            include_default_tools
            and can_materialize_host_builtin_tools(
                profile=self.profile,
                config=self.config,
                backend=effective_backend,
                subagents=subagents,
            )
        )
        resolved_backend = effective_backend.resolve(workspace_dir=self.workspace.path)
        self._owns_session_command_executor = effective_backend.uses_host_runtime()
        self._sandbox_mode = normalize_sandbox_mode(
            sandbox_mode
            if sandbox_mode is not None
            else self.config.sandbox_mode
            if self.config.sandbox_mode is not None
            else resolved_backend.sandbox_mode
        )
        self._builtin_tool_factory = (
            functools.partial(
                materialize_host_builtin_tools,
                config=self.config.model_copy(deep=True),
                subagents=None,
                workspace_dir=self.workspace.path,
                sandbox_dir=self.sandbox_dir,
                sandbox_mode=self._sandbox_mode,
                harness=self,
            )
            if self._builtin_tool_factory_enabled
            else None
        )
        self._session_command_executor = resolved_backend.command_executor
        self._elevated_command_executor = LocalCommandExecutor(workspace_dir=self.workspace.path)
        self._owned_sync_resources: list[Any] = [self._elevated_command_executor]
        if self._owns_session_command_executor:
            self._owned_sync_resources.append(self._session_command_executor)
        self._ensure_sandbox_dir()
        resolved_skill_runtime_backend = resolved_backend.skill_runtime
        self._guardrails = RuntimeGuardrails(
            workspace_dir=self.workspace.path,
            enabled=self.config.guardrails_enabled,
            path_enabled=self.config.path_guardrails_enabled,
            path_allowed_roots=self.config.path_allowed_roots,
            path_blocked_roots=self.config.path_blocked_roots,
            network_enabled=self.config.network_enabled,
            network_enforce_https=self.config.network_enforce_https,
            network_allowed_hosts=self.config.network_allowed_hosts,
            network_blocked_hosts=self.config.network_blocked_hosts,
            network_block_private_hosts=self.config.network_block_private_hosts,
            network_block_in_bash=self.config.network_block_in_bash,
        )

        # Skills registry
        self.skills = SkillRegistry(
            self.workspace.skills_dir(),
            runtime_backend=resolved_skill_runtime_backend,
            install_approver=skill_install_approver,
            working_dir=self.workspace.path,
        )
        if skills_dirs:
            for path, trust in skills_dirs:
                self.skills.add_directory(path, trust=trust)

        pack_tools: list[Any] = []
        extension_tool_bindings: list[LegacyToolBinding] = []
        if packs:
            from .packs import load_pack

            for pack_path in packs:
                loaded_pack = load_pack(pack_path, trusted=trusted_packs)
                self._loaded_packs.append(loaded_pack)
                for capability in loaded_pack.capabilities:
                    self._capability_registry.register(capability)
                for skills_dir in loaded_pack.skills_dirs:
                    requested_trust = loaded_pack.manifest.trust.default
                    skill_trust = (
                        "local"
                        if loaded_pack.trusted and requested_trust == "local"
                        else "community"
                    )
                    self.skills.add_directory(skills_dir, trust=skill_trust)
                pack_tools.extend(loaded_pack.tools)
                self._pre_run_hooks.extend(
                    self._wrap_pack_pre_hook(loaded_pack.manifest.name, hook)
                    for hook in loaded_pack.pre_run_hooks
                )
                self._post_run_hooks.extend(
                    self._wrap_pack_post_hook(loaded_pack.manifest.name, hook)
                    for hook in loaded_pack.post_run_hooks
                )
                for event_type, hooks in loaded_pack.lifecycle_hooks.items():
                    self._lifecycle_hooks.setdefault(event_type, []).extend(
                        self._wrap_pack_lifecycle_hook(
                            loaded_pack.manifest.name,
                            event_type,
                            hook,
                        )
                        for hook in hooks
                    )

                self._context_providers.extend(loaded_pack.context_providers)
                self._policy_engines.extend(
                    (f"pack:{loaded_pack.manifest.name}", policy) for policy in loaded_pack.policies
                )
                if loaded_pack.policies and not isinstance(
                    self._policy_engine, AllowAllPolicyEngine
                ):
                    logger.info(
                        "Pack %s provided %d policy engine(s); they will run after the "
                        "explicit harness policy engine.",
                        loaded_pack.manifest.name,
                        len(loaded_pack.policies),
                    )
            extension_tool_bindings.extend(
                normalize_legacy_tools(tuple(pack_tools), source="packs")
            )

        # System prompt builder
        self._prompt_builder = SystemPromptBuilder(
            self.workspace.path,
            sandbox_dir=self.sandbox_dir,
            sandbox_mode=self._sandbox_mode.value,
        )

        # Context budget monitoring
        # Build tool list (pass through named subagent definitions)
        _all_tools = []
        default_tool_ids: set[int] = set()
        if include_default_tools:

            def _wrap_command_executor(executor: CommandExecutor) -> CommandExecutor:
                wrapped = _ElevatedSessionCommandExecutor(
                    harness=self,
                    sandbox_executor=executor,
                    host_executor=self._elevated_command_executor,
                    owns_sandbox_executor=self._owns_session_command_executor,
                )
                self._owned_sync_resources.append(wrapped)
                return wrapped

            default_tools = get_default_tools(
                self.config,
                subagents=subagents,
                workspace_dir=self.workspace.path,
                sandbox_dir=self.sandbox_dir,
                sandbox_mode=self._sandbox_mode,
                backend=effective_backend,
                command_executor_wrapper=_wrap_command_executor,
                network_policy=self._guardrails,
            )
            self._owned_sync_resources.extend(default_tools)
            default_tool_ids = {id(tool) for tool in default_tools}
            _all_tools = list(default_tools)
            extension_tool_bindings.extend(
                normalize_legacy_tools(
                    tuple(
                        tool
                        for tool in default_tools
                        if isinstance(tool, Toolkit) and not toolkit_functions(tool)
                    ),
                    source="dynamic_default_toolkits",
                )
            )
        if _tools:
            _all_tools.extend(_tools)
        if pack_tools:
            _all_tools.extend(pack_tools)
        if self._context_providers:
            provider_tools = self._collect_context_provider_tools(
                self._context_providers,
                existing_tools=_all_tools,
            )
            _all_tools.extend(provider_tools)
            extension_tool_bindings.extend(
                normalize_legacy_tools(tuple(provider_tools), source="context_providers")
            )

        # Plugins
        self._plugin_loader = None
        if self.config.enable_plugins:
            from .plugins import PluginLoader

            self._plugin_loader = PluginLoader()
            manifests = self._plugin_loader.discover()
            # Also load explicitly configured plugin paths
            for path in self.config.plugin_paths:
                self._plugin_loader.load_from_path(path)

            # Merge plugin contributions
            plugin_tools = self._plugin_loader.get_all_tools()
            _all_tools.extend(plugin_tools)
            extension_tool_bindings.extend(
                normalize_legacy_tools(tuple(plugin_tools), source="plugins")
            )
            for capability in self._plugin_loader.get_all_capabilities():
                self._capability_registry.register(capability)
            for plugin_skill_dir in self._plugin_loader.get_all_skills_dirs():
                self.skills.add_directory(plugin_skill_dir, trust="community")
            self._pre_run_hooks.extend(self._plugin_loader.get_all_pre_run_hooks())
            self._post_run_hooks.extend(self._plugin_loader.get_all_post_run_hooks())

            if manifests:
                logger.info(
                    "Loaded %d plugin(s): %s",
                    len(manifests),
                    ", ".join(m.name for m in manifests),
                )

        # Agno 2.9 owns progressive skill-tool disclosure inside its model loop;
        # agnoclaw supplies the trust-filtered adapter and lifecycle hooks. Keep the
        # surface tied to the default harness tool suite so
        # include_default_tools=False remains an exact opt-out.
        self._agno_skills: AgnoClawSkills | None = (
            AgnoClawSkills(
                self.skills,
                on_activation=self._activate_model_skill,
                prepare_function=self._prepare_model_skill_function,
                enabled=self._model_skill_tools_enabled,
            )
            if include_default_tools
            else None
        )

        self._capability_bindings = build_agno_capability_bindings(
            self._capability_registry.snapshot(),
            invoke=self._invoke_agno_capability,
        )
        existing_tool_names = self._tool_names(_all_tools)
        for binding in self._capability_bindings:
            if binding.tool_name in existing_tool_names:
                raise HarnessError(
                    code="CAPABILITY_TOOL_NAME_CONFLICT",
                    category="configuration",
                    message=(
                        f"Capability tool '{binding.tool_name}' conflicts with an "
                        "existing Agno tool."
                    ),
                    retryable=False,
                    details={"capability": binding.spec.name},
                )
            existing_tool_names.add(binding.tool_name)
            self._capability_tool_map[binding.tool_name] = binding
            if binding.spec.kind is CapabilityKind.CONTEXT_PROVIDER:
                self._context_provider_tool_map[binding.tool_name] = {
                    "provider_id": binding.spec.name.removeprefix("query_"),
                    "provider_name": binding.spec.name.removeprefix("query_"),
                    "operation": "query",
                }
            _all_tools.append(binding.function)

        self._extension_tool_bindings = tuple(extension_tool_bindings)

        self._attach_tool_runtime_hooks(_all_tools)
        self._plan_signal_toolkit: PlanSignalToolkit = (
            self._find_plan_signal_toolkit(_all_tools) or PlanSignalToolkit()
        )

        # Resolve learning intent before building the system prompt. The explicit
        # v0.12 policy is mutually exclusive with storage-shaped legacy flags.
        legacy_learning_requested = bool(
            enable_user_memory
            or enable_learning is not None
            or enable_session_context is not None
            or learning_mode is not None
            or learning_namespace is not None
            or learning_knowledge is not None
            or self.config.enable_learning
            or self.config.enable_session_context
        )
        if learning is not None and not isinstance(learning, LearningPolicy):
            raise HarnessError(
                code="LEARNING_POLICY_INVALID",
                category="learning",
                message="learning must be a LearningPolicy created by LearningProfile.",
                retryable=False,
                details={"parameter": "learning"},
            )
        if learning is not None and legacy_learning_requested:
            raise HarnessError(
                code="LEARNING_CONFIGURATION_CONFLICT",
                category="learning",
                message=(
                    "learning=LearningPolicy cannot be combined with legacy learning "
                    "booleans, modes, namespaces, or knowledge parameters."
                ),
                retryable=False,
                details={"parameter": "learning"},
            )
        self._learning_policy = learning if learning is not None and learning.enabled else None
        if learning_ledger is not None and self._learning_policy is None:
            raise HarnessError(
                code="LEARNING_LEDGER_POLICY_REQUIRED",
                category="learning",
                message="learning_ledger requires an enabled explicit learning policy.",
                retryable=False,
                details={"parameter": "learning_ledger"},
            )
        if learning_ledger is not None and artifact_store is None:
            raise HarnessError(
                code="LEARNING_ARTIFACT_STORE_REQUIRED",
                category="learning",
                message="learning_ledger requires artifact_store for candidate content.",
                retryable=False,
                details={"parameter": "artifact_store"},
            )
        if learning_promotion_adapter is not None and learning_ledger is None:
            raise HarnessError(
                code="LEARNING_LEDGER_REQUIRED",
                category="learning",
                message="learning_promotion_adapter requires learning_ledger.",
                retryable=False,
                details={"parameter": "learning_ledger"},
            )

        _enable_learning = (
            enable_learning if enable_learning is not None else self.config.enable_learning
        )
        _learning_mode = learning_mode or self.config.learning_mode
        _enable_session_context = (
            enable_session_context
            if enable_session_context is not None
            else self.config.enable_session_context
        )
        if _enable_learning or enable_user_memory or _enable_session_context:
            warnings.warn(
                "enable_learning, enable_user_memory, enable_session_context, "
                "learning_mode, learning_namespace, and learning_knowledge are the "
                "legacy direct-Agno adapter and will be removed no earlier than "
                "agnoclaw 0.14.0. Use learning=LearningProfile.*(...).",
                DeprecationWarning,
                stacklevel=2,
            )

        # Persist prompt options so per-run skill injection can be one-shot
        self._extra_instructions = _instructions
        self._include_learning = bool(
            self._learning_policy
            or _enable_learning
            or enable_user_memory
            or _enable_session_context
        )
        self._plan_mode = False

        # Assemble system prompt (skills are injected per-run, then reset).
        # Split-prompt cache mode (duck-typed: any model exposing Agno's
        # cache_system_prompt + system_prompt_blocks contract): the
        # volatile "# Runtime" trailer moves to an uncached trailing
        # system block so the stable prefix stays byte-identical across
        # rebuilds and sessions — see _split_prompt_cache_mode.
        self._prompt_session_id: str | None = session_id
        self._caller_system_blocks: Any = None
        if self._split_prompt_cache_mode():
            # Preserve caller-configured blocks: our callable composes
            # them ahead of the runtime block instead of dropping them.
            self._caller_system_blocks = getattr(self._model, "system_prompt_blocks", None)
            system_prompt = self._build_system_prompt(session_id=session_id, include_runtime=False)
            cast(Any, self._model).system_prompt_blocks = self._runtime_system_blocks
        else:
            system_prompt = self._build_system_prompt(session_id=session_id)

        # Storage backend
        provided_db = db
        db = provided_db if provided_db is not None else _make_db(self.config)
        self._owns_storage = provided_db is None
        self._finalizer = weakref.finalize(
            self,
            AgentHarness._finalize_resources,
            db if self._owns_storage else None,
            str(self.sandbox_dir) if sandbox_dir is None else None,
            self._owned_sync_resources,
        )

        # Unified per-user and institutional learning
        learning_machine = None
        if _enable_learning or enable_user_memory or _enable_session_context:
            from .memory import build_learning_machine

            learning_machine = build_learning_machine(
                db=db,
                namespace=learning_namespace or name,
                mode=_learning_mode,
                enable_user_memory=enable_user_memory,
                enable_session_context=_enable_session_context,
                enable_institutional_learning=_enable_learning,
                knowledge=learning_knowledge,
            )
        self._learning_db = db
        if learning_ledger is not None and artifact_store is not None:
            promotion_adapter = learning_promotion_adapter
            if promotion_adapter is None:
                policy = self._learning_policy
                if policy is None:  # pragma: no cover - constructor invariant
                    raise AssertionError("learning ledger without explicit policy")

                def _machine_for_candidate(candidate: LearningCandidate) -> Any:
                    from .memory import build_learning_machine

                    return build_learning_machine(
                        db=self._learning_db,
                        policy=policy,
                        scope=LearningScope(
                            tenant_id=candidate.tenant_id,
                            org_id=None,
                            agent_id=self._agent_id,
                            user_id=None,
                            session_id=None,
                            namespace=policy.namespace,
                            storage_namespace=candidate.storage_namespace,
                            storage_user_id=None,
                            storage_session_id=None,
                            retention_days=policy.retention_days,
                            consented=False,
                        ),
                    )

                promotion_adapter = AgnoLearningPromotionAdapter(_machine_for_candidate)
            self._learning_gateway = LearningGateway(
                learning_ledger,
                artifact_store,
                promotion_adapter=promotion_adapter,
            )

        # Context compression
        _enable_compression = (
            enable_compression if enable_compression is not None else self.config.enable_compression
        )
        _compress_token_limit = compress_token_limit or self.config.compress_token_limit
        compression_manager = None
        self._compression_manager_factory: Callable[[], Any] | None = None
        if _enable_compression:
            from agno.compression.manager import CompressionManager

            if _compress_token_limit:
                self._compression_manager_factory = functools.partial(
                    CompressionManager,
                    compress_token_limit=_compress_token_limit,
                )
            else:
                self._compression_manager_factory = CompressionManager
            compression_manager = self._compression_manager_factory()

        # Session summaries
        _enable_session_summary = (
            enable_session_summary
            if enable_session_summary is not None
            else self.config.enable_session_summary
        )
        session_summary_manager = None
        self._session_summary_manager_factory: Callable[[], Any] | None = None
        if _enable_session_summary:
            from agno.session import SessionSummaryManager

            self._session_summary_manager_factory = SessionSummaryManager
            session_summary_manager = self._session_summary_manager_factory()

        # Agno 2.9 can persist a complete tool-result batch before the next
        # provider call. Older supported releases omit the constructor option,
        # so this is capability-detected rather than version-guessed.
        agno_compatibility = inspect_agno_compatibility()
        self._agno_tool_batch_checkpoint_enabled = bool(
            agno_compatibility.has(AgnoFeature.TOOL_BATCH_CHECKPOINT)
            and db is not None
            and runtime_store is not None
            and artifact_store is not None
        )

        # Core Agno Agent — model accepted as "provider:model_id" string
        self._agent_constructor = Agent
        self._agent_blueprint = dict(
            model=self._model,
            name=name,
            id=agent_id,
            system_message=system_prompt,
            tools=_all_tools,
            db=db,
            session_id=session_id,
            user_id=user_id,
            dependencies=self._dependencies or None,
            add_dependencies_to_context=self._add_dependencies_to_context,
            session_state=self._session_state or None,
            add_session_state_to_context=self._add_session_state_to_context,
            add_history_to_context=True,
            num_history_runs=num_history_runs or self.config.session_history_runs,
            num_history_messages=num_history_messages,
            max_tool_calls_from_history=max_tool_calls_from_history,
            output_schema=output_schema,
            parser_model=parser_model,
            parser_model_prompt=parser_model_prompt,
            output_model=output_model,
            output_model_prompt=output_model_prompt,
            parse_response=parse_response,
            structured_outputs=structured_outputs,
            use_json_mode=use_json_mode,
            skills=self._agno_skills,
            markdown=True,
            debug_mode=debug or self.config.debug,
            # Unified memory via LearningMachine (per-user + institutional)
            learning=learning_machine,
            # A full system_message makes Agno bypass its automatic learning
            # renderer. AgnoClaw therefore renders the same public LearningMachine
            # surfaces once, before prompt policy, while Agno retains capture/tools.
            add_learnings_to_context=False,
            # Context window management
            compress_tool_results=_enable_compression,
            compression_manager=compression_manager,
            # Session continuity
            enable_session_summaries=_enable_session_summary,
            session_summary_manager=session_summary_manager,
            add_session_summary_to_context=_enable_session_summary,
        )
        if self._agno_tool_batch_checkpoint_enabled:
            self._agent_blueprint["checkpoint"] = "tool-batch"
        self._agent = self._agent_constructor(**self._agent_blueprint)
        owned_model = getattr(self._agent, "model", None)
        if self._owns_model_transport and owned_model is not None:
            self._owned_sync_resources.append(OwnedAgnoModelResource(owned_model))
        capability_function_ids = {id(binding.function) for binding in self._capability_bindings}
        self._has_non_capability_tools = any(
            id(tool) not in capability_function_ids for tool in _all_tools
        )
        self._base_default_tool_ids = frozenset(default_tool_ids)
        self._has_unmaterialized_agent_tools = any(
            id(tool) not in capability_function_ids
            and not (self._builtin_tool_factory_enabled and id(tool) in default_tool_ids)
            for tool in _all_tools
        )
        self._materialized_base_tool_names = frozenset(
            self._tool_names(
                [
                    tool
                    for tool in _all_tools
                    if id(tool) in capability_function_ids
                    or (self._builtin_tool_factory_enabled and id(tool) in default_tool_ids)
                ]
            )
        )
        model_materializer = (
            run_factory_materializer(
                resource_id="model",
                resource_type=callable_resource_type(self._model_factory.factory),
                factory=functools.partial(
                    materialize_factory_value,
                    self._model_factory.create,
                ),
            )
            if self._model_factory is not None
            else classify_resource("model", self._model, profile=self.profile.value)
        )
        materializers = [
            model_materializer,
            host_managed_resource(
                "agno_db",
                db,
                lifetime=ResourceLifetime.PROCESS_POOL,
                recovery=ResourceRecovery.LIVE_ONLY,
            ),
        ]
        if runtime_store is not None:
            materializers.append(
                host_managed_resource(
                    "runtime_store",
                    runtime_store,
                    lifetime=ResourceLifetime.PROCESS_POOL,
                    recovery=ResourceRecovery.RECONCILABLE,
                )
            )
        if artifact_store is not None:
            materializers.append(
                host_managed_resource(
                    "artifact_store",
                    artifact_store,
                    lifetime=ResourceLifetime.PROCESS_POOL,
                    recovery=ResourceRecovery.RECONCILABLE,
                )
            )
        if context_lock_provider is not None:
            materializers.append(
                host_managed_resource(
                    "context_lock_provider",
                    context_lock_provider,
                    lifetime=ResourceLifetime.PROCESS_POOL,
                    recovery=ResourceRecovery.RECREATABLE,
                )
            )
        if learning_ledger is not None:
            materializers.append(
                host_managed_resource(
                    "learning_ledger",
                    learning_ledger,
                    lifetime=ResourceLifetime.PROCESS_POOL,
                    recovery=ResourceRecovery.RECONCILABLE,
                )
            )
        if self._learning_policy is not None and self._learning_policy.knowledge is not None:
            materializers.append(
                host_managed_resource(
                    "learning_knowledge",
                    self._learning_policy.knowledge,
                    lifetime=ResourceLifetime.PROCESS_POOL,
                    recovery=ResourceRecovery.RECONCILABLE,
                )
            )
        if self._builtin_tool_factory is not None:
            materializers.append(
                run_factory_materializer(
                    resource_id="builtin_tools",
                    resource_type="agnoclaw.runtime.builtin_materialization.BuiltinToolBundle",
                    factory=self._builtin_tool_factory,
                )
            )
        for binding in self._capability_bindings:
            materializers.append(
                run_factory_materializer(
                    resource_id=f"capability:{binding.reference}",
                    resource_type="agno.tools.function.Function",
                    factory=functools.partial(
                        self._materialize_capability_function,
                        binding.spec,
                    ),
                )
            )
        for index, tool in enumerate(_all_tools):
            if id(tool) in capability_function_ids:
                continue
            if self._builtin_tool_factory_enabled and id(tool) in default_tool_ids:
                continue
            materializers.append(
                classify_resource(
                    f"tool:{index}:{self._single_tool_name(tool) or tool.__class__.__name__}",
                    tool,
                    profile=self.profile.value,
                )
            )
        for key, value in sorted(self._dependencies.items()):
            materializers.append(
                classify_resource(
                    f"dependency:{key}",
                    value,
                    profile=self.profile.value,
                )
            )
        for key, value in sorted(self._session_state.items()):
            materializers.append(
                classify_resource(
                    f"session_state:{key}",
                    value,
                    profile=self.profile.value,
                )
            )
        if self._compression_manager_factory is not None:
            materializers.append(
                run_factory_materializer(
                    resource_id="compression_manager",
                    resource_type=callable_resource_type(self._compression_manager_factory),
                    factory=functools.partial(
                        materialize_factory_value,
                        self._compression_manager_factory,
                    ),
                )
            )
        if self._session_summary_manager_factory is not None:
            materializers.append(
                run_factory_materializer(
                    resource_id="session_summary_manager",
                    resource_type=callable_resource_type(self._session_summary_manager_factory),
                    factory=functools.partial(
                        materialize_factory_value,
                        self._session_summary_manager_factory,
                    ),
                )
            )
        self._resource_materializers = tuple(materializers)
        serialized_agent_inputs = any(
            item.descriptor.concurrency is ResourceConcurrency.SERIALIZED
            and item.descriptor.resource_id.startswith(("dependency:", "session_state:"))
            for item in self._resource_materializers
        )
        self._run_agent_factory_enabled = (
            (isinstance(self._model, str) or self._model_factory is not None)
            and learning_machine is None
            and parser_model is None
            and output_model is None
            and (output_schema is None or isinstance(output_schema, (dict, type)))
            and not serialized_agent_inputs
            and not self._has_unmaterialized_agent_tools
        )
        self._isolated_agent_factory_enabled = (
            self._run_agent_factory_enabled and not self._has_non_capability_tools
        )
        self._durable_model_loop_enabled = bool(
            self._agno_tool_batch_checkpoint_enabled
            and self._agent_constructor is _AGNO_AGENT_TYPE
            and self._isolated_agent_factory_enabled
            and parser_model is None
            and output_model is None
        )
        self._spec = compile_harness_spec(
            harness_name=name,
            agent_id=agent_id,
            profile=self.profile.value,
            settings={
                "model": self.model_name,
                "model_factory_digest": (
                    self._model_factory.implementation_digest
                    if self._model_factory is not None
                    else None
                ),
                "workspace_dir": str(self.workspace.path),
                "sandbox_mode": self._sandbox_mode.value,
                "include_default_tools": include_default_tools,
                "builtin_tools": self._builtin_tool_settings,
                "learning": {
                    **(
                        self._learning_policy.descriptor()
                        if self._learning_policy is not None
                        else {
                            "adapter": "legacy_direct_agno",
                            "user_memory": enable_user_memory,
                            "institutional": bool(_enable_learning),
                            "session_context": bool(_enable_session_context),
                            "mode": _learning_mode,
                            "namespace": learning_namespace or name,
                        }
                    ),
                    "candidate_gateway": self._learning_gateway is not None,
                },
                "context": {
                    "compression": bool(_enable_compression),
                    "compress_token_limit": _compress_token_limit,
                    "session_summary": bool(_enable_session_summary),
                    "history_runs": num_history_runs or self.config.session_history_runs,
                    "history_messages": num_history_messages,
                    "history_tool_calls": max_tool_calls_from_history,
                    "max_context_tokens": self._max_context_tokens,
                    "auto_compact": self._auto_compact_context,
                    "lock_provider_digest": (
                        context_lock_provider.identity_digest
                        if context_lock_provider is not None
                        else None
                    ),
                    "agno_tool_batch_checkpoint": self._agno_tool_batch_checkpoint_enabled,
                },
                "output": {
                    "schema": (
                        output_schema
                        if isinstance(output_schema, dict)
                        else getattr(output_schema, "__qualname__", None)
                    ),
                    "parse_response": parse_response,
                    "structured_outputs": structured_outputs,
                    "json_mode": use_json_mode,
                    "max_inline_chars": self._max_inline_output_chars,
                },
                "capabilities": [binding.spec.manifest() for binding in self._capability_bindings],
                "legacy_tools": [binding.manifest() for binding in self._legacy_tool_bindings],
                "extension_tools": [
                    binding.manifest() for binding in self._extension_tool_bindings
                ],
            },
            materializers=self._resource_materializers,
        )
        # Per-run tool step tracking for progress events and duration metrics.
        self._tool_step_state: dict[str, dict[str, Any]] = {}

    def _collect_context_provider_tools(
        self,
        providers: list[Any],
        *,
        existing_tools: list[Any] | None = None,
    ) -> list[Any]:
        """Return provider tools and reject ambiguous tool names up front."""
        tools: list[Any] = []
        existing_names = self._tool_names(list(existing_tools or []))
        for provider in providers:
            if not isinstance(provider, ContextProvider):
                raise TypeError(
                    f"context provider {provider!r} must be an instance of "
                    f"agno.context.provider.ContextProvider"
                )
            provider_tools = list(provider.get_tools() or [])
            provider_id = (provider.id or provider.name or "").strip()
            provider_name = (provider.name or provider_id).strip()
            query_name = str(provider.query_tool_name or "")
            update_name = str(provider.update_tool_name or "")

            for tool in provider_tools:
                for tool_name in self._tool_names([tool]):
                    if tool_name in existing_names:
                        raise ValueError(f"Duplicate context provider tool name: {tool_name!r}")
                    existing_names.add(tool_name)
                    operation = "query"
                    if tool_name == update_name:
                        operation = "update"
                    elif tool_name == query_name:
                        operation = "query"
                    self._context_provider_tool_map[tool_name] = {
                        "provider_id": provider_id or provider_name or tool_name,
                        "provider_name": provider_name or provider_id or tool_name,
                        "operation": operation,
                    }
            tools.extend(provider_tools)
        return tools

    @staticmethod
    def _tool_names(tools: list[Any]) -> set[str]:
        names: set[str] = set()
        for tool in tools:
            if isinstance(tool, Toolkit):
                names.update(str(name) for name in toolkit_functions(tool))
            elif isinstance(tool, Function):
                if tool.name:
                    names.add(str(tool.name))
            else:
                name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
                if name:
                    names.add(str(name))
        return names

    @staticmethod
    def _single_tool_name(tool: Any) -> str | None:
        """Return the advertised name of a standalone (non-Toolkit) tool."""
        if isinstance(tool, Function):
            return str(tool.name) if tool.name else None
        name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
        return str(name) if name else None

    def _resolve_function_objects(self, tools: list[Any]) -> dict[str, Any]:
        """Map advertised names to live mutable Function objects."""
        resolved: dict[str, Any] = {}
        for tool in tools:
            if isinstance(tool, Toolkit):
                for name, function in toolkit_functions(tool).items():
                    resolved.setdefault(str(name), function)
            elif isinstance(tool, Function):
                if tool.name:
                    resolved.setdefault(str(tool.name), tool)
        return resolved

    def _apply_tool_scope(
        self,
        *,
        allowed: list[str] | None = None,
        schema_overrides: dict[str, dict[str, Any]] | None = None,
        arg_bindings: dict[str, dict[str, Any]] | None = None,
    ) -> _ToolScope | None:
        """Apply one reversible run-local tool view, or return None when unchanged."""
        allowed_present = allowed is not None
        overrides_present = bool(schema_overrides)
        bindings_present = bool(arg_bindings)
        if not allowed_present and not overrides_present and not bindings_present:
            return None

        original_tools = self._agent.tools
        scope = _ToolScope(self._agent, original_tools)

        current: list[Any] = list(original_tools) if isinstance(original_tools, list) else []
        if allowed_present:
            allowed_set = {str(name) for name in (allowed or [])}
            scoped: list[Any] = []
            seen: set[str] = set()
            for tool in current:
                if isinstance(tool, Toolkit):
                    for raw_name, function in toolkit_functions(tool).items():
                        fn_name = str(raw_name)
                        if fn_name in allowed_set and fn_name not in seen:
                            seen.add(fn_name)
                            scoped.append(function)
                else:
                    tool_name = self._single_tool_name(tool)
                    if tool_name is not None and tool_name in allowed_set and tool_name not in seen:
                        seen.add(tool_name)
                        scoped.append(tool)
            working = scoped
            self._agent.tools = scoped
        else:
            working = current

        by_name = (
            self._resolve_function_objects(working) if overrides_present or bindings_present else {}
        )

        if overrides_present:
            for override_name, schema in (schema_overrides or {}).items():
                target_fn = by_name.get(str(override_name))
                if target_fn is None or not hasattr(target_fn, "parameters"):
                    continue
                scope.record_param_override(target_fn, target_fn.parameters)
                # Deep-copy so the advertised schema can't be mutated in place by
                # Agno's per-run processing (which sets additionalProperties/required).
                target_fn.parameters = copy.deepcopy(schema)

        if bindings_present:
            for bind_name, values in (arg_bindings or {}).items():
                if not values:
                    continue
                target_fn = by_name.get(str(bind_name))
                if target_fn is None or not hasattr(target_fn, "parameters"):
                    continue
                original_entrypoint = getattr(target_fn, "entrypoint", None)
                if original_entrypoint is None:
                    continue
                self._bind_tool_args(scope, target_fn, dict(values))

        return scope

    @staticmethod
    def _schema_has_properties(schema: Any) -> bool:
        return isinstance(schema, dict) and bool(schema.get("properties"))

    def _bind_tool_args(
        self,
        scope: _ToolScope,
        target_fn: Any,
        values: dict[str, Any],
    ) -> None:
        """Bind hidden run-local arguments and record the reversible mutation."""
        original_entrypoint = target_fn.entrypoint
        original_skip = getattr(target_fn, "skip_entrypoint_processing", False)

        # Resolve the baseline schema to strip from. A tool whose schema was
        # already specialized this run (schema_overrides) carries it on
        # ``parameters``; an untouched toolkit tool has an empty default schema
        # until Agno processes it per-run, so generate it on a throwaway copy.
        current_params = target_fn.parameters
        if self._schema_has_properties(current_params):
            baseline = copy.deepcopy(current_params)
        else:
            baseline = None
            try:
                probe = target_fn.model_copy(deep=True)
                probe.skip_entrypoint_processing = False
                probe.process_entrypoint()
                if self._schema_has_properties(probe.parameters):
                    baseline = copy.deepcopy(probe.parameters)
            except Exception:
                logger.debug("Could not derive schema for tool binding", exc_info=True)
            if baseline is None:
                baseline = (
                    copy.deepcopy(current_params)
                    if isinstance(current_params, dict)
                    else {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    }
                )

        props = baseline.get("properties")
        if isinstance(props, dict):
            for arg in values:
                props.pop(arg, None)
        required = baseline.get("required")
        if isinstance(required, list):
            baseline["required"] = [name for name in required if name not in values]

        scope.record_param_override(target_fn, current_params)
        scope.record_entrypoint_binding(target_fn, original_entrypoint, original_skip)
        target_fn.parameters = baseline
        target_fn.entrypoint = functools.partial(original_entrypoint, **values)
        # Keep the stripped schema verbatim — Agno's process_entrypoint would
        # otherwise recompute `required` from the (now partial) signature.
        target_fn.skip_entrypoint_processing = True

    @staticmethod
    def _restore_tool_scope(scope: _ToolScope | None) -> None:
        """Restore a toolset previously scoped by :meth:`_apply_tool_scope`."""
        if scope is not None:
            scope.restore()

    @staticmethod
    def _skill_tool_scope_args(
        skill_obj: Any,
        tool_schema_overrides: dict[str, dict[str, Any]] | None,
        tool_arg_bindings: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[
        list[str] | None,
        dict[str, dict[str, Any]] | None,
        dict[str, dict[str, Any]] | None,
    ]:
        """Resolve skill and run-local restrictions, schemas, and bindings."""
        allowed: list[str] | None = None
        skill_schemas: dict[str, dict[str, Any]] = {}
        skill_bindings: dict[str, dict[str, Any]] = {}
        if skill_obj is not None:
            meta = getattr(skill_obj, "meta", None)
            if meta is not None:
                if getattr(meta, "allowed_tools", None):
                    allowed = list(meta.allowed_tools)
                if getattr(meta, "tool_schemas", None):
                    skill_schemas = dict(meta.tool_schemas)
                if getattr(meta, "tool_arg_bindings", None):
                    skill_bindings = dict(meta.tool_arg_bindings)
        overrides = {**skill_schemas, **(tool_schema_overrides or {})}
        bindings = {**skill_bindings, **(tool_arg_bindings or {})}
        return allowed, (overrides or None), (bindings or None)

    @staticmethod
    def _merge_run_mapping(
        base: dict[str, Any] | None,
        override: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Merge run-local values over immutable construction defaults."""
        if not base and not override:
            return None
        merged = dict(base or {})
        if override:
            merged.update(override)
        return merged or None

    def _resolve_run_context_kwargs(
        self,
        *,
        dependencies: dict[str, Any] | None,
        session_state: dict[str, Any] | None,
        add_dependencies_to_context: bool | None,
        add_session_state_to_context: bool | None,
        knowledge_filters: dict[str, Any] | list[Any] | None,
    ) -> dict[str, Any]:
        """Build isolated per-run state kwargs forwarded to Agno."""
        resolved: dict[str, Any] = {}
        if dependencies is not None:
            resolved["dependencies"] = self._merge_run_mapping(self._dependencies, dependencies)
        if session_state is not None:
            resolved["session_state"] = self._merge_run_mapping(self._session_state, session_state)
        if add_dependencies_to_context is not None:
            resolved["add_dependencies_to_context"] = add_dependencies_to_context
        if add_session_state_to_context is not None:
            resolved["add_session_state_to_context"] = add_session_state_to_context
        if knowledge_filters is not None:
            resolved["knowledge_filters"] = knowledge_filters
        return resolved

    @staticmethod
    def _find_plan_signal_toolkit(tools: list[Any]) -> PlanSignalToolkit | None:
        for tool in tools:
            if isinstance(tool, PlanSignalToolkit):
                return tool
        return None

    def _context_provider_instructions(self) -> str | None:
        if not self._context_providers:
            return None
        lines = ["## External Context Providers"]
        for provider in self._context_providers:
            text = str(provider.instructions()).strip()
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines)

    async def asetup_context_providers(self) -> None:
        """Run async setup for registered context providers once."""
        if self._context_providers_setup:
            return
        for provider in self._context_providers:
            await provider.asetup()
        self._context_providers_setup = True

    @staticmethod
    def _close_storage_resource(storage: Any) -> None:
        if storage is None:
            return

        session_factory = getattr(storage, "Session", None)
        remover = getattr(session_factory, "remove", None)
        if callable(remover):
            try:
                remover()
            except Exception:
                logger.debug("Failed to remove scoped storage sessions", exc_info=True)

        closer = getattr(storage, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                logger.debug("Failed to close storage backend", exc_info=True)

    @staticmethod
    def _sync_resource_owner(resource: Any) -> Any:
        entrypoint = getattr(resource, "entrypoint", None)
        return getattr(entrypoint, "__self__", None) or resource

    @staticmethod
    def _close_sync_resources(resources: list[Any]) -> None:
        seen: set[int] = set()
        for candidate in reversed(tuple(resources)):
            resource = AgentHarness._sync_resource_owner(candidate)
            identity = id(resource)
            if identity in seen:
                continue
            seen.add(identity)
            closer = getattr(resource, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    logger.debug("Failed to close owned runtime resource", exc_info=True)

    @staticmethod
    def _finalize_resources(
        storage: Any,
        sandbox_dir: str | None,
        sync_resources: list[Any],
    ) -> None:
        AgentHarness._close_sync_resources(sync_resources)
        AgentHarness._close_storage_resource(storage)
        if sandbox_dir:
            shutil.rmtree(sandbox_dir, ignore_errors=True)

    def _resolve_sandbox_dir(
        self,
        sandbox_dir: str | Path | None,
        *,
        session_id: str | None,
    ) -> Path:
        if sandbox_dir is not None:
            return Path(sandbox_dir).expanduser().resolve(strict=False)
        prefix = f"agnoclaw-{session_id or uuid4().hex[:8]}-"
        return Path(tempfile.mkdtemp(prefix=prefix)).resolve(strict=False)

    def _ensure_sandbox_dir(self) -> None:
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._session_command_executor.run(
                command=f"mkdir -p {shlex.quote(str(self.sandbox_dir))}",
                workdir=None,
                timeout_seconds=30,
            )
        except Exception:
            logger.debug(
                "Could not pre-create sandbox dir %s via runtime backend", self.sandbox_dir
            )

    def _list_created_sandbox_files(self) -> list[str]:
        command = (
            f"if [ -d {shlex.quote(str(self.sandbox_dir))} ]; then "
            f"find {shlex.quote(str(self.sandbox_dir))} -type f -print | sort; "
            f"fi"
        )
        try:
            result = self._session_command_executor.run(
                command=command,
                workdir=None,
                timeout_seconds=30,
            )
            if result.exit_code == 0 and result.stdout.strip():
                return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except Exception:
            logger.debug("Could not enumerate sandbox files via runtime backend", exc_info=True)

        if not self.sandbox_dir.exists():
            return []
        return sorted(str(path) for path in self.sandbox_dir.rglob("*") if path.is_file())

    def _cleanup_sandbox_dir(self) -> None:
        command = f"rm -rf {shlex.quote(str(self.sandbox_dir))}"
        try:
            self._session_command_executor.run(
                command=command,
                workdir=None,
                timeout_seconds=30,
            )
        except Exception:
            logger.debug("Could not remove sandbox dir %s via runtime backend", self.sandbox_dir)
        shutil.rmtree(self.sandbox_dir, ignore_errors=True)

    async def _maybe_await(self, result: Any) -> None:
        if inspect.isawaitable(result):
            await result

    async def _emit_session_end_callback(
        self,
        summary: str,
        *,
        created_files: list[str] | None,
    ) -> None:
        callback = self._on_session_end
        if callback is None:
            return

        signature = inspect.signature(callback)
        params = list(signature.parameters.values())
        accepts_kwargs = any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params)
        accepts_varargs = any(param.kind is inspect.Parameter.VAR_POSITIONAL for param in params)
        positional_params = [
            param
            for param in params
            if param.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        has_created_files_param = "created_files" in signature.parameters

        if has_created_files_param or accepts_kwargs:
            await self._maybe_await(callback(summary, created_files=created_files))
            return
        if accepts_varargs or len(positional_params) >= 2:
            await self._maybe_await(callback(summary, created_files))
            return
        await self._maybe_await(callback(summary))

    def _build_system_prompt(
        self,
        *,
        skill_content: str | None = None,
        session_id: str | None = None,
        include_runtime: bool = True,
    ) -> str:
        """Build the canonical prompt, optionally without its volatile trailer."""
        include_learning = self._include_learning and not self._plan_mode

        # Compose extra context: user instructions + skill catalog for auto-selection
        extra_parts = []
        if self._extra_instructions:
            extra_parts.append(self._extra_instructions)
        provider_instructions = self._context_provider_instructions()
        if provider_instructions:
            extra_parts.append(provider_instructions)

        # Inject available skill descriptions so the model can auto-select skills.
        # This mirrors Claude Code's skill awareness: the model sees all available
        # skills and can request activation of the most relevant one.
        if (
            not skill_content
            and self._agno_skills is not None
            and self._model_skill_tools_enabled()
        ):
            skill_descriptions = self.skills.get_skill_descriptions()
            if skill_descriptions:
                extra_parts.append(skill_descriptions)

        extra_context = "\n\n".join(extra_parts) if extra_parts else None

        runtime_datetime = None
        if include_runtime and self._active_durable_model_loop.get():
            run_id = self._active_runtime_run_id.get()
            if run_id is None:  # pragma: no cover - internal context invariant
                raise HarnessError(
                    code="RUN_PROMPT_CLOCK_REQUIRED",
                    category="recovery",
                    message="Durable model execution requires a frozen run clock.",
                    retryable=False,
                )
            snapshot = self._get_runtime_store().get_run(run_id)
            try:
                runtime_datetime = datetime.fromisoformat(snapshot.created_at)
            except ValueError as exc:  # pragma: no cover - RunSnapshot owns this value
                raise HarnessError(
                    code="RUN_PROMPT_CLOCK_INVALID",
                    category="recovery",
                    message="The durable run clock is invalid.",
                    retryable=False,
                    details={"run_id": run_id},
                ) from exc

        return self._prompt_builder.build(
            skill_content=skill_content,
            extra_context=extra_context,
            include_learning=include_learning,
            include_plan_mode=self._plan_mode,
            include_datetime=include_runtime,
            runtime_datetime=runtime_datetime,
            include_sandbox=not self._active_durable_model_loop.get(),
            session_id=session_id if session_id is not None else self.session_id,
        )

    def _split_prompt_cache_mode(self) -> bool:
        """Duck-type models that support a stable cached prefix plus runtime trailer."""
        model = self._model
        return (
            not isinstance(model, str)
            and getattr(model, "cache_system_prompt", False)
            and hasattr(model, "system_prompt_blocks")
        )

    def _runtime_system_blocks(self) -> list[Any]:
        """Build caller blocks plus the fresh, uncached runtime trailer."""
        from agno.models.anthropic.claude import SystemPromptBlock

        blocks: list[Any] = []
        caller_blocks = self._caller_system_blocks
        if caller_blocks is not None:
            resolved = caller_blocks() if callable(caller_blocks) else caller_blocks
            blocks.extend(resolved or [])
        # include_time: this block is delivered uncached after the cached
        # prefix, so wall-clock time here costs no cache hits.
        text = self._prompt_builder.build_runtime_block(
            session_id=self._prompt_session_id or self.session_id,
            include_time=True,
        )
        blocks.append(SystemPromptBlock(text=text, cache=False))
        return blocks

    def _set_system_prompt(
        self,
        *,
        skill_content: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Update the underlying agent's system prompt."""
        self._prompt_session_id = session_id if session_id is not None else self.session_id
        if self._split_prompt_cache_mode():
            self._agent.system_message = self._build_system_prompt(
                skill_content=skill_content,
                session_id=session_id,
                include_runtime=False,
            )
            cast(Any, self._model).system_prompt_blocks = self._runtime_system_blocks
            return
        self._agent.system_message = self._build_system_prompt(
            skill_content=skill_content,
            session_id=session_id,
        )

    def _dispatch_command_tool(
        self,
        tool_name: str,
        arguments: Any,
        *,
        run_id: str | None = None,
        context: ExecutionContext | None = None,
    ) -> str:
        """Invoke a registered Function through the governed synthetic-call path."""
        target: Function | None = None
        registered_tools = self._agent.tools
        if callable(registered_tools):
            registered_tools = registered_tools()
        for tool in registered_tools or []:
            if isinstance(tool, Toolkit):
                for fname, func in toolkit_functions(tool).items():
                    if fname == tool_name:
                        target = func
                        break
            elif isinstance(tool, Function):
                if tool.name == tool_name:
                    target = tool
            elif callable(tool) and getattr(tool, "__name__", "") == tool_name:
                raise HarnessError(
                    code="SKILL_COMMAND_UNGOVERNED_TOOL",
                    category="skill",
                    message=(
                        f"Command-dispatch tool '{tool_name}' is a plain callable and "
                        "cannot traverse the governed Function dispatcher."
                    ),
                    retryable=False,
                    details={"tool_name": tool_name},
                )
            if target is not None:
                break

        if target is None:
            return f"[error] Command-dispatch tool '{tool_name}' not found in registered tools."
        entrypoint = target.entrypoint
        if entrypoint is None:
            return f"[error] Command-dispatch tool '{tool_name}' has no callable entrypoint."

        active_context = context or self._build_execution_context(
            user_id=self.user_id,
            session_id=self.session_id,
            metadata={"source": "skill_command_dispatch"},
        )
        active_run_id = run_id or f"skill_command_{uuid4().hex}"
        call_arguments = dict(arguments) if isinstance(arguments, dict) else {"input": arguments}
        fc = FunctionCall(
            function=target,
            arguments=call_arguments,
            call_id=f"skill_call_{uuid4().hex}",
        )
        run_context = SimpleNamespace(
            run_id=active_run_id,
            user_id=active_context.user_id,
            session_id=active_context.session_id,
            metadata={"_agnoclaw_context": self._context_to_metadata(active_context)},
        )
        pre_hook = target.pre_hook
        post_hook = target.post_hook
        pre_completed = False
        try:
            if callable(pre_hook):
                pre_hook(agent=self._agent, run_context=run_context, fc=fc)
            else:
                self._handle_tool_pre_hook(fc=fc, run_context=run_context)
            pre_completed = True

            dispatched_arguments = dict(fc.arguments or {})
            if isinstance(arguments, dict):
                try:
                    fc.result = entrypoint(**dispatched_arguments)
                except TypeError:
                    fc.result = entrypoint(dispatched_arguments)
            elif "input" in dispatched_arguments:
                try:
                    fc.result = entrypoint(dispatched_arguments["input"])
                except TypeError:
                    fc.result = entrypoint()
            else:
                fc.result = entrypoint()
            return str(fc.result)
        except Exception as exc:
            fc.error = str(exc)
            raise
        finally:
            if pre_completed:
                if callable(post_hook):
                    post_hook(agent=self._agent, run_context=run_context, fc=fc)
                else:
                    self._handle_tool_post_hook(fc=fc, run_context=run_context)

    def set_event_sink(self, sink: EventSink, mode: str | None = None) -> None:
        """Swap event sink at runtime."""
        self._event_sink = sink
        if mode is not None:
            self._event_sink_mode = EventSinkMode(mode)

    def set_policy_engine(self, engine: PolicyEngine) -> None:
        """Swap policy engine at runtime."""
        self._policy_engine = engine
        pack_engines = [
            item for item in getattr(self, "_policy_engines", []) if item[0].startswith("pack:")
        ]
        self._policy_engines = [("harness", engine), *pack_engines]

    def set_permission_mode(self, mode: str) -> None:
        """Set runtime permission mode for tool calls."""
        self._permission_controller.set_mode(mode)

    def set_permission_approver(
        self,
        approver: PermissionApprover | None,
        *,
        require_approver: bool | None = None,
    ) -> None:
        """Set or clear the runtime permission approver."""
        self._permission_controller.approver = approver
        if require_approver is not None:
            self._permission_controller.require_approver = bool(require_approver)

    def set_elevated_mode(self, mode: str | ElevatedSessionMode) -> str:
        """Set session-wide elevated execution mode for bash tool calls."""
        self._elevated_session_mode = normalize_elevated_session_mode(mode)
        return self._elevated_session_mode.value

    @property
    def elevated_mode(self) -> str:
        """Current session-wide elevated execution mode."""
        return self._elevated_session_mode.value

    @property
    def sandbox_mode(self) -> str:
        """Current default tool sandbox mode."""
        return self._sandbox_mode.value

    @property
    def permission_mode(self) -> str:
        """Current runtime permission mode."""
        return self._permission_controller.current_mode().value

    def admin_harness_capabilities(self) -> dict[str, Any]:
        """Return harness-owned capability metadata for admin surfaces."""
        return {
            "registry": {
                "registered": len(self._capability_registry),
                "model_tools": [
                    {
                        "tool_name": binding.tool_name,
                        "reference": binding.reference,
                        "digest": binding.spec.digest,
                    }
                    for binding in self._capability_bindings
                ],
                "legacy_compatibility_tools": [
                    {
                        "advertised_name": binding.advertised_name,
                        "normalized_reference": (f"{binding.spec.name}@{binding.spec.version}"),
                        "digest": binding.spec.digest,
                        "trust": binding.spec.trust.value,
                        "recovery": binding.spec.recovery.value,
                        "precedence": binding.precedence,
                        "shadowed": binding.shadowed,
                    }
                    for binding in self._legacy_tool_bindings
                ],
                "extension_compatibility_tools": [
                    {
                        "advertised_name": binding.advertised_name,
                        "source": binding.source,
                        "trust": binding.spec.trust.value,
                        "recovery": binding.spec.recovery.value,
                    }
                    for binding in self._extension_tool_bindings
                ],
            },
            "skills": len(self.skills.list_skills()),
            "context_providers": [
                {
                    "id": getattr(provider, "id", None),
                    "name": getattr(provider, "name", None),
                    "tools": [
                        tool_name
                        for tool_name, info in self._context_provider_tool_map.items()
                        if info.get("provider_id")
                        == str(
                            getattr(provider, "id", "")
                            or getattr(provider, "name", "")
                            or tool_name
                        ).strip()
                    ],
                }
                for provider in self._context_providers
            ],
            "packs": [
                getattr(getattr(pack, "manifest", None), "name", None)
                for pack in self._loaded_packs
            ],
            "permission_mode": self.permission_mode,
        }

    def admin_runtime_info(self) -> dict[str, Any]:
        """Return harness runtime metadata without exposing mutable internals."""
        return {
            "model": self.model_name,
            "session_id": self.session_id,
            "workspace_id": str(self.workspace.path),
            "sandbox_dir": str(self.sandbox_dir),
            "sandbox_mode": self._sandbox_mode.value,
            "event_sink": self._event_sink.__class__.__name__,
            "event_sink_mode": self._event_sink_mode.value,
            "policy_engine": self._policy_engine.__class__.__name__,
            "policy_fail_open": self._policy_fail_open,
            "agentos": {
                "approvals_enabled": bool(getattr(self, "_agentos_approvals_enabled", False)),
                "scheduler_enabled": bool(getattr(self, "_agentos_scheduler_enabled", False)),
                "mcp_enabled": bool(getattr(self, "_agentos_mcp_enabled", False)),
            },
        }

    def admin_list_skills(self) -> list[dict[str, Any]]:
        """Return registered skills."""
        return list(self.skills.list_skills())

    def admin_list_packs(self) -> list[dict[str, Any]]:
        """Return loaded pack manifest metadata."""
        packs: list[dict[str, Any]] = []
        for loaded in self._loaded_packs:
            manifest = getattr(loaded, "manifest", None)
            if manifest is None:
                continue
            packs.append(
                {
                    "name": manifest.name,
                    "version": manifest.version,
                    "description": manifest.description,
                    "root": str(manifest.root),
                    "provides": {
                        "skills": list(manifest.provides.skills),
                        "tools": list(manifest.provides.tools),
                        "hooks": list(manifest.provides.hooks),
                        "context_providers": list(manifest.provides.context_providers),
                        "policies": list(manifest.provides.policies),
                        "commands": list(manifest.provides.commands),
                    },
                    "trust": {
                        "default": manifest.trust.default,
                        "requires_code_execution": manifest.trust.requires_code_execution,
                    },
                }
            )
        return packs

    def admin_list_hooks(self) -> dict[str, Any]:
        """Return registered lifecycle and discovered workspace hook metadata."""
        return {
            "lifecycle": {
                event_type: len(hooks)
                for event_type, hooks in sorted(self._lifecycle_hooks.items())
            },
            "workspace": list(self._workspace_hook_specs),
        }

    def admin_list_policies(self) -> dict[str, Any]:
        """Return active policy configuration."""
        return {
            "engine": self._policy_engine.__class__.__name__,
            "fail_open": self._policy_fail_open,
            "checkpoints": [
                "before_run",
                "before_prompt_send",
                "before_skill_load",
                "before_tool_call",
                "after_tool_call",
            ],
        }

    def admin_list_permissions(self) -> dict[str, Any]:
        """Return active permission configuration."""
        controller = self._permission_controller
        return {
            "mode": controller.current_mode().value,
            "elevated_mode": self.elevated_mode,
            "require_approver": controller.require_approver,
            "has_approver": controller.approver is not None,
            "preapproved_tools": sorted(controller._approved_tools),
            "preapproved_categories": sorted(controller._approved_categories),
        }

    def admin_list_events(self, *, run_id: str | None = None) -> list[dict[str, Any]]:
        """Return in-memory events when the configured sink retains them."""
        events = getattr(self._event_sink, "events", None)
        if not isinstance(events, list):
            return []
        items = []
        for event in events:
            if run_id is not None and getattr(event, "run_id", None) != run_id:
                continue
            if hasattr(event, "to_dict"):
                items.append(event.to_dict())
        return items

    def _authorize_admin_session(
        self,
        session_id: str | None,
        context: ExecutionContext | None = None,
    ) -> str:
        """Authorize access to this harness instance's session-bound sandbox."""
        target = session_id or self.session_id
        if not target or not self.session_id:
            raise HarnessError(
                code="SANDBOX_SESSION_SCOPE_REQUIRED",
                category="identity",
                message="Sandbox administration requires a session-bound harness.",
                retryable=False,
            )
        if target != self.session_id:
            raise HarnessError(
                code="SANDBOX_SESSION_FORBIDDEN",
                category="identity",
                message="Requested session does not own this sandbox.",
                retryable=False,
                details={"session_id": target},
            )
        if context is not None:
            if context.session_id is not None and context.session_id != target:
                raise HarnessError(
                    code="SANDBOX_SESSION_FORBIDDEN",
                    category="identity",
                    message="Authenticated session does not own this sandbox.",
                    retryable=False,
                    details={"session_id": target},
                )
            ownership = (
                ("tenant_id", self._tenant_id, context.tenant_id),
                ("user_id", self.user_id, context.user_id),
            )
            for field, expected, actual in ownership:
                if expected is not None and actual != expected:
                    raise HarnessError(
                        code="SANDBOX_OWNER_FORBIDDEN",
                        category="identity",
                        message=f"Authenticated {field} does not own this sandbox.",
                        retryable=False,
                        details={"field": field, "session_id": target},
                    )
        return target

    def admin_sandbox_info(
        self,
        *,
        session_id: str | None = None,
        context: ExecutionContext | None = None,
    ) -> dict[str, Any]:
        """Return sandbox metadata for a session-scoped admin view."""
        target = self._authorize_admin_session(session_id, context)
        files = self.admin_list_sandbox_files(
            session_id=target,
            context=context,
        )
        return {
            "session_id": target,
            "sandbox_dir": str(self.sandbox_dir),
            "sandbox_mode": self._sandbox_mode.value,
            "exists": self.sandbox_dir.exists(),
            "file_count": len(files),
            "total_bytes": sum(int(item["size_bytes"]) for item in files),
        }

    def admin_list_sandbox_files(
        self,
        *,
        session_id: str | None = None,
        context: ExecutionContext | None = None,
    ) -> list[dict[str, Any]]:
        """Return files only for the session that owns this sandbox."""
        self._authorize_admin_session(session_id, context)
        files: list[dict[str, Any]] = []
        if not self.sandbox_dir.exists():
            return files
        root = self.sandbox_dir.resolve(strict=False)
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                rel_path = path.resolve(strict=False).relative_to(root).as_posix()
            except ValueError:
                continue
            stat = path.stat()
            files.append(
                {
                    "path": rel_path,
                    "size_bytes": stat.st_size,
                    "modified_at": stat.st_mtime,
                }
            )
        return files

    def admin_sandbox_artifact_path(
        self,
        artifact_path: str,
        *,
        session_id: str | None = None,
        context: ExecutionContext | None = None,
    ) -> Path | None:
        """Resolve an artifact path if it points at a file inside the sandbox."""
        self._authorize_admin_session(session_id, context)
        root = self.sandbox_dir.resolve(strict=False)
        candidate = (root / artifact_path).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        return candidate

    def admin_snapshot_sandbox(
        self,
        *,
        session_id: str | None = None,
        context: ExecutionContext | None = None,
    ) -> dict[str, Any]:
        """Return a lightweight sandbox snapshot."""
        return {
            **self.admin_sandbox_info(session_id=session_id, context=context),
            "files": self.admin_list_sandbox_files(
                session_id=session_id,
                context=context,
            ),
        }

    def admin_reset_sandbox(
        self,
        *,
        session_id: str | None = None,
        context: ExecutionContext | None = None,
    ) -> dict[str, Any]:
        """Reset the harness sandbox after permission and policy checks."""
        target_session = self._authorize_admin_session(session_id, context)
        run_id = f"admin_sandbox_reset_{uuid4().hex}"
        context = context or self._build_execution_context(
            user_id=self.user_id,
            session_id=target_session,
            metadata={"source": "agnoclaw_admin"},
        )
        request = ToolCallRequest(
            run_id=run_id,
            tool_name="sandbox.reset",
            arguments={"sandbox_dir": str(self.sandbox_dir)},
            metadata={"source": "agnoclaw_admin"},
        )
        permission_decision = self._permission_controller.check_tool_call(
            request,
            context,
            resolve_sync_value=self._resolve_sync_value,
        )
        self._enforce_policy_decision(
            decision=permission_decision,
            checkpoint="permission.before_tool_call",
            run_id=run_id,
            context=context,
        )
        decision = self._run_policy_sync(
            method_name="before_tool_call",
            payload=request,
            run_input=None,
            context=context,
        )
        self._enforce_policy_decision(
            decision=decision,
            checkpoint="before_tool_call",
            run_id=run_id,
            context=context,
        )
        before = self.admin_sandbox_info(
            session_id=target_session,
            context=context,
        )
        self._cleanup_sandbox_dir()
        self._ensure_sandbox_dir()
        after = self.admin_sandbox_info(
            session_id=target_session,
            context=context,
        )
        self._emit_event_sync(
            event_type="sandbox.reset",
            run_id=run_id,
            context=context,
            payload={
                "before": before,
                "after": after,
            },
        )
        return after

    def run_elevated_command(
        self,
        command: str,
        *,
        reason: str,
        working_dir: str | Path | None = None,
        timeout_seconds: int | None = None,
        context: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
        _skip_approval: bool = False,
    ) -> ElevatedCommandResult:
        """Run a host-elevated command after explicit approval and audit events."""
        request, ctx = self._build_elevated_command_request(
            command,
            reason=reason,
            working_dir=working_dir,
            timeout_seconds=timeout_seconds,
            context=context,
            metadata=metadata,
        )
        tool_request = self._elevated_tool_request(request)
        payload = self._elevated_request_payload(request)
        try:
            self._authorize_elevated_command_sync(
                request=request,
                tool_request=tool_request,
                context=ctx,
                payload=payload,
                skip_approval=_skip_approval,
            )
            self._emit_event_sync(
                event_type="elevated.command.started",
                run_id=request.run_id,
                context=ctx,
                payload=payload,
            )
            command_result = self._elevated_command_executor.run(
                command=request.command,
                workdir=request.working_dir,
                timeout_seconds=request.timeout_seconds,
            )
            tool_result = ToolCallResult(
                run_id=request.run_id,
                tool_name=tool_request.tool_name,
                arguments=tool_request.arguments,
                output=command_result.stdout,
                error=command_result.stderr if command_result.exit_code else None,
                metadata=tool_request.metadata,
            )
            decision = self._run_policy_sync(
                method_name="after_tool_call",
                payload=tool_result,
                run_input=None,
                context=ctx,
            )
            self._enforce_policy_decision(
                decision=decision,
                checkpoint="after_tool_call",
                run_id=request.run_id,
                context=ctx,
            )
            stdout = command_result.stdout
            stderr = command_result.stderr
            if decision.action == PolicyAction.ALLOW_WITH_REDACTION:
                stdout = self._apply_redactions_to_object(stdout, decision.redactions)
                stderr = self._apply_redactions_to_object(stderr, decision.redactions)
            result = ElevatedCommandResult(
                run_id=request.run_id,
                command=request.command,
                stdout=stdout,
                stderr=stderr,
                exit_code=command_result.exit_code,
                duration_ms=command_result.duration_ms,
                working_dir=request.working_dir,
                metadata=dict(request.metadata),
            )
            self._emit_event_sync(
                event_type="elevated.command.completed",
                run_id=request.run_id,
                context=ctx,
                payload={
                    **payload,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "stdout_chars": len(result.stdout),
                    "stderr_chars": len(result.stderr),
                },
            )
            return result
        except HarnessError:
            raise
        except Exception as exc:
            self._emit_event_sync(
                event_type="elevated.command.failed",
                run_id=request.run_id,
                context=ctx,
                payload={**payload, "error": str(exc)},
            )
            raise HarnessError(
                code="ELEVATED_COMMAND_FAILED",
                category="elevated",
                message=f"Elevated command failed: {exc}",
                retryable=False,
                details={"run_id": request.run_id},
            ) from exc

    async def arun_elevated_command(
        self,
        command: str,
        *,
        reason: str,
        working_dir: str | Path | None = None,
        timeout_seconds: int | None = None,
        context: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
        _skip_approval: bool = False,
    ) -> ElevatedCommandResult:
        """Async variant of run_elevated_command."""
        request, ctx = self._build_elevated_command_request(
            command,
            reason=reason,
            working_dir=working_dir,
            timeout_seconds=timeout_seconds,
            context=context,
            metadata=metadata,
        )
        tool_request = self._elevated_tool_request(request)
        payload = self._elevated_request_payload(request)
        try:
            await self._authorize_elevated_command_async(
                request=request,
                tool_request=tool_request,
                context=ctx,
                payload=payload,
                skip_approval=_skip_approval,
            )
            await self._emit_event_async(
                event_type="elevated.command.started",
                run_id=request.run_id,
                context=ctx,
                payload=payload,
            )
            command_result = await asyncio.to_thread(
                self._elevated_command_executor.run,
                command=request.command,
                workdir=request.working_dir,
                timeout_seconds=request.timeout_seconds,
            )
            tool_result = ToolCallResult(
                run_id=request.run_id,
                tool_name=tool_request.tool_name,
                arguments=tool_request.arguments,
                output=command_result.stdout,
                error=command_result.stderr if command_result.exit_code else None,
                metadata=tool_request.metadata,
            )
            decision = await self._run_policy_async(
                method_name="after_tool_call",
                payload=tool_result,
                run_input=None,
                context=ctx,
            )
            await self._enforce_policy_decision_async(
                decision=decision,
                checkpoint="after_tool_call",
                run_id=request.run_id,
                context=ctx,
            )
            stdout = command_result.stdout
            stderr = command_result.stderr
            if decision.action == PolicyAction.ALLOW_WITH_REDACTION:
                stdout = self._apply_redactions_to_object(stdout, decision.redactions)
                stderr = self._apply_redactions_to_object(stderr, decision.redactions)
            result = ElevatedCommandResult(
                run_id=request.run_id,
                command=request.command,
                stdout=stdout,
                stderr=stderr,
                exit_code=command_result.exit_code,
                duration_ms=command_result.duration_ms,
                working_dir=request.working_dir,
                metadata=dict(request.metadata),
            )
            await self._emit_event_async(
                event_type="elevated.command.completed",
                run_id=request.run_id,
                context=ctx,
                payload={
                    **payload,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "stdout_chars": len(result.stdout),
                    "stderr_chars": len(result.stderr),
                },
            )
            return result
        except HarnessError:
            raise
        except Exception as exc:
            await self._emit_event_async(
                event_type="elevated.command.failed",
                run_id=request.run_id,
                context=ctx,
                payload={**payload, "error": str(exc)},
            )
            raise HarnessError(
                code="ELEVATED_COMMAND_FAILED",
                category="elevated",
                message=f"Elevated command failed: {exc}",
                retryable=False,
                details={"run_id": request.run_id},
            ) from exc

    def _build_elevated_command_request(
        self,
        command: str,
        *,
        reason: str,
        working_dir: str | Path | None,
        timeout_seconds: int | None,
        context: ExecutionContext | None,
        metadata: dict[str, Any] | None,
    ) -> tuple[ElevatedCommandRequest, ExecutionContext]:
        command_text = str(command or "").strip()
        reason_text = str(reason or "").strip()
        if not command_text:
            raise HarnessError(
                code="ELEVATED_COMMAND_REQUIRED",
                category="elevated",
                message="Elevated command cannot be empty.",
                retryable=False,
            )
        if not reason_text:
            raise HarnessError(
                code="ELEVATED_REASON_REQUIRED",
                category="elevated",
                message="Elevated command requires a human-readable reason.",
                retryable=False,
            )
        request_metadata = dict(metadata or {})
        request_metadata.setdefault("source", "elevated_command")
        ctx = (
            context.with_metadata(request_metadata)
            if context
            else self._build_execution_context(
                user_id=self.user_id,
                session_id=self.session_id,
                metadata=request_metadata,
            )
        )
        request = ElevatedCommandRequest(
            run_id=f"elevated_{uuid4().hex}",
            command=command_text,
            reason=reason_text,
            working_dir=str(working_dir) if working_dir is not None else None,
            timeout_seconds=timeout_seconds,
            metadata=request_metadata,
        )
        return request, ctx

    @staticmethod
    def _elevated_request_payload(
        request: ElevatedCommandRequest,
    ) -> dict[str, Any]:
        return {
            "command": request.command,
            "reason": request.reason,
            "working_dir": request.working_dir,
            "timeout_seconds": request.timeout_seconds,
            "metadata": dict(request.metadata),
        }

    @staticmethod
    def _elevated_tool_request(
        request: ElevatedCommandRequest,
    ) -> ToolCallRequest:
        return ToolCallRequest(
            run_id=request.run_id,
            tool_name="bash.elevated",
            arguments={
                "command": request.command,
                "reason": request.reason,
                "working_dir": request.working_dir,
                "timeout_seconds": request.timeout_seconds,
            },
            metadata={"elevated": True, **request.metadata},
        )

    def _authorize_elevated_command_sync(
        self,
        *,
        request: ElevatedCommandRequest,
        tool_request: ToolCallRequest,
        context: ExecutionContext,
        payload: dict[str, Any],
        skip_approval: bool = False,
    ) -> None:
        self._emit_event_sync(
            event_type="elevated.command.requested",
            run_id=request.run_id,
            context=context,
            payload=payload,
        )
        self._run_elevated_preflight_sync(
            request=request,
            tool_request=tool_request,
            context=context,
        )
        if skip_approval:
            self._emit_event_sync(
                event_type="elevated.command.approval_skipped",
                run_id=request.run_id,
                context=context,
                payload={**payload, "reason_code": "ELEVATED_FULL_MODE"},
            )
            return
        self._approve_elevated_command_sync(
            request=request,
            tool_request=tool_request,
            context=context,
        )

    async def _authorize_elevated_command_async(
        self,
        *,
        request: ElevatedCommandRequest,
        tool_request: ToolCallRequest,
        context: ExecutionContext,
        payload: dict[str, Any],
        skip_approval: bool = False,
    ) -> None:
        await self._emit_event_async(
            event_type="elevated.command.requested",
            run_id=request.run_id,
            context=context,
            payload=payload,
        )
        await self._run_elevated_preflight_async(
            request=request,
            tool_request=tool_request,
            context=context,
        )
        if skip_approval:
            await self._emit_event_async(
                event_type="elevated.command.approval_skipped",
                run_id=request.run_id,
                context=context,
                payload={**payload, "reason_code": "ELEVATED_FULL_MODE"},
            )
            return
        await self._approve_elevated_command_async(
            request=request,
            tool_request=tool_request,
            context=context,
        )

    def _run_elevated_preflight_sync(
        self,
        *,
        request: ElevatedCommandRequest,
        tool_request: ToolCallRequest,
        context: ExecutionContext,
    ) -> None:
        violations = self._guardrails.check(tool_request)
        if violations:
            for violation in violations:
                self._emit_event_sync(
                    event_type="guardrail.violation",
                    run_id=request.run_id,
                    context=context,
                    payload={
                        "tool_name": tool_request.tool_name,
                        "code": violation.code,
                        "message": violation.message,
                        "details": violation.details,
                    },
                )
            self._emit_event_sync(
                event_type="elevated.command.rejected",
                run_id=request.run_id,
                context=context,
                payload={
                    **self._elevated_request_payload(request),
                    "reason_code": "ELEVATED_GUARDRAIL_DENIED",
                },
            )
            raise HarnessError(
                code="GUARDRAIL_DENIED",
                category="guardrail",
                message="Guardrail denied elevated command.",
                retryable=False,
                details={"run_id": request.run_id},
            )
        decision = self._run_policy_sync(
            method_name="before_tool_call",
            payload=tool_request,
            run_input=None,
            context=context,
        )
        if decision.action == PolicyAction.DENY:
            self._emit_event_sync(
                event_type="elevated.command.rejected",
                run_id=request.run_id,
                context=context,
                payload={
                    **self._elevated_request_payload(request),
                    "reason_code": decision.reason_code or "ELEVATED_POLICY_DENIED",
                    "message": decision.message,
                },
            )
        self._enforce_policy_decision(
            decision=decision,
            checkpoint="before_tool_call",
            run_id=request.run_id,
            context=context,
        )

    async def _run_elevated_preflight_async(
        self,
        *,
        request: ElevatedCommandRequest,
        tool_request: ToolCallRequest,
        context: ExecutionContext,
    ) -> None:
        violations = self._guardrails.check(tool_request)
        if violations:
            for violation in violations:
                await self._emit_event_async(
                    event_type="guardrail.violation",
                    run_id=request.run_id,
                    context=context,
                    payload={
                        "tool_name": tool_request.tool_name,
                        "code": violation.code,
                        "message": violation.message,
                        "details": violation.details,
                    },
                )
            await self._emit_event_async(
                event_type="elevated.command.rejected",
                run_id=request.run_id,
                context=context,
                payload={
                    **self._elevated_request_payload(request),
                    "reason_code": "ELEVATED_GUARDRAIL_DENIED",
                },
            )
            raise HarnessError(
                code="GUARDRAIL_DENIED",
                category="guardrail",
                message="Guardrail denied elevated command.",
                retryable=False,
                details={"run_id": request.run_id},
            )
        decision = await self._run_policy_async(
            method_name="before_tool_call",
            payload=tool_request,
            run_input=None,
            context=context,
        )
        if decision.action == PolicyAction.DENY:
            await self._emit_event_async(
                event_type="elevated.command.rejected",
                run_id=request.run_id,
                context=context,
                payload={
                    **self._elevated_request_payload(request),
                    "reason_code": decision.reason_code or "ELEVATED_POLICY_DENIED",
                    "message": decision.message,
                },
            )
        await self._enforce_policy_decision_async(
            decision=decision,
            checkpoint="before_tool_call",
            run_id=request.run_id,
            context=context,
        )

    def _approve_elevated_command_sync(
        self,
        *,
        request: ElevatedCommandRequest,
        tool_request: ToolCallRequest,
        context: ExecutionContext,
    ) -> None:
        approver = self._permission_controller.approver
        if approver is None:
            self._reject_elevated_command_sync(
                request=request,
                context=context,
                reason_code="ELEVATED_APPROVER_REQUIRED",
                message="Elevated commands require a permission approver.",
            )
        permission_request = PermissionRequest(
            run_id=request.run_id,
            tool_name=tool_request.tool_name,
            category="elevated_exec",
            arguments=dict(tool_request.arguments),
        )
        try:
            allowed = self._resolve_sync_value(
                approver.approve(permission_request, context),
                operation="permission.approve:bash.elevated",
            )
        except Exception as exc:
            self._reject_elevated_command_sync(
                request=request,
                context=context,
                reason_code="ELEVATED_APPROVAL_FAILED",
                message=str(exc),
            )
        if not bool(allowed):
            self._reject_elevated_command_sync(
                request=request,
                context=context,
                reason_code="ELEVATED_REJECTED",
                message="Elevated command approval was rejected.",
            )
        self._emit_event_sync(
            event_type="elevated.command.approved",
            run_id=request.run_id,
            context=context,
            payload=self._elevated_request_payload(request),
        )

    async def _approve_elevated_command_async(
        self,
        *,
        request: ElevatedCommandRequest,
        tool_request: ToolCallRequest,
        context: ExecutionContext,
    ) -> None:
        approver = self._permission_controller.approver
        if approver is None:
            await self._reject_elevated_command_async(
                request=request,
                context=context,
                reason_code="ELEVATED_APPROVER_REQUIRED",
                message="Elevated commands require a permission approver.",
            )
        permission_request = PermissionRequest(
            run_id=request.run_id,
            tool_name=tool_request.tool_name,
            category="elevated_exec",
            arguments=dict(tool_request.arguments),
        )
        try:
            allowed = await self._resolve_async_value(approver.approve(permission_request, context))
        except Exception as exc:
            await self._reject_elevated_command_async(
                request=request,
                context=context,
                reason_code="ELEVATED_APPROVAL_FAILED",
                message=str(exc),
            )
        if not bool(allowed):
            await self._reject_elevated_command_async(
                request=request,
                context=context,
                reason_code="ELEVATED_REJECTED",
                message="Elevated command approval was rejected.",
            )
        await self._emit_event_async(
            event_type="elevated.command.approved",
            run_id=request.run_id,
            context=context,
            payload=self._elevated_request_payload(request),
        )

    def _reject_elevated_command_sync(
        self,
        *,
        request: ElevatedCommandRequest,
        context: ExecutionContext,
        reason_code: str,
        message: str,
    ) -> Never:
        self._emit_event_sync(
            event_type="elevated.command.rejected",
            run_id=request.run_id,
            context=context,
            payload={
                **self._elevated_request_payload(request),
                "reason_code": reason_code,
                "message": message,
            },
        )
        raise HarnessError(
            code=reason_code,
            category="elevated",
            message=message,
            retryable=False,
            details={"run_id": request.run_id},
        )

    async def _reject_elevated_command_async(
        self,
        *,
        request: ElevatedCommandRequest,
        context: ExecutionContext,
        reason_code: str,
        message: str,
    ) -> Never:
        await self._emit_event_async(
            event_type="elevated.command.rejected",
            run_id=request.run_id,
            context=context,
            payload={
                **self._elevated_request_payload(request),
                "reason_code": reason_code,
                "message": message,
            },
        )
        raise HarnessError(
            code=reason_code,
            category="elevated",
            message=message,
            retryable=False,
            details={"run_id": request.run_id},
        )

    def add_pre_run_hook(self, hook: PreRunHook) -> None:
        """Register a pre-run hook."""
        self._pre_run_hooks.append(hook)

    def add_post_run_hook(self, hook: PostRunHook) -> None:
        """Register a post-run hook."""
        self._post_run_hooks.append(hook)

    def _prepare_model_skill_function(self, function: Function) -> None:
        """Attach replay semantics and the ordinary governed tool boundary."""
        declare_builtin_effects([function])
        self._attach_function_runtime_hooks(function)

    def _model_skill_tools_enabled(self) -> bool:
        """Keep internal summarization and memory maintenance strictly tool-free."""
        return self._internal_run_kind.get() not in {"summary", "memory_flush"}

    def _activate_model_skill(self, activation: ModelSkillActivation) -> str:
        """Bind one Agno-disclosed skill to the active run and return its envelope."""
        runtime = get_current_tool_runtime()
        run_id = runtime.get("parent_run_id") if runtime is not None else None
        if not isinstance(run_id, str) or not run_id:
            return json.dumps(
                {
                    "status": "error",
                    "code": "SKILL_ACTIVE_RUN_REQUIRED",
                    "message": "Model skill activation requires an active governed run.",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        with self._active_skill_lock:
            existing = self._active_skills.get(run_id)
            if existing is not None and existing.name != activation.name:
                return json.dumps(
                    {
                        "status": "error",
                        "code": "SKILL_ACTIVATION_CONFLICT",
                        "message": (
                            f"Run already activated skill '{existing.name}'; "
                            "start a new turn to activate another skill."
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            self._active_skills[run_id] = activation

        context = runtime.get("context") if runtime is not None else None
        if isinstance(context, ExecutionContext):
            self._emit_event_sync(
                event_type="skill.load.completed",
                run_id=run_id,
                context=context,
                payload={
                    "name": activation.name,
                    "loaded": True,
                    "source": "model_progressive_disclosure",
                    "trust": activation.trust,
                    "content_digest": activation.content_digest,
                    "allowed_tools": list(activation.allowed_tools or ()),
                },
            )
        return json.dumps(
            {
                "status": "activated",
                "skill_name": activation.name,
                "description": activation.description,
                "content_digest": activation.content_digest,
                "allowed_tools": (
                    list(activation.allowed_tools)
                    if activation.allowed_tools is not None
                    else None
                ),
                "instructions": activation.content,
                "execution_note": (
                    "Inline command syntax was not executed during activation. "
                    "Use only the allowed governed tools when the instructions require work."
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def _register_explicit_skill(
        self,
        *,
        run_id: str,
        skill_obj: Any,
        content: str,
    ) -> None:
        """Mirror caller-owned skill restrictions into the final tool boundary."""
        allowed = tuple(skill_obj.meta.allowed_tools) if skill_obj.meta.allowed_tools else None
        activation = ModelSkillActivation(
            name=skill_obj.name,
            description=skill_obj.description,
            content=content,
            trust=self.skills._trust_level(skill_obj),
            allowed_tools=allowed,
            content_digest=(
                "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
            ),
        )
        with self._active_skill_lock:
            self._active_skills[run_id] = activation

    def _active_skill(self, run_id: str) -> ModelSkillActivation | None:
        with self._active_skill_lock:
            return self._active_skills.get(run_id)

    def _attach_tool_runtime_hooks(self, tools: list[Any]) -> None:
        for tool in tools:
            if isinstance(tool, Function):
                self._attach_function_runtime_hooks(tool)
            elif isinstance(tool, Toolkit):
                for function in toolkit_functions(tool).values():
                    self._attach_function_runtime_hooks(function)

    def _attach_function_runtime_hooks(self, function: Function) -> None:
        pre_wrapped = getattr(function.pre_hook, "_agnoclaw_runtime_pre", False)
        post_wrapped = getattr(function.post_hook, "_agnoclaw_runtime_post", False)
        if pre_wrapped and post_wrapped:
            return

        original_pre_hook = function.pre_hook
        original_post_hook = function.post_hook

        def runtime_pre_hook(agent=None, team=None, run_context=None, fc=None):
            if original_pre_hook is not None:
                try:
                    self._invoke_original_tool_hook(
                        original_pre_hook,
                        agent=agent,
                        team=team,
                        run_context=run_context,
                        fc=fc,
                    )
                except HarnessError as exc:
                    self._raise_agent_run_exception(exc)
            self._handle_tool_pre_hook(fc=fc, run_context=run_context)
            # Expose the active RunContext to custom dispatch adapters for the
            # duration of this tool call (read via get_current_run_context()).
            self._set_active_run_context(fc, run_context)
            runtime = getattr(fc, "_agnoclaw_tool_runtime", None)
            if isinstance(runtime, dict):
                self._set_active_tool_runtime(fc, runtime)
            activate_builtin_ingress(function, fc=fc, harness=self)

        def runtime_post_hook(agent=None, team=None, run_context=None, fc=None):
            try:
                if original_post_hook is not None:
                    try:
                        self._invoke_original_tool_hook(
                            original_post_hook,
                            agent=agent,
                            team=team,
                            run_context=run_context,
                            fc=fc,
                        )
                    except HarnessError as exc:
                        self._raise_agent_run_exception(exc)
                self._handle_tool_post_hook(fc=fc, run_context=run_context)
            finally:
                restore_builtin_ingress(function, fc=fc)
                self._clear_active_tool_runtime(fc)
                self._clear_active_run_context(fc)

        runtime_pre_hook._agnoclaw_runtime_pre = True  # type: ignore[attr-defined]
        runtime_post_hook._agnoclaw_runtime_post = True  # type: ignore[attr-defined]
        function.pre_hook = runtime_pre_hook
        function.post_hook = runtime_post_hook

    def _invoke_original_tool_hook(
        self,
        hook,
        *,
        agent=None,
        team=None,
        run_context=None,
        fc=None,
    ) -> None:
        signature = inspect.signature(hook).parameters
        kwargs: dict[str, Any] = {}
        if "agent" in signature:
            kwargs["agent"] = agent
        if "team" in signature:
            kwargs["team"] = team
        if "run_context" in signature:
            kwargs["run_context"] = run_context
        if "fc" in signature:
            kwargs["fc"] = fc
        result = hook(**kwargs)
        self._resolve_sync_value(
            result,
            operation=f"tool_hook:{getattr(hook, '__name__', hook.__class__.__name__)}",
        )

    @staticmethod
    def _apply_redactions_to_object(value: Any, redactions) -> Any:
        if not redactions:
            return value
        if isinstance(value, str):
            return apply_redactions(value, redactions)
        if isinstance(value, list):
            return [AgentHarness._apply_redactions_to_object(item, redactions) for item in value]
        if isinstance(value, tuple):
            return tuple(
                AgentHarness._apply_redactions_to_object(item, redactions) for item in value
            )
        if isinstance(value, dict):
            return {
                key: AgentHarness._apply_redactions_to_object(item, redactions)
                for key, item in value.items()
            }
        return value

    @staticmethod
    def _set_active_tool_runtime(fc, runtime: dict[str, Any]) -> None:
        token = _CURRENT_TOOL_RUNTIME.set(dict(runtime))
        if fc is not None:
            fc._agnoclaw_tool_runtime_token = token

    @staticmethod
    def _clear_active_tool_runtime(fc) -> None:
        if fc is None:
            return
        token = getattr(fc, "_agnoclaw_tool_runtime_token", None)
        if token is None:
            return
        _CURRENT_TOOL_RUNTIME.reset(token)
        delattr(fc, "_agnoclaw_tool_runtime_token")

    @staticmethod
    def _set_active_run_context(fc, run_context: Any) -> None:
        token = _CURRENT_RUN_CONTEXT.set(run_context)
        if fc is not None:
            fc._agnoclaw_run_context_token = token

    @staticmethod
    def _clear_active_run_context(fc) -> None:
        if fc is None:
            return
        token = getattr(fc, "_agnoclaw_run_context_token", None)
        if token is None:
            return
        _CURRENT_RUN_CONTEXT.reset(token)
        delattr(fc, "_agnoclaw_run_context_token")

    @staticmethod
    def _context_to_metadata(context: ExecutionContext) -> dict[str, Any]:
        return {
            "tenant_id": context.tenant_id,
            "org_id": context.org_id,
            "team_id": context.team_id,
            "workspace_id": context.workspace_id,
            "session_id": context.session_id,
            "user_id": context.user_id,
            "request_id": context.request_id,
            "trace_id": context.trace_id,
            "roles": list(context.roles),
            "scopes": list(context.scopes),
            # These keys are generated by the harness after admission. They are
            # deliberately outside caller-controlled context.metadata.
            "trusted_permission_tools": list(context.trusted_permission_tools),
            "trusted_permission_categories": list(context.trusted_permission_categories),
            "metadata": dict(context.metadata),
            "identity_source": context.identity_source.value,
            "admission": (context.admission.to_dict() if context.admission is not None else None),
        }

    def _build_agent_run_metadata(
        self,
        *,
        context: ExecutionContext,
        run_input: RunInput,
    ) -> dict[str, Any]:
        payload = dict(run_input.metadata)
        payload["_agnoclaw_context"] = self._context_to_metadata(context)
        payload["_agnoclaw_harness_run_id"] = run_input.run_id
        return payload

    _trace_payload_from_context = staticmethod(trace_payload_from_context)
    _build_subagent_execution_context = staticmethod(build_subagent_execution_context)

    def _context_from_run_context(self, run_context) -> ExecutionContext:
        payload = {}
        if run_context is not None and isinstance(getattr(run_context, "metadata", None), dict):
            payload = dict(run_context.metadata or {})

        raw_context = payload.get("_agnoclaw_context")
        if isinstance(raw_context, dict):
            admission = None
            raw_admission = raw_context.get("admission")
            if isinstance(raw_admission, dict):
                admission = AdmissionEnvelope.from_dict(raw_admission)
            return ExecutionContext.create(
                user_id=raw_context.get("user_id"),
                session_id=raw_context.get("session_id"),
                workspace_id=raw_context.get("workspace_id") or str(self.workspace.path),
                tenant_id=raw_context.get("tenant_id"),
                org_id=raw_context.get("org_id"),
                team_id=raw_context.get("team_id"),
                roles=raw_context.get("roles") or (),
                scopes=raw_context.get("scopes") or (),
                request_id=raw_context.get("request_id"),
                trace_id=raw_context.get("trace_id"),
                trusted_permission_tools=(raw_context.get("trusted_permission_tools") or ()),
                trusted_permission_categories=(
                    raw_context.get("trusted_permission_categories") or ()
                ),
                metadata=raw_context.get("metadata") or {},
                identity_source=IdentitySource(
                    raw_context.get("identity_source") or IdentitySource.INTERNAL_PARENT.value
                ),
                admission=admission,
            )

        return ExecutionContext.create(
            user_id=getattr(run_context, "user_id", None),
            session_id=getattr(run_context, "session_id", None),
            workspace_id=str(self.workspace.path),
            metadata=payload,
            identity_source=IdentitySource.INTERNAL_PARENT,
        )

    def _run_id_from_tool_hook(self, *, run_context, fc) -> str:
        if run_context is not None:
            metadata = getattr(run_context, "metadata", None)
            if isinstance(metadata, dict):
                harness_run_id = metadata.get("_agnoclaw_harness_run_id")
                if isinstance(harness_run_id, str) and harness_run_id:
                    return harness_run_id
            run_id = getattr(run_context, "run_id", None)
            if isinstance(run_id, str) and run_id:
                return run_id
        if fc is not None:
            call_id = getattr(fc, "call_id", None)
            if isinstance(call_id, str) and call_id:
                return f"run_from_{call_id}"
        return f"run_{uuid4().hex}"

    @staticmethod
    def _tool_call_id(fc) -> str | None:
        call_id = getattr(fc, "call_id", None)
        if isinstance(call_id, str) and call_id:
            return call_id
        return None

    @staticmethod
    def _truncate_text(value: str, *, limit: int) -> str:
        if len(value) <= limit:
            return value
        return f"{value[: limit - 1].rstrip()}…"

    @staticmethod
    def _normalize_error_message(value: Any) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            text = text[1:-1].strip()
        return text

    @staticmethod
    def _status_is_error(value: Any) -> bool:
        return _run_output_is_error(value)

    @staticmethod
    def _extract_error_signal_from_run_output(run_output: Any) -> dict[str, Any]:
        status = _run_output_status_value(run_output)
        raw_message = getattr(run_output, "content", None)
        message = AgentHarness._normalize_error_message(raw_message)
        signal: dict[str, Any] = {
            "status": status,
            "message": message,
            "error_type": None,
            "error_id": None,
            "additional_data": {},
            "source": "run_output",
        }

        events = getattr(run_output, "events", None)
        if isinstance(events, list):
            for event in reversed(events):
                event_name = str(getattr(event, "event", "")).lower()
                if event_name not in {"runerror", "run_error"}:
                    continue
                signal["error_type"] = getattr(event, "error_type", None)
                signal["error_id"] = getattr(event, "error_id", None)
                additional = getattr(event, "additional_data", None)
                if isinstance(additional, dict):
                    signal["additional_data"] = dict(additional)
                event_content = getattr(event, "content", None)
                if event_content:
                    signal["message"] = AgentHarness._normalize_error_message(event_content)
                break

        return signal

    @staticmethod
    def _extract_error_signal_from_stream_event(event: Any) -> dict[str, Any] | None:
        event_name = str(getattr(event, "event", "")).lower()
        if event_name not in {"runerror", "run_error"}:
            return None
        content = AgentHarness._normalize_error_message(getattr(event, "content", ""))
        additional = getattr(event, "additional_data", None)
        return {
            "status": "error",
            "message": content,
            "error_type": getattr(event, "error_type", None),
            "error_id": getattr(event, "error_id", None),
            "additional_data": dict(additional) if isinstance(additional, dict) else {},
            "source": "stream_event",
        }

    @staticmethod
    def _classify_error_signal(signal: dict[str, Any]) -> str:
        error_type = str(signal.get("error_type") or "").lower()
        error_id = str(signal.get("error_id") or "").lower()
        message = str(signal.get("message") or "").lower()

        auth_markers = (
            "model_authentication_error",
            "authentication",
            "auth token",
            "auth_token",
            "api key",
            "api_key",
            "unauthorized",
            "invalid_api_key",
            "could not resolve authentication method",
            "anthropic_api_key",
            "openai_api_key",
            "access token",
        )
        if any(marker in error_type for marker in auth_markers) or any(
            marker in error_id for marker in auth_markers
        ):
            return "auth"
        if any(marker in message for marker in auth_markers):
            return "auth"

        config_markers = (
            "invalid model",
            "model not found",
            "does not support",
            "unknown model",
            "unknown provider",
            "not configured",
            "must be set",
            "missing required",
            "unsupported",
            "configuration",
        )
        if any(marker in message for marker in config_markers):
            return "config"

        recoverable_markers = (
            "rate limit",
            "429",
            "timeout",
            "timed out",
            "temporarily unavailable",
            "connection error",
            "network error",
            "try again",
            "overloaded",
            "retry",
        )
        if any(marker in message for marker in recoverable_markers):
            return "recoverable"

        return "unknown"

    def _begin_context_overflow_recovery_sync(
        self,
        *,
        context: ExecutionContext,
        run_lease: _RunGateLease,
        run_id: str,
        stream: bool,
        source: str,
    ) -> Any:
        if not self._auto_compact_context:
            raise context_overflow_error(run_id=run_id, reason="disabled", source=source)
        if stream:
            raise context_overflow_error(run_id=run_id, reason="stream", source=source)
        if self._tool_step_state.get(run_id):
            raise context_overflow_error(run_id=run_id, reason="tool_activity", source=source)
        return self._prepare_context_overflow_retry_sync(context, run_lease)

    async def _begin_context_overflow_recovery_async(
        self,
        *,
        context: ExecutionContext,
        run_lease: _RunGateLease,
        run_id: str,
        stream: bool,
        source: str,
    ) -> Any:
        if not self._auto_compact_context:
            raise context_overflow_error(run_id=run_id, reason="disabled", source=source)
        if stream:
            raise context_overflow_error(run_id=run_id, reason="stream", source=source)
        if self._tool_step_state.get(run_id):
            raise context_overflow_error(run_id=run_id, reason="tool_activity", source=source)
        return await self._prepare_context_overflow_retry_async(context, run_lease)

    def _raise_if_fatal_error_signal(self, signal: dict[str, Any]) -> None:
        if str(signal.get("status") or "").lower() != "error":
            return
        category = self._classify_error_signal(signal)
        message = self._normalize_error_message(signal.get("message"))
        if not message:
            message = "Model invocation failed."
        message = self._truncate_text(message, limit=_ERROR_MESSAGE_LIMIT)
        details = {
            "error_type": signal.get("error_type"),
            "error_id": signal.get("error_id"),
            "source": signal.get("source"),
            "additional_data": signal.get("additional_data") or {},
        }
        if category == "auth":
            raise AgnoAuthError(message, details=details)
        if category == "config":
            raise AgnoConfigError(message, details=details)

    def _raise_stream_error_signal(self, signal: dict[str, Any], *, run_id: str) -> None:
        """Raise a typed error for stream failures after run.failed emission."""
        if is_context_overflow_signal(signal):
            raise context_overflow_error(
                run_id=run_id,
                reason="stream",
                source="stream_event",
            )
        self._raise_if_fatal_error_signal(signal)
        category = self._classify_error_signal(signal)
        message = self._normalize_error_message(signal.get("message"))
        if not message:
            message = "Model invocation failed."
        message = self._truncate_text(message, limit=_ERROR_MESSAGE_LIMIT)
        details = {
            "error_type": signal.get("error_type"),
            "error_id": signal.get("error_id"),
            "source": signal.get("source"),
            "additional_data": signal.get("additional_data") or {},
        }
        raise HarnessError(
            code="MODEL_STREAM_FAILED",
            category="model",
            message=message,
            retryable=(category == "recoverable"),
            details=details,
        )

    @staticmethod
    def _format_result_preview(value: Any) -> str:
        if value is None:
            return ""
        text = " ".join(str(value).split())
        return AgentHarness._truncate_text(text, limit=_RESULT_PREVIEW_LIMIT)

    @staticmethod
    def _result_identity(
        value: Any, ref_keys: tuple[str, ...] = _RESULT_REF_KEYS
    ) -> dict[str, Any] | None:
        """Small JSON-safe identity from a live tool result — dict, Pydantic model
        (.model_dump), or a JSON string. Only the recognized identity keys
        (id/name/title/type/version/filename plus any consumer-configured keys);
        never full content. None for scalars / non-identifying results."""
        obj: Any = value
        if hasattr(obj, "model_dump"):
            try:
                # Dump only the identity fields — avoids serializing the full
                # (possibly large/nested) model graph on the tool-completion hot path.
                obj = obj.model_dump(mode="json", include=set(ref_keys))
            except Exception:
                return None
        if isinstance(obj, str):
            # Only an object-shaped JSON string can carry identity; skip parsing
            # large non-object results (HTML, plain text) on the hot path.
            if not obj.lstrip().startswith("{"):
                return None
            try:
                obj = json.loads(obj)
            except Exception:
                return None
        if not isinstance(obj, dict):
            return None
        ref: dict[str, Any] = {}
        for key in ref_keys:
            candidate = obj.get(key)
            # bool is an int subclass — exclude it so a boolean flag can't pose as
            # identity; accept str/int/float scalars with non-blank content.
            if isinstance(candidate, bool) or not isinstance(candidate, (str, int, float)):
                continue
            if str(candidate).strip():
                ref[key] = candidate
        return ref or None

    @staticmethod
    def _scheduler_invocation_payload(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(metadata, dict):
            return None
        scheduler = metadata.get("scheduler")
        if not isinstance(scheduler, dict):
            agentos = metadata.get("agentos")
            if isinstance(agentos, dict):
                scheduler = agentos.get("scheduler")
        if not isinstance(scheduler, dict):
            scheduler = {}
        payload = {
            key: value
            for key, value in {
                "schedule_id": scheduler.get("schedule_id") or metadata.get("schedule_id"),
                "schedule_run_id": scheduler.get("schedule_run_id")
                or metadata.get("schedule_run_id"),
                "schedule_name": scheduler.get("schedule_name") or metadata.get("schedule_name"),
                "scheduled_at": scheduler.get("scheduled_at") or metadata.get("scheduled_at"),
            }.items()
            if value is not None
        }
        return payload or None

    def _start_tool_step(
        self,
        *,
        run_id: str,
        tool_name: str,
        tool_call_id: str | None,
        context: ExecutionContext,
    ) -> dict[str, Any]:
        state = self._tool_step_state.setdefault(run_id, {"next_index": 1, "active": {}})
        step_index = int(state.get("next_index", 1))
        state["next_index"] = step_index + 1

        if tool_call_id:
            step_id = tool_call_id
        else:
            step_id = f"{run_id}:step:{step_index}"
        step_name = f"Running {tool_name}"

        step_data = {
            "step_id": step_id,
            "step_name": step_name,
            "step_index": step_index,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "started_at": monotonic(),
        }
        state["active"][step_id] = step_data
        self._emit_event_sync(
            event_type="step_started",
            run_id=run_id,
            context=context,
            payload={
                "step_id": step_id,
                "step_name": step_name,
                "step_index": step_index,
                "total_steps": None,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
            },
        )
        return step_data

    def _finish_tool_step(
        self,
        *,
        run_id: str,
        tool_name: str,
        tool_call_id: str | None,
        context: ExecutionContext,
    ) -> tuple[dict[str, Any], int]:
        state = self._tool_step_state.get(run_id, {})
        active = state.get("active", {})
        step_data: dict[str, Any] | None = None

        if isinstance(active, dict):
            if tool_call_id and tool_call_id in active:
                step_data = active.pop(tool_call_id, None)
            if step_data is None and active:
                # Fallback for missing call IDs: finish earliest active step.
                first_key = next(iter(active.keys()))
                step_data = active.pop(first_key)

        if step_data is None:
            state = self._tool_step_state.setdefault(run_id, {"next_index": 1, "active": {}})
            step_index = int(state.get("next_index", 1))
            state["next_index"] = step_index + 1
            step_data = {
                "step_id": tool_call_id or f"{run_id}:step:{step_index}",
                "step_name": f"Running {tool_name}",
                "step_index": step_index,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "started_at": monotonic(),
            }

        started_at = float(step_data.get("started_at") or monotonic())
        duration_ms = max(0, int((monotonic() - started_at) * 1000))
        self._emit_event_sync(
            event_type="step_completed",
            run_id=run_id,
            context=context,
            payload={
                "step_id": step_data.get("step_id"),
                "step_name": step_data.get("step_name"),
                "step_index": step_data.get("step_index"),
                "total_steps": None,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "duration_ms": duration_ms,
            },
        )
        return step_data, duration_ms

    def _cleanup_tool_step_state(self, run_id: str) -> None:
        self._tool_step_state.pop(run_id, None)
        with self._active_skill_lock:
            self._active_skills.pop(run_id, None)

    def _raise_agent_run_exception(self, error: HarnessError) -> None:
        raise AgentRunException(error, user_message=error.message)

    @staticmethod
    def _extract_harness_error(exc: Exception) -> HarnessError | None:
        if isinstance(exc, HarnessError):
            return exc
        if isinstance(exc, AgentRunException) and exc.args:
            inner = exc.args[0]
            if isinstance(inner, HarnessError):
                return inner
        return None

    def _handle_tool_pre_hook(self, *, fc, run_context) -> None:
        if fc is None or getattr(fc, "function", None) is None:
            return

        try:
            run_id = self._run_id_from_tool_hook(run_context=run_context, fc=fc)
            context = self._context_from_run_context(run_context)
            tool_name = getattr(fc.function, "name", "unknown_tool")
            active_skill = self._active_skill(run_id)
            if (
                active_skill is not None
                and active_skill.allowed_tools is not None
                and tool_name not in active_skill.allowed_tools
            ):
                raise HarnessError(
                    code="SKILL_TOOL_NOT_ALLOWED",
                    category="skill",
                    message=(
                        f"Active skill '{active_skill.name}' does not allow tool "
                        f"'{tool_name}'."
                    ),
                    retryable=False,
                    details={
                        "skill": active_skill.name,
                        "tool_name": tool_name,
                        "allowed_tools": list(active_skill.allowed_tools),
                    },
                )
            capability_binding = self._capability_tool_map.get(tool_name)
            builtin_spec = builtin_effect(fc.function)
            governed_builtin = (
                builtin_spec is not None and self._active_runtime_run_id.get() is not None
            )
            assert not governed_builtin or builtin_spec is not None
            if capability_binding is not None:
                lifecycle_run_id = self._active_runtime_run_id.get()
                if lifecycle_run_id is None:
                    raise HarnessError(
                        code="CAPABILITY_ACTIVE_RUN_REQUIRED",
                        category="lifecycle",
                        message=(
                            "Generated capability tools require AgentHarness.start(); "
                            "direct run/arun remains a compatibility path."
                        ),
                        retryable=False,
                    )
                run_id = lifecycle_run_id
            tool_call_id = self._tool_call_id(fc)
            arguments = dict(getattr(fc, "arguments", None) or {})
            provider_info = self._context_provider_tool_map.get(tool_name)
            request_metadata = {"tool_call_id": tool_call_id}
            if capability_binding is not None:
                request_metadata.update(
                    {
                        "capability_digest": capability_binding.spec.digest,
                        "capability_kind": capability_binding.spec.kind.value,
                        "capability_reference": capability_binding.reference,
                        "effect_class": capability_binding.spec.effect_class.value,
                    }
                )
            elif governed_builtin:
                assert builtin_spec is not None
                request_metadata.update(
                    {
                        "builtin_target": builtin_spec.target,
                        "effect_class": builtin_spec.effect_class.value,
                    }
                )
            request = ToolCallRequest(
                run_id=run_id,
                tool_name=tool_name,
                arguments=arguments,
                metadata=request_metadata,
            )

            violations = self._guardrails.check(request)
            if violations:
                for violation in violations:
                    self._emit_event_sync(
                        event_type="guardrail.violation",
                        run_id=run_id,
                        context=context,
                        payload={
                            "tool_name": tool_name,
                            "code": violation.code,
                            "message": violation.message,
                            "details": violation.details,
                        },
                    )
                raise HarnessError(
                    code="GUARDRAIL_DENIED",
                    category="guardrail",
                    message=f"Guardrail denied tool call: {tool_name}",
                    retryable=False,
                    details={
                        "tool_name": tool_name,
                        "violations": [
                            {
                                "code": violation.code,
                                "message": violation.message,
                                "details": violation.details,
                            }
                            for violation in violations
                        ],
                    },
                )

            capability_policy_evidence: list[dict[str, Any]] | None = None
            capability_timeout_seconds: float | None = None
            tool_policy_evidence: list[dict[str, Any]] = []
            tool_policy_constraints: dict[str, Any] = {}
            if capability_binding is None:
                permission_decision = self._permission_controller.check_tool_call(
                    request,
                    context,
                    resolve_sync_value=self._resolve_sync_value,
                )
                self._enforce_policy_decision(
                    decision=permission_decision,
                    checkpoint="permission.before_tool_call",
                    run_id=run_id,
                    context=context,
                )
                tool_policy_evidence.append(
                    {
                        "action": permission_decision.action.value,
                        "checkpoint": "permission.before_tool_call",
                        "reason_code": permission_decision.reason_code,
                    }
                )
                decision = self._run_policy_sync(
                    method_name="before_tool_call",
                    payload=request,
                    run_input=None,
                    context=context,
                )
                tool_policy_constraints = dict(decision.constraints)
                tool_policy_evidence.append(
                    {
                        "action": decision.action.value,
                        "checkpoint": "before_tool_call",
                        "reason_code": decision.reason_code,
                    }
                )
                self._enforce_policy_decision(
                    decision=decision,
                    checkpoint="before_tool_call",
                    run_id=run_id,
                    context=context,
                )
                if decision.action == PolicyAction.ALLOW_WITH_REDACTION and getattr(
                    fc, "arguments", None
                ):
                    fc.arguments = self._apply_redactions_to_object(
                        dict(fc.arguments),
                        decision.redactions,
                    )

            emitted_arguments = self._normalize_tool_arguments(getattr(fc, "arguments", None))
            step = self._start_tool_step(
                run_id=run_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                context=context,
            )
            self._emit_event_sync(
                event_type="tool.call.started",
                run_id=run_id,
                context=context,
                payload={
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "step_id": step["step_id"],
                    "step_name": step["step_name"],
                    "step_index": step["step_index"],
                    "argument_keys": sorted(emitted_arguments.keys()),
                    "arguments": emitted_arguments,
                },
            )
            if provider_info:
                operation = provider_info["operation"]
                self._emit_event_sync(
                    event_type=f"context.provider.{operation}.started",
                    run_id=run_id,
                    context=context,
                    payload={
                        "provider_id": provider_info["provider_id"],
                        "provider_name": provider_info["provider_name"],
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "argument_keys": sorted(emitted_arguments.keys()),
                    },
                )

            fc._agnoclaw_tool_runtime = {
                "context": context,
                "run_context": run_context,
                "event_sink": self._event_sink,
                "event_sink_mode": self._event_sink_mode.value,
                "session_metadata": dict(self._session_metadata),
                "parent_run_id": run_id,
                "parent_tool_name": tool_name,
                "parent_tool_call_id": tool_call_id,
                "parent_step_id": step["step_id"],
                **(
                    {
                        "capability_reference": capability_binding.reference,
                        "capability_policy_evidence": capability_policy_evidence,
                        "capability_timeout_seconds": capability_timeout_seconds,
                    }
                    if capability_binding is not None
                    else {}
                ),
                **(
                    {
                        "arguments": dict(getattr(fc, "arguments", None) or {}),
                        "policy_evidence": tuple(tool_policy_evidence),
                        "policy_constraints": tool_policy_constraints,
                        "policy_version": capability_policy_version(
                            self._policy_engines,
                            self._permission_controller,
                        ),
                    }
                    if governed_builtin
                    else {}
                ),
            }
        except HarnessError as exc:
            self._raise_agent_run_exception(exc)

    def _handle_tool_post_hook(self, *, fc, run_context) -> None:
        if fc is None or getattr(fc, "function", None) is None:
            return

        try:
            run_id = self._run_id_from_tool_hook(run_context=run_context, fc=fc)
            context = self._context_from_run_context(run_context)
            tool_name = getattr(fc.function, "name", "unknown_tool")
            capability_binding = self._capability_tool_map.get(tool_name)
            if capability_binding is not None:
                run_id = self._active_runtime_run_id.get() or run_id
            tool_call_id = self._tool_call_id(fc)
            provider_info = self._context_provider_tool_map.get(tool_name)
            result = ToolCallResult(
                run_id=run_id,
                tool_name=tool_name,
                arguments=dict(getattr(fc, "arguments", None) or {}),
                output=getattr(fc, "result", None),
                error=getattr(fc, "error", None),
                metadata={"tool_call_id": tool_call_id},
            )

            if capability_binding is None and not getattr(
                fc, "_agnoclaw_builtin_post_policy", False
            ):
                decision = self._run_policy_sync(
                    method_name="after_tool_call",
                    payload=result,
                    run_input=None,
                    context=context,
                )
                self._enforce_policy_decision(
                    decision=decision,
                    checkpoint="after_tool_call",
                    run_id=run_id,
                    context=context,
                )

                if decision.action == PolicyAction.ALLOW_WITH_REDACTION and hasattr(fc, "result"):
                    fc.result = self._apply_redactions_to_object(
                        fc.result,
                        decision.redactions,
                    )

            step, duration_ms = self._finish_tool_step(
                run_id=run_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                context=context,
            )
            result_preview = self._format_result_preview(getattr(fc, "result", None))
            event_type = "tool.call.failed" if getattr(fc, "error", None) else "tool.call.completed"
            payload = {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "step_id": step.get("step_id"),
                "step_name": step.get("step_name"),
                "step_index": step.get("step_index"),
                "argument_keys": sorted(result.arguments.keys()),
                "arguments": self._normalize_tool_arguments(result.arguments),
                "error": getattr(fc, "error", None),
                "duration_ms": duration_ms,
                "result_preview": result_preview,
                "result_ref": self._result_identity(
                    getattr(fc, "result", None), self._result_ref_keys
                ),
                "result_chars": (
                    len(str(getattr(fc, "result", "")))
                    if getattr(fc, "result", None) is not None
                    else 0
                ),
            }
            self._emit_event_sync(
                event_type=event_type,
                run_id=run_id,
                context=context,
                payload=payload,
            )
            if provider_info:
                operation = provider_info["operation"]
                provider_event_type = (
                    f"context.provider.{operation}.failed"
                    if getattr(fc, "error", None)
                    else f"context.provider.{operation}.completed"
                )
                self._emit_event_sync(
                    event_type=provider_event_type,
                    run_id=run_id,
                    context=context,
                    payload={
                        "provider_id": provider_info["provider_id"],
                        "provider_name": provider_info["provider_name"],
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "duration_ms": duration_ms,
                        "result_preview": result_preview,
                    },
                )
        except HarnessError as exc:
            self._raise_agent_run_exception(exc)

    def _active_session_id(self, override: str | None) -> str | None:
        if override is not None:
            return override
        candidate = self.session_id or getattr(self._agent, "session_id", None)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
        return None

    def _build_execution_context(
        self,
        *,
        user_id: str | None,
        session_id: str | None,
        workspace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExecutionContext:
        merged_metadata = dict(self._context_metadata)
        merged_metadata.setdefault("permission_mode", self.permission_mode)
        if metadata:
            merged_metadata.update(metadata)
        return ExecutionContext.create(
            user_id=user_id,
            session_id=session_id,
            workspace_id=workspace_id or str(self.workspace.path),
            tenant_id=self._tenant_id,
            org_id=self._org_id,
            team_id=self._team_id,
            roles=self._roles,
            scopes=self._scopes,
            request_id=self._request_id,
            trace_id=self._trace_id,
            metadata=merged_metadata,
        )

    def _emit_event_sync(
        self,
        *,
        event_type: str,
        run_id: str,
        context: ExecutionContext,
        payload: dict[str, Any] | None = None,
    ) -> None:
        merged_payload = {
            **self._session_metadata,
            **self._trace_payload_from_context(context),
            **(payload or {}),
        }
        event = build_event(
            event_type=event_type,
            run_id=run_id,
            context=context,
            payload=merged_payload,
        )
        durable_projection = project_runtime_event_sync(self, event)
        try:
            maybe_awaitable = self._event_sink.emit(event)
            if inspect.isawaitable(maybe_awaitable):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    owning_loop = self._owning_loop
                    if (
                        owning_loop is not None
                        and not owning_loop.is_closed()
                        and owning_loop.is_running()
                    ):
                        # Worker threads must route loop-bound sinks back to the
                        # active owning loop; a stale loop cannot drain them.
                        future = asyncio.run_coroutine_threadsafe(
                            self._await_value(maybe_awaitable), owning_loop
                        )
                        if (
                            self._event_sink_mode == EventSinkMode.FAIL_CLOSED
                            and not durable_projection
                        ):
                            future.result()
                        else:
                            future.add_done_callback(self._build_event_task_callback(event_type))
                    else:
                        # Fully synchronous runs can safely use a temporary loop.
                        asyncio.run(self._await_value(maybe_awaitable))
                else:
                    if (
                        self._event_sink_mode == EventSinkMode.FAIL_CLOSED
                        and not durable_projection
                    ):
                        raise HarnessError(
                            code="EVENT_SINK_ASYNC_IN_SYNC",
                            category="event",
                            message=(
                                "Event sink returned an awaitable during sync run while "
                                "fail-closed mode is active."
                            ),
                            retryable=False,
                            details={"event_type": event_type},
                        )
                    task = loop.create_task(self._await_value(maybe_awaitable))
                    task.add_done_callback(self._build_event_task_callback(event_type))
        except Exception as exc:
            if self._event_sink_mode == EventSinkMode.FAIL_CLOSED and not durable_projection:
                raise HarnessError(
                    code="EVENT_SINK_FAILED",
                    category="event",
                    message=f"Failed to emit event '{event_type}': {exc}",
                    retryable=True,
                    details={"event_type": event_type},
                ) from exc
            logger.warning("Event sink failure for %s: %s", event_type, exc)

    def _build_event_task_callback(self, event_type: str):
        # Handles the done-callback for both an asyncio.Task (running-loop path)
        # and a concurrent.futures.Future (run_coroutine_threadsafe path). Both
        # expose .result(); the callback only reads it to surface swallowed
        # errors, so the same handler serves either type.
        def _callback(
            task: asyncio.Future | concurrent.futures.Future,
        ) -> None:
            try:
                task.result()
            except Exception as exc:  # pragma: no cover - loop callback path
                logger.warning("Async event sink failure for %s: %s", event_type, exc)

        return _callback

    @staticmethod
    async def _await_value(value: Awaitable[Any]) -> Any:
        return await value

    async def _emit_event_async(
        self,
        *,
        event_type: str,
        run_id: str,
        context: ExecutionContext,
        payload: dict[str, Any] | None = None,
    ) -> None:
        merged_payload = {
            **self._session_metadata,
            **self._trace_payload_from_context(context),
            **(payload or {}),
        }
        event = build_event(
            event_type=event_type,
            run_id=run_id,
            context=context,
            payload=merged_payload,
        )
        durable_projection = await project_runtime_event_async(self, event)
        try:
            maybe_awaitable = self._event_sink.emit(event)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        except Exception as exc:
            if self._event_sink_mode == EventSinkMode.FAIL_CLOSED and not durable_projection:
                raise HarnessError(
                    code="EVENT_SINK_FAILED",
                    category="event",
                    message=f"Failed to emit event '{event_type}': {exc}",
                    retryable=True,
                    details={"event_type": event_type},
                ) from exc
            logger.warning("Event sink failure for %s: %s", event_type, exc)

    def _resolve_sync_value(self, value: Any, *, operation: str) -> Any:
        if not inspect.isawaitable(value):
            return value
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._await_value(value))
        raise HarnessError(
            code="ASYNC_VALUE_IN_SYNC_RUN",
            category="validation",
            message=(
                f"{operation} returned awaitable in sync run. Use arun() or sync implementations."
            ),
            retryable=False,
        )

    async def _resolve_async_value(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    def _enforce_policy_decision(
        self,
        *,
        decision: PolicyDecision,
        checkpoint: str,
        run_id: str,
        context: ExecutionContext,
    ) -> None:
        self._emit_event_sync(
            event_type="policy.decision",
            run_id=run_id,
            context=context,
            payload={
                "checkpoint": checkpoint,
                "action": decision.action.value,
                "reason_code": decision.reason_code,
                "message": decision.message,
            },
        )
        if decision.action == PolicyAction.DENY:
            raise HarnessError(
                code="POLICY_DENIED",
                category="policy",
                message=decision.message or f"Policy denied at {checkpoint}",
                retryable=False,
                details={
                    "checkpoint": checkpoint,
                    "reason_code": decision.reason_code,
                },
            )

    async def _enforce_policy_decision_async(
        self,
        *,
        decision: PolicyDecision,
        checkpoint: str,
        run_id: str,
        context: ExecutionContext,
    ) -> None:
        await self._emit_event_async(
            event_type="policy.decision",
            run_id=run_id,
            context=context,
            payload={
                "checkpoint": checkpoint,
                "action": decision.action.value,
                "reason_code": decision.reason_code,
                "message": decision.message,
            },
        )
        if decision.action == PolicyAction.DENY:
            raise HarnessError(
                code="POLICY_DENIED",
                category="policy",
                message=decision.message or f"Policy denied at {checkpoint}",
                retryable=False,
                details={
                    "checkpoint": checkpoint,
                    "reason_code": decision.reason_code,
                },
            )

    def _run_policy_sync(
        self,
        *,
        method_name: str,
        payload: Any,
        run_input: RunInput | None,
        context: ExecutionContext,
    ) -> PolicyDecision:
        run_id = self._policy_run_id(payload=payload, run_input=run_input)
        for engine_name, engine in self._policy_engines:
            method = getattr(engine, method_name, None)
            if method is None:
                continue
            is_pack_policy = engine_name.startswith("pack:")
            if is_pack_policy:
                self._emit_event_sync(
                    event_type="pack.policy.started",
                    run_id=run_id,
                    context=context,
                    payload={
                        "pack": engine_name.removeprefix("pack:"),
                        "checkpoint": method_name,
                    },
                )
            try:
                decision = self._resolve_sync_value(
                    method(payload, context),
                    operation=f"policy.{engine_name}.{method_name}",
                )
            except Exception as exc:
                if is_pack_policy:
                    self._emit_event_sync(
                        event_type="pack.policy.failed",
                        run_id=run_id,
                        context=context,
                        payload={
                            "pack": engine_name.removeprefix("pack:"),
                            "checkpoint": method_name,
                            "error": str(exc),
                        },
                    )
                if self._policy_fail_open:
                    logger.warning(
                        "Policy engine failed at %s; fail-open enabled: %s",
                        method_name,
                        exc,
                    )
                    continue
                raise HarnessError(
                    code="POLICY_EVALUATION_FAILED",
                    category="policy",
                    message=f"Policy evaluation failed at {method_name}: {exc}",
                    retryable=False,
                ) from exc
            if not isinstance(decision, PolicyDecision):
                raise HarnessError(
                    code="POLICY_INVALID_DECISION",
                    category="policy",
                    message=(
                        f"Policy method {method_name} returned invalid type: "
                        f"{type(decision).__name__}"
                    ),
                    retryable=False,
                )
            if is_pack_policy:
                self._emit_event_sync(
                    event_type="pack.policy.completed",
                    run_id=run_id,
                    context=context,
                    payload={
                        "pack": engine_name.removeprefix("pack:"),
                        "checkpoint": method_name,
                        "action": decision.action.value,
                        "reason_code": decision.reason_code,
                    },
                )
            if (
                run_input is not None
                and decision.action == PolicyAction.ALLOW_WITH_CONSTRAINTS
                and decision.constraints
            ):
                run_input.metadata.setdefault("policy_constraints", {}).update(decision.constraints)
            if decision.action != PolicyAction.ALLOW:
                return decision
        return PolicyDecision.allow()

    async def _run_policy_async(
        self,
        *,
        method_name: str,
        payload: Any,
        run_input: RunInput | None,
        context: ExecutionContext,
    ) -> PolicyDecision:
        run_id = self._policy_run_id(payload=payload, run_input=run_input)
        for engine_name, engine in self._policy_engines:
            method = getattr(engine, method_name, None)
            if method is None:
                continue
            is_pack_policy = engine_name.startswith("pack:")
            if is_pack_policy:
                await self._emit_event_async(
                    event_type="pack.policy.started",
                    run_id=run_id,
                    context=context,
                    payload={
                        "pack": engine_name.removeprefix("pack:"),
                        "checkpoint": method_name,
                    },
                )
            try:
                decision = await self._resolve_async_value(method(payload, context))
            except Exception as exc:
                if is_pack_policy:
                    await self._emit_event_async(
                        event_type="pack.policy.failed",
                        run_id=run_id,
                        context=context,
                        payload={
                            "pack": engine_name.removeprefix("pack:"),
                            "checkpoint": method_name,
                            "error": str(exc),
                        },
                    )
                if self._policy_fail_open:
                    logger.warning(
                        "Policy engine failed at %s; fail-open enabled: %s",
                        method_name,
                        exc,
                    )
                    continue
                raise HarnessError(
                    code="POLICY_EVALUATION_FAILED",
                    category="policy",
                    message=f"Policy evaluation failed at {method_name}: {exc}",
                    retryable=False,
                ) from exc
            if not isinstance(decision, PolicyDecision):
                raise HarnessError(
                    code="POLICY_INVALID_DECISION",
                    category="policy",
                    message=(
                        f"Policy method {method_name} returned invalid type: "
                        f"{type(decision).__name__}"
                    ),
                    retryable=False,
                )
            if is_pack_policy:
                await self._emit_event_async(
                    event_type="pack.policy.completed",
                    run_id=run_id,
                    context=context,
                    payload={
                        "pack": engine_name.removeprefix("pack:"),
                        "checkpoint": method_name,
                        "action": decision.action.value,
                        "reason_code": decision.reason_code,
                    },
                )
            if (
                run_input is not None
                and decision.action == PolicyAction.ALLOW_WITH_CONSTRAINTS
                and decision.constraints
            ):
                run_input.metadata.setdefault("policy_constraints", {}).update(decision.constraints)
            if decision.action != PolicyAction.ALLOW:
                return decision
        return PolicyDecision.allow()

    @staticmethod
    def _policy_run_id(*, payload: Any, run_input: RunInput | None) -> str:
        if run_input is not None and run_input.run_id:
            return run_input.run_id
        payload_run_id = getattr(payload, "run_id", None)
        if isinstance(payload_run_id, str) and payload_run_id:
            return payload_run_id
        return f"policy_{uuid4().hex}"

    def _wrap_pack_pre_hook(self, pack_name: str, hook: PreRunHook) -> PreRunHook:
        hook_name = getattr(hook, "__name__", hook.__class__.__name__)

        async def _await_result(result: Awaitable[RunInput | None], run_input, context):
            try:
                resolved = await result
            except Exception as exc:
                await self._emit_event_async(
                    event_type="pack.hook.failed",
                    run_id=run_input.run_id,
                    context=context,
                    payload={
                        "pack": pack_name,
                        "hook": hook_name,
                        "kind": "pre",
                        "error": str(exc),
                    },
                )
                raise
            await self._emit_event_async(
                event_type="pack.hook.completed",
                run_id=run_input.run_id,
                context=context,
                payload={"pack": pack_name, "hook": hook_name, "kind": "pre"},
            )
            return resolved

        def _wrapped(run_input: RunInput, context) -> RunInput | None | Awaitable[RunInput | None]:
            self._emit_event_sync(
                event_type="pack.hook.started",
                run_id=run_input.run_id,
                context=context,
                payload={"pack": pack_name, "hook": hook_name, "kind": "pre"},
            )
            try:
                result = hook(run_input, context)
            except Exception as exc:
                self._emit_event_sync(
                    event_type="pack.hook.failed",
                    run_id=run_input.run_id,
                    context=context,
                    payload={
                        "pack": pack_name,
                        "hook": hook_name,
                        "kind": "pre",
                        "error": str(exc),
                    },
                )
                raise
            if inspect.isawaitable(result):
                return _await_result(result, run_input, context)
            self._emit_event_sync(
                event_type="pack.hook.completed",
                run_id=run_input.run_id,
                context=context,
                payload={"pack": pack_name, "hook": hook_name, "kind": "pre"},
            )
            return result

        return _wrapped

    def _wrap_pack_post_hook(self, pack_name: str, hook: PostRunHook) -> PostRunHook:
        hook_name = getattr(hook, "__name__", hook.__class__.__name__)

        async def _await_result(
            result: Awaitable[RunResultEnvelope | None],
            run_input,
            context,
        ):
            try:
                resolved = await result
            except Exception as exc:
                await self._emit_event_async(
                    event_type="pack.hook.failed",
                    run_id=run_input.run_id,
                    context=context,
                    payload={
                        "pack": pack_name,
                        "hook": hook_name,
                        "kind": "post",
                        "error": str(exc),
                    },
                )
                raise
            await self._emit_event_async(
                event_type="pack.hook.completed",
                run_id=run_input.run_id,
                context=context,
                payload={"pack": pack_name, "hook": hook_name, "kind": "post"},
            )
            return resolved

        def _wrapped(
            run_input: RunInput,
            result: RunResultEnvelope,
            context,
        ) -> RunResultEnvelope | None | Awaitable[RunResultEnvelope | None]:
            self._emit_event_sync(
                event_type="pack.hook.started",
                run_id=run_input.run_id,
                context=context,
                payload={"pack": pack_name, "hook": hook_name, "kind": "post"},
            )
            try:
                maybe_result = hook(run_input, result, context)
            except Exception as exc:
                self._emit_event_sync(
                    event_type="pack.hook.failed",
                    run_id=run_input.run_id,
                    context=context,
                    payload={
                        "pack": pack_name,
                        "hook": hook_name,
                        "kind": "post",
                        "error": str(exc),
                    },
                )
                raise
            if inspect.isawaitable(maybe_result):
                return _await_result(maybe_result, run_input, context)
            self._emit_event_sync(
                event_type="pack.hook.completed",
                run_id=run_input.run_id,
                context=context,
                payload={"pack": pack_name, "hook": hook_name, "kind": "post"},
            )
            return maybe_result

        return _wrapped

    def _wrap_pack_lifecycle_hook(
        self,
        pack_name: str,
        event_type: str,
        hook: LifecycleHook,
    ) -> LifecycleHook:
        hook_name = getattr(hook, "__name__", hook.__class__.__name__)

        async def _await_result(
            result: Awaitable[LifecycleHookRequest | None],
            event: LifecycleHookRequest,
            context,
        ):
            try:
                resolved = await result
            except Exception as exc:
                await self._emit_event_async(
                    event_type="pack.hook.failed",
                    run_id=event.run_id or "",
                    context=context,
                    payload={
                        "pack": pack_name,
                        "hook": hook_name,
                        "kind": "lifecycle",
                        "lifecycle_event": event_type,
                        "error": str(exc),
                    },
                )
                raise
            await self._emit_event_async(
                event_type="pack.hook.completed",
                run_id=event.run_id or "",
                context=context,
                payload={
                    "pack": pack_name,
                    "hook": hook_name,
                    "kind": "lifecycle",
                    "lifecycle_event": event_type,
                },
            )
            return resolved

        def _wrapped(
            event: LifecycleHookRequest,
            context,
        ) -> LifecycleHookRequest | None | Awaitable[LifecycleHookRequest | None]:
            self._emit_event_sync(
                event_type="pack.hook.started",
                run_id=event.run_id or "",
                context=context,
                payload={
                    "pack": pack_name,
                    "hook": hook_name,
                    "kind": "lifecycle",
                    "lifecycle_event": event_type,
                },
            )
            try:
                result = hook(event, context)
            except Exception as exc:
                self._emit_event_sync(
                    event_type="pack.hook.failed",
                    run_id=event.run_id or "",
                    context=context,
                    payload={
                        "pack": pack_name,
                        "hook": hook_name,
                        "kind": "lifecycle",
                        "lifecycle_event": event_type,
                        "error": str(exc),
                    },
                )
                raise
            if inspect.isawaitable(result):
                return _await_result(result, event, context)
            self._emit_event_sync(
                event_type="pack.hook.completed",
                run_id=event.run_id or "",
                context=context,
                payload={
                    "pack": pack_name,
                    "hook": hook_name,
                    "kind": "lifecycle",
                    "lifecycle_event": event_type,
                },
            )
            return result

        return _wrapped

    def _load_workspace_lifecycle_hooks(self) -> None:
        """Register lifecycle hooks discovered from workspace/project/global files."""
        for spec in self._workspace_hook_specs:
            event_type = str(spec["event"])
            self._lifecycle_hooks.setdefault(event_type, []).append(
                self._build_workspace_lifecycle_hook(spec)
            )

    def _build_workspace_lifecycle_hook(
        self,
        spec: dict[str, Any],
    ) -> LifecycleHook:
        name = str(spec.get("name") or "workspace-hook")
        scope = str(spec.get("scope") or "workspace")
        command = str(spec["command"])
        try:
            command_argv = shlex.split(command, posix=os.name != "nt")
        except ValueError as exc:
            raise ValueError(f"invalid workspace hook command for {scope}:{name}") from exc
        if not command_argv:
            raise ValueError(f"empty workspace hook command for {scope}:{name}")
        cwd = str(spec.get("cwd") or self.workspace.path)
        path = str(spec.get("path") or "")
        timeout = spec.get("timeout_seconds")
        timeout_seconds = int(timeout) if timeout is not None else 30

        def _hook(
            event: LifecycleHookRequest,
            context: ExecutionContext,
        ) -> LifecycleHookRequest | None:
            payload = {
                "event_type": event.event_type,
                "run_id": event.run_id,
                "metadata": dict(event.metadata),
                "context": self._context_to_metadata(context),
            }
            inherited_names = {
                "COMSPEC",
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "PATH",
                "PATHEXT",
                "SYSTEMROOT",
                "WINDIR",
                *self._workspace_hook_env_allowlist,
            }
            env = {
                **{key: os.environ[key] for key in inherited_names if key in os.environ},
                "AGNOCLAW_HOOK_EVENT": event.event_type,
                "AGNOCLAW_HOOK_NAME": name,
                "AGNOCLAW_HOOK_SCOPE": scope,
                "AGNOCLAW_HOOK_RUN_ID": event.run_id or "",
                "AGNOCLAW_WORKSPACE_DIR": str(self.workspace.path),
                "AGNOCLAW_WORKTREE_DIR": str(Path.cwd().resolve()),
                "AGNOCLAW_HOOK_METADATA_JSON": json.dumps(
                    event.metadata,
                    sort_keys=True,
                    default=str,
                ),
                "AGNOCLAW_HOOK_PAYLOAD_JSON": json.dumps(
                    payload,
                    sort_keys=True,
                    default=str,
                ),
            }
            if self.workspace._project_dir:
                env["AGNOCLAW_PROJECT_DIR"] = str(self.workspace._project_dir)
            if self.workspace._global_dir:
                env["AGNOCLAW_GLOBAL_DIR"] = str(self.workspace._global_dir)

            self._emit_event_sync(
                event_type="workspace.hook.started",
                run_id=event.run_id or "",
                context=context,
                payload={
                    "name": name,
                    "scope": scope,
                    "hook_event": event.event_type,
                    "path": path,
                },
            )
            try:
                result = subprocess.run(
                    command_argv,
                    shell=False,
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
            except Exception as exc:
                self._emit_event_sync(
                    event_type="workspace.hook.failed",
                    run_id=event.run_id or "",
                    context=context,
                    payload={
                        "name": name,
                        "scope": scope,
                        "hook_event": event.event_type,
                        "path": path,
                        "error": str(exc),
                    },
                )
                raise
            if result.returncode != 0:
                message = result.stderr.strip() or result.stdout.strip()
                self._emit_event_sync(
                    event_type="workspace.hook.failed",
                    run_id=event.run_id or "",
                    context=context,
                    payload={
                        "name": name,
                        "scope": scope,
                        "hook_event": event.event_type,
                        "path": path,
                        "exit_code": result.returncode,
                        "error": message,
                    },
                )
                raise RuntimeError(
                    f"workspace hook {scope}:{name} exited {result.returncode}: {message}"
                )

            updated = self._workspace_hook_result(event, result.stdout)
            self._emit_event_sync(
                event_type="workspace.hook.completed",
                run_id=event.run_id or "",
                context=context,
                payload={
                    "name": name,
                    "scope": scope,
                    "hook_event": event.event_type,
                    "path": path,
                    "stdout_chars": len(result.stdout or ""),
                },
            )
            return updated

        _hook.__name__ = f"workspace_hook_{scope}_{name}".replace("-", "_")
        return _hook

    @staticmethod
    def _workspace_hook_result(
        event: LifecycleHookRequest,
        stdout: str,
    ) -> LifecycleHookRequest:
        text = str(stdout or "").strip()
        if not text:
            return event
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return event
        if not isinstance(payload, dict):
            return event
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            event.metadata.update(metadata)
        return event

    def add_lifecycle_hook(self, event_type: str, hook: LifecycleHook) -> None:
        """Register a generic lifecycle hook for a named checkpoint."""
        self._lifecycle_hooks.setdefault(event_type, []).append(hook)

    def _run_lifecycle_hooks_sync(
        self,
        event_type: str,
        *,
        context: ExecutionContext,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LifecycleHookRequest:
        current = LifecycleHookRequest(
            event_type=event_type,
            run_id=run_id,
            metadata=self._lifecycle_metadata(metadata),
        )
        for hook in self._lifecycle_hooks.get(event_type, []):
            hook_name = getattr(hook, "__name__", hook.__class__.__name__)
            try:
                maybe_result = self._resolve_sync_value(
                    hook(current, context),
                    operation=f"lifecycle_hook:{event_type}:{hook_name}",
                )
            except Exception as exc:
                raise HarnessError(
                    code="HOOK_LIFECYCLE_FAILED",
                    category="hook",
                    message=f"Lifecycle hook failed: {event_type}:{hook_name}: {exc}",
                    retryable=False,
                ) from exc
            if maybe_result is None:
                continue
            if not isinstance(maybe_result, LifecycleHookRequest):
                raise HarnessError(
                    code="HOOK_LIFECYCLE_INVALID_RETURN",
                    category="hook",
                    message=(
                        f"Lifecycle hook {event_type}:{hook_name} must return "
                        "LifecycleHookRequest or None"
                    ),
                    retryable=False,
                )
            current = maybe_result
        return current

    async def _run_lifecycle_hooks_async(
        self,
        event_type: str,
        *,
        context: ExecutionContext,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LifecycleHookRequest:
        current = LifecycleHookRequest(
            event_type=event_type,
            run_id=run_id,
            metadata=self._lifecycle_metadata(metadata),
        )
        for hook in self._lifecycle_hooks.get(event_type, []):
            hook_name = getattr(hook, "__name__", hook.__class__.__name__)
            try:
                maybe_result = await self._resolve_async_value(hook(current, context))
            except Exception as exc:
                raise HarnessError(
                    code="HOOK_LIFECYCLE_FAILED",
                    category="hook",
                    message=f"Lifecycle hook failed: {event_type}:{hook_name}: {exc}",
                    retryable=False,
                ) from exc
            if maybe_result is None:
                continue
            if not isinstance(maybe_result, LifecycleHookRequest):
                raise HarnessError(
                    code="HOOK_LIFECYCLE_INVALID_RETURN",
                    category="hook",
                    message=(
                        f"Lifecycle hook {event_type}:{hook_name} must return "
                        "LifecycleHookRequest or None"
                    ),
                    retryable=False,
                )
            current = maybe_result
        return current

    def _lifecycle_metadata(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        values = dict(metadata or {})
        values.setdefault("workspace_dir", str(self.workspace.path))
        values.setdefault("worktree_dir", str(Path.cwd().resolve()))
        if self.workspace._project_dir:
            values.setdefault("project_dir", str(self.workspace._project_dir))
        if self.workspace._global_dir:
            values.setdefault("global_dir", str(self.workspace._global_dir))
        return values

    def _run_pre_hooks_sync(
        self,
        *,
        run_input: RunInput,
        context: ExecutionContext,
    ) -> RunInput:
        current = run_input
        for hook in self._pre_run_hooks:
            hook_name = getattr(hook, "__name__", hook.__class__.__name__)
            try:
                maybe_result = self._resolve_sync_value(
                    hook(current, context),
                    operation=f"pre_hook:{hook_name}",
                )
            except Exception as exc:
                raise HarnessError(
                    code="HOOK_PRE_FAILED",
                    category="hook",
                    message=f"Pre-run hook failed: {hook_name}: {exc}",
                    retryable=False,
                ) from exc
            if maybe_result is None:
                continue
            if not isinstance(maybe_result, RunInput):
                raise HarnessError(
                    code="HOOK_PRE_INVALID_RETURN",
                    category="hook",
                    message=f"Pre-run hook {hook_name} must return RunInput or None",
                    retryable=False,
                )
            current = maybe_result
        return current

    async def _run_pre_hooks_async(
        self,
        *,
        run_input: RunInput,
        context: ExecutionContext,
    ) -> RunInput:
        current = run_input
        for hook in self._pre_run_hooks:
            hook_name = getattr(hook, "__name__", hook.__class__.__name__)
            try:
                maybe_result = await self._resolve_async_value(hook(current, context))
            except Exception as exc:
                raise HarnessError(
                    code="HOOK_PRE_FAILED",
                    category="hook",
                    message=f"Pre-run hook failed: {hook_name}: {exc}",
                    retryable=False,
                ) from exc
            if maybe_result is None:
                continue
            if not isinstance(maybe_result, RunInput):
                raise HarnessError(
                    code="HOOK_PRE_INVALID_RETURN",
                    category="hook",
                    message=f"Pre-run hook {hook_name} must return RunInput or None",
                    retryable=False,
                )
            current = maybe_result
        return current

    def _run_post_hooks_sync(
        self,
        *,
        run_input: RunInput,
        result: RunResultEnvelope,
        context: ExecutionContext,
    ) -> RunResultEnvelope:
        current = result
        for hook in self._post_run_hooks:
            hook_name = getattr(hook, "__name__", hook.__class__.__name__)
            try:
                maybe_result = self._resolve_sync_value(
                    hook(run_input, current, context),
                    operation=f"post_hook:{hook_name}",
                )
            except Exception as exc:
                raise HarnessError(
                    code="HOOK_POST_FAILED",
                    category="hook",
                    message=f"Post-run hook failed: {hook_name}: {exc}",
                    retryable=False,
                ) from exc
            if maybe_result is None:
                continue
            if not isinstance(maybe_result, RunResultEnvelope):
                raise HarnessError(
                    code="HOOK_POST_INVALID_RETURN",
                    category="hook",
                    message=f"Post-run hook {hook_name} must return RunResultEnvelope or None",
                    retryable=False,
                )
            current = maybe_result
        return current

    async def _run_post_hooks_async(
        self,
        *,
        run_input: RunInput,
        result: RunResultEnvelope,
        context: ExecutionContext,
    ) -> RunResultEnvelope:
        current = result
        for hook in self._post_run_hooks:
            hook_name = getattr(hook, "__name__", hook.__class__.__name__)
            try:
                maybe_result = await self._resolve_async_value(hook(run_input, current, context))
            except Exception as exc:
                raise HarnessError(
                    code="HOOK_POST_FAILED",
                    category="hook",
                    message=f"Post-run hook failed: {hook_name}: {exc}",
                    retryable=False,
                ) from exc
            if maybe_result is None:
                continue
            if not isinstance(maybe_result, RunResultEnvelope):
                raise HarnessError(
                    code="HOOK_POST_INVALID_RETURN",
                    category="hook",
                    message=f"Post-run hook {hook_name} must return RunResultEnvelope or None",
                    retryable=False,
                )
            current = maybe_result
        return current

    @staticmethod
    def _extract_event_content(event: Any) -> str:
        if event is None:
            return ""
        if isinstance(event, str):
            return event
        event_name = AgentHarness._event_name(event)
        if event_name and event_name not in _ASSISTANT_STREAM_EVENTS:
            return ""
        content = getattr(event, "content", None)
        if content is not None:
            return str(content)
        if isinstance(event, dict) and "content" in event:
            return str(event["content"])
        return ""

    @staticmethod
    def _event_name(event: Any) -> str:
        if event is None:
            return ""
        raw_event = getattr(event, "event", None)
        if raw_event is None and isinstance(event, dict):
            raw_event = event.get("event")
        return str(raw_event or "")

    @staticmethod
    def _event_attr(event: Any, key: str, default: Any = None) -> Any:
        if isinstance(event, dict):
            return event.get(key, default)
        return getattr(event, key, default)

    @staticmethod
    def _normalize_tool_arguments(arguments: Any) -> dict[str, Any]:
        if arguments is None:
            return {}
        if isinstance(arguments, dict):
            raw = arguments
        else:
            items = getattr(arguments, "items", None)
            if not callable(items):
                return {}
            try:
                raw = dict(items())
            except Exception:
                return {}
        serialized = AgentHarness._serialize_event_value(raw)
        return serialized if isinstance(serialized, dict) else {}

    @staticmethod
    def _serialize_event_value(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {
                str(key): AgentHarness._serialize_event_value(item) for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [AgentHarness._serialize_event_value(item) for item in value]

        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            try:
                return AgentHarness._serialize_event_value(to_dict())
            except Exception:
                pass

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return AgentHarness._serialize_event_value(model_dump())
            except Exception:
                pass

        if hasattr(value, "__dict__"):
            return {
                str(key): AgentHarness._serialize_event_value(item)
                for key, item in vars(value).items()
                if not key.startswith("_")
            }

        return str(value)

    @staticmethod
    def _stream_event_details(event: Any) -> dict[str, Any]:
        if event is None:
            return {}
        if isinstance(event, dict):
            details = AgentHarness._serialize_event_value(event)
        else:
            to_dict = getattr(event, "to_dict", None)
            if callable(to_dict):
                try:
                    details = AgentHarness._serialize_event_value(to_dict())
                except Exception:
                    details = AgentHarness._serialize_event_value(vars(event))
            elif hasattr(event, "__dict__"):
                details = AgentHarness._serialize_event_value(vars(event))
            else:
                details = {"value": AgentHarness._serialize_event_value(event)}

        if not isinstance(details, dict):
            details = {"value": details}
        event_name = AgentHarness._event_name(event)
        if event_name and "event" not in details:
            details["event"] = event_name
        return details

    def _tool_stream_payload(self, event: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        tool_obj = AgentHarness._event_attr(event, "tool", None)

        arguments = AgentHarness._normalize_tool_arguments(
            AgentHarness._event_attr(event, "arguments", None)
        )
        if not arguments and tool_obj is not None:
            arguments = AgentHarness._normalize_tool_arguments(getattr(tool_obj, "arguments", None))
        if arguments:
            payload["argument_keys"] = sorted(arguments.keys())
            payload["arguments"] = arguments

        duration_ms = AgentHarness._event_attr(event, "duration_ms", None)
        if duration_ms is None and tool_obj is not None:
            duration_ms = getattr(tool_obj, "duration_ms", None)
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms

        error = AgentHarness._event_attr(event, "error", None)
        if error is None and tool_obj is not None:
            error = getattr(tool_obj, "error", None)
        if error is not None:
            payload["error"] = AgentHarness._serialize_event_value(error)

        result = AgentHarness._event_attr(event, "result", None)
        if result is None and tool_obj is not None:
            result = getattr(tool_obj, "result", None)
        if result is None and AgentHarness._event_name(event) in {
            "ToolCallCompleted",
            "ToolCallError",
        }:
            result = AgentHarness._event_attr(event, "content", None)
        if result is not None:
            payload["result_preview"] = AgentHarness._format_result_preview(result)
            payload["result_ref"] = self._result_identity(result, self._result_ref_keys)
            payload["result_chars"] = len(str(result))

        return payload

    def _stream_event_summary(self, event: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key in (
            "parent_run_id",
            "step_id",
            "step_name",
            "step_index",
            "tool_name",
            "tool_call_id",
        ):
            value = AgentHarness._event_attr(event, key, None)
            if value is not None:
                payload[key] = value

        tool_obj = AgentHarness._event_attr(event, "tool", None)
        if tool_obj is not None:
            tool_name = getattr(tool_obj, "tool_name", None) or getattr(tool_obj, "name", None)
            tool_call_id = getattr(tool_obj, "tool_call_id", None)
            if tool_name and "tool_name" not in payload:
                payload["tool_name"] = tool_name
            if tool_call_id and "tool_call_id" not in payload:
                payload["tool_call_id"] = tool_call_id
        payload.update(self._tool_stream_payload(event))
        return payload

    @staticmethod
    def _format_tool_invocation_label(
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        limit: int = 140,
    ) -> str:
        if not arguments:
            return tool_name

        rendered_parts: list[str] = []
        for key, value in arguments.items():
            if isinstance(value, str):
                rendered = json.dumps(" ".join(value.split()), ensure_ascii=True)
            else:
                try:
                    rendered = json.dumps(value, ensure_ascii=True, sort_keys=True)
                except TypeError:
                    rendered = json.dumps(str(value), ensure_ascii=True)
            rendered = AgentHarness._truncate_text(rendered, limit=48)
            rendered_parts.append(f"{key}={rendered}")

        label = f"{tool_name}({', '.join(rendered_parts)})"
        return AgentHarness._truncate_text(label, limit=limit)

    @staticmethod
    def _extract_thinking_content(event: Any) -> str:
        reasoning_content = AgentHarness._event_attr(event, "reasoning_content", None)
        if reasoning_content:
            return str(reasoning_content)

        if AgentHarness._event_name(event) == "ReasoningStep":
            content = AgentHarness._event_attr(event, "content", None)
            if content is None:
                return ""
            if isinstance(content, str):
                return content
            summary = getattr(content, "summary", None)
            title = getattr(content, "title", None)
            reasoning = getattr(content, "reasoning", None)
            for value in (summary, title, reasoning):
                if value:
                    return str(value)
            return str(content)

        return ""

    @staticmethod
    def _thinking_phase(event: Any) -> str:
        event_name = AgentHarness._event_name(event)
        if event_name == "ReasoningStarted":
            return "planning"
        if event_name in {"ReasoningStep", "ReasoningContentDelta"}:
            return "analyzing"
        if event_name == "ReasoningCompleted":
            return "evaluating"
        return "analyzing"

    @staticmethod
    def _map_agno_event_type(event: Any) -> str | None:
        raw_event = AgentHarness._event_name(event)
        if not raw_event:
            return None
        mapping = {
            "ToolCallStarted": "tool.call.started",
            "ToolCallCompleted": "tool.call.completed",
            "ToolCallError": "tool.call.failed",
            "ReasoningStarted": "reasoning.started",
            "ReasoningStep": "reasoning.step",
            "ReasoningContentDelta": "reasoning.delta",
            "ReasoningCompleted": "reasoning.completed",
            "MemoryUpdateStarted": "memory.write.started",
            "MemoryUpdateCompleted": "memory.write.completed",
            "SessionSummaryStarted": "session.summary.started",
            "SessionSummaryCompleted": "session.summary.completed",
        }
        return mapping.get(str(raw_event))

    def _can_materialize_isolated_run(
        self,
        *,
        stream: bool,
        skill: str | None,
    ) -> bool:
        """Return whether this invocation has a certified independent Agent view."""
        return (
            self._can_materialize_run_agent(stream=stream, skill=skill)
            and self._isolated_agent_factory_enabled
            and skill is None
            and not self._plan_mode
            and not self._has_non_capability_tools
            and not self._context_providers
            and not self._loaded_packs
            and self._runtime_extensions_allow_isolation()
        )

    def _runtime_extensions_allow_isolation(self) -> bool:
        """Return whether harness-side callbacks are absent for parallel execution."""
        return bool(
            isinstance(self._event_sink, NullEventSink)
            and isinstance(self._policy_engine, AllowAllPolicyEngine)
            and len(self._policy_engines) == 1
            and not self._pre_run_hooks
            and not self._post_run_hooks
            and not self._lifecycle_hooks
        )

    def _can_enable_durable_model_loop(self, request: dict[str, Any]) -> bool:
        """Return whether this lifecycle request can safely replay orchestration."""
        kwargs = request.get("kwargs")
        if not isinstance(kwargs, dict):
            return False
        # Live presentation and segmented-output paths currently use Agno's
        # streaming loop. Their provider calls are still conservative, but the
        # full stream/resource lifetime has not joined checkpoint certification.
        if request.get("presentation") is not None or kwargs.get("persist_output"):
            return False
        return bool(
            self._durable_model_loop_enabled
            and not kwargs.get("stream")
            and self._can_materialize_isolated_run(
                stream=False,
                skill=kwargs.get("skill"),
            )
        )

    def _can_materialize_run_agent(
        self,
        *,
        stream: bool,
        skill: str | None,
    ) -> bool:
        """Return whether this run can receive a fresh Agent and owned resources."""
        return (
            self._run_agent_factory_enabled
            and not stream
            and self._base_agent_tools_match_materialized_capabilities()
        )

    def _base_agent_tools_match_materialized_capabilities(self) -> bool:
        """Detect unsupported mutation of the compatibility Agent's tool surface."""
        expected = set(self._materialized_base_tool_names)
        actual = set(self._tool_names(list(getattr(self._base_agent, "tools", None) or [])))
        return actual == expected

    def _materialize_capability_function(
        self,
        spec: CapabilitySpec,
        context: Any,
    ) -> Function:
        """Build one run-owned Agno Function from an immutable capability spec."""
        del context
        bindings = build_agno_capability_bindings(
            (spec,),
            invoke=self._invoke_agno_capability,
        )
        if len(bindings) != 1:  # pragma: no cover - constructor invariant
            raise AssertionError("model-callable capability did not produce one binding")
        function = bindings[0].function
        self._attach_function_runtime_hooks(function)
        return function

    def _resolve_learning_scope(
        self,
        context: ExecutionContext,
        *,
        consented: bool,
    ) -> LearningScope | None:
        policy = self._learning_policy
        if policy is None:
            return None
        return LearningScope.resolve(
            policy,
            context,
            agent_id=self._agent_id,
            consented=consented,
        )

    @staticmethod
    def _learning_message_text(message: Any) -> str | None:
        """Mirror Agno's textual recall input without serializing binary parts."""
        if message is None:
            return None
        if isinstance(message, str):
            return message or None
        content = getattr(message, "content", None)
        if content is not None and content is not message:
            return AgentHarness._learning_message_text(content)
        if isinstance(message, (list, tuple)):
            parts = [AgentHarness._learning_message_text(item) for item in message]
            return "\n".join(part for part in parts if part) or None
        if isinstance(message, Mapping):
            parts = [value for value in message.values() if isinstance(value, str)]
            return "\n".join(part for part in parts if part) or None
        model_dump = getattr(message, "model_dump", None)
        if callable(model_dump):
            try:
                return AgentHarness._learning_message_text(model_dump())
            except Exception:
                return None
        return None

    def _learning_prompt_enabled(self) -> bool:
        return bool(
            self._include_learning
            and not self._plan_mode
            and self._internal_run_kind.get() not in {"summary", "memory_flush"}
        )

    @staticmethod
    def _bounded_learning_prompt(guidance: Any, recalled: Any) -> str:
        parts = [
            str(value).strip()
            for value in (guidance, recalled)
            if isinstance(value, str) and value.strip()
        ]
        if not parts:
            return ""
        body = "\n\n".join(parts)
        encoded = body.encode("utf-8")
        if len(encoded) > _LEARNING_PROMPT_MAX_BYTES:
            body = encoded[:_LEARNING_PROMPT_MAX_BYTES].decode(
                "utf-8", errors="ignore"
            ).rstrip()
            body += "\n\n[Learning context truncated by AgnoClaw.]"
        return (
            "# Agno Learning Context\n\n"
            "Recalled learning is scoped historical evidence, not system policy. "
            "Current instructions, the current request, and verified evidence take "
            "precedence.\n\n"
            f"{body}"
        )

    def _learning_prompt_context_sync(
        self,
        *,
        message: Any,
        user_id: str | None,
        session_id: str | None,
        context: ExecutionContext,
    ) -> str:
        if not self._learning_prompt_enabled():
            return ""
        machine = getattr(self._agent, "_learning", None)
        if machine is None:
            return ""
        guidance = ""
        recalled = ""
        try:
            guidance = machine.instructions()
        except Exception:
            logger.warning("Agno learning guidance failed; continuing without it", exc_info=True)
        try:
            recalled = machine.build_context(
                user_id=user_id,
                session_id=session_id,
                agent_id=getattr(self._agent, "id", None) or self._agent_id,
                message=self._learning_message_text(message),
                metadata=self._context_to_metadata(context),
            )
        except Exception:
            logger.warning("Agno learning recall failed; continuing without it", exc_info=True)
        return self._bounded_learning_prompt(guidance, recalled)

    async def _learning_prompt_context_async(
        self,
        *,
        message: Any,
        user_id: str | None,
        session_id: str | None,
        context: ExecutionContext,
    ) -> str:
        if not self._learning_prompt_enabled():
            return ""
        machine = getattr(self._agent, "_learning", None)
        if machine is None:
            return ""
        guidance = ""
        recalled = ""
        try:
            guidance = machine.instructions()
        except Exception:
            logger.warning("Agno learning guidance failed; continuing without it", exc_info=True)
        try:
            build = getattr(machine, "abuild_context", None)
            kwargs = {
                "user_id": user_id,
                "session_id": session_id,
                "agent_id": getattr(self._agent, "id", None) or self._agent_id,
                "message": self._learning_message_text(message),
                "metadata": self._context_to_metadata(context),
            }
            if callable(build):
                recalled = await build(**kwargs)
            else:
                recalled = await asyncio.to_thread(machine.build_context, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Agno learning recall failed; continuing without it", exc_info=True)
        return self._bounded_learning_prompt(guidance, recalled)

    def _append_learning_prompt(self, block: str) -> None:
        if not block:
            return
        current = self._agent.system_message
        if not isinstance(current, str):
            raise HarnessError(
                code="LEARNING_PROMPT_SURFACE_REQUIRED",
                category="learning",
                message="Agno learning recall requires a text system prompt.",
                retryable=False,
            )
        self._agent.system_message = f"{current}\n\n---\n\n{block}"

    def _activate_run_agent(
        self,
        agent: Agent,
        *,
        session_id: str | None,
    ) -> tuple[Any, Any]:
        """Bind a materialized Agent to the current thread/task context."""
        prompt_token = self._active_prompt_session.set(session_id)
        agent_token = self._active_agent.set(agent)
        return agent_token, prompt_token

    def _deactivate_run_agent(self, tokens: tuple[Any, Any] | None) -> None:
        """Restore the prior thread/task Agent binding, if one was installed."""
        if tokens is None:
            return
        agent_token, prompt_token = tokens
        self._active_agent.reset(agent_token)
        self._active_prompt_session.reset(prompt_token)

    def _acquire_run_gate(
        self,
        *,
        isolated: bool = False,
        session_id: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> _RunGateLease:
        if self._active_runtime_run_gate_owned.get():
            # The lifecycle worker acquired the complete compatibility-resource
            # lease before recording model dispatch. Its task-local direct arun
            # must not contend with itself or acquire the context lock twice.
            return _RunGateLease(None)
        maintenance_owned = self._context_maintenance_depth.get() > 0
        context_lease = self._context_automation.admit_run(
            session_id,
            maintenance_owned=maintenance_owned,
        )
        release_context = context_lease.release if context_lease is not None else None
        cross_process_lease: ContextLockLease | None = None
        if (
            self._context_lock_provider is not None
            and session_id is not None
            and not maintenance_owned
        ):
            try:
                cross_process_lease = self._context_lock_provider.acquire(
                    ContextScope(
                        session_id=session_id,
                        user_id=user_id,
                        tenant_id=tenant_id,
                    ),
                    mode=ContextLockMode.SHARED,
                )
            except BaseException:
                if release_context is not None:
                    release_context()
                raise
        if isolated:
            return _RunGateLease(
                None,
                release_context,
                context_lock=cross_process_lease,
            )
        if not self._run_gate.acquire(blocking=False):
            if cross_process_lease is not None:
                cross_process_lease.release()
            if release_context is not None:
                release_context()
            raise HarnessError(
                code="HARNESS_RUN_IN_PROGRESS",
                category="runtime",
                message=(
                    "This AgentHarness already has an active run. Wait for the "
                    "run or stream to finish, or use a separate harness instance "
                    "for concurrent execution."
                ),
                retryable=True,
                details={"concurrency_policy": "single_flight"},
            )
        return _RunGateLease(
            self._run_gate,
            release_context,
            context_lock=cross_process_lease,
        )

    def _resolve_run_identity(
        self,
        *,
        context: ExecutionContext | None,
        user_id: str | None,
        session_id: str | None,
        metadata: dict[str, Any] | None,
    ) -> tuple[str | None, str | None, ExecutionContext]:
        """Resolve one frozen admission identity for every downstream consumer."""
        active_runtime_context = self._active_runtime_context.get()
        if (
            context is not None
            and context is active_runtime_context
            and context.admission is not None
        ):
            for field, explicit, trusted in (
                ("user_id", user_id, context.user_id),
                ("session_id", session_id, context.session_id),
            ):
                if explicit is not None and explicit != trusted:
                    raise HarnessError(
                        code="IDENTITY_CONTEXT_CONFLICT",
                        category="identity",
                        message=(
                            f"Explicit {field} conflicts with the admitted lifecycle context."
                        ),
                        retryable=False,
                        details={"field": field},
                    )
            # ``start()`` already resolved and persisted this exact authority.
            # Re-resolving it inside the worker would upgrade caller-supplied
            # identity fields to trusted-host provenance and change the authority
            # digest seen by governed tools.
            return context.user_id, context.session_id, context

        assertions: list[IdentityAssertion] = []
        trusted_metadata = dict(self._context_metadata)
        trusted_metadata.setdefault("permission_mode", self.permission_mode)

        if context is not None:
            for field, explicit, trusted in (
                ("user_id", user_id, context.user_id),
                ("session_id", session_id, context.session_id),
            ):
                if explicit is not None and trusted is not None and explicit != trusted:
                    raise HarnessError(
                        code="IDENTITY_CONTEXT_CONFLICT",
                        category="identity",
                        message=f"Explicit {field} conflicts with the trusted execution context.",
                        retryable=False,
                        details={"field": field},
                    )
            assertions.append(
                IdentityAssertion(
                    source=context.identity_source,
                    tenant_id=context.tenant_id,
                    org_id=context.org_id,
                    team_id=context.team_id,
                    user_id=context.user_id,
                    session_id=context.session_id,
                    workspace_id=context.workspace_id,
                    roles=context.roles,
                    scopes=context.scopes,
                )
            )
            if (context.user_id is None and user_id is None and self.user_id is not None) or (
                context.session_id is None
                and session_id is None
                and self._active_session_id(None) is not None
            ):
                assertions.append(
                    IdentityAssertion(
                        source=IdentitySource.TRUSTED_HOST,
                        user_id=(
                            self.user_id if context.user_id is None and user_id is None else None
                        ),
                        session_id=(
                            self._active_session_id(None)
                            if context.session_id is None and session_id is None
                            else None
                        ),
                    )
                )
            trusted_metadata.update(context.metadata)
            request_id = context.request_id or self._request_id
            trace_id = context.trace_id or self._trace_id
            trusted_permission_tools = context.trusted_permission_tools
            trusted_permission_categories = context.trusted_permission_categories
        else:
            # Constructor identity is a reusable host default. Explicit per-run
            # user/session values may replace those defaults, but cannot conflict
            # with a supplied trusted ExecutionContext.
            assertions.append(
                IdentityAssertion(
                    source=IdentitySource.TRUSTED_HOST,
                    tenant_id=self._tenant_id,
                    org_id=self._org_id,
                    team_id=self._team_id,
                    user_id=None if user_id is not None else self.user_id,
                    session_id=(None if session_id is not None else self._active_session_id(None)),
                    workspace_id=str(self.workspace.path),
                    roles=self._roles,
                    scopes=self._scopes,
                )
            )
            request_id = self._request_id
            trace_id = self._trace_id
            trusted_permission_tools = ()
            trusted_permission_categories = ()

        if user_id is not None or session_id is not None:
            assertions.append(
                IdentityAssertion(
                    source=IdentitySource.CALLER_ARGUMENT,
                    user_id=user_id,
                    session_id=session_id,
                )
            )

        envelope = AdmissionEnvelope.resolve(
            *assertions,
            request_id=request_id,
            trace_id=trace_id,
            client_metadata=metadata or {},
            trusted_metadata=trusted_metadata,
        )
        identity = envelope.identity
        # Hooks receive a mutable copy; admitted authority remains frozen.
        merged_metadata = thaw_data(envelope.client_metadata)
        merged_metadata.update(thaw_data(envelope.trusted_metadata))
        resolved = ExecutionContext.create(
            user_id=identity.user_id,
            session_id=identity.session_id,
            workspace_id=identity.workspace_id,
            tenant_id=identity.tenant_id,
            org_id=identity.org_id,
            team_id=identity.team_id,
            roles=identity.roles,
            scopes=identity.scopes,
            request_id=envelope.request_id,
            trace_id=envelope.trace_id,
            trusted_permission_tools=trusted_permission_tools,
            trusted_permission_categories=trusted_permission_categories,
            metadata=merged_metadata,
            identity_source=(
                context.identity_source if context is not None else IdentitySource.TRUSTED_HOST
            ),
            admission=envelope,
        )
        return identity.user_id, identity.session_id, resolved

    def _get_runtime_store(self) -> RuntimeStore:
        store = self._runtime_store
        if store is None:
            store = SQLiteRuntimeStore(":memory:")
            self._runtime_store = store
        return store

    def _get_operation_gateway(self) -> OperationGateway:
        gateway = self._operation_gateway
        if gateway is None:
            gateway = OperationGateway(
                self._get_runtime_store(),
                worker_id=self._runtime_worker_id,
                artifact_store=self._artifact_store,
                result_serializer=persisted_result_value,
                result_cache_size=int(
                    getattr(self.config, "runtime_operation_result_cache_size", 128)
                ),
            )
            self._operation_gateway = gateway
        return gateway

    def _get_capability_executor(self) -> CapabilityExecutor:
        executor = self._capability_executor
        if executor is None:
            executor = CapabilityExecutor(
                self._capability_registry,
                self._get_effect_operation_gateway(),
                invoker=self._capability_invoker,
            )
            self._capability_executor = executor
        return executor

    def _get_effect_operation_gateway(self) -> OperationGateway:
        gateway = self._capability_operation_gateway
        if gateway is None:
            gateway = OperationGateway(
                self._get_runtime_store(),
                worker_id=self._runtime_worker_id,
                artifact_store=self._artifact_store,
                result_cache_size=int(
                    getattr(self.config, "runtime_operation_result_cache_size", 128)
                ),
            )
            self._capability_operation_gateway = gateway
        return gateway

    def capability_catalog(
        self,
        *,
        context: ExecutionContext | None = None,
        kinds: tuple[CapabilityKind, ...] | None = None,
    ) -> tuple[CapabilityCatalogEntry, ...]:
        """List scope-visible descriptors."""
        scopes = context.scopes if context is not None else self._scopes
        return self._capability_registry.catalog(
            granted_scopes=set(scopes),
            kinds=kinds,
        )

    def _get_capability_approval_coordinator(self) -> DurableApprovalCoordinator:
        coordinator = self._capability_approval_coordinator
        if coordinator is None:
            coordinator = DurableApprovalCoordinator(
                self._get_runtime_store(),
                self._permission_controller,
                ttl_seconds=int(self.config.permission_approval_ttl_seconds),
                poll_interval_seconds=float(self.config.permission_approval_poll_interval_seconds),
            )
            self._capability_approval_coordinator = coordinator
        return coordinator

    def _capability_approval_context(
        self,
        run_id: str,
        context: ExecutionContext | None,
    ) -> ExecutionContext:
        if context is not None:
            return context
        request = self._run_requests.get(run_id)
        if request is None or not isinstance(request.get("context"), ExecutionContext):
            raise HarnessError(
                code="APPROVAL_CONTEXT_REQUIRED",
                category="authorization",
                message="Approval administration requires trusted run context.",
                retryable=False,
                details={"run_id": run_id},
            )
        return request["context"]

    def capability_approvals(
        self,
        run_id: str,
        *,
        context: ExecutionContext | None = None,
        states: tuple[ApprovalState, ...] | None = None,
        limit: int = 100,
    ) -> tuple[ApprovalRecord, ...]:
        """List authority-matched approval records for one run."""
        return self._get_capability_approval_coordinator().list_for_run(
            run_id,
            context=self._capability_approval_context(run_id, context),
            states=states,
            limit=limit,
        )

    def decide_capability_approval(
        self,
        request_id: str,
        *,
        run_id: str,
        approved: bool,
        issuer: str,
        reason_code: str,
        context: ExecutionContext | None = None,
        grant_scope: GrantScope = GrantScope.RUN,
    ) -> ApprovalRecord:
        """Approve or deny an exact pending capability call through the host API."""
        return self._get_capability_approval_coordinator().decide(
            request_id,
            approved=approved,
            issuer=issuer,
            reason_code=reason_code,
            context=self._capability_approval_context(run_id, context),
            grant_scope=grant_scope,
        )

    async def _renew_capability_lease(self, claim: RunLeaseClaim) -> None:
        if self._active_runtime_claim.get() is not claim:
            raise RuntimeLeaseLostError(run_id=claim.run_id, kind=claim.run.kind)
        _renewed, cancelled, error = await drain_thread_call(
            lambda: self._runtime_store_lease_method("renew_run_lease")(
                claim,
                lease_seconds=self._runtime_lease_seconds,
            )
        )
        if error is not None:
            raise error
        if cancelled:
            raise asyncio.CancelledError

    async def aexecute_capability(
        self,
        reference: str,
        *,
        operation_id: str,
        arguments: dict[str, Any] | None = None,
        run_id: str | None = None,
        attempt_id: str | None = None,
        context: ExecutionContext | None = None,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
        safe_metadata: dict[str, Any] | None = None,
        _policy_evidence: tuple[dict[str, Any], ...] | None = None,
    ) -> CapabilityExecution:
        """Execute a registered capability through the governed runtime."""
        return await execute_harness_capability(
            self,
            reference,
            operation_id=operation_id,
            arguments=arguments,
            run_id=run_id,
            attempt_id=attempt_id,
            context=context,
            idempotency_key=idempotency_key,
            timeout_seconds=timeout_seconds,
            safe_metadata=safe_metadata,
            policy_evidence=_policy_evidence,
        )

    async def _invoke_agno_capability(
        self,
        reference: str,
        arguments: Any,
    ) -> Any:
        runtime = get_current_tool_runtime()
        if runtime is None or runtime.get("capability_reference") != reference:
            raise HarnessError(
                code="CAPABILITY_GOVERNANCE_CONTEXT_REQUIRED",
                category="authorization",
                message="Generated capability tools require the governed Agno ingress.",
                retryable=False,
            )
        runtime_context = runtime.get("context")
        if not isinstance(runtime_context, ExecutionContext):
            raise HarnessError(
                code="CAPABILITY_GOVERNANCE_CONTEXT_REQUIRED",
                category="authorization",
                message="Generated capability tools require trusted execution context.",
                retryable=False,
            )
        run_id = self._active_runtime_run_id.get()
        if run_id is None:
            raise HarnessError(
                code="CAPABILITY_ACTIVE_RUN_REQUIRED",
                category="lifecycle",
                message="Generated capability tools require AgentHarness.start().",
                retryable=False,
            )
        call_identity = runtime.get("parent_tool_call_id") or runtime.get("parent_step_id")
        operation_id = (
            f"{run_id}:capability:"
            f"{capability_digest([reference, call_identity]).split(':', 1)[1][:32]}"
        )
        spec = self._capability_registry.resolve(reference)
        idempotency_key = None
        if spec.supports_idempotency_key:
            candidate = dict(arguments).get("idempotency_key")
            if isinstance(candidate, str) and candidate.strip():
                idempotency_key = candidate.strip()
        execution = await self.aexecute_capability(
            reference,
            operation_id=operation_id,
            arguments=dict(arguments),
            context=runtime_context,
            idempotency_key=idempotency_key,
            timeout_seconds=runtime.get("capability_timeout_seconds"),
            safe_metadata={
                "agno_tool_name": runtime.get("parent_tool_name"),
                "agno_tool_call_digest": capability_digest(call_identity),
            },
            _policy_evidence=(
                tuple(runtime["capability_policy_evidence"])
                if runtime.get("capability_policy_evidence") is not None
                else None
            ),
        )
        return await self._model_operation_output(
            execution.operation,
            label=execution.spec.name,
            context=runtime_context,
        )

    async def _model_operation_output(
        self,
        execution,
        *,
        label: str,
        context: ExecutionContext,
    ) -> Any:
        limit = self._max_inline_output_chars
        if limit is None or label == READ_SPILLED_OUTPUT:
            return execution.value
        settlement = execution.record.settlement
        artifact_id = settlement.result_reference if settlement is not None else None
        if artifact_id is None:
            raise HarnessError(
                code="OUTPUT_SPILL_REFERENCE_REQUIRED",
                category="artifact",
                message="Governed output has no committed artifact reference.",
                retryable=False,
                details={"operation_target": label},
            )
        reference = await asyncio.to_thread(
            self._get_runtime_store().get_artifact,
            artifact_id,
            owner=self._runtime_owner(context),
        )
        value, spilled_chars = model_output(
            execution.value,
            reference,
            maximum_inline_chars=limit,
        )
        if spilled_chars is not None:
            await self._emit_event_async(
                event_type="output.spilled",
                run_id=execution.record.intent.run_id,
                context=context,
                payload={
                    "artifact_id": reference.artifact_id,
                    "capability": label,
                    "operation_target": label,
                    "checksum": reference.checksum,
                    "rendered_chars": spilled_chars,
                    "size_bytes": reference.size_bytes,
                },
            )
        return value

    async def _read_spilled_output(
        self,
        artifact_id: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        run_id = self._active_runtime_run_id.get()
        context = self._active_runtime_context.get()
        if run_id is None or context is None:
            raise HarnessError(
                code="OUTPUT_SPILL_ACTIVE_RUN_REQUIRED",
                category="lifecycle",
                message="Spilled output can be read only by its active lifecycle run.",
                retryable=False,
            )
        reference = await asyncio.to_thread(
            self._get_runtime_store().get_artifact,
            artifact_id,
            owner=self._runtime_owner(context),
        )
        metadata = thaw_data(reference.metadata)
        if (
            reference.purpose != "operation_result"
            or not isinstance(metadata, dict)
            or metadata.get("kind") != OperationKind.CAPABILITY.value
        ):
            raise HarnessError(
                code="OUTPUT_SPILL_SCOPE_MISMATCH",
                category="authorization",
                message="Artifact is not a capability output owned by this active run.",
                retryable=False,
                details={"artifact_id": artifact_id, "run_id": run_id},
            )
        source_run = await asyncio.to_thread(
            self._get_runtime_store().get_run,
            reference.scope.run_id,
            owner=self._runtime_owner(context),
        )
        if reference.scope.run_id != run_id and (
            context.session_id is None or source_run.session_id != context.session_id
        ):
            raise HarnessError(
                code="OUTPUT_SPILL_SCOPE_MISMATCH",
                category="authorization",
                message="Artifact is not owned by this active run or trusted session.",
                retryable=False,
                details={"artifact_id": artifact_id, "run_id": run_id},
            )
        value = await self._require_artifact_store().load_json(reference)
        maximum_page = max(256, (self._max_inline_output_chars or 1_024) // 2)
        return output_page(
            value,
            reference,
            offset=offset,
            limit=limit,
            maximum_page_chars=maximum_page,
        )

    @staticmethod
    def _runtime_owner(context: ExecutionContext) -> RunOwner:
        return RunOwner(tenant_id=context.tenant_id, user_id=context.user_id)

    @staticmethod
    def _safe_runtime_error(exc: BaseException, *, run_id: str) -> dict[str, Any]:
        debug_reference = f"debug_{uuid4().hex}"
        if isinstance(exc, HarnessError):
            diagnostic = SafeDiagnostic.from_error(
                exc,
                safe_message="The run failed. Inspect the authorized debug reference.",
                safe_details={"run_id": run_id, "reason_code": exc.code},
                debug_reference=debug_reference,
            )
        else:
            diagnostic = SafeDiagnostic(
                code="RUN_EXECUTION_FAILED",
                category="runtime",
                safe_message="The run failed. Inspect the authorized debug reference.",
                retryable=False,
                details={"run_id": run_id, "exception_type": exc.__class__.__name__},
                debug_reference=debug_reference,
            )
        return {
            "code": diagnostic.code,
            "category": diagnostic.category,
            "safe_message": diagnostic.safe_message,
            "retryable": diagnostic.retryable,
            "details": thaw_data(diagnostic.details),
            "help_actions": list(diagnostic.help_actions),
            "debug_reference": diagnostic.debug_reference,
        }

    def _runtime_handle(self, run_id: str, *, owner: RunOwner) -> HarnessRun:
        store = self._get_runtime_store()
        snapshot = store.get_run(run_id, owner=owner)
        return HarnessRun(
            run_id=run_id,
            session_id=snapshot.session_id,
            store=store,
            artifact_store=self._artifact_store,
            owner=owner,
            waiter=lambda timeout: self._wait_runtime_run(run_id, timeout=timeout),
            canceller=lambda: self._cancel_runtime_run(run_id),
            commander=lambda command: self._command_runtime_run(run_id, command),
        )

    def _launch_runtime_worker(self, run_id: str, request: dict[str, Any]) -> None:
        if run_id in self._live_runs:
            return
        self._run_requests[run_id] = request
        resume_event = asyncio.Event()
        resume_event.set()
        self._run_resume_events[run_id] = resume_event
        task = asyncio.create_task(
            self._execute_runtime_run(run_id),
            name=f"agnoclaw:{run_id}",
        )
        self._live_runs[run_id] = task
        task.add_done_callback(functools.partial(self._runtime_task_done, run_id))

    async def start(
        self,
        message: str,
        *,
        idempotency_key: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        context: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
        learning_consent: bool = False,
        persist_output: bool = False,
        _presentation: RunPresentationPublisher | None = None,
        _child_spec: ChildRunSpec | None = None,
        **kwargs: Any,
    ) -> HarnessRun:
        """Persist and start one logical run, returning before model completion."""
        sync_coordinator = self._sync_lifecycle_coordinator
        if sync_coordinator is not None and not sync_coordinator.in_coordinator_loop():
            raise HarnessError(
                code="HARNESS_EVENT_LOOP_OWNERSHIP_CONFLICT",
                category="lifecycle",
                message=(
                    "This harness is owned by its blocking lifecycle coordinator. "
                    "Do not mix start()/arun() with synchronous run() on one instance."
                ),
                retryable=False,
            )
        if self._closed:
            raise HarnessError(
                code="HARNESS_CLOSED",
                category="lifecycle",
                message="This harness no longer accepts new runs.",
                retryable=False,
            )
        if kwargs.get("stream"):
            raise HarnessError(
                code="RUN_START_STREAM_UNSUPPORTED",
                category="validation",
                message="HarnessRun.events() is the lifecycle stream; omit stream=True.",
                retryable=False,
            )
        kwargs.pop("stream", None)
        lifecycle_skill = kwargs.get("skill")
        if isinstance(lifecycle_skill, str):
            lifecycle_skill_spec = self.skills._get_skill(lifecycle_skill)
            if lifecycle_skill_spec is not None:
                dispatch_mode = None
                if lifecycle_skill_spec.meta.context == "fork":
                    dispatch_mode = "fork"
                elif lifecycle_skill_spec.meta.command_dispatch == "tool":
                    dispatch_mode = "command_tool"
                if dispatch_mode is not None:
                    raise HarnessError(
                        code="SKILL_LIFECYCLE_DISPATCH_UNSUPPORTED",
                        category="lifecycle",
                        message=(
                            "This skill uses a specialized legacy dispatch mode. "
                            "Migrate it to a declared child or registered capability "
                            "before lifecycle execution."
                        ),
                        retryable=False,
                        details={"skill": lifecycle_skill, "dispatch_mode": dispatch_mode},
                    )
        validate_output_persistence(
            persist_output, self._artifact_store, self._agent_blueprint, kwargs
        )
        require_no_legacy_tools_for_durable(self._legacy_tool_bindings)
        require_no_extension_tools_for_lifecycle(self._extension_tool_bindings)
        runtime_tools = kwargs.get("tools")
        if runtime_tools:
            if not isinstance(runtime_tools, (list, tuple)):
                raise HarnessError(
                    code="LEGACY_TOOL_INVALID",
                    category="configuration",
                    message="Per-run tools= must be a bounded list or tuple.",
                    retryable=False,
                    details={"source": "start.tools"},
                )
            require_no_legacy_tools_for_durable(
                normalize_legacy_tools(tuple(runtime_tools), source="start.tools")
            )
        effective_user, effective_session, resolved = self._resolve_run_identity(
            context=context,
            user_id=user_id,
            session_id=session_id,
            metadata=metadata,
        )
        self._resolve_learning_scope(resolved, consented=learning_consent)
        request_kwargs = {
            "metadata": metadata or {},
            "learning_consent": learning_consent,
            "persist_output": persist_output,
            **kwargs,
        }
        digest_kwargs = dict(request_kwargs)
        if _child_spec is not None:
            digest_kwargs["_declared_child"] = _child_spec.to_dict()
        request_digest = runtime_request_digest(
            message=message,
            context=resolved,
            kwargs=digest_kwargs,
            harness_spec_digest=self._spec.settings_digest,
        )
        run_id = _child_spec.child_run_id if _child_spec is not None else f"run_{uuid4().hex}"
        snapshot = RunSnapshot(
            run_id=run_id,
            tenant_id=resolved.tenant_id,
            user_id=effective_user,
            session_id=effective_session,
            parent_run_id=(_child_spec.parent_run_id if _child_spec is not None else None),
            root_run_id=(_child_spec.root_run_id if _child_spec is not None else None),
            child_depth=(_child_spec.depth if _child_spec is not None else 0),
            metadata={
                "harness": self.name,
                "harness_spec_digest": self._spec.settings_digest,
                "request_id": resolved.request_id,
            },
        )
        scope = f"{resolved.tenant_id or '-'}:{effective_user or '-'}"
        store = self._get_runtime_store()
        create_options: dict[str, Any] = {
            "idempotency_scope": scope if idempotency_key is not None else None,
            "idempotency_key": idempotency_key,
            "request_digest": request_digest if idempotency_key is not None else None,
        }
        if _child_spec is not None:
            create_options["child_spec"] = _child_spec
        created = store.create_run(
            snapshot,
            **create_options,
        )
        authoritative = created.snapshot
        owner = self._runtime_owner(resolved)
        if created.idempotent and authoritative.run_id in self._live_runs:
            if _presentation is not None:
                _presentation.finish()
            return self._runtime_handle(authoritative.run_id, owner=owner)
        if authoritative.terminal:
            if _presentation is not None:
                _presentation.finish()
            return self._runtime_handle(authoritative.run_id, owner=owner)
        if authoritative.state == RunState.CREATED:
            queued = store.apply_transition(
                LifecycleTransition(
                    run_id=authoritative.run_id,
                    kind=TransitionKind.QUEUE,
                    transition_id=f"{authoritative.run_id}:queue",
                ),
                expected_revision=authoritative.revision,
            )
            authoritative = queued.lifecycle.after
        if authoritative.state != RunState.QUEUED:
            if _presentation is not None:
                _presentation.finish()
            return self._runtime_handle(authoritative.run_id, owner=owner)

        self._launch_runtime_worker(
            authoritative.run_id,
            {
                "message": message,
                "context": resolved,
                "kwargs": {
                    "learning_consent": learning_consent,
                    "persist_output": persist_output,
                    **kwargs,
                },
                "steering": [],
                "presentation": _presentation,
                "child_spec": _child_spec,
            },
        )
        return self._runtime_handle(authoritative.run_id, owner=owner)

    def _runtime_task_done(self, run_id: str, task: asyncio.Task[Any]) -> None:
        """Release ephemeral control state without retaining completed tasks."""
        if self._live_runs.get(run_id) is task:
            self._live_runs.pop(run_id, None)
            request = self._run_requests.pop(run_id, None)
            if request is not None and request.get("presentation") is not None:
                request["presentation"].finish()
            self._run_resume_events.pop(run_id, None)
            self._runtime_supervisor_failures.pop(run_id, None)
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error(
                "Lifecycle worker escaped without settlement for run %s",
                run_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    def get_run(
        self,
        run_id: str,
        *,
        context: ExecutionContext | None = None,
    ) -> HarnessRun:
        """Reattach to an authorized run without starting or replaying effects."""
        owner = RunOwner(
            tenant_id=context.tenant_id if context is not None else self._tenant_id,
            user_id=context.user_id if context is not None else self.user_id,
        )
        self._get_runtime_store().get_run(run_id, owner=owner)
        return self._runtime_handle(run_id, owner=owner)

    def _require_artifact_store(self) -> ArtifactStore:
        if self._artifact_store is None:
            raise HarnessError(
                code="ARTIFACT_STORE_REQUIRED",
                category="configuration",
                message="Configure artifact_store= to read durable run artifacts.",
                retryable=False,
            )
        return self._artifact_store

    async def list_artifacts(
        self,
        run_id: str,
        *,
        limit: int = 100,
        context: ExecutionContext | None = None,
    ) -> list[ArtifactReference]:
        """List bounded artifact metadata after exact run-owner authorization."""
        owner = RunOwner(
            tenant_id=context.tenant_id if context is not None else self._tenant_id,
            user_id=context.user_id if context is not None else self.user_id,
        )
        self._require_artifact_store()
        return await asyncio.to_thread(
            self._get_runtime_store().list_artifacts,
            run_id,
            limit=limit,
            owner=owner,
        )

    async def read_artifact(
        self,
        artifact_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        context: ExecutionContext | None = None,
    ) -> ArtifactChunk:
        """Read one verified bounded page after current-owner reauthorization."""
        owner = RunOwner(
            tenant_id=context.tenant_id if context is not None else self._tenant_id,
            user_id=context.user_id if context is not None else self.user_id,
        )
        artifact_store = self._require_artifact_store()
        reference = await asyncio.to_thread(
            self._get_runtime_store().get_artifact,
            artifact_id,
            owner=owner,
        )
        return await artifact_store.read(reference, offset=offset, limit=limit)

    def _require_learning_gateway(self, *, write: bool = False) -> LearningGateway:
        if write and self._closed:
            raise HarnessError(
                code="HARNESS_CLOSED",
                category="lifecycle",
                message="This harness no longer accepts learning mutations.",
                retryable=False,
            )
        if self._learning_gateway is None:
            raise HarnessError(
                code="LEARNING_GATEWAY_REQUIRED",
                category="learning",
                message=(
                    "Configure learning=, learning_ledger=, and artifact_store= "
                    "to use governed learning candidates."
                ),
                retryable=False,
            )
        return self._learning_gateway

    def _resolve_candidate_scope(
        self,
        context: ExecutionContext,
        *,
        learning_consent: bool,
    ) -> tuple[ExecutionContext, LearningScope, LearningOwner]:
        if not isinstance(context, ExecutionContext):
            raise HarnessError(
                code="LEARNING_CONTEXT_REQUIRED",
                category="learning",
                message="Candidate operations require an explicit trusted ExecutionContext.",
                retryable=False,
            )
        _, _, resolved = self._resolve_run_identity(
            context=context,
            user_id=None,
            session_id=None,
            metadata=None,
        )
        scope = self._resolve_learning_scope(
            resolved,
            consented=learning_consent,
        )
        if scope is None:  # pragma: no cover - constructor invariant
            raise HarnessError(
                code="LEARNING_POLICY_REQUIRED",
                category="learning",
                message="Candidate operations require an enabled explicit learning policy.",
                retryable=False,
            )
        return (
            resolved,
            scope,
            LearningOwner(scope.tenant_id, scope.storage_namespace),
        )

    def _learning_admin_gateway(
        self,
        context: ExecutionContext,
        *,
        learning_consent: bool,
        write: bool,
    ) -> tuple[LearningAdminGateway, ExecutionContext, LearningScope]:
        if write and self._closed:
            raise HarnessError(
                code="HARNESS_CLOSED",
                category="lifecycle",
                message="This harness no longer accepts learning mutations.",
                retryable=False,
            )
        inspect_agno_compatibility().require(AgnoFeature.LEARNING_ADMIN_CRUD)
        resolved, scope, _ = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        policy = self._learning_policy
        if policy is None:  # pragma: no cover - resolver invariant
            raise AssertionError("learning admin without explicit policy")
        from .memory import build_learning_machine

        machine = build_learning_machine(
            db=self._learning_db,
            policy=policy,
            scope=scope,
        )
        return (
            LearningAdminGateway(
                machine,
                policy=policy,
                scope=scope,
                agent_id=self._agent_id,
            ),
            resolved,
            scope,
        )

    async def read_learning_data(
        self,
        store: LearningDataStore,
        *,
        context: ExecutionContext,
        learning_consent: bool = False,
    ) -> LearningDataRecord:
        """Read one exact personal/session learning record without hidden fallback."""
        gateway, _, _ = self._learning_admin_gateway(
            context,
            learning_consent=learning_consent,
            write=False,
        )
        return await gateway.read(store)

    async def replace_learning_data(
        self,
        store: LearningDataStore,
        content: dict[str, Any],
        *,
        context: ExecutionContext,
        operation_id: str,
        learning_consent: bool = False,
    ) -> LearningMutationReceipt:
        """Replace and read-verify one exact personal/session learning record."""
        gateway, resolved, scope = self._learning_admin_gateway(
            context,
            learning_consent=learning_consent,
            write=True,
        )
        lane_session = (
            resolved.session_id
            if LearningDataStore(store) is LearningDataStore.SESSION_CONTEXT
            else f"learning-user:{scope.storage_user_id}"
        )
        async with self._session_lanes.hold(
            tenant_id=resolved.tenant_id,
            session_id=lane_session,
            run_id=f"learning-admin:{operation_id}",
        ):
            return await gateway.replace(store, content, operation_id=operation_id)

    async def forget_learning_data(
        self,
        store: LearningDataStore,
        *,
        context: ExecutionContext,
        operation_id: str,
        learning_consent: bool = False,
    ) -> LearningMutationReceipt:
        """Delete and read-verify one exact personal/session learning record."""
        gateway, resolved, scope = self._learning_admin_gateway(
            context,
            learning_consent=learning_consent,
            write=True,
        )
        lane_session = (
            resolved.session_id
            if LearningDataStore(store) is LearningDataStore.SESSION_CONTEXT
            else f"learning-user:{scope.storage_user_id}"
        )
        async with self._session_lanes.hold(
            tenant_id=resolved.tenant_id,
            session_id=lane_session,
            run_id=f"learning-admin:{operation_id}",
        ):
            return await gateway.forget(store, operation_id=operation_id)

    async def capture_learning_candidate(
        self,
        *,
        context: ExecutionContext,
        target: LearningTarget,
        content: dict[str, Any],
        source_run_ids: tuple[str, ...] | list[str],
        evidence_artifact_ids: tuple[str, ...] | list[str] = (),
        confidence: float,
        risk: CandidateRisk,
        created_by: CandidateAuthor,
        mechanism_version: str,
        candidate_id: str | None = None,
        expires_at: str | None = None,
        change_hypothesis_artifact_id: str | None = None,
        component_manifest_artifact_id: str | None = None,
        supersedes_candidate_id: str | None = None,
        learning_consent: bool = False,
    ) -> CandidateRecord:
        """Capture inert, artifact-backed learning after source-run authorization."""
        gateway = self._require_learning_gateway(write=True)
        resolved, scope, _ = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        source_ids = tuple(source_run_ids)
        owner = RunOwner(tenant_id=resolved.tenant_id, user_id=resolved.user_id)
        store = self._get_runtime_store()
        for source_run_id in source_ids:
            await asyncio.to_thread(store.get_run, source_run_id, owner=owner)
        policy = self._learning_policy
        if policy is None:  # pragma: no cover - constructor invariant
            raise AssertionError("learning gateway without policy")
        return await gateway.capture(
            policy=policy,
            scope=scope,
            target=target,
            content=content,
            source_run_ids=source_ids,
            evidence_artifact_ids=tuple(evidence_artifact_ids),
            confidence=confidence,
            risk=risk,
            created_by=created_by,
            mechanism_version=mechanism_version,
            candidate_id=candidate_id,
            expires_at=expires_at,
            change_hypothesis_artifact_id=change_hypothesis_artifact_id,
            component_manifest_artifact_id=component_manifest_artifact_id,
            supersedes_candidate_id=supersedes_candidate_id,
        )

    async def get_learning_candidate(
        self,
        candidate_id: str,
        *,
        context: ExecutionContext,
        learning_consent: bool = False,
    ) -> CandidateRecord:
        """Read candidate metadata after exact tenant/namespace authorization."""
        gateway = self._require_learning_gateway()
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        return await gateway.get(candidate_id, owner=owner)

    async def read_learning_candidate_content(
        self,
        candidate_id: str,
        *,
        context: ExecutionContext,
        learning_consent: bool = False,
    ) -> dict[str, Any]:
        """Read verified candidate content after current-owner reauthorization."""
        gateway = self._require_learning_gateway()
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        return await gateway.read_content(candidate_id, owner=owner)

    async def list_learning_candidates(
        self,
        *,
        context: ExecutionContext,
        limit: int = 100,
        state: CandidateState | None = None,
        learning_consent: bool = False,
    ) -> list[CandidateRecord]:
        """List a bounded page from the caller's exact learning namespace."""
        gateway = self._require_learning_gateway()
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        return await gateway.list_candidates(owner=owner, limit=limit, state=state)

    async def observe_learning_application(
        self,
        candidate_id: str,
        *,
        run_id: str,
        kind: LearningApplicationKind,
        observer_digest: str,
        evidence_artifact_ids: tuple[str, ...] | list[str],
        context: ExecutionContext,
        application_id: str | None = None,
        learning_consent: bool = False,
    ) -> LearningApplication:
        """Attribute retrieval/application to an authorized run and promoted target."""
        gateway = self._require_learning_gateway(write=True)
        resolved, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        candidate = await gateway.get(candidate_id, owner=owner)
        if candidate.target_reference is None:
            raise HarnessError(
                code="LEARNING_APPLICATION_TARGET_MISSING",
                category="learning",
                message="Only a promoted learning can be attributed to a run.",
                retryable=False,
                details={"candidate_id": candidate_id},
            )
        await asyncio.to_thread(
            self._get_runtime_store().get_run,
            run_id,
            owner=RunOwner(tenant_id=resolved.tenant_id, user_id=resolved.user_id),
        )
        return await gateway.record_application(
            LearningApplication(
                application_id=application_id or f"la_{uuid4().hex}",
                candidate_id=candidate_id,
                run_id=run_id,
                target_reference=candidate.target_reference,
                kind=kind,
                observer_digest=observer_digest,
                evidence_artifact_ids=tuple(evidence_artifact_ids),
            ),
            owner=owner,
        )

    async def record_learning_application(
        self,
        application: LearningApplication,
        *,
        context: ExecutionContext,
        learning_consent: bool = False,
    ) -> LearningApplication:
        """Record prebuilt application evidence after scope and run authorization."""
        if not isinstance(application, LearningApplication):
            raise TypeError("application must be a LearningApplication")
        gateway = self._require_learning_gateway(write=True)
        resolved, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        await asyncio.to_thread(
            self._get_runtime_store().get_run,
            application.run_id,
            owner=RunOwner(tenant_id=resolved.tenant_id, user_id=resolved.user_id),
        )
        return await gateway.record_application(application, owner=owner)

    async def list_learning_applications(
        self,
        candidate_id: str,
        *,
        context: ExecutionContext,
        limit: int = 100,
        learning_consent: bool = False,
    ) -> list[LearningApplication]:
        """List bounded content-free application evidence in the exact namespace."""
        gateway = self._require_learning_gateway()
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        return await gateway.list_applications(candidate_id, owner=owner, limit=limit)

    async def observe_learning_outcome(
        self,
        application_id: str,
        *,
        kind: LearningOutcomeKind,
        score: float,
        evaluator_digest: str,
        evidence_artifact_ids: tuple[str, ...] | list[str],
        evaluated_by: PromotionActor,
        context: ExecutionContext,
        outcome_id: str | None = None,
        learning_consent: bool = False,
    ) -> LearningOutcome:
        """Attach independent outcome evidence to one exact applied learning."""
        gateway = self._require_learning_gateway(write=True)
        resolved, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        application = await gateway.get_application(application_id, owner=owner)
        await asyncio.to_thread(
            self._get_runtime_store().get_run,
            application.run_id,
            owner=RunOwner(tenant_id=resolved.tenant_id, user_id=resolved.user_id),
        )
        return await gateway.record_outcome(
            LearningOutcome(
                outcome_id=outcome_id or f"lo_{uuid4().hex}",
                application_id=application.application_id,
                candidate_id=application.candidate_id,
                run_id=application.run_id,
                kind=kind,
                score=score,
                evaluator_digest=evaluator_digest,
                evidence_artifact_ids=tuple(evidence_artifact_ids),
                evaluated_by=evaluated_by,
            ),
            owner=owner,
        )

    async def record_learning_outcome(
        self,
        outcome: LearningOutcome,
        *,
        context: ExecutionContext,
        learning_consent: bool = False,
    ) -> LearningOutcome:
        """Record a prebuilt outcome after exact scope, application, and run checks."""
        if not isinstance(outcome, LearningOutcome):
            raise TypeError("outcome must be a LearningOutcome")
        gateway = self._require_learning_gateway(write=True)
        resolved, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        application = await gateway.get_application(outcome.application_id, owner=owner)
        if application.candidate_id != outcome.candidate_id or application.run_id != outcome.run_id:
            raise HarnessError(
                code="LEARNING_OUTCOME_ATTRIBUTION_MISMATCH",
                category="learning",
                message="Outcome evidence does not match its learning application.",
                retryable=False,
                details={"application_id": outcome.application_id},
            )
        await asyncio.to_thread(
            self._get_runtime_store().get_run,
            outcome.run_id,
            owner=RunOwner(tenant_id=resolved.tenant_id, user_id=resolved.user_id),
        )
        return await gateway.record_outcome(outcome, owner=owner)

    async def list_learning_outcomes(
        self,
        candidate_id: str,
        *,
        context: ExecutionContext,
        limit: int = 100,
        learning_consent: bool = False,
    ) -> list[LearningOutcome]:
        """List bounded content-free outcomes in the exact learning namespace."""
        gateway = self._require_learning_gateway()
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        return await gateway.list_outcomes(candidate_id, owner=owner, limit=limit)

    async def summarize_learning_effectiveness(
        self,
        candidate_id: str,
        *,
        context: ExecutionContext,
        policy: LearningEffectivenessPolicy | None = None,
        learning_consent: bool = False,
    ) -> LearningEffectivenessSummary:
        """Return a conservative recommendation without mutating the candidate."""
        gateway = self._require_learning_gateway()
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        return await gateway.summarize_effectiveness(
            candidate_id,
            owner=owner,
            policy=policy,
        )

    async def query_learning_evaluation_archive(
        self,
        *,
        context: ExecutionContext,
        query: EvaluationArchiveQuery | None = None,
        learning_consent: bool = False,
    ) -> EvaluationArchivePage:
        """Query content-free evaluation history in the exact learning namespace."""
        gateway = self._require_learning_gateway()
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        return await gateway.query_evaluation_archive(
            owner=owner,
            query=query,
        )

    async def export_learning_candidate(
        self,
        candidate_id: str,
        *,
        context: ExecutionContext,
        evaluation_limit: int = 100,
        learning_consent: bool = False,
    ) -> LearningCandidateExport:
        """Export authorized candidate metadata, content, and evaluation provenance."""
        gateway = self._require_learning_gateway()
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        return await gateway.export(
            candidate_id,
            owner=owner,
            evaluation_limit=evaluation_limit,
        )

    async def list_learning_candidate_events(
        self,
        candidate_id: str,
        *,
        context: ExecutionContext,
        after_sequence: int = 0,
        limit: int = 100,
        learning_consent: bool = False,
    ) -> list[LearningEvent]:
        """Read canonical candidate events using an authorized monotonic cursor."""
        gateway = self._require_learning_gateway()
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        return await gateway.list_events(
            candidate_id,
            owner=owner,
            after_sequence=after_sequence,
            limit=limit,
        )

    async def evaluate_learning_candidate(
        self,
        candidate_id: str,
        *,
        context: ExecutionContext,
        verdict: EvaluationVerdict,
        evaluator_digest: str,
        evidence_artifact_ids: tuple[str, ...] | list[str],
        safety_passed: bool,
        evaluated_by: PromotionActor,
        mutation_id: str,
        evaluation_id: str | None = None,
        metrics: dict[str, Any] | None = None,
        control_metrics: dict[str, Any] | None = None,
        notes: str | None = None,
        learning_consent: bool = False,
    ) -> CandidateRecord:
        """Record an independent evidence-backed candidate evaluation."""
        evaluation = CandidateEvaluation(
            evaluation_id=evaluation_id or f"le_{uuid4().hex}",
            candidate_id=candidate_id,
            verdict=verdict,
            evaluator_digest=evaluator_digest,
            evidence_artifact_ids=tuple(evidence_artifact_ids),
            safety_passed=safety_passed,
            evaluated_by=evaluated_by,
            metrics=metrics or {},
            control_metrics=control_metrics or {},
            notes=notes,
        )
        return await self.record_learning_candidate_evaluation(
            evaluation,
            context=context,
            mutation_id=mutation_id,
            learning_consent=learning_consent,
        )

    async def record_learning_candidate_evaluation(
        self,
        evaluation: CandidateEvaluation,
        *,
        context: ExecutionContext,
        mutation_id: str,
        learning_consent: bool = False,
    ) -> CandidateRecord:
        """Record a prebuilt, immutable evaluation after exact-scope authorization."""
        if not isinstance(evaluation, CandidateEvaluation):
            raise TypeError("evaluation must be a CandidateEvaluation")
        gateway = self._require_learning_gateway(write=True)
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        return await gateway.evaluate(
            evaluation,
            owner=owner,
            mutation_id=mutation_id,
        )

    async def promote_learning_candidate(
        self,
        candidate_id: str,
        *,
        context: ExecutionContext,
        actor: PromotionActor,
        mutation_id: str,
        learning_consent: bool = False,
    ) -> CandidateRecord:
        """Apply one reviewed promotion through the host-only configured adapter."""
        gateway = self._require_learning_gateway(write=True)
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        policy = self._learning_policy
        if policy is None:  # pragma: no cover - constructor invariant
            raise AssertionError("learning gateway without policy")
        return await gateway.promote(
            candidate_id,
            policy=policy,
            owner=owner,
            actor=actor,
            mutation_id=mutation_id,
        )

    async def transition_learning_candidate(
        self,
        candidate_id: str,
        *,
        context: ExecutionContext,
        action: CandidateAction,
        mutation_id: str,
        learning_consent: bool = False,
    ) -> CandidateRecord:
        """Quarantine, restore, or tombstone an unpromoted candidate."""
        gateway = self._require_learning_gateway(write=True)
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        return await gateway.transition(
            candidate_id,
            owner=owner,
            action=action,
            mutation_id=mutation_id,
        )

    async def rollback_learning_candidate(
        self,
        candidate_id: str,
        *,
        context: ExecutionContext,
        actor: PromotionActor,
        mutation_id: str,
        learning_consent: bool = False,
    ) -> CandidateRecord:
        """Reverse one promoted learning through a certified host adapter."""
        gateway = self._require_learning_gateway(write=True)
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        return await gateway.rollback(
            candidate_id,
            owner=owner,
            actor=actor,
            mutation_id=mutation_id,
        )

    async def reconcile_learning_candidate(
        self,
        candidate_id: str,
        *,
        context: ExecutionContext,
        kind: ReconciliationKind,
        verdict: ReconciliationVerdict,
        reconciler_digest: str,
        evidence_artifact_ids: tuple[str, ...] | list[str],
        reconciled_by: PromotionActor,
        mutation_id: str,
        reconciliation_id: str | None = None,
        target_reference: str | None = None,
        notes: str | None = None,
        learning_consent: bool = False,
    ) -> CandidateRecord:
        """Resolve an ambiguous promotion/rollback from independent evidence."""
        gateway = self._require_learning_gateway(write=True)
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        reconciliation = CandidateReconciliation(
            reconciliation_id=reconciliation_id or f"lrec_{uuid4().hex}",
            candidate_id=candidate_id,
            kind=kind,
            verdict=verdict,
            reconciler_digest=reconciler_digest,
            evidence_artifact_ids=tuple(evidence_artifact_ids),
            reconciled_by=reconciled_by,
            target_reference=target_reference,
            notes=notes,
        )
        return await gateway.reconcile(
            reconciliation,
            owner=owner,
            mutation_id=mutation_id,
        )

    async def recover_run(
        self,
        run_id: str,
        *,
        context: ExecutionContext | None = None,
    ) -> HarnessRun:
        """Claim and classify one stranded run without blind effect replay."""
        owner = RunOwner(
            tenant_id=context.tenant_id if context is not None else self._tenant_id,
            user_id=context.user_id if context is not None else self.user_id,
        )
        store = self._get_runtime_store()
        snapshot = await asyncio.to_thread(store.get_run, run_id, owner=owner)
        handle = self._runtime_handle(run_id, owner=owner)
        if snapshot.terminal:
            return handle

        acquire = self._runtime_store_lease_method("acquire_run_lease")
        release = self._runtime_store_lease_method("release_run_lease")
        claim_id = f"recovery:{self._runtime_worker_id}:{run_id}"
        decision, cancelled, error = await drain_thread_call(
            lambda: acquire(
                run_id,
                worker_id=self._runtime_worker_id,
                claim_id=claim_id,
                lease_seconds=self._runtime_lease_seconds,
                owner=owner,
            )
        )
        if error is not None:
            if cancelled:
                raise asyncio.CancelledError
            raise error
        claim = decision.claim
        if cancelled:
            await drain_thread_call(lambda: release(claim))
            raise asyncio.CancelledError

        recovered_request = None
        recovered_child_spec = None
        try:
            snapshot = await asyncio.to_thread(store.get_run, run_id, owner=owner)
            if snapshot.terminal:
                return handle
            child_recovery = inspect_child_recovery(store, snapshot, owner=owner)
            try:
                operation = await asyncio.to_thread(
                    store.get_operation,
                    f"{run_id}:model:1",
                    owner=owner,
                )
            except OperationNotFoundError:
                operation = None

            operation_safety_blocked = model_operation_has_unknown_effects(store, run_id)
            recovery_preparation_error: BaseException | None = None
            restartable_operation = operation is None or (
                operation.state is OperationState.PLANNED
            )
            if (
                child_recovery.error is None
                and not operation_safety_blocked
                and operation is not None
                and operation.state is OperationState.DISPATCHING
                and _is_durable_model_loop_intent(operation)
            ):
                try:
                    operation = await self._get_operation_gateway().recover_interrupted(
                        operation.intent.operation_id,
                        recovery_id=(
                            f"{operation.intent.operation_id}:recover:"
                            f"{operation.revision + 1}"
                        ),
                    )
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    recovery_preparation_error = exc
                else:
                    restartable_operation = operation.state is OperationState.PLANNED

            if child_recovery.error is not None:
                self._settle_runtime_failed(
                    run_id,
                    cause=child_recovery.error,
                    reason_code=child_recovery.error.code,
                )
            elif operation_safety_blocked:
                wait_for_model_operation_reconciliation(store, run_id)
            elif recovery_preparation_error is not None:
                reason_code = (
                    recovery_preparation_error.code
                    if isinstance(recovery_preparation_error, HarnessError)
                    else "RUN_RECOVERY_OPERATION_RECLAIM_FAILED"
                )
                self._settle_runtime_failed(
                    run_id,
                    cause=recovery_preparation_error,
                    reason_code=reason_code,
                )
            elif operation is not None and operation.state in {
                OperationState.DISPATCHING,
                OperationState.UNKNOWN,
            }:
                # A dispatching outer operation is restartable only when its
                # persisted intent explicitly identifies the nested provider
                # gateway mode reclaimed above.
                wait_for_model_operation_reconciliation(store, run_id)
            elif operation is not None and operation.state is OperationState.CANCELLED:
                self._settle_runtime_cancel(run_id)
            elif operation is not None and operation.state is OperationState.FAILED:
                self._settle_runtime_failed(
                    run_id,
                    cause=HarnessError(
                        code="RUN_RECOVERY_OPERATION_FAILED",
                        category="recovery",
                        message="The interrupted operation has a known failed settlement.",
                        retryable=False,
                        details={"run_id": run_id},
                    ),
                    reason_code="RUN_RECOVERY_OPERATION_FAILED",
                )
            elif operation is not None and operation.state is OperationState.SUCCEEDED:
                result_reference = (
                    operation.settlement.result_reference
                    if operation.settlement is not None
                    else None
                )
                if self._artifact_store is None or result_reference is None:
                    self._settle_runtime_failed(
                        run_id,
                        cause=HarnessError(
                            code="RUN_RECOVERY_RESULT_UNAVAILABLE",
                            category="recovery",
                            message=(
                                "The model operation succeeded, but no certified "
                                "durable result artifact can complete the run."
                            ),
                            retryable=False,
                            details={
                                "run_id": run_id,
                                "result_reference": result_reference,
                            },
                        ),
                        reason_code="RUN_RECOVERY_RESULT_UNAVAILABLE",
                    )
                else:
                    try:
                        artifact = await asyncio.to_thread(
                            store.get_artifact,
                            result_reference,
                            owner=owner,
                        )
                        restored = await self._artifact_store.load_json(artifact)
                        if child_recovery.spec is not None:
                            settlement = operation.settlement
                            if settlement is None:  # pragma: no cover - operation invariant
                                raise RuntimeError(
                                    "successful child recovery lacks operation settlement"
                                )
                            enforce_child_result_contracts(
                                store=store,
                                owner=owner,
                                spec=child_recovery.spec,
                                settlement=settlement,
                                result=restored,
                            )
                    except BaseException as exc:
                        if isinstance(exc, asyncio.CancelledError):
                            raise
                        reason_code = (
                            exc.code
                            if isinstance(exc, HarnessError)
                            else "RUN_RECOVERY_ARTIFACT_FAILED"
                        )
                        self._settle_runtime_failed(
                            run_id,
                            cause=exc,
                            reason_code=reason_code,
                        )
                    else:
                        current = store.get_run(run_id, owner=owner)
                        if current.state is RunState.WAITING_FOR_RECONCILIATION:
                            current = store.apply_transition(
                                LifecycleTransition(
                                    run_id=run_id,
                                    kind=TransitionKind.RESUME,
                                    transition_id=f"{run_id}:reconciliation-resume",
                                    reason_code="MODEL_OPERATION_RECONCILED",
                                ),
                                expected_revision=current.revision,
                            ).lifecycle.after
                        self._run_results[run_id] = restored
                        store.apply_transition(
                            LifecycleTransition(
                                run_id=run_id,
                                kind=TransitionKind.COMPLETE,
                                transition_id=f"{run_id}:complete",
                                reason_code="RUN_RECOVERED_FROM_RESULT_ARTIFACT",
                            ),
                            expected_revision=current.revision,
                            terminal=TerminalRecord(
                                run_id=run_id,
                                state=RunState.COMPLETED,
                                value=restored,
                            ),
                        )
            elif restartable_operation:
                current = store.get_run(run_id, owner=owner)
                if child_recovery.terminal_ancestor is not None:
                    self._settle_runtime_cancel(
                        run_id,
                        reason_code="CHILD_RECOVERY_ANCESTOR_TERMINAL",
                    )
                elif current.state is RunState.CANCELLING:
                    self._settle_runtime_cancel(run_id)
                else:
                    try:
                        candidate_request = await load_runtime_request_checkpoint(
                            store=store,
                            artifact_store=self._artifact_store,
                            snapshot=current,
                            owner=owner,
                            harness_spec_digest=self._spec.settings_digest,
                        )
                        if operation is not None:
                            durable_model_loop = _is_durable_model_loop_intent(operation)
                            validate_recoverable_model_intent(
                                operation,
                                run_id=run_id,
                                model_target=self.model_name,
                                request_digest=runtime_request_digest(
                                    message=candidate_request.message,
                                    context=candidate_request.context,
                                    kwargs=candidate_request.kwargs,
                                    harness_spec_digest=self._spec.settings_digest,
                                ),
                                harness_spec_digest=self._spec.settings_digest,
                                timeout_seconds=(
                                    child_recovery.spec.budget.timeout_seconds + 1
                                    if child_recovery.spec is not None
                                    else None
                                ),
                                effect_class=(
                                    EffectClass.READ_ONLY
                                    if durable_model_loop
                                    else EffectClass.NON_REPEATABLE
                                ),
                                orchestration_mode=(
                                    _DURABLE_MODEL_LOOP_MODE
                                    if durable_model_loop
                                    else None
                                ),
                            )
                        recovered_request = candidate_request
                        recovered_child_spec = child_recovery.spec
                    except BaseException as exc:
                        if isinstance(exc, asyncio.CancelledError):
                            raise
                        reason_code = (
                            exc.code
                            if isinstance(exc, HarnessError)
                            else "RUN_RECOVERY_CHECKPOINT_INVALID"
                        )
                        self._settle_runtime_failed(
                            run_id,
                            cause=exc,
                            reason_code=reason_code,
                        )
            else:  # pragma: no cover - OperationState is exhaustively handled above
                assert operation is not None
                raise AssertionError(f"unhandled operation state: {operation.state}")
        finally:
            _released, release_cancelled, release_error = await drain_thread_call(
                lambda: release(claim)
            )
            if release_error is not None and not isinstance(
                release_error,
                RuntimeLeaseLostError,
            ):
                raise release_error
            if release_cancelled:
                raise asyncio.CancelledError
        if recovered_request is not None:
            recovered_model_loop = (
                _DURABLE_MODEL_LOOP_MODE
                if operation is not None and _is_durable_model_loop_intent(operation)
                else "legacy"
            )
            self._launch_runtime_worker(
                run_id,
                {
                    "message": recovered_request.message,
                    "context": recovered_request.context,
                    "kwargs": recovered_request.kwargs,
                    "steering": [],
                    "child_spec": recovered_child_spec,
                    "model_loop_mode": recovered_model_loop,
                    "resume_agno_checkpoint": (
                        recovered_model_loop == _DURABLE_MODEL_LOOP_MODE
                    ),
                },
            )
        return handle

    async def recover_pending_runs(
        self,
        *,
        context: ExecutionContext | None = None,
        cursor: str | None = None,
        limit: int = 25,
        concurrency: int = 4,
        minimum_age_seconds: int | None = None,
    ) -> RuntimeRecoveryBatch:
        """Recover one bounded, exact-owner page of stranded executable runs."""
        return await recover_pending_runs(
            store=self._get_runtime_store(),
            recover_run=self.recover_run,
            default_owner=RunOwner(self._tenant_id, self.user_id),
            context=context,
            cursor=cursor,
            limit=limit,
            concurrency=concurrency,
            minimum_age_seconds=(
                self._runtime_lease_seconds if minimum_age_seconds is None else minimum_age_seconds
            ),
        )

    async def reconcile_pending_operations(
        self,
        observer: OperationReconciliationObserver,
        *,
        observer_digest: str,
        context: ExecutionContext | None = None,
        cursor: str | None = None,
        limit: int = 25,
        concurrency: int = 4,
        minimum_age_seconds: int | None = None,
    ) -> OperationReconciliationBatch:
        if self._artifact_store is None:
            raise HarnessError(
                code="OPERATION_RECONCILIATION_ARTIFACT_STORE_REQUIRED",
                category="operation",
                message="Operation reconciliation requires a durable ArtifactStore.",
                retryable=False,
            )
        return await reconcile_runtime_operations(
            store=self._get_runtime_store(),
            artifact_store=self._artifact_store,
            observer=observer,
            observer_digest=observer_digest,
            recover_run=self.recover_run,
            default_owner=RunOwner(self._tenant_id, self.user_id),
            context=context,
            cursor=cursor,
            limit=limit,
            concurrency=concurrency,
            minimum_age_seconds=(
                self._runtime_lease_seconds if minimum_age_seconds is None else minimum_age_seconds
            ),
        )

    async def _wait_runtime_run(self, run_id: str, *, timeout: float | None) -> Any:
        task = self._live_runs.get(run_id)
        if task is not None:
            awaited = asyncio.shield(task)
            if timeout is None:
                await awaited
            else:
                await asyncio.wait_for(awaited, timeout=timeout)
        elif timeout is not None:
            deadline = monotonic() + timeout
            while not self._get_runtime_store().get_run(run_id).terminal:
                if monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for run '{run_id}'.")
                await asyncio.sleep(0.05)
        return self._run_results.get(run_id)

    def _settle_runtime_cancel(
        self,
        run_id: str,
        *,
        reason_code: str = "RUN_CANCELLED",
    ) -> RunSnapshot:
        store = self._get_runtime_store()
        snapshot = store.get_run(run_id)
        if snapshot.terminal or snapshot.state is RunState.WAITING_FOR_RECONCILIATION:
            return snapshot
        if snapshot.state != RunState.CANCELLING:
            requested = store.apply_transition(
                LifecycleTransition(
                    run_id=run_id,
                    kind=TransitionKind.REQUEST_CANCEL,
                    transition_id=f"{run_id}:cancel-request",
                    reason_code=reason_code,
                ),
                expected_revision=snapshot.revision,
            )
            snapshot = requested.lifecycle.after
        settled = store.apply_transition(
            LifecycleTransition(
                run_id=run_id,
                kind=TransitionKind.CONFIRM_CANCEL,
                transition_id=f"{run_id}:cancelled",
                reason_code=reason_code,
            ),
            expected_revision=snapshot.revision,
            terminal=TerminalRecord(
                run_id=run_id,
                state=RunState.CANCELLED,
                error={
                    "code": reason_code,
                    "safe_message": "The run was cancelled.",
                },
            ),
        )
        return settled.lifecycle.after

    def _settle_runtime_failed(
        self,
        run_id: str,
        *,
        cause: BaseException,
        reason_code: str = "RUN_EXECUTION_FAILED",
    ) -> RunSnapshot:
        store = self._get_runtime_store()
        snapshot = store.get_run(run_id)
        if snapshot.terminal:
            return snapshot
        settled = store.apply_transition(
            LifecycleTransition(
                run_id=run_id,
                kind=TransitionKind.FAIL,
                transition_id=f"{run_id}:failed",
                reason_code=reason_code,
            ),
            expected_revision=snapshot.revision,
            terminal=TerminalRecord(
                run_id=run_id,
                state=RunState.FAILED,
                error=self._safe_runtime_error(cause, run_id=run_id),
            ),
        )
        return settled.lifecycle.after

    def _settle_runtime_cancel_or_unknown(
        self,
        run_id: str,
        *,
        cause: BaseException,
    ) -> RunSnapshot:
        supervisor_failure = self._runtime_supervisor_failures.pop(run_id, None)
        cause = supervisor_failure or cause
        store = self._get_runtime_store()
        if model_operation_has_unknown_effects(store, run_id):
            return wait_for_model_operation_reconciliation(store, run_id)
        if isinstance(cause, RuntimeLeaseLostError):
            return self._settle_runtime_failed(
                run_id,
                cause=cause,
                reason_code="RUNTIME_LEASE_LOST",
            )
        if supervisor_failure is not None:
            return self._settle_runtime_failed(
                run_id,
                cause=cause,
                reason_code="RUNTIME_SUPERVISOR_FAILED",
            )
        return self._settle_runtime_cancel(run_id)

    def _runtime_store_lease_method(self, name: str) -> Callable[..., Any]:
        method = getattr(self._get_runtime_store(), name, None)
        if not callable(method):
            raise HarnessError(
                code="RUNTIME_STORE_LEASES_REQUIRED",
                category="configuration",
                message=(
                    "Lifecycle execution requires a RuntimeStore implementing "
                    "schema-v5 run/session leases."
                ),
                retryable=False,
                details={"missing_method": name},
            )
        return method

    async def _acquire_runtime_run_lease(self, run_id: str) -> RunLeaseClaim:
        request = self._run_requests[run_id]
        owner = self._runtime_owner(request["context"])
        claim_id = f"{self._runtime_worker_id}:{run_id}"
        lease_seconds = self._runtime_lease_seconds
        acquire = self._runtime_store_lease_method("acquire_run_lease")
        release = self._runtime_store_lease_method("release_run_lease")
        while True:
            decision, cancelled, error = await drain_thread_call(
                lambda: acquire(
                    run_id,
                    worker_id=self._runtime_worker_id,
                    claim_id=claim_id,
                    lease_seconds=lease_seconds,
                    owner=owner,
                )
            )
            if error is not None:
                if cancelled:
                    raise asyncio.CancelledError
                if not isinstance(error, RuntimeLeaseUnavailableError):
                    raise error
                exc = error
                details = exc.details or {}
                retry_after = float(details.get("retry_after_seconds", 0.1))
                await asyncio.sleep(min(1.0, max(0.05, retry_after)))
                continue
            claim = decision.claim
            if cancelled:
                (
                    _released,
                    _release_cancelled,
                    release_error,
                ) = await drain_thread_call(functools.partial(release, claim))
                if release_error is not None and not isinstance(
                    release_error,
                    RuntimeLeaseLostError,
                ):
                    logger.warning(
                        "Could not release cancelled pre-dispatch lease for run %s",
                        run_id,
                        exc_info=(
                            type(release_error),
                            release_error,
                            release_error.__traceback__,
                        ),
                    )
                raise asyncio.CancelledError
            return claim

    async def _runtime_lease_heartbeat(
        self,
        run_id: str,
        claim: RunLeaseClaim,
        worker_task: asyncio.Task[Any],
    ) -> None:
        interval = self._runtime_lease_interval_seconds
        lease_seconds = self._runtime_lease_seconds
        renew = self._runtime_store_lease_method("renew_run_lease")
        get_run = self._get_runtime_store().get_run
        try:
            while True:
                await asyncio.sleep(interval)
                renewed, cancelled, error = await drain_thread_call(
                    functools.partial(
                        renew,
                        claim,
                        lease_seconds=lease_seconds,
                    )
                )
                if cancelled:
                    raise asyncio.CancelledError
                if error is not None:
                    raise error
                claim = renewed
                current, cancelled, error = await drain_thread_call(
                    functools.partial(get_run, run_id)
                )
                if cancelled:
                    raise asyncio.CancelledError
                if error is not None:
                    raise error
                if current.state is RunState.CANCELLING:
                    worker_task.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failure = (
                exc
                if isinstance(exc, RuntimeLeaseLostError)
                else RuntimeLeaseLostError(run_id=run_id, kind=claim.run.kind)
            )
            self._runtime_supervisor_failures[run_id] = failure
            worker_task.cancel()

    async def _execute_runtime_run_with_lease(self, run_id: str) -> None:
        claim = await self._acquire_runtime_run_lease(run_id)
        request = self._run_requests[run_id]
        worker_task = asyncio.current_task()
        if worker_task is None:  # pragma: no cover - asyncio always owns this coroutine
            raise RuntimeError("lifecycle worker requires an asyncio task owner")
        claim_token = self._active_runtime_claim.set(claim)
        context_token = self._active_runtime_context.set(request["context"])
        heartbeat = asyncio.create_task(
            self._runtime_lease_heartbeat(run_id, claim, worker_task),
            name=f"agnoclaw:lease:{run_id}",
        )
        child_spec = request.get("child_spec")
        deadline = (
            asyncio.create_task(
                supervise_child_deadline(
                    store=self._get_runtime_store(),
                    run_id=run_id,
                    spec=child_spec,
                    worker_task=worker_task,
                    failures=self._runtime_supervisor_failures,
                ),
                name=f"agnoclaw:child-deadline:{run_id}",
            )
            if isinstance(child_spec, ChildRunSpec)
            else None
        )
        try:
            await self._execute_runtime_run_in_lane(run_id)
        finally:
            try:
                if deadline is not None:
                    deadline.cancel()
                    try:
                        await deadline
                    except asyncio.CancelledError:
                        pass
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                _released, release_cancelled, release_error = await drain_thread_call(
                    lambda: self._runtime_store_lease_method("release_run_lease")(claim)
                )
                if release_error is not None and not isinstance(
                    release_error,
                    RuntimeLeaseLostError,
                ):
                    logger.warning(
                        "Could not release execution lease for run %s",
                        run_id,
                        exc_info=(
                            type(release_error),
                            release_error,
                            release_error.__traceback__,
                        ),
                    )
                if release_cancelled:
                    raise asyncio.CancelledError
                if isinstance(release_error, RuntimeLeaseLostError):
                    # Lease-loss already cancelled and classified the worker. A stale
                    # worker must never release ownership acquired under a newer fence.
                    pass
            finally:
                self._active_runtime_context.reset(context_token)
                self._active_runtime_claim.reset(claim_token)

    async def _cancel_runtime_run(self, run_id: str) -> RunSnapshot:
        store = self._get_runtime_store()
        snapshot = store.get_run(run_id)
        if snapshot.terminal or snapshot.state is RunState.WAITING_FOR_RECONCILIATION:
            return snapshot
        if snapshot.state != RunState.CANCELLING:
            snapshot = store.apply_transition(
                LifecycleTransition(
                    run_id=run_id,
                    kind=TransitionKind.REQUEST_CANCEL,
                    transition_id=f"{run_id}:cancel-request",
                ),
                expected_revision=snapshot.revision,
            ).lifecycle.after
        resume = self._run_resume_events.get(run_id)
        if resume is not None:
            resume.set()
        task = self._live_runs.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
        current = store.get_run(run_id)
        if current.state == RunState.CANCELLING:
            current = self._settle_runtime_cancel(run_id)
        return current

    async def _command_runtime_run(self, run_id: str, command: RunCommand) -> Any:
        store = self._get_runtime_store()
        snapshot = store.get_run(run_id)
        if isinstance(command, Fork):
            raise HarnessError(
                code="RUN_FORK_CHECKPOINT_REQUIRED",
                category="lifecycle",
                message="Fork requires a certified durable checkpoint.",
                retryable=False,
                details={"run_id": run_id},
            )
        if isinstance(command, Pause) and snapshot.state != RunState.QUEUED:
            raise HarnessError(
                code="RUN_PAUSE_SAFE_POINT_UNAVAILABLE",
                category="lifecycle",
                message="This run can only pause before provider dispatch.",
                retryable=False,
                details={"run_id": run_id, "state": snapshot.state.value},
            )
        if isinstance(command, Steer) and snapshot.state not in {
            RunState.CREATED,
            RunState.QUEUED,
        }:
            raise HarnessError(
                code="RUN_STEERING_CLOSED",
                category="lifecycle",
                message="The run has passed its steering safe point.",
                retryable=False,
                details={"run_id": run_id, "state": snapshot.state.value},
            )
        intent = command_decision(snapshot, command)
        if intent.transition is None:
            return intent
        applied = store.apply_transition(
            intent.transition,
            expected_revision=snapshot.revision,
        )
        after = applied.lifecycle.after
        if isinstance(command, Pause):
            resume = self._run_resume_events.get(run_id)
            if resume is not None:
                resume.clear()
        elif isinstance(command, Resume):
            resume = self._run_resume_events.get(run_id)
            if resume is not None:
                resume.set()
        elif isinstance(command, Steer):
            request = self._run_requests.get(run_id)
            if request is not None:
                request["steering"].append(command.instruction)
        elif isinstance(command, Respond):
            # The durable provider/tool continuation loop consumes the response
            # record in T6; the lifecycle binding is already exact here.
            pass
        return after

    async def _execute_runtime_run(self, run_id: str) -> None:
        request = self._run_requests[run_id]
        context = request["context"]
        try:
            async with self._session_lanes.hold(
                tenant_id=context.tenant_id,
                session_id=context.session_id,
                run_id=run_id,
            ):
                await self._execute_runtime_run_with_lease(run_id)
        except asyncio.CancelledError:
            self._settle_runtime_cancel_or_unknown(
                run_id,
                cause=asyncio.CancelledError(),
            )
        except BaseException as exc:
            store = self._get_runtime_store()
            if model_operation_has_unknown_effects(store, run_id):
                wait_for_model_operation_reconciliation(store, run_id)
            else:
                self._settle_runtime_failed(run_id, cause=exc)

    async def _execute_runtime_run_in_lane(self, run_id: str) -> None:
        store = self._get_runtime_store()
        request = self._run_requests[run_id]
        runtime_run_lease: _RunGateLease | None = None
        runtime_gate_token: Any = None
        try:
            await asyncio.sleep(0)
            snapshot = store.get_run(run_id)
            if snapshot.state == RunState.PAUSED:
                await self._run_resume_events[run_id].wait()
                snapshot = store.get_run(run_id)
            if snapshot.state == RunState.CANCELLING:
                self._settle_runtime_cancel(run_id)
                return
            if snapshot.state == RunState.QUEUED:
                started = store.apply_transition(
                    LifecycleTransition(
                        run_id=run_id,
                        kind=TransitionKind.START,
                        transition_id=f"{run_id}:start",
                    ),
                    expected_revision=snapshot.revision,
                ).lifecycle.after
            elif snapshot.state == RunState.RUNNING:
                started = snapshot
            else:
                raise HarnessError(
                    code="RUN_WORKER_STATE_INVALID",
                    category="lifecycle",
                    message=f"Worker cannot start a run in state '{snapshot.state.value}'.",
                    retryable=False,
                    details={"run_id": run_id, "state": snapshot.state.value},
                )
            store.apply_transition(
                LifecycleTransition(
                    run_id=run_id,
                    kind=TransitionKind.CLOSE_STEERING,
                    transition_id=f"{run_id}:steering-closed",
                ),
                expected_revision=started.revision,
            )
            message = request["message"]
            steering = request["steering"]
            if steering:
                message += "\n\nOperator steering:\n" + "\n".join(f"- {item}" for item in steering)
            checkpoint = RuntimeRequestCheckpoint.create(
                run_id=run_id,
                message=message,
                context=request["context"],
                kwargs=request["kwargs"],
                harness_spec_digest=self._spec.settings_digest,
            )
            await persist_runtime_request_checkpoint(
                checkpoint,
                store=store,
                artifact_store=self._artifact_store,
                worker_id=self._runtime_worker_id,
            )
            requested_loop_mode = request.get("model_loop_mode")
            if requested_loop_mode is None:
                durable_model_loop = self._can_enable_durable_model_loop(request)
                model_loop_mode = (
                    _DURABLE_MODEL_LOOP_MODE if durable_model_loop else "legacy"
                )
                request["model_loop_mode"] = model_loop_mode
            elif requested_loop_mode in {"legacy", _DURABLE_MODEL_LOOP_MODE}:
                model_loop_mode = requested_loop_mode
                durable_model_loop = model_loop_mode == _DURABLE_MODEL_LOOP_MODE
                if durable_model_loop and not self._can_enable_durable_model_loop(request):
                    raise HarnessError(
                        code="RUN_RECOVERY_MODEL_LOOP_UNAVAILABLE",
                        category="recovery",
                        message=(
                            "The persisted durable model loop cannot be materialized "
                            "by this harness instance."
                        ),
                        retryable=False,
                        details={"run_id": run_id},
                    )
            else:
                raise HarnessError(
                    code="RUN_MODEL_LOOP_MODE_INVALID",
                    category="recovery",
                    message="The lifecycle request has an invalid model-loop mode.",
                    retryable=False,
                    details={"run_id": run_id},
                )
            request_kwargs = request.get("kwargs")
            if not isinstance(request_kwargs, dict):
                raise HarnessError(
                    code="RUN_REQUEST_OPTIONS_INVALID",
                    category="recovery",
                    message="The lifecycle request options are not a mapping.",
                    retryable=False,
                    details={"run_id": run_id},
                )
            uses_streaming_inner_call = bool(
                request.get("presentation") is not None
                or request_kwargs.get("persist_output")
            )
            isolated_inner_call = self._can_materialize_isolated_run(
                stream=uses_streaming_inner_call,
                skill=request_kwargs.get("skill"),
            )
            context = request["context"]
            runtime_run_lease = self._acquire_run_gate(
                isolated=isolated_inner_call,
                session_id=context.session_id,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
            )
            runtime_gate_token = self._active_runtime_run_gate_owned.set(True)
            token = self._active_runtime_run_id.set(run_id)
            durable_token = self._active_durable_model_loop.set(durable_model_loop)
            resume_token = self._active_runtime_checkpoint_resume.set(
                bool(request.get("resume_agno_checkpoint", False))
            )
            prepared_model_token = None
            prepared_model_resource = None
            try:
                if self._model_factory is not None:
                    prepared_model = self._model_factory.create()
                    prepared_model_resource = OwnedAgnoModelResource(prepared_model)
                    prepared_model_token = self._prepared_run_model.set(
                        (prepared_model, prepared_model_resource)
                    )
                child_spec = request.get("child_spec")
                intent_metadata = {
                    "harness_spec_digest": self._spec.settings_digest,
                    "operation_ordinal": 1,
                }
                if durable_model_loop:
                    intent_metadata["orchestration_mode"] = _DURABLE_MODEL_LOOP_MODE
                intent = OperationIntent(
                    operation_id=f"{run_id}:model:1",
                    run_id=run_id,
                    attempt_id=f"{run_id}:attempt:1",
                    kind=OperationKind.MODEL,
                    target=self.model_name,
                    request_digest=runtime_request_digest(
                        message=message,
                        context=request["context"],
                        kwargs=request["kwargs"],
                        harness_spec_digest=self._spec.settings_digest,
                    ),
                    effect_class=(
                        EffectClass.READ_ONLY
                        if durable_model_loop
                        else EffectClass.NON_REPEATABLE
                    ),
                    timeout_seconds=(
                        child_spec.budget.timeout_seconds + 1
                        if isinstance(child_spec, ChildRunSpec)
                        else None
                    ),
                    metadata=intent_metadata,
                )
                async def dispatch_model_loop() -> Any:
                    value = await execute_presented_model_request(
                        self,
                        run_id=run_id,
                        message=message,
                        request=request,
                    )
                    if durable_model_loop and model_operation_has_unknown_effects(
                        store, run_id
                    ):
                        raise OperationDispatchDeferredError(
                            run_id=run_id,
                            reason_code="NESTED_MODEL_EFFECT_UNRESOLVED",
                        )
                    return value

                execution = await self._get_operation_gateway().execute(
                    intent,
                    dispatch_model_loop,
                    settlement_evidence=agno_result_settlement_evidence,
                )
                result = execution.value
            finally:
                if prepared_model_token is not None:
                    self._prepared_run_model.reset(prepared_model_token)
                if prepared_model_resource is not None:
                    await prepared_model_resource.aclose()
                self._active_runtime_checkpoint_resume.reset(resume_token)
                self._active_durable_model_loop.reset(durable_token)
                self._active_runtime_run_id.reset(token)
            current = store.get_run(run_id)
            if current.state == RunState.CANCELLING:
                self._settle_runtime_cancel(run_id)
                return
            persisted_result = persisted_result_value(result)
            if isinstance(child_spec, ChildRunSpec):
                settlement = execution.record.settlement
                if settlement is None:  # pragma: no cover - successful gateway invariant
                    raise RuntimeError("successful child model operation lacks settlement")
                enforce_child_result_contracts(
                    store=store,
                    owner=self._runtime_owner(request["context"]),
                    spec=child_spec,
                    settlement=settlement,
                    result=persisted_result,
                )
            self._run_results[run_id] = result
            store.apply_transition(
                LifecycleTransition(
                    run_id=run_id,
                    kind=TransitionKind.COMPLETE,
                    transition_id=f"{run_id}:complete",
                ),
                expected_revision=current.revision,
                terminal=TerminalRecord(
                    run_id=run_id,
                    state=RunState.COMPLETED,
                    value=persisted_result,
                ),
            )
        except asyncio.CancelledError:
            self._settle_runtime_cancel_or_unknown(
                run_id,
                cause=asyncio.CancelledError(),
            )
        except BaseException as exc:
            current = store.get_run(run_id)
            if current.terminal:
                return
            if model_operation_has_unknown_effects(store, run_id):
                wait_for_model_operation_reconciliation(store, run_id)
                return
            self._settle_runtime_failed(run_id, cause=exc)
        finally:
            if runtime_gate_token is not None:
                self._active_runtime_run_gate_owned.reset(runtime_gate_token)
            if runtime_run_lease is not None:
                runtime_run_lease.release()

    def _route_convenience_call_through_lifecycle(self) -> bool:
        """Return whether a public convenience call must enter ``start()``.

        The lifecycle worker calls :meth:`arun` again after binding the logical
        run ID in a task-local context variable.  That inner call is the one
        certified direct execution boundary; only an outer explicit-profile
        call is adapted through ``start()`` plus ``wait()``.  ``legacy`` is the
        named compatibility escape hatch during the 0.12 migration window.
        """
        return (
            self.profile is not RuntimeProfile.LEGACY
            and self._active_runtime_run_id.get() is None
        )

    def _get_sync_lifecycle_coordinator(self) -> SyncLifecycleCoordinator:
        with self._sync_lifecycle_lock:
            coordinator = self._sync_lifecycle_coordinator
            if coordinator is None:
                coordinator = SyncLifecycleCoordinator(name=f"{self.name}:{id(self)}")
                self._sync_lifecycle_coordinator = coordinator
            return coordinator

    @staticmethod
    def _convenience_lifecycle_options(
        *,
        stream_events: bool,
        max_turns: int | None,
        skill: str | None,
        output_schema: type | None,
        tool_schema_overrides: dict[str, dict[str, Any]] | None,
        tool_arg_bindings: dict[str, dict[str, Any]] | None,
        dependencies: dict[str, Any] | None,
        session_state: dict[str, Any] | None,
        add_dependencies_to_context: bool | None,
        add_session_state_to_context: bool | None,
        knowledge_filters: dict[str, Any] | list[Any] | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Project public convenience options into one lifecycle request."""
        options = dict(kwargs)
        optional = {
            "max_turns": max_turns,
            "skill": skill,
            "output_schema": output_schema,
            "tool_schema_overrides": tool_schema_overrides,
            "tool_arg_bindings": tool_arg_bindings,
            "dependencies": dependencies,
            "session_state": session_state,
            "add_dependencies_to_context": add_dependencies_to_context,
            "add_session_state_to_context": add_session_state_to_context,
            "knowledge_filters": knowledge_filters,
        }
        options.update({key: value for key, value in optional.items() if value is not None})
        if stream_events:
            options["stream_events"] = True
        return options

    async def _arun_via_lifecycle(
        self,
        message: str,
        *,
        stream: bool,
        session_id: str | None,
        user_id: str | None,
        context: ExecutionContext | None,
        metadata: dict[str, Any] | None,
        learning_consent: bool,
        options: dict[str, Any],
    ) -> Any:
        """Execute one public convenience call through the lifecycle kernel."""
        if stream:
            run, events = await self._open_lifecycle_presentation(
                message,
                session_id=session_id,
                user_id=user_id,
                context=context,
                metadata=metadata,
                learning_consent=learning_consent,
                options=options,
            )
            return self._lifecycle_raw_events(run, events)

        run = await self.start(
            message,
            session_id=session_id,
            user_id=user_id,
            context=context,
            metadata=metadata,
            learning_consent=learning_consent,
            **options,
        )
        return await run.wait()

    async def _open_lifecycle_presentation(
        self,
        message: str,
        *,
        session_id: str | None,
        user_id: str | None,
        context: ExecutionContext | None,
        metadata: dict[str, Any] | None,
        learning_consent: bool,
        options: dict[str, Any],
    ) -> tuple[HarnessRun, AsyncIterator[Any]]:
        """Start one raw-event presentation without changing lifecycle authority."""
        presentation_options = dict(options)
        presentation_options.pop("stream_events", None)
        presentation = LiveRunPresentation()
        run = await self.start(
            message,
            session_id=session_id,
            user_id=user_id,
            context=context,
            metadata=metadata,
            learning_consent=learning_consent,
            _presentation=presentation,
            **presentation_options,
        )
        presentation.bind(run.run_id)
        return run, presentation.events()

    @staticmethod
    async def _lifecycle_raw_events(
        run: HarnessRun,
        events: AsyncIterator[Any],
    ) -> AsyncIterator[Any]:
        """Yield raw display events, then reconcile completion/error to run truth."""
        completed = False
        try:
            async for event in events:
                yield event
            completed = True
        finally:
            if not completed:
                closer = getattr(events, "aclose", None)
                if callable(closer):
                    await closer()
        await run.wait()

    async def _open_sync_lifecycle_stream(
        self,
        message: str,
        *,
        session_id: str | None,
        user_id: str | None,
        context: ExecutionContext | None,
        metadata: dict[str, Any] | None,
        learning_consent: bool,
        options: dict[str, Any],
    ) -> tuple[str | None, AsyncIterator[Any]]:
        run, events = await self._open_lifecycle_presentation(
            message,
            session_id=session_id,
            user_id=user_id,
            context=context,
            metadata=metadata,
            learning_consent=learning_consent,
            options=options,
        )
        return run.run_id, self._lifecycle_raw_events(run, events)

    def run(
        self,
        message: str,
        *,
        stream: bool = False,
        stream_events: bool = False,
        max_turns: int | None = None,
        skill: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        context: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
        learning_consent: bool = False,
        output_schema: type | None = None,
        tool_schema_overrides: dict[str, dict[str, Any]] | None = None,
        tool_arg_bindings: dict[str, dict[str, Any]] | None = None,
        dependencies: dict[str, Any] | None = None,
        session_state: dict[str, Any] | None = None,
        add_dependencies_to_context: bool | None = None,
        add_session_state_to_context: bool | None = None,
        knowledge_filters: dict[str, Any] | list[Any] | None = None,
        **kwargs,
    ) -> RunOutput | Iterator[RunOutputEvent] | str:
        """Run the agent on a message.

        Bindings, schema overrides, dependencies, session state, filters, and output
        schemas are run-local and restored exactly. Eligible non-streaming calls get
        independent Agents/resources; unclassified or effectful compatibility paths
        fail fast on overlap. A stream retains its lease until exhausted or closed.
        """
        if self._route_convenience_call_through_lifecycle():
            options = self._convenience_lifecycle_options(
                stream_events=stream_events,
                max_turns=max_turns,
                skill=skill,
                output_schema=output_schema,
                tool_schema_overrides=tool_schema_overrides,
                tool_arg_bindings=tool_arg_bindings,
                dependencies=dependencies,
                session_state=session_state,
                add_dependencies_to_context=add_dependencies_to_context,
                add_session_state_to_context=add_session_state_to_context,
                knowledge_filters=knowledge_filters,
                kwargs=kwargs,
            )
            coordinator = self._get_sync_lifecycle_coordinator()
            if stream:
                return cast(
                    Iterator[RunOutputEvent],
                    coordinator.stream(
                        lambda: self._open_sync_lifecycle_stream(
                            message,
                            session_id=session_id,
                            user_id=user_id,
                            context=context,
                            metadata=metadata,
                            learning_consent=learning_consent,
                            options=options,
                        )
                    ),
                )
            return cast(
                RunOutput | str,
                coordinator.run(
                    self._arun_via_lifecycle(
                        message,
                        stream=False,
                        session_id=session_id,
                        user_id=user_id,
                        context=context,
                        metadata=metadata,
                        learning_consent=learning_consent,
                        options=options,
                    )
                ),
            )
        run_id = self._active_runtime_run_id.get() or f"run_{uuid4().hex}"
        internal_run_kind = self._internal_run_kind.get()
        effective_user, effective_session, ctx = self._resolve_run_identity(
            context=context,
            user_id=user_id,
            session_id=session_id,
            metadata=metadata,
        )
        self._prepare_context_for_run_sync(ctx)
        learning_scope = self._resolve_learning_scope(
            ctx,
            consented=learning_consent,
        )
        agno_user = learning_scope.storage_user_id if learning_scope is not None else effective_user
        agno_session = (
            learning_scope.storage_session_id if learning_scope is not None else effective_session
        )
        run_input = RunInput(
            run_id=run_id,
            message=message,
            skill=skill,
            stream=stream,
            stream_events=stream_events,
            metadata=dict(metadata or {}),
        )
        if max_turns is not None:
            run_input.metadata.setdefault("max_turns", int(max_turns))

        materialized_run = self._can_materialize_run_agent(
            stream=stream,
            skill=skill,
        )
        isolated_run = self._can_materialize_isolated_run(
            stream=stream,
            skill=skill,
        )
        run_lease = self._acquire_run_gate(
            isolated=isolated_run,
            session_id=effective_session,
            user_id=effective_user,
            tenant_id=ctx.tenant_id,
        )
        run_agent_tokens: tuple[Any, Any] | None = None
        run_tool_bundle: BuiltinToolBundle | None = None
        run_model_resource: OwnedAgnoModelResource | None = None
        try:
            if materialized_run or learning_scope is not None:
                run_agent, run_tool_bundle = materialize_run_agent(
                    self,
                    session_id=effective_session,
                    user_id=effective_user,
                    learning_scope=learning_scope,
                )
                owned_model = getattr(run_agent, "model", None)
                if self._owns_model_transport and owned_model is not None:
                    prepared = self._prepared_run_model.get()
                    run_model_resource = (
                        prepared[1]
                        if prepared is not None and prepared[0] is owned_model
                        else OwnedAgnoModelResource(owned_model)
                    )
                run_agent_tokens = self._activate_run_agent(
                    run_agent,
                    session_id=effective_session,
                )
            base_prompt = self._agent.system_message
            base_prompt_session = self._prompt_session_id
        except BaseException:
            self._deactivate_run_agent(run_agent_tokens)
            if run_model_resource is not None:
                run_model_resource.close()
            if run_tool_bundle is not None:
                run_tool_bundle.close()
            run_lease.release()
            raise
        stream_cleanup_deferred = False
        tool_scope: _ToolScope | None = None
        scoped_skill_obj: Any = None
        overflow_maintenance: Any = None

        try:
            self._run_lifecycle_hooks_sync(
                "message.received",
                context=ctx,
                run_id=run_id,
                metadata={
                    "message": message,
                    "skill": skill,
                    "stream": stream,
                    "stream_events": stream_events,
                },
            )
            # Runtime-block session scoping mirrors the system_message
            # save/restore: set for this run, restored wherever base_prompt is.
            self._prompt_session_id = effective_session

            self._emit_event_sync(
                event_type="run.started",
                run_id=run_id,
                context=ctx,
                payload={
                    "stream": stream,
                    "stream_events": stream_events,
                    "skill": skill,
                    "max_turns": max_turns,
                    "learning_scope": (
                        learning_scope.descriptor() if learning_scope is not None else None
                    ),
                },
            )
            scheduler_payload = self._scheduler_invocation_payload(run_input.metadata)
            if scheduler_payload:
                self._emit_event_sync(
                    event_type="scheduler.invocation",
                    run_id=run_id,
                    context=ctx,
                    payload=scheduler_payload,
                )

            run_input = self._run_pre_hooks_sync(run_input=run_input, context=ctx)

            before_run_decision = self._run_policy_sync(
                method_name="before_run",
                payload=run_input,
                run_input=run_input,
                context=ctx,
            )
            self._enforce_policy_decision(
                decision=before_run_decision,
                checkpoint="before_run",
                run_id=run_id,
                context=ctx,
            )
            if before_run_decision.action == PolicyAction.ALLOW_WITH_REDACTION:
                run_input.message = apply_redactions(
                    run_input.message, before_run_decision.redactions
                )

            skill_content: str | None = None
            if run_input.skill:
                self._emit_event_sync(
                    event_type="skill.load.started",
                    run_id=run_id,
                    context=ctx,
                    payload={"name": run_input.skill},
                )
                skill_decision = self._run_policy_sync(
                    method_name="before_skill_load",
                    payload=SkillLoadRequest(name=run_input.skill),
                    run_input=run_input,
                    context=ctx,
                )
                self._enforce_policy_decision(
                    decision=skill_decision,
                    checkpoint="before_skill_load",
                    run_id=run_id,
                    context=ctx,
                )
                skill_content = self.skills.load_skill(run_input.skill)
                self._emit_event_sync(
                    event_type="skill.load.completed",
                    run_id=run_id,
                    context=ctx,
                    payload={"name": run_input.skill, "loaded": bool(skill_content)},
                )

                if skill_content:
                    # ── Skill enforcement: context:fork ──────────────────
                    # If the skill declares context: fork, run it in an isolated
                    # subagent instead of the main agent loop.
                    skill_obj = self.skills._get_skill(run_input.skill)
                    if skill_obj and skill_obj.meta.context == "fork":
                        from .tools.tasks import _run_subagent

                        # Subagent forks build their own model from the
                        # "provider:id" spec — instance-level options on
                        # this harness's model (caching, effort, custom
                        # clients) intentionally don't propagate.
                        base_model = self.model_name
                        active_provider = base_model.split(":", 1)[0] if ":" in base_model else None
                        subagent_model = _resolve_model(
                            skill_obj.meta.model or base_model,
                            active_provider,
                            self.config,
                        )
                        fork_result = _run_subagent(
                            task=run_input.message,
                            instructions=skill_content,
                            model_id=subagent_model,
                            tool_names=skill_obj.meta.allowed_tools or None,
                            workspace_dir=self.workspace.path,
                            sandbox_dir=self.sandbox_dir,
                            sandbox_mode=self._sandbox_mode.value,
                            config=self.config,
                        )
                        self._emit_event_sync(
                            event_type="skill.fork.completed",
                            run_id=run_id,
                            context=ctx,
                            payload={"name": run_input.skill, "result_chars": len(fork_result)},
                        )
                        if self._agent.system_message != base_prompt:
                            self._agent.system_message = base_prompt
                        self._prompt_session_id = base_prompt_session
                        return fork_result

                    # ── Skill enforcement: command-dispatch ──────────────
                    # If the skill declares command-dispatch: tool, invoke the
                    # specified tool directly — bypassing the LLM entirely.
                    if (
                        skill_obj
                        and skill_obj.meta.command_dispatch == "tool"
                        and skill_obj.meta.command_tool
                    ):
                        tool_result = self._dispatch_command_tool(
                            tool_name=skill_obj.meta.command_tool,
                            arguments=run_input.message,
                            run_id=run_id,
                            context=ctx,
                        )
                        self._emit_event_sync(
                            event_type="skill.command_dispatch.completed",
                            run_id=run_id,
                            context=ctx,
                            payload={"name": run_input.skill, "tool": skill_obj.meta.command_tool},
                        )
                        if self._agent.system_message != base_prompt:
                            self._agent.system_message = base_prompt
                        self._prompt_session_id = base_prompt_session
                        return tool_result

                    self._set_system_prompt(
                        skill_content=skill_content, session_id=effective_session
                    )
                    scoped_skill_obj = skill_obj
                    self._register_explicit_skill(
                        run_id=run_id,
                        skill_obj=skill_obj,
                        content=skill_content,
                    )

            self._append_learning_prompt(
                self._learning_prompt_context_sync(
                    message=run_input.message,
                    user_id=agno_user,
                    session_id=agno_session,
                    context=ctx,
                )
            )

            prompt = PromptEnvelope(
                system_prompt=self.system_prompt,
                user_message=run_input.message,
                skill=run_input.skill,
            )
            prompt_decision = self._run_policy_sync(
                method_name="before_prompt_send",
                payload=prompt,
                run_input=run_input,
                context=ctx,
            )
            self._enforce_policy_decision(
                decision=prompt_decision,
                checkpoint="before_prompt_send",
                run_id=run_id,
                context=ctx,
            )
            if prompt_decision.action == PolicyAction.ALLOW_WITH_REDACTION:
                prompt.user_message = apply_redactions(
                    prompt.user_message, prompt_decision.redactions
                )
                prompt.system_prompt = apply_redactions(
                    prompt.system_prompt, prompt_decision.redactions
                )
                self._agent.system_message = prompt.system_prompt

            self._emit_event_sync(
                event_type="prompt.built",
                run_id=run_id,
                context=ctx,
                payload={
                    "system_chars": len(prompt.system_prompt),
                    "user_chars": len(prompt.user_message),
                    "skill": run_input.skill,
                },
            )
            self._emit_event_sync(
                event_type="model.request.started",
                run_id=run_id,
                context=ctx,
                payload={"stream": stream, "stream_events": stream_events},
            )

            call_kwargs = dict(kwargs)
            extra_metadata = call_kwargs.pop("metadata", None)
            call_kwargs.pop("run_id", None)
            if output_schema is not None:
                call_kwargs["output_schema"] = output_schema
            if max_turns is not None:
                call_kwargs["max_turns"] = int(max_turns)
            agent_metadata = self._build_agent_run_metadata(
                context=ctx,
                run_input=run_input,
            )
            if isinstance(extra_metadata, dict):
                agent_metadata.update(extra_metadata)
            agent_metadata["_agnoclaw_context"] = self._context_to_metadata(ctx)
            agent_metadata["_agnoclaw_harness_run_id"] = run_id
            agent_metadata.pop("_agnoclaw_context_kind", None)
            if internal_run_kind is not None:
                agent_metadata["_agnoclaw_context_kind"] = internal_run_kind

            # Caller context is scoped to this Agno run without mutating the agent.
            call_kwargs.update(
                self._resolve_run_context_kwargs(
                    dependencies=dependencies,
                    session_state=session_state,
                    add_dependencies_to_context=add_dependencies_to_context,
                    add_session_state_to_context=add_session_state_to_context,
                    knowledge_filters=knowledge_filters,
                )
            )

            # Lazy streams own their tool scope inside the generator; eager runs use this frame.
            scope_allowed, scope_overrides, scope_bindings = self._skill_tool_scope_args(
                scoped_skill_obj, tool_schema_overrides, tool_arg_bindings
            )
            if internal_run_kind == "summary":
                # Transcript text is untrusted data.  A summary model must never
                # turn quoted historical tool instructions into live effects.
                scope_allowed = []
            if not stream:
                tool_scope = self._apply_tool_scope(
                    allowed=scope_allowed,
                    schema_overrides=scope_overrides,
                    arg_bindings=scope_bindings,
                )

            overflow_retry_attempted = False
            while True:
                try:
                    result = cast(Callable[..., Any], self._agent.run)(
                        prompt.user_message,
                        stream=stream,
                        stream_events=stream_events,
                        session_id=agno_session,
                        user_id=agno_user,
                        run_id=run_id,
                        metadata=agent_metadata,
                        **call_kwargs,
                    )
                except Exception as exc:
                    if not is_context_overflow_exception(exc):
                        raise
                    if overflow_retry_attempted:
                        raise context_overflow_error(
                            run_id=run_id,
                            reason="exhausted",
                            source="exception",
                        ) from exc
                    overflow_maintenance = self._begin_context_overflow_recovery_sync(
                        context=ctx,
                        run_lease=run_lease,
                        run_id=run_id,
                        stream=stream,
                        source="exception",
                    )
                else:
                    signal = self._extract_error_signal_from_run_output(result)
                    if not self._status_is_error(result) or not is_context_overflow_signal(signal):
                        break
                    if overflow_retry_attempted:
                        raise context_overflow_error(
                            run_id=run_id,
                            reason="exhausted",
                            source="run_output",
                        )
                    overflow_maintenance = self._begin_context_overflow_recovery_sync(
                        context=ctx,
                        run_lease=run_lease,
                        run_id=run_id,
                        stream=stream,
                        source="run_output",
                    )
                overflow_retry_attempted = True
                self._agent.system_message = prompt.system_prompt
                self._prompt_session_id = effective_session
                self._emit_event_sync(
                    event_type="context.overflow.retrying",
                    run_id=run_id,
                    context=ctx,
                    payload={"attempt": 2},
                )

            if self._agent.system_message != base_prompt:
                self._agent.system_message = base_prompt
            self._prompt_session_id = base_prompt_session

            if stream:

                def _wrapped_stream() -> Iterator[RunOutputEvent]:
                    collected: list[str] = []
                    cumulative = ""
                    stream_error_signal: dict[str, Any] | None = None
                    run_failed_emitted = False
                    stream_scope: _ToolScope | None = None
                    try:
                        # Apply the tool scope here so its lifetime equals the
                        # generator's. Agno resolves tools lazily during iteration,
                        # so this is active before the first model request.
                        stream_scope = self._apply_tool_scope(
                            allowed=scope_allowed,
                            schema_overrides=scope_overrides,
                            arg_bindings=scope_bindings,
                        )
                        for event in result:
                            source_event = self._event_name(event) or None
                            stream_summary = self._stream_event_summary(event)
                            stream_details = self._stream_event_details(event)
                            if source_event:
                                self._emit_event_sync(
                                    event_type="agno.event",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={
                                        "source_event": source_event,
                                        **stream_summary,
                                        "details": stream_details,
                                    },
                                )

                            mapped_event = self._map_agno_event_type(event)
                            if mapped_event and mapped_event not in _TOOL_LIFECYCLE_EVENT_TYPES:
                                self._emit_event_sync(
                                    event_type=mapped_event,
                                    run_id=run_id,
                                    context=ctx,
                                    payload={
                                        "source_event": source_event,
                                        **stream_summary,
                                        "details": stream_details,
                                    },
                                )

                            thinking = self._extract_thinking_content(event)
                            if thinking:
                                self._emit_event_sync(
                                    event_type="thinking",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={
                                        "content": thinking,
                                        "phase": self._thinking_phase(event),
                                        "source_event": self._event_name(event),
                                    },
                                )

                            error_signal = self._extract_error_signal_from_stream_event(event)
                            if error_signal is not None and stream_error_signal is None:
                                stream_error_signal = error_signal

                            text = self._extract_event_content(event)
                            if text:
                                collected.append(text)
                                cumulative += text
                                self._emit_event_sync(
                                    event_type="run.content",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={"chars": len(text)},
                                )
                                self._emit_event_sync(
                                    event_type="response_chunk",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={
                                        "content": text,
                                        "cumulative": cumulative,
                                        "is_final": False,
                                    },
                                )
                            yield event
                        self._emit_event_sync(
                            event_type="response_chunk",
                            run_id=run_id,
                            context=ctx,
                            payload={
                                "content": "",
                                "cumulative": cumulative,
                                "is_final": True,
                            },
                        )
                        post_result = RunResultEnvelope(
                            run_id=run_id,
                            content="".join(collected),
                            raw_output=None,
                            metadata=dict(run_input.metadata),
                        )
                        post_result = self._run_post_hooks_sync(
                            run_input=run_input,
                            result=post_result,
                            context=ctx,
                        )
                        output_text = (
                            str(post_result.content) if post_result.content is not None else ""
                        )
                        if stream_error_signal is not None:
                            error_message = (
                                self._normalize_error_message(stream_error_signal.get("message"))
                                or "Model invocation failed."
                            )
                            error_message = self._truncate_text(
                                error_message, limit=_ERROR_MESSAGE_LIMIT
                            )
                            code = stream_error_signal.get("error_id") or stream_error_signal.get(
                                "error_type"
                            )
                            self._emit_event_sync(
                                event_type="model.request.failed",
                                run_id=run_id,
                                context=ctx,
                                payload={
                                    "error": error_message,
                                    "code": code,
                                    "output_chars": len(output_text),
                                },
                            )
                            self._emit_event_sync(
                                event_type="run.failed",
                                run_id=run_id,
                                context=ctx,
                                payload={"error": error_message, "code": code},
                            )
                            run_failed_emitted = True
                            self._raise_stream_error_signal(stream_error_signal, run_id=run_id)
                        self._emit_event_sync(
                            event_type="model.request.completed",
                            run_id=run_id,
                            context=ctx,
                            payload={"output_chars": len(output_text)},
                        )
                        self._emit_event_sync(
                            event_type="run.completed",
                            run_id=run_id,
                            context=ctx,
                            payload={"output_chars": len(output_text)},
                        )
                    except Exception as exc:
                        overflow_error = (
                            context_overflow_error(
                                run_id=run_id,
                                reason="stream",
                                source="exception",
                            )
                            if is_context_overflow_exception(exc)
                            else None
                        )
                        harness_error = overflow_error or self._extract_harness_error(exc)
                        error_code = harness_error.code if harness_error is not None else None
                        if not run_failed_emitted:
                            self._emit_event_sync(
                                event_type="run.failed",
                                run_id=run_id,
                                context=ctx,
                                payload={"error": str(exc), "code": error_code},
                            )
                        if harness_error is not None:
                            raise harness_error from exc
                        raise
                    finally:
                        self._restore_tool_scope(stream_scope)
                        self._cleanup_tool_step_state(run_id)
                        run_lease.release()

                stream_cleanup_deferred = True
                return _LeasedIterator(_wrapped_stream(), run_lease)

            post_result = RunResultEnvelope(
                run_id=run_id,
                content=getattr(result, "content", result),
                raw_output=result,
                metadata=dict(run_input.metadata),
            )
            post_result = self._run_post_hooks_sync(
                run_input=run_input,
                result=post_result,
                context=ctx,
            )
            output_text = str(post_result.content) if post_result.content is not None else ""
            resolved_output = (
                post_result.raw_output if post_result.raw_output is not None else result
            )
            signal = self._extract_error_signal_from_run_output(resolved_output)
            if self._status_is_error(resolved_output):
                self._raise_if_fatal_error_signal(signal)
                error_message = (
                    self._normalize_error_message(signal.get("message"))
                    or "Model invocation failed."
                )
                error_message = self._truncate_text(error_message, limit=_ERROR_MESSAGE_LIMIT)
                code = signal.get("error_id") or signal.get("error_type")
                self._emit_event_sync(
                    event_type="model.request.failed",
                    run_id=run_id,
                    context=ctx,
                    payload={
                        "error": error_message,
                        "code": code,
                        "output_chars": len(output_text),
                    },
                )
                self._emit_event_sync(
                    event_type="run.failed",
                    run_id=run_id,
                    context=ctx,
                    payload={"error": error_message, "code": code},
                )
                return resolved_output

            self._emit_event_sync(
                event_type="model.request.completed",
                run_id=run_id,
                context=ctx,
                payload={"output_chars": len(output_text)},
            )
            self._emit_event_sync(
                event_type="run.completed",
                run_id=run_id,
                context=ctx,
                payload={"output_chars": len(output_text)},
            )
            return resolved_output
        except Exception as exc:
            if self._agent.system_message != base_prompt:
                self._agent.system_message = base_prompt
            self._prompt_session_id = base_prompt_session
            harness_error = self._extract_harness_error(exc)
            error_code = harness_error.code if harness_error is not None else None
            self._emit_event_sync(
                event_type="run.failed",
                run_id=run_id,
                context=ctx,
                payload={"error": str(exc), "code": error_code},
            )
            if harness_error is not None:
                raise harness_error from exc
            raise HarnessError(
                code="MODEL_RUN_FAILED",
                category="model",
                message=str(exc),
                retryable=True,
            ) from exc
        finally:
            if not stream_cleanup_deferred:
                try:
                    self._restore_tool_scope(tool_scope)
                    self._cleanup_tool_step_state(run_id)
                finally:
                    try:
                        self._deactivate_run_agent(run_agent_tokens)
                    finally:
                        try:
                            if run_model_resource is not None:
                                run_model_resource.close()
                            if run_tool_bundle is not None:
                                run_tool_bundle.close()
                        finally:
                            if overflow_maintenance is not None:
                                overflow_maintenance.release()
                            run_lease.release()

    async def arun(
        self,
        message: str,
        *,
        stream: bool = False,
        stream_events: bool = False,
        max_turns: int | None = None,
        skill: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        context: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
        learning_consent: bool = False,
        output_schema: type | None = None,
        tool_schema_overrides: dict[str, dict[str, Any]] | None = None,
        tool_arg_bindings: dict[str, dict[str, Any]] | None = None,
        dependencies: dict[str, Any] | None = None,
        session_state: dict[str, Any] | None = None,
        add_dependencies_to_context: bool | None = None,
        add_session_state_to_context: bool | None = None,
        knowledge_filters: dict[str, Any] | list[Any] | None = None,
        **kwargs,
    ) -> RunOutput | AsyncIterator[RunOutputEvent]:
        """Async version of run().

        Accepts the same per-run ``tool_arg_bindings`` / ``dependencies`` /
        ``session_state`` / ``add_dependencies_to_context`` /
        ``add_session_state_to_context`` / ``knowledge_filters`` kwargs as
        :meth:`run`, with identical merge-over-construction-defaults and per-run
        scoping semantics (including the streaming path).
        """
        if self._route_convenience_call_through_lifecycle():
            options = self._convenience_lifecycle_options(
                stream_events=stream_events,
                max_turns=max_turns,
                skill=skill,
                output_schema=output_schema,
                tool_schema_overrides=tool_schema_overrides,
                tool_arg_bindings=tool_arg_bindings,
                dependencies=dependencies,
                session_state=session_state,
                add_dependencies_to_context=add_dependencies_to_context,
                add_session_state_to_context=add_session_state_to_context,
                knowledge_filters=knowledge_filters,
                kwargs=kwargs,
            )
            return cast(
                RunOutput | AsyncIterator[RunOutputEvent],
                await self._arun_via_lifecycle(
                    message,
                    stream=stream,
                    session_id=session_id,
                    user_id=user_id,
                    context=context,
                    metadata=metadata,
                    learning_consent=learning_consent,
                    options=options,
                ),
            )
        run_id = self._active_runtime_run_id.get() or f"run_{uuid4().hex}"
        internal_run_kind = self._internal_run_kind.get()
        effective_user, effective_session, ctx = self._resolve_run_identity(
            context=context,
            user_id=user_id,
            session_id=session_id,
            metadata=metadata,
        )
        await self._prepare_context_for_run_async(ctx)
        learning_scope = self._resolve_learning_scope(
            ctx,
            consented=learning_consent,
        )
        agno_user = learning_scope.storage_user_id if learning_scope is not None else effective_user
        agno_session = (
            learning_scope.storage_session_id if learning_scope is not None else effective_session
        )
        run_input = RunInput(
            run_id=run_id,
            message=message,
            skill=skill,
            stream=stream,
            stream_events=stream_events,
            metadata=dict(metadata or {}),
        )
        if max_turns is not None:
            run_input.metadata.setdefault("max_turns", int(max_turns))

        materialized_run = self._can_materialize_run_agent(
            stream=stream,
            skill=skill,
        )
        isolated_run = self._can_materialize_isolated_run(
            stream=stream,
            skill=skill,
        )
        run_lease = self._acquire_run_gate(
            isolated=isolated_run,
            session_id=effective_session,
            user_id=effective_user,
            tenant_id=ctx.tenant_id,
        )
        run_agent_tokens: tuple[Any, Any] | None = None
        run_tool_bundle: BuiltinToolBundle | None = None
        run_model_resource: OwnedAgnoModelResource | None = None
        try:
            if materialized_run or learning_scope is not None:
                run_agent, run_tool_bundle = materialize_run_agent(
                    self,
                    session_id=effective_session,
                    user_id=effective_user,
                    learning_scope=learning_scope,
                )
                owned_model = getattr(run_agent, "model", None)
                if self._owns_model_transport and owned_model is not None:
                    prepared = self._prepared_run_model.get()
                    run_model_resource = (
                        prepared[1]
                        if prepared is not None and prepared[0] is owned_model
                        else OwnedAgnoModelResource(owned_model)
                    )
                run_agent_tokens = self._activate_run_agent(
                    run_agent,
                    session_id=effective_session,
                )
            base_prompt = self._agent.system_message
            base_prompt_session = self._prompt_session_id
        except BaseException:
            self._deactivate_run_agent(run_agent_tokens)
            if run_model_resource is not None:
                await run_model_resource.aclose()
            if run_tool_bundle is not None:
                await run_tool_bundle.aclose()
            run_lease.release()
            raise
        stream_cleanup_deferred = False
        tool_scope: _ToolScope | None = None
        scoped_skill_obj: Any = None
        overflow_maintenance: Any = None

        try:
            # Capture the loop that owns this async run so sync-tool lifecycle
            # hooks can route consumer coroutines back to the correct loop.
            if not isolated_run:
                self._owning_loop = asyncio.get_running_loop()

            await self._run_lifecycle_hooks_async(
                "message.received",
                context=ctx,
                run_id=run_id,
                metadata={
                    "message": message,
                    "skill": skill,
                    "stream": stream,
                    "stream_events": stream_events,
                },
            )
            # Runtime-block session scoping mirrors the system_message
            # save/restore: set for this run, restored wherever base_prompt is.
            self._prompt_session_id = effective_session

            await self._emit_event_async(
                event_type="run.started",
                run_id=run_id,
                context=ctx,
                payload={
                    "stream": stream,
                    "stream_events": stream_events,
                    "skill": skill,
                    "max_turns": max_turns,
                    "learning_scope": (
                        learning_scope.descriptor() if learning_scope is not None else None
                    ),
                },
            )
            scheduler_payload = self._scheduler_invocation_payload(run_input.metadata)
            if scheduler_payload:
                await self._emit_event_async(
                    event_type="scheduler.invocation",
                    run_id=run_id,
                    context=ctx,
                    payload=scheduler_payload,
                )

            run_input = await self._run_pre_hooks_async(run_input=run_input, context=ctx)

            before_run_decision = await self._run_policy_async(
                method_name="before_run",
                payload=run_input,
                run_input=run_input,
                context=ctx,
            )
            await self._enforce_policy_decision_async(
                decision=before_run_decision,
                checkpoint="before_run",
                run_id=run_id,
                context=ctx,
            )
            if before_run_decision.action == PolicyAction.ALLOW_WITH_REDACTION:
                run_input.message = apply_redactions(
                    run_input.message, before_run_decision.redactions
                )

            skill_content: str | None = None
            if run_input.skill:
                await self._emit_event_async(
                    event_type="skill.load.started",
                    run_id=run_id,
                    context=ctx,
                    payload={"name": run_input.skill},
                )
                skill_decision = await self._run_policy_async(
                    method_name="before_skill_load",
                    payload=SkillLoadRequest(name=run_input.skill),
                    run_input=run_input,
                    context=ctx,
                )
                await self._enforce_policy_decision_async(
                    decision=skill_decision,
                    checkpoint="before_skill_load",
                    run_id=run_id,
                    context=ctx,
                )
                skill_content = self.skills.load_skill(run_input.skill)
                await self._emit_event_async(
                    event_type="skill.load.completed",
                    run_id=run_id,
                    context=ctx,
                    payload={"name": run_input.skill, "loaded": bool(skill_content)},
                )
                if skill_content:
                    self._set_system_prompt(
                        skill_content=skill_content, session_id=effective_session
                    )
                    scoped_skill_obj = self.skills._get_skill(run_input.skill)
                    if scoped_skill_obj is not None:
                        self._register_explicit_skill(
                            run_id=run_id,
                            skill_obj=scoped_skill_obj,
                            content=skill_content,
                        )

            self._append_learning_prompt(
                await self._learning_prompt_context_async(
                    message=run_input.message,
                    user_id=agno_user,
                    session_id=agno_session,
                    context=ctx,
                )
            )

            prompt = PromptEnvelope(
                system_prompt=self.system_prompt,
                user_message=run_input.message,
                skill=run_input.skill,
            )
            prompt_decision = await self._run_policy_async(
                method_name="before_prompt_send",
                payload=prompt,
                run_input=run_input,
                context=ctx,
            )
            await self._enforce_policy_decision_async(
                decision=prompt_decision,
                checkpoint="before_prompt_send",
                run_id=run_id,
                context=ctx,
            )
            if prompt_decision.action == PolicyAction.ALLOW_WITH_REDACTION:
                prompt.user_message = apply_redactions(
                    prompt.user_message, prompt_decision.redactions
                )
                prompt.system_prompt = apply_redactions(
                    prompt.system_prompt, prompt_decision.redactions
                )
                self._agent.system_message = prompt.system_prompt

            await self._emit_event_async(
                event_type="prompt.built",
                run_id=run_id,
                context=ctx,
                payload={
                    "system_chars": len(prompt.system_prompt),
                    "user_chars": len(prompt.user_message),
                    "skill": run_input.skill,
                },
            )
            await self._emit_event_async(
                event_type="model.request.started",
                run_id=run_id,
                context=ctx,
                payload={"stream": stream, "stream_events": stream_events},
            )

            call_kwargs = dict(kwargs)
            extra_metadata = call_kwargs.pop("metadata", None)
            call_kwargs.pop("run_id", None)
            if output_schema is not None:
                call_kwargs["output_schema"] = output_schema
            if max_turns is not None:
                call_kwargs["max_turns"] = int(max_turns)
            agent_metadata = self._build_agent_run_metadata(
                context=ctx,
                run_input=run_input,
            )
            if isinstance(extra_metadata, dict):
                agent_metadata.update(extra_metadata)
            agent_metadata["_agnoclaw_context"] = self._context_to_metadata(ctx)
            agent_metadata["_agnoclaw_harness_run_id"] = run_id
            agent_metadata.pop("_agnoclaw_context_kind", None)
            if internal_run_kind is not None:
                agent_metadata["_agnoclaw_context_kind"] = internal_run_kind

            # Caller context is scoped to this Agno run without mutating the agent.
            call_kwargs.update(
                self._resolve_run_context_kwargs(
                    dependencies=dependencies,
                    session_state=session_state,
                    add_dependencies_to_context=add_dependencies_to_context,
                    add_session_state_to_context=add_session_state_to_context,
                    knowledge_filters=knowledge_filters,
                )
            )

            # Lazy streams own their tool scope inside the generator; eager runs use this frame.
            scope_allowed, scope_overrides, scope_bindings = self._skill_tool_scope_args(
                scoped_skill_obj, tool_schema_overrides, tool_arg_bindings
            )
            if internal_run_kind == "summary":
                # Keep internal transcript synthesis effect-free even when the
                # enclosing harness exposes effectful tools.
                scope_allowed = []

            continue_from_tool_checkpoint = False
            if self._active_runtime_checkpoint_resume.get():
                checkpointed_run = await cast(Callable[..., Any], self._agent.aget_run_output)(
                    run_id,
                    session_id=agno_session,
                    user_id=agno_user,
                )
                continue_from_tool_checkpoint = has_valid_tool_batch_checkpoint(
                    checkpointed_run
                )
                if continue_from_tool_checkpoint:
                    # The checkpoint already contains the exact history used by
                    # the interrupted run. Agno 2.9 otherwise re-fetches that
                    # same RUNNING run from its session and duplicates the
                    # user/assistant/tool suffix before continuation.
                    self._agent.add_history_to_context = False
                await self._emit_event_async(
                    event_type="run.checkpoint.resume.selected",
                    run_id=run_id,
                    context=ctx,
                    payload={"tool_batch_checkpoint": continue_from_tool_checkpoint},
                )

            overflow_retry_attempted = False
            while True:
                try:
                    if continue_from_tool_checkpoint:
                        agno_call = cast(Callable[..., Any], self._agent.acontinue_run)(
                            run_id=run_id,
                            stream=stream,
                            stream_events=stream_events,
                            session_id=agno_session,
                            user_id=agno_user,
                            metadata=agent_metadata,
                            **call_kwargs,
                        )
                    else:
                        agno_call = cast(Callable[..., Any], self._agent.arun)(
                            prompt.user_message,
                            stream=stream,
                            stream_events=stream_events,
                            session_id=agno_session,
                            user_id=agno_user,
                            run_id=run_id,
                            metadata=agent_metadata,
                            **call_kwargs,
                        )
                    if hasattr(agno_call, "__anext__") or hasattr(agno_call, "__aiter__"):
                        result = agno_call
                    else:
                        if tool_scope is None:
                            tool_scope = self._apply_tool_scope(
                                allowed=scope_allowed,
                                schema_overrides=scope_overrides,
                                arg_bindings=scope_bindings,
                            )
                        result = await agno_call
                except Exception as exc:
                    if not is_context_overflow_exception(exc):
                        raise
                    if overflow_retry_attempted:
                        raise context_overflow_error(
                            run_id=run_id,
                            reason="exhausted",
                            source="exception",
                        ) from exc
                    overflow_maintenance = await self._begin_context_overflow_recovery_async(
                        context=ctx,
                        run_lease=run_lease,
                        run_id=run_id,
                        stream=stream,
                        source="exception",
                    )
                else:
                    signal = self._extract_error_signal_from_run_output(result)
                    if not self._status_is_error(result) or not is_context_overflow_signal(signal):
                        break
                    if overflow_retry_attempted:
                        raise context_overflow_error(
                            run_id=run_id,
                            reason="exhausted",
                            source="run_output",
                        )
                    overflow_maintenance = await self._begin_context_overflow_recovery_async(
                        context=ctx,
                        run_lease=run_lease,
                        run_id=run_id,
                        stream=stream,
                        source="run_output",
                    )
                overflow_retry_attempted = True
                self._agent.system_message = prompt.system_prompt
                self._prompt_session_id = effective_session
                await self._emit_event_async(
                    event_type="context.overflow.retrying",
                    run_id=run_id,
                    context=ctx,
                    payload={"attempt": 2},
                )

            if self._agent.system_message != base_prompt:
                self._agent.system_message = base_prompt
            self._prompt_session_id = base_prompt_session

            if stream and hasattr(result, "__aiter__"):

                async def _wrapped_stream() -> AsyncIterator[RunOutputEvent]:
                    collected: list[str] = []
                    cumulative = ""
                    stream_error_signal: dict[str, Any] | None = None
                    run_failed_emitted = False
                    stream_scope: _ToolScope | None = None
                    try:
                        # Apply the tool scope here so its lifetime equals the
                        # generator's. Agno resolves tools lazily during iteration,
                        # so this is active before the first model request.
                        stream_scope = self._apply_tool_scope(
                            allowed=scope_allowed,
                            schema_overrides=scope_overrides,
                            arg_bindings=scope_bindings,
                        )
                        async for event in result:
                            source_event = self._event_name(event) or None
                            stream_summary = self._stream_event_summary(event)
                            stream_details = self._stream_event_details(event)
                            if source_event:
                                await self._emit_event_async(
                                    event_type="agno.event",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={
                                        "source_event": source_event,
                                        **stream_summary,
                                        "details": stream_details,
                                    },
                                )

                            mapped_event = self._map_agno_event_type(event)
                            if mapped_event and mapped_event not in _TOOL_LIFECYCLE_EVENT_TYPES:
                                await self._emit_event_async(
                                    event_type=mapped_event,
                                    run_id=run_id,
                                    context=ctx,
                                    payload={
                                        "source_event": source_event,
                                        **stream_summary,
                                        "details": stream_details,
                                    },
                                )

                            thinking = self._extract_thinking_content(event)
                            if thinking:
                                await self._emit_event_async(
                                    event_type="thinking",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={
                                        "content": thinking,
                                        "phase": self._thinking_phase(event),
                                        "source_event": self._event_name(event),
                                    },
                                )

                            error_signal = self._extract_error_signal_from_stream_event(event)
                            if error_signal is not None and stream_error_signal is None:
                                stream_error_signal = error_signal

                            text = self._extract_event_content(event)
                            if text:
                                collected.append(text)
                                cumulative += text
                                await self._emit_event_async(
                                    event_type="run.content",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={"chars": len(text)},
                                )
                                await self._emit_event_async(
                                    event_type="response_chunk",
                                    run_id=run_id,
                                    context=ctx,
                                    payload={
                                        "content": text,
                                        "cumulative": cumulative,
                                        "is_final": False,
                                    },
                                )
                            yield event
                        await self._emit_event_async(
                            event_type="response_chunk",
                            run_id=run_id,
                            context=ctx,
                            payload={
                                "content": "",
                                "cumulative": cumulative,
                                "is_final": True,
                            },
                        )
                        post_result = RunResultEnvelope(
                            run_id=run_id,
                            content="".join(collected),
                            raw_output=None,
                            metadata=dict(run_input.metadata),
                        )
                        post_result = await self._run_post_hooks_async(
                            run_input=run_input,
                            result=post_result,
                            context=ctx,
                        )
                        output_text = (
                            str(post_result.content) if post_result.content is not None else ""
                        )
                        if stream_error_signal is not None:
                            error_message = (
                                self._normalize_error_message(stream_error_signal.get("message"))
                                or "Model invocation failed."
                            )
                            error_message = self._truncate_text(
                                error_message, limit=_ERROR_MESSAGE_LIMIT
                            )
                            code = stream_error_signal.get("error_id") or stream_error_signal.get(
                                "error_type"
                            )
                            await self._emit_event_async(
                                event_type="model.request.failed",
                                run_id=run_id,
                                context=ctx,
                                payload={
                                    "error": error_message,
                                    "code": code,
                                    "output_chars": len(output_text),
                                },
                            )
                            await self._emit_event_async(
                                event_type="run.failed",
                                run_id=run_id,
                                context=ctx,
                                payload={"error": error_message, "code": code},
                            )
                            run_failed_emitted = True
                            self._raise_stream_error_signal(stream_error_signal, run_id=run_id)
                        await self._emit_event_async(
                            event_type="model.request.completed",
                            run_id=run_id,
                            context=ctx,
                            payload={"output_chars": len(output_text)},
                        )
                        await self._emit_event_async(
                            event_type="run.completed",
                            run_id=run_id,
                            context=ctx,
                            payload={"output_chars": len(output_text)},
                        )
                    except Exception as exc:
                        overflow_error = (
                            context_overflow_error(
                                run_id=run_id,
                                reason="stream",
                                source="exception",
                            )
                            if is_context_overflow_exception(exc)
                            else None
                        )
                        harness_error = overflow_error or self._extract_harness_error(exc)
                        error_code = harness_error.code if harness_error is not None else None
                        if not run_failed_emitted:
                            await self._emit_event_async(
                                event_type="run.failed",
                                run_id=run_id,
                                context=ctx,
                                payload={"error": str(exc), "code": error_code},
                            )
                        if harness_error is not None:
                            raise harness_error from exc
                        raise
                    finally:
                        self._restore_tool_scope(stream_scope)
                        self._cleanup_tool_step_state(run_id)
                        run_lease.release()

                stream_cleanup_deferred = True
                return _LeasedAsyncIterator(_wrapped_stream(), run_lease)

            post_result = RunResultEnvelope(
                run_id=run_id,
                content=getattr(result, "content", result),
                raw_output=result,
                metadata=dict(run_input.metadata),
            )
            post_result = await self._run_post_hooks_async(
                run_input=run_input,
                result=post_result,
                context=ctx,
            )
            output_text = str(post_result.content) if post_result.content is not None else ""
            resolved_output = (
                post_result.raw_output if post_result.raw_output is not None else result
            )
            signal = self._extract_error_signal_from_run_output(resolved_output)
            if self._status_is_error(resolved_output):
                self._raise_if_fatal_error_signal(signal)
                error_message = (
                    self._normalize_error_message(signal.get("message"))
                    or "Model invocation failed."
                )
                error_message = self._truncate_text(error_message, limit=_ERROR_MESSAGE_LIMIT)
                code = signal.get("error_id") or signal.get("error_type")
                await self._emit_event_async(
                    event_type="model.request.failed",
                    run_id=run_id,
                    context=ctx,
                    payload={
                        "error": error_message,
                        "code": code,
                        "output_chars": len(output_text),
                    },
                )
                await self._emit_event_async(
                    event_type="run.failed",
                    run_id=run_id,
                    context=ctx,
                    payload={"error": error_message, "code": code},
                )
                return resolved_output

            await self._emit_event_async(
                event_type="model.request.completed",
                run_id=run_id,
                context=ctx,
                payload={"output_chars": len(output_text)},
            )
            await self._emit_event_async(
                event_type="run.completed",
                run_id=run_id,
                context=ctx,
                payload={"output_chars": len(output_text)},
            )
            return resolved_output
        except Exception as exc:
            if self._agent.system_message != base_prompt:
                self._agent.system_message = base_prompt
            self._prompt_session_id = base_prompt_session
            harness_error = self._extract_harness_error(exc)
            error_code = harness_error.code if harness_error is not None else None
            await self._emit_event_async(
                event_type="run.failed",
                run_id=run_id,
                context=ctx,
                payload={"error": str(exc), "code": error_code},
            )
            if harness_error is not None:
                raise harness_error from exc
            raise HarnessError(
                code="MODEL_RUN_FAILED",
                category="model",
                message=str(exc),
                retryable=True,
            ) from exc
        finally:
            if not stream_cleanup_deferred:
                try:
                    self._restore_tool_scope(tool_scope)
                    self._cleanup_tool_step_state(run_id)
                finally:
                    try:
                        self._deactivate_run_agent(run_agent_tokens)
                    finally:
                        try:
                            if run_model_resource is not None:
                                await run_model_resource.aclose()
                            if run_tool_bundle is not None:
                                await run_tool_bundle.aclose()
                        finally:
                            if overflow_maintenance is not None:
                                overflow_maintenance.release()
                            run_lease.release()

    def print_response(
        self, message: str, *, stream: bool = True, skill: str | None = None, **kwargs
    ) -> None:
        """
        Run the agent through the full runtime pipeline and pretty-print the response.

        Unlike calling self._agent.print_response() directly, this routes through
        run() so that hooks, policy, events, permissions, and guardrails are enforced.
        """
        if stream:
            # Stream through run() pipeline → print each chunk
            response = cast(
                Iterator[RunOutputEvent],
                self.run(message, stream=True, skill=skill, **kwargs),
            )
            for event in response:
                content = self._extract_event_content(event)
                if content:
                    print(content, end="", flush=True)
            print()  # final newline
        else:
            result = self.run(message, stream=False, skill=skill, **kwargs)
            result_content = result.content if isinstance(result, RunOutput) else str(result)
            if result_content:
                print(result_content)

    def enter_plan_mode(self) -> None:
        """
        Activate plan mode: injects plan mode instructions into the system prompt.

        In plan mode the agent is instructed to:
        - Only read/search — no writes, edits, or shell commands
        - Write a .plan.md file with the implementation plan
        - Wait for user approval before implementing

        Use exit_plan_mode() to return to normal operation.
        """
        self._plan_mode = True
        current = self._permission_controller.current_mode()
        if current != PermissionMode.PLAN:
            self._plan_mode_restore_permission_mode = current
            self._permission_controller.set_mode(PermissionMode.PLAN)
        self._set_system_prompt(session_id=self.session_id)

    def exit_plan_mode(self) -> None:
        """Deactivate plan mode: restores normal system prompt."""
        self._plan_mode = False
        restore = self._plan_mode_restore_permission_mode
        if restore is not None:
            self._permission_controller.set_mode(restore)
        else:
            self._permission_controller.set_mode(
                normalize_permission_mode(self.config.permission_mode)
            )
        self._plan_mode_restore_permission_mode = None
        self._set_system_prompt(session_id=self.session_id)

    def ask_user_question(
        self,
        question: str,
        *,
        options: list[str] | tuple[str, ...] | str | None = None,
        allow_freeform: bool = True,
        context: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PlanQuestionSignal:
        """Record and emit a structured planning question signal."""
        signal = self._plan_signal_toolkit.record_question(
            question,
            options=options,
            allow_freeform=allow_freeform,
            metadata=metadata,
        )
        ctx = (
            context.with_metadata(metadata)
            if context
            else self._build_execution_context(
                user_id=self.user_id,
                session_id=self.session_id,
                metadata=metadata,
            )
        )
        self._emit_event_sync(
            event_type="plan.question.requested",
            run_id=signal.signal_id,
            context=ctx,
            payload={
                "signal_id": signal.signal_id,
                "question": signal.question,
                "options": list(signal.options),
                "allow_freeform": signal.allow_freeform,
                "metadata": dict(signal.metadata),
            },
        )
        return signal

    def signal_plan_completion(
        self,
        summary: str,
        *,
        plan_path: str | None = None,
        ready_for_approval: bool = True,
        context: ExecutionContext | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PlanExitSignal:
        """Record a structured plan-complete signal and exit plan mode."""
        signal = self._plan_signal_toolkit.record_exit(
            summary,
            plan_path=plan_path,
            ready_for_approval=ready_for_approval,
            metadata=metadata,
        )
        self.exit_plan_mode()
        ctx = (
            context.with_metadata(metadata)
            if context
            else self._build_execution_context(
                user_id=self.user_id,
                session_id=self.session_id,
                metadata=metadata,
            )
        )
        self._emit_event_sync(
            event_type="plan.completed",
            run_id=signal.signal_id,
            context=ctx,
            payload={
                "signal_id": signal.signal_id,
                "summary": signal.summary,
                "plan_path": signal.plan_path,
                "ready_for_approval": signal.ready_for_approval,
                "metadata": dict(signal.metadata),
            },
        )
        return signal

    def plan_signals(self) -> list[PlanQuestionSignal | PlanExitSignal]:
        """Return captured plan question/completion signals."""
        return list(self._plan_signal_toolkit.signals)

    def clear_plan_signals(self) -> None:
        """Clear captured plan signals."""
        self._plan_signal_toolkit.clear()

    def add_tool(self, tool) -> None:
        """Add a tool or toolkit to the agent."""
        self._isolated_agent_factory_enabled = False
        self._attach_tool_runtime_hooks([tool])
        self._agent.add_tool(tool)

    def get_chat_history(self) -> list:
        """Return the chat history for the current session."""
        active_session = self.session_id or getattr(self._agent, "session_id", "")
        return self._agent.get_chat_history(active_session or "")

    def session(
        self,
        *,
        user_id: str | None = None,
        workspace_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        context: ExecutionContext | None = None,
    ) -> HarnessSession:
        """Return an SDK session bound to explicit values or one trusted context."""
        if context is not None:
            comparisons = {
                "user_id": (user_id, context.user_id),
                "workspace_id": (workspace_id, context.workspace_id),
                "session_id": (session_id, context.session_id),
            }
            for field_name, (requested, trusted) in comparisons.items():
                if requested is not None and requested != trusted:
                    raise HarnessError(
                        code="SESSION_SCOPE_CONFLICT",
                        category="identity",
                        message=f"Requested {field_name} conflicts with the trusted context.",
                        retryable=False,
                        details={"field": field_name},
                    )
            resolved_context = context.with_metadata(metadata) if metadata else context
        else:
            resolved_context = self._build_execution_context(
                user_id=user_id or self.user_id,
                session_id=session_id or self.session_id,
                workspace_id=workspace_id or str(self.workspace.path),
                metadata=metadata,
            )
        self._run_lifecycle_hooks_sync(
            "session.created",
            context=resolved_context,
            metadata={
                "session_id": resolved_context.session_id,
                "workspace_id": resolved_context.workspace_id,
            },
        )
        return HarnessSession(
            self,
            user_id=user_id,
            workspace_id=workspace_id,
            session_id=session_id,
            metadata=metadata,
            context=resolved_context,
        )

    def as_agentos_agent(self, *, agent_id: str | None = None, name: str | None = None):
        """Return an AgentOS-compatible adapter that preserves harness runtime controls."""
        from .runtime.agentos import as_agentos_agent

        return as_agentos_agent(self, agent_id=agent_id, name=name)

    def get_session_messages(self, session_id: str | None = None) -> list:
        """
        Return chat history for a specific session ID.

        Falls back to the currently active session when not provided.
        """
        target_session = session_id or self.session_id or getattr(self._agent, "session_id", "")
        if not target_session:
            return []
        messages = self._agent.get_chat_history(target_session)
        return list(messages or [])

    @staticmethod
    def _normalize_session_record(record: Any) -> dict[str, Any]:
        if isinstance(record, dict):
            data = dict(record)
        else:
            data = {}

        keys = (
            "session_id",
            "id",
            "user_id",
            "tenant_id",
            "created_at",
            "updated_at",
            "summary",
            "run_count",
            "title",
        )
        for key in keys:
            if key in data:
                continue
            value = getattr(record, key, None)
            if value is not None:
                data[key] = value

        if "session_id" not in data and "id" in data and data["id"] is not None:
            data["session_id"] = str(data["id"])
        for container_key in ("metadata", "session_data"):
            container = data.get(container_key)
            if not isinstance(container, dict):
                continue
            scoped = container.get("_agnoclaw_context", container)
            if not isinstance(scoped, dict):
                continue
            for identity_key in ("user_id", "tenant_id"):
                if data.get(identity_key) is None and scoped.get(identity_key) is not None:
                    data[identity_key] = scoped[identity_key]
        return data

    @property
    def dependencies(self) -> dict[str, Any]:
        """A copy of the harness's construction-time dependency default.

        Per-run ``dependencies`` passed to :meth:`run`/:meth:`arun` are merged
        over this mapping. To read the *active run's* dependencies from inside a
        tool dispatch, use :func:`get_current_dependencies` instead.
        """
        return dict(self._dependencies)

    def update_dependencies(self, dependencies: dict[str, Any]) -> dict[str, Any]:
        """Update the harness's default dependency mapping at runtime.

        Merges ``dependencies`` over the existing default and propagates the
        result to the underlying Agno agent so subsequent runs (without a per-run
        override) see it. Returns a copy of the new default.
        """
        self._dependencies.update(dependencies)
        self._agent.dependencies = dict(self._dependencies) or None
        return dict(self._dependencies)

    def get_session_state(self, *, session_id: str | None = None) -> dict[str, Any]:
        """Return Agno's persisted session_state for a session.

        Proxies ``Agent.get_session_state``. Defaults to the harness's active
        session when ``session_id`` is omitted.
        """
        return self._agent.get_session_state(session_id=session_id or self._active_session_id(None))

    def update_session_state(
        self,
        session_state_updates: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> str:
        """Merge updates into Agno's persisted session_state for a session.

        Proxies ``Agent.update_session_state``. Defaults to the harness's active
        session when ``session_id`` is omitted.
        """
        return self._agent.update_session_state(
            session_state_updates,
            session_id=session_id or self._active_session_id(None),
        )

    async def aget_session_state(self, *, session_id: str | None = None) -> dict[str, Any]:
        """Async variant of :meth:`get_session_state`."""
        return await self._agent.aget_session_state(
            session_id=session_id or self._active_session_id(None)
        )

    async def aupdate_session_state(
        self,
        session_state_updates: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> str:
        """Async variant of :meth:`update_session_state`."""
        return await self._agent.aupdate_session_state(
            session_state_updates,
            session_id=session_id or self._active_session_id(None),
        )

    def list_sessions(
        self,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        context: ExecutionContext | None = None,
        limit: int | None = 50,
    ) -> list[dict[str, Any]]:
        """
        List known sessions from the configured storage backend.

        This is a best-effort adapter over storage backends with different
        method names/signatures.
        """
        db = getattr(self._agent, "db", None)
        if db is None:
            return []
        method_names = (
            "list_sessions",
            "get_sessions",
            "get_all_sessions",
        )
        if not any(callable(getattr(db, name, None)) for name in method_names):
            return []

        if context is not None:
            if user_id is not None and context.user_id != user_id:
                raise HarnessError(
                    code="SESSION_SCOPE_CONFLICT",
                    category="identity",
                    message="Requested user_id conflicts with the trusted context.",
                    retryable=False,
                    details={"field": "user_id"},
                )
            if tenant_id is not None and context.tenant_id != tenant_id:
                raise HarnessError(
                    code="SESSION_SCOPE_CONFLICT",
                    category="identity",
                    message="Requested tenant_id conflicts with the trusted context.",
                    retryable=False,
                    details={"field": "tenant_id"},
                )
        effective_user = context.user_id if context is not None else (user_id or self.user_id)
        effective_tenant = (
            context.tenant_id if context is not None else (tenant_id or self._tenant_id)
        )
        if not effective_user or not effective_tenant:
            raise HarnessError(
                code="SESSION_SCOPE_REQUIRED",
                category="identity",
                message="Session enumeration requires both tenant_id and user_id.",
                retryable=False,
            )
        raw = None
        for method_name in method_names:
            method = getattr(db, method_name, None)
            if not callable(method):
                continue

            attempts = []
            kwargs: dict[str, Any] = {
                "user_id": effective_user,
                "tenant_id": effective_tenant,
            }
            if limit is not None:
                kwargs["limit"] = int(limit)
            attempts.append(kwargs)
            # Agno 2.6 storage backends do not all accept tenant_id. A user-bound
            # read is allowed only because exact tenant ownership is rechecked below.
            fallback = dict(kwargs)
            fallback.pop("tenant_id", None)
            attempts.append(fallback)

            for call_kwargs in attempts:
                try:
                    raw = method(**call_kwargs)
                    break
                except TypeError:
                    continue
            if raw is not None:
                break

        if raw is None:
            return []

        if isinstance(raw, dict):
            if isinstance(raw.get("sessions"), list):
                records = raw["sessions"]
            elif isinstance(raw.get("items"), list):
                records = raw["items"]
            else:
                records = [raw]
        elif isinstance(raw, list | tuple):
            records = list(raw)
        else:
            records = [raw]

        normalized = [self._normalize_session_record(item) for item in records]
        normalized = [item for item in normalized if item.get("session_id")]

        normalized = [
            item
            for item in normalized
            if item.get("user_id") == effective_user and item.get("tenant_id") == effective_tenant
        ]

        if limit is not None and limit >= 0:
            normalized = normalized[: int(limit)]
        return normalized

    def _session_exists(self, session_id: str) -> bool:
        getter = getattr(self._agent, "get_session", None)
        if callable(getter):
            try:
                if getter(session_id):
                    return True
            except TypeError:
                pass
        return bool(self.get_session_messages(session_id))

    def resume_session(self, session_id: str, *, verify_exists: bool = False) -> str:
        """
        Activate an existing session ID for subsequent runs.
        """
        target = str(session_id or "").strip()
        if not target:
            raise ValueError("session_id is required")
        if verify_exists and not self._session_exists(target):
            raise HarnessError(
                code="SESSION_NOT_FOUND",
                category="session",
                message=f"Session not found: {target}",
                retryable=False,
                details={"session_id": target},
            )

        if target != self.session_id:
            # The current 0.11 runtime owns one scratch directory per harness.
            # Clear it at the session boundary so a resumed session cannot read
            # another session's scratch artifacts.
            self._cleanup_sandbox_dir()
            self._ensure_sandbox_dir()
        self.session_id = target
        if hasattr(self._agent, "session_id"):
            self._agent.session_id = target
        self._set_system_prompt(session_id=target)
        return target

    def clear_session_context(self, new_session_id: str | None = None) -> str:
        """
        Switch to a fresh session ID so subsequent turns start with empty history.

        Prior sessions remain stored in the database for audit/replay.
        """
        session = new_session_id or f"session-{uuid4().hex[:12]}"
        if session != self.session_id:
            self._cleanup_sandbox_dir()
            self._ensure_sandbox_dir()
        self.session_id = session
        if hasattr(self._agent, "session_id"):
            self._agent.session_id = session
        self._set_system_prompt(session_id=session)
        context = self._build_execution_context(
            user_id=self.user_id,
            session_id=session,
            metadata={"lifecycle": "session.cleared"},
        )
        self._run_lifecycle_hooks_sync(
            "session.cleared",
            context=context,
            metadata={"session_id": session},
        )
        return session

    def save_session_summary(self, summary: str) -> None:
        """
        Persist a session summary to today's daily log in the workspace.

        Useful for context compaction: call this at the end of long sessions
        to preserve important context for future sessions.
        """
        self.workspace.write_session_summary(summary)

    async def end_session(self, generate_summary: bool = True) -> str | None:
        """
        End the current session.

        Optionally generates a conversation summary via a lightweight LLM call,
        fires the on_session_end callback so the platform can persist it, and
        returns the summary text.
        """
        context = self._build_execution_context(
            user_id=self.user_id,
            session_id=self.session_id,
            metadata={"lifecycle": "session.end"},
        )
        await self._run_lifecycle_hooks_async(
            "session.end.started",
            context=context,
            metadata={
                "session_id": self.session_id,
                "generate_summary": generate_summary,
            },
        )
        summary = None
        if generate_summary:
            summary = await self._generate_session_summary()
        created_files = self._list_created_sandbox_files()
        try:
            if self._on_session_end and summary:
                try:
                    await self._emit_session_end_callback(
                        summary,
                        created_files=created_files,
                    )
                except Exception:
                    logger.exception("on_session_end callback failed")
        finally:
            self._cleanup_sandbox_dir()
        released_capability_resources = 0
        if self.session_id is not None and self._capability_executor is not None:
            released_capability_resources = await self._capability_executor.release_session(
                self.session_id
            )
        await self._run_lifecycle_hooks_async(
            "session.end.completed",
            context=context,
            metadata={
                "session_id": self.session_id,
                "summary_generated": summary is not None,
                "created_files": created_files,
                "released_capability_resources": released_capability_resources,
            },
        )
        return summary

    async def _release_owned_resources(self) -> None:
        if self._resources_closed:
            return
        if self._capability_executor is not None:
            await self._capability_executor.aclose()
        if self._context_providers:
            await asyncio.gather(
                *(p.aclose() for p in self._context_providers),
                return_exceptions=True,
            )
        seen: set[int] = set()
        for candidate in reversed(tuple(self._owned_sync_resources)):
            resource = self._sync_resource_owner(candidate)
            identity = id(resource)
            if identity in seen:
                continue
            seen.add(identity)
            async_closer = getattr(resource, "aclose", None)
            try:
                if callable(async_closer):
                    result = async_closer()
                    if inspect.isawaitable(result):
                        await result
                else:
                    closer = getattr(resource, "close", None)
                    if callable(closer):
                        await asyncio.to_thread(closer)
            except Exception:
                logger.debug("Failed to close owned runtime resource", exc_info=True)
        self._owned_sync_resources.clear()
        if self._owns_runtime_store and self._runtime_store is not None:
            closer = getattr(self._runtime_store, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    logger.debug("Failed to close runtime store", exc_info=True)
        if self._finalizer.alive:
            self._finalizer()
        self._resources_closed = True

    def _request_runtime_cancellation(self) -> None:
        for run_id, task in tuple(self._live_runs.items()):
            resume = self._run_resume_events.get(run_id)
            if resume is not None:
                resume.set()
            if not task.done():
                task.cancel()

    async def _shutdown_runtime(self, policy: RuntimeClosePolicy) -> None:
        if policy is RuntimeClosePolicy.CANCEL:
            self._request_runtime_cancellation()
        tasks = tuple(task for task in self._live_runs.values() if not task.done())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._release_owned_resources()

    def close(
        self,
        *,
        policy: str | RuntimeClosePolicy | None = None,
        timeout: float | None = None,
    ) -> None:
        """Synchronously stop admission and apply an explicit live-run policy.

        Async applications must call ``await aclose(...)`` so the owning event
        loop can drain or cancel lifecycle workers without cross-loop hazards.
        """
        configured_policy = (
            policy
            if policy is not None
            else str(getattr(self.config, "runtime_close_policy", RuntimeClosePolicy.DRAIN.value))
        )
        try:
            resolved_policy = RuntimeClosePolicy.parse(configured_policy)
        except ValueError as exc:
            raise HarnessError(
                code="HARNESS_CLOSE_POLICY_INVALID",
                category="validation",
                message=str(exc),
                retryable=False,
            ) from exc
        if resolved_policy is RuntimeClosePolicy.DETACH:
            raise HarnessError(
                code="HARNESS_SYNC_DETACH_UNSUPPORTED",
                category="lifecycle",
                message=(
                    "Synchronous close cannot own detached async workers; use "
                    "await aclose(policy='detach') on their persistent event loop."
                ),
                retryable=False,
            )
        sync_coordinator = self._sync_lifecycle_coordinator
        if sync_coordinator is not None:
            try:
                sync_coordinator.run(
                    self.aclose(policy=resolved_policy, timeout=timeout)
                )
            finally:
                if self._resources_closed:
                    sync_coordinator.stop()
                    with self._sync_lifecycle_lock:
                        if self._sync_lifecycle_coordinator is sync_coordinator:
                            self._sync_lifecycle_coordinator = None
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose(policy=resolved_policy, timeout=timeout))
            return
        raise HarnessError(
            code="HARNESS_ASYNC_CLOSE_REQUIRED",
            category="lifecycle",
            message="close() cannot run inside an event loop; use await aclose().",
            retryable=False,
        )

    async def aclose(
        self,
        *,
        policy: str | RuntimeClosePolicy | None = None,
        timeout: float | None = None,
    ) -> None:
        """Stop admission, then drain, detach, or cancel live lifecycle runs."""
        if self._resources_closed:
            return
        sync_coordinator = self._sync_lifecycle_coordinator
        if sync_coordinator is not None and not sync_coordinator.in_coordinator_loop():
            wrapped = asyncio.wrap_future(
                sync_coordinator.submit(self.aclose(policy=policy, timeout=timeout))
            )
            try:
                await wrapped
            finally:
                if self._resources_closed:
                    await asyncio.to_thread(sync_coordinator.stop)
                    with self._sync_lifecycle_lock:
                        if self._sync_lifecycle_coordinator is sync_coordinator:
                            self._sync_lifecycle_coordinator = None
            return
        configured_policy = (
            policy
            if policy is not None
            else str(getattr(self.config, "runtime_close_policy", RuntimeClosePolicy.DRAIN.value))
        )
        try:
            resolved_policy = RuntimeClosePolicy.parse(configured_policy)
        except ValueError as exc:
            raise HarnessError(
                code="HARNESS_CLOSE_POLICY_INVALID",
                category="validation",
                message=str(exc),
                retryable=False,
            ) from exc
        wait_timeout = (
            timeout
            if timeout is not None
            else getattr(self.config, "runtime_close_timeout_seconds", None)
        )
        if wait_timeout is not None and wait_timeout <= 0:
            raise HarnessError(
                code="HARNESS_CLOSE_TIMEOUT_INVALID",
                category="validation",
                message="close timeout must be greater than zero.",
                retryable=False,
            )

        if self._shutdown_task is None:
            self._closed = True
            self._shutdown_policy = resolved_policy
            self._shutdown_task = asyncio.create_task(
                self._shutdown_runtime(resolved_policy),
                name=f"agnoclaw:{self.name}:shutdown",
            )
        elif resolved_policy is RuntimeClosePolicy.CANCEL:
            # A later explicit cancel may escalate an earlier drain/detach.
            self._request_runtime_cancellation()

        if resolved_policy is RuntimeClosePolicy.DETACH:
            return
        try:
            awaited = asyncio.shield(self._shutdown_task)
            if wait_timeout is None:
                await awaited
            else:
                await asyncio.wait_for(awaited, timeout=wait_timeout)
        except TimeoutError as exc:
            raise HarnessError(
                code="HARNESS_CLOSE_TIMEOUT",
                category="lifecycle",
                message=(
                    "Harness shutdown timed out; admission remains closed and the "
                    "shutdown supervisor will release resources after runs settle."
                ),
                retryable=True,
                details={
                    "policy": resolved_policy.value,
                    "active_runs": len(self._live_runs),
                },
            ) from exc

    def __enter__(self) -> AgentHarness:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    async def __aenter__(self) -> AgentHarness:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _generate_session_summary(
        self,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> str | None:
        """Generate a short summary of the current session via a cheap LLM call."""
        target_session = session_id or self.session_id
        messages = (
            self.get_session_messages(target_session)
            if target_session is not None
            else self.get_chat_history()
        )
        if not messages:
            return None

        # Take the last N messages to keep the summary call cheap
        recent = messages[-20:]
        history_text = "\n".join(
            f"{getattr(m, 'role', 'unknown')}: {getattr(m, 'content', str(m))}"
            for m in recent
            if getattr(m, "content", None)
        )
        if not history_text:
            return None

        prompt = (
            "Summarize this conversation in 3-5 bullets focusing on "
            "decisions made and artifacts produced. Be concise.\n\n"
            f"{history_text}"
        )
        token = self._internal_run_kind.set("summary")
        try:
            result = await self.arun(
                prompt,
                session_id=target_session,
                user_id=user_id if user_id is not None else self.user_id,
                add_history_to_context=False,
            )
        finally:
            self._internal_run_kind.reset(token)
        result_messages = list(getattr(result, "messages", None) or [])
        attempted_tool_use = any(
            str(getattr(message, "role", "")).casefold() in {"tool", "function"}
            or bool(getattr(message, "tool_calls", None))
            for message in result_messages
        )
        rendered = str(getattr(result, "content", result) or "").strip()
        if attempted_tool_use or not rendered:
            logger.warning(
                "Internal summary synthesis produced tool activity or empty output; "
                "using deterministic transcript fallback"
            )
            return self._emergency_context_summary(recent)
        return rendered

    @property
    def underlying_agent(self) -> Agent:
        """
        Access the underlying Agno Agent.

        .. deprecated::
            Use narrow accessors (model_name, storage, chat_history, etc.)
            instead. Direct access bypasses all harness protections (hooks,
            policy, events, permissions, guardrails). The temporary escape hatch
            exists only for quick/legacy migration and will be removed in v1.0.
        """
        if self.profile in {RuntimeProfile.DURABLE, RuntimeProfile.SERVICE}:
            raise HarnessError(
                code="UNDERLYING_AGENT_PROFILE_UNSUPPORTED",
                category="lifecycle",
                message=(
                    "Direct Agno Agent access is unavailable in durable/service "
                    "profiles because it bypasses lifecycle and effect authority."
                ),
                retryable=False,
                details={"profile": self.profile.value},
            )
        warnings.warn(
            "underlying_agent is deprecated — use narrow accessors (model_name, "
            "storage, chat_history, etc.) instead. Direct access bypasses harness "
            "protections.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._agent

    @property
    def model_name(self) -> str:
        """Return the resolved model string (provider:model_id)."""
        if self._model_factory is not None:
            return self._model_factory.resolved_name
        if isinstance(self._model, str):
            return self._model
        _prov = (getattr(self._model, "provider", "") or "").lower()
        model_id = getattr(self._model, "id", "") or ""
        return f"{_prov}:{model_id}" if _prov else model_id

    @property
    def storage(self):
        """Return the storage backend (SqliteDb or PostgresDb)."""
        return self._agent.db

    @property
    def chat_history(self) -> list:
        """Return chat history for the current session."""
        return self.get_chat_history()

    @property
    def system_prompt(self) -> str:
        """Return the current system prompt."""
        value = self._agent.system_message
        return value if isinstance(value, str) else ""

    def remove_tool(self, tool_name: str) -> bool:
        """
        Remove a tool by name from the agent's tool registry.

        Returns True if the tool was found and removed, False otherwise.
        """
        if not hasattr(self._agent, "_tools") or self._agent._tools is None:
            return False
        before = len(self._agent._tools)
        self._agent._tools = [
            t for t in self._agent._tools if getattr(t, "name", None) != tool_name
        ]
        return len(self._agent._tools) < before

    def remove_hook(self, hook, *, kind: str = "pre") -> bool:
        """
        Remove a pre-run or post-run hook.

        Args:
            hook: The hook function/callable to remove.
            kind: "pre" or "post".

        Returns True if the hook was found and removed, False otherwise.
        """
        target = self._pre_run_hooks if kind == "pre" else self._post_run_hooks
        try:
            target.remove(hook)
            return True
        except ValueError:
            return False


# Backward-compatible alias
HarnessAgent = AgentHarness
