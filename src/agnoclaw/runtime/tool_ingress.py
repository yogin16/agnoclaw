"""Declared first-party tool effects and lifecycle-only operation ingress."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import json
from dataclasses import dataclass
from typing import Any

from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from ..capability_governance import capability_policy_version
from .errors import HarnessError
from .hooks import ToolCallRequest, ToolCallResult
from .operations import EffectClass, OperationIntent, OperationKind
from .policy import PolicyAction


@dataclass(frozen=True)
class BuiltinEffectSpec:
    """Versioned replay semantics for one Agnoclaw-owned Agno function."""

    name: str
    effect_class: EffectClass
    version: str = "1"

    @property
    def target(self) -> str:
        return f"agnoclaw.builtin/{self.name}@{self.version}"


_READ_ONLY = frozenset(
    {
        "bash_output",
        "browser_snapshot",
        "glob_files",
        "get_skill_instructions",
        "grep_files",
        "list_dir",
        "list_todos",
        "notebook_read",
        "read_features",
        "read_file",
        "read_image",
        "read_pdf",
        "read_progress",
        "search_mcp_tools",
        "web_fetch",
        "web_search",
    }
)
_NON_REPEATABLE = frozenset(
    {
        "AskUserQuestion",
        "ExitPlanMode",
        "bash",
        "bash_kill",
        "bash_start",
        "browser_click",
        "browser_close",
        "browser_fill_form",
        "browser_navigate",
        "browser_screenshot",
        "browser_scroll",
        "browser_type",
        "call_mcp_tool",
        "create_todo",
        "delete_todo",
        "edit_file",
        "multi_edit_file",
        "notebook_add_cell",
        "notebook_edit_cell",
        "spawn_subagent",
        "update_feature_status",
        "update_todo",
        "write_features",
        "write_file",
        "write_progress",
    }
)
_DECLARED_EFFECTS = {
    **dict.fromkeys(_READ_ONLY, EffectClass.READ_ONLY),
    **dict.fromkeys(_NON_REPEATABLE, EffectClass.NON_REPEATABLE),
}
_SPEC_ATTRIBUTE = "_agnoclaw_builtin_effect_spec"
_ORIGINAL_ATTRIBUTE = "_agnoclaw_original_entrypoint"


def toolkit_functions(toolkit: Toolkit) -> dict[str, Function]:
    """Return Agno sync and async toolkit functions through one stable view."""
    functions = dict(toolkit.functions)
    functions.update(getattr(toolkit, "async_functions", {}) or {})
    return functions


def _functions(tools: list[Any] | tuple[Any, ...]):
    for tool in tools:
        if isinstance(tool, Function):
            yield tool
        elif isinstance(tool, Toolkit):
            yield from toolkit_functions(tool).values()


def declare_builtin_effects(tools: list[Any] | tuple[Any, ...]) -> None:
    """Attach explicit effects; a newly added first-party tool must update this table."""
    for function in _functions(tools):
        effect = _DECLARED_EFFECTS.get(str(function.name))
        if effect is None:
            raise HarnessError(
                code="BUILTIN_EFFECT_UNCLASSIFIED",
                category="configuration",
                message="A first-party tool has no declared replay semantics.",
                retryable=False,
                details={"tool_name": function.name},
            )
        setattr(function, _SPEC_ATTRIBUTE, BuiltinEffectSpec(str(function.name), effect))


def builtin_effect(function: Function) -> BuiltinEffectSpec | None:
    value = getattr(function, _SPEC_ATTRIBUTE, None)
    return value if isinstance(value, BuiltinEffectSpec) else None


def builtin_effect_manifest() -> dict[str, dict[str, str]]:
    """Return the complete versioned declaration included in the harness spec."""
    return {
        name: {"effect_class": effect.value, "version": "1"}
        for name, effect in sorted(_DECLARED_EFFECTS.items())
    }


async def _sync_call(call) -> Any:
    """Observe a worker thread through completion so run-owned resources stay valid."""
    task = asyncio.create_task(asyncio.to_thread(call))
    cancelled = False
    while True:
        try:
            value = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return value


async def _call_entrypoint(entrypoint, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if inspect.iscoroutinefunction(entrypoint):
        return await entrypoint(*args, **kwargs)
    value = await _sync_call(lambda: entrypoint(*args, **kwargs))
    return await value if inspect.isawaitable(value) else value


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"


async def _result_policy(harness, *, runtime: dict[str, Any], value: Any) -> Any:
    result = ToolCallResult(
        run_id=runtime["parent_run_id"],
        tool_name=runtime["parent_tool_name"],
        arguments=dict(runtime["arguments"]),
        output=value,
        metadata={"tool_call_id": runtime.get("parent_tool_call_id")},
    )
    decision = await harness._run_policy_async(
        method_name="after_tool_call",
        payload=result,
        run_input=None,
        context=runtime["context"],
    )
    await harness._enforce_policy_decision_async(
        decision=decision,
        checkpoint="after_tool_call",
        run_id=result.run_id,
        context=runtime["context"],
    )
    if decision.action is PolicyAction.ALLOW_WITH_CONSTRAINTS and decision.constraints:
        raise HarnessError(
            code="BUILTIN_RESULT_CONSTRAINT_UNSUPPORTED",
            category="policy",
            message="First-party tool result constraints require an explicit adapter.",
            retryable=False,
            details={"tool_name": result.tool_name},
        )
    if decision.action is PolicyAction.ALLOW_WITH_REDACTION:
        return harness._apply_redactions_to_object(value, decision.redactions)
    return value


async def _execute(harness, spec: BuiltinEffectSpec, entrypoint, fc, runtime, args, kwargs):
    run_id = harness._active_runtime_run_id.get()
    claim = harness._active_runtime_claim.get()
    context = harness._active_runtime_context.get()
    if run_id is None or claim is None or claim.run_id != run_id or context is None:
        raise HarnessError(
            code="BUILTIN_ACTIVE_LEASE_REQUIRED",
            category="runtime_lease",
            message="First-party tool execution requires the active lifecycle lease.",
            retryable=False,
        )
    if runtime["policy_constraints"]:
        raise HarnessError(
            code="BUILTIN_POLICY_CONSTRAINT_UNSUPPORTED",
            category="policy",
            message="First-party tool argument constraints require an explicit adapter.",
            retryable=False,
            details={"tool_name": spec.name},
        )
    violations = harness._guardrails.check(
        ToolCallRequest(
            run_id=run_id,
            tool_name=spec.name,
            arguments=dict(runtime["arguments"]),
            metadata={"effect_class": spec.effect_class.value},
        )
    )
    if violations:
        raise HarnessError(
            code="GUARDRAIL_DENIED",
            category="guardrail",
            message=f"Guardrail denied final tool arguments: {spec.name}",
            retryable=False,
            details={"tool_name": spec.name, "violation_codes": [v.code for v in violations]},
        )
    current_policy = capability_policy_version(
        harness._policy_engines,
        harness._permission_controller,
    )
    if current_policy != runtime["policy_version"]:
        raise HarnessError(
            code="BUILTIN_POLICY_DRIFT",
            category="authorization",
            message="Tool policy authority changed before dispatch.",
            retryable=False,
            details={"tool_name": spec.name},
        )
    call_identity = runtime.get("parent_tool_call_id") or runtime["parent_step_id"]
    operation_suffix = _digest([spec.target, call_identity]).split(":", 1)[1][:32]
    intent = OperationIntent(
        operation_id=f"{run_id}:builtin:{operation_suffix}",
        run_id=run_id,
        attempt_id=f"{run_id}:attempt:fence:{claim.run.fence_token}",
        kind=OperationKind.CAPABILITY,
        target=spec.target,
        request_digest=_digest(
            {
                "arguments": runtime["arguments"],
                "authority": context.admission.authority_digest,
                "target": spec.target,
            }
        ),
        effect_class=spec.effect_class,
        metadata={
            "ingress": "first_party_builtin",
            "lease_fence_token": claim.run.fence_token,
            "policy_evidence": runtime["policy_evidence"],
            "policy_version": current_policy,
            "tool_call_digest": _digest(call_identity),
        },
    )

    async def dispatch():
        value = await _call_entrypoint(entrypoint, args, kwargs)
        return await _result_policy(harness, runtime=runtime, value=value)

    execution = await harness._get_effect_operation_gateway().execute(
        intent,
        dispatch,
        pre_dispatch=lambda: harness._renew_capability_lease(claim),
    )
    fc._agnoclaw_builtin_post_policy = True
    return await harness._model_operation_output(
        execution,
        label=spec.name,
        context=context,
    )


def activate_builtin_ingress(function: Function, *, fc: Any, harness: Any) -> None:
    """Wrap one active call without changing direct run/arun compatibility."""
    spec = builtin_effect(function)
    if spec is None or harness._active_runtime_run_id.get() is None:
        return
    if getattr(fc, _ORIGINAL_ATTRIBUTE, None) is not None:
        return
    if function.cache_results:
        raise HarnessError(
            code="BUILTIN_AGNO_CACHE_UNSUPPORTED",
            category="configuration",
            message="Lifecycle tools use the operation ledger, not Agno's local tool cache.",
            retryable=False,
            details={"tool_name": spec.name},
        )
    entrypoint = function.entrypoint
    if (
        entrypoint is None
        or inspect.isgeneratorfunction(entrypoint)
        or inspect.isasyncgenfunction(entrypoint)
    ):
        raise HarnessError(
            code="BUILTIN_ENTRYPOINT_UNSUPPORTED",
            category="configuration",
            message="A governed first-party tool requires a non-streaming entrypoint.",
            retryable=False,
            details={"tool_name": spec.name},
        )
    runtime = getattr(fc, "_agnoclaw_tool_runtime", None)
    if not isinstance(runtime, dict):
        raise HarnessError(
            code="BUILTIN_GOVERNANCE_CONTEXT_REQUIRED",
            category="authorization",
            message="First-party lifecycle tool is missing trusted ingress context.",
            retryable=False,
        )

    # The governed wrapper must keep the original's sync/async identity: Agno
    # chooses the dispatch lane (event loop vs worker thread) from
    # iscoroutinefunction(entrypoint) before activation runs.
    if inspect.iscoroutinefunction(entrypoint):

        @functools.wraps(entrypoint)
        async def governed(*args, **kwargs):
            return await _execute(harness, spec, entrypoint, fc, runtime, args, kwargs)

    else:

        @functools.wraps(entrypoint)
        def governed(*args, **kwargs):
            coroutine = _execute(harness, spec, entrypoint, fc, runtime, args, kwargs)
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # Agno routes sync entrypoints to a worker thread with no
                # running loop; drive the governed pipeline to completion
                # there. Before 0.12 this lane silently bypassed governance
                # because the wrapper landed on a stale Function object.
                return harness._resolve_sync_value(
                    coroutine,
                    operation=f"builtin_ingress:{spec.name}",
                )
            # A running loop means an async caller dispatched directly; hand
            # back the coroutine for it to await.
            return coroutine

    # Agno dispatches parallel calls of one tool against the same registry
    # Function object and reads `entrypoint` at dispatch time, so the shared
    # Function must never carry per-call governance state. Bind the governed
    # wrapper to this call's own Function copy instead.
    governed_function = function.model_copy()
    governed_function.entrypoint = governed
    setattr(fc, _ORIGINAL_ATTRIBUTE, function)
    fc.function = governed_function


def restore_builtin_ingress(function: Function, *, fc: Any) -> None:
    original = getattr(fc, _ORIGINAL_ATTRIBUTE, None)
    if original is not None:
        fc.function = original
        delattr(fc, _ORIGINAL_ATTRIBUTE)


__all__ = [
    "BuiltinEffectSpec",
    "activate_builtin_ingress",
    "builtin_effect",
    "builtin_effect_manifest",
    "declare_builtin_effects",
    "restore_builtin_ingress",
]
