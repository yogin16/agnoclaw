"""Construction of one run-owned Agno Agent from a validated harness blueprint."""

from __future__ import annotations

import copy
from typing import Any

from ..models.ownership import OwnedAgnoModelResource
from .builtin_materialization import BuiltinToolBundle
from .errors import HarnessError
from .model_gateway import install_agno_provider_gateway


def materialize_run_agent(
    harness: Any,
    *,
    session_id: str | None,
    user_id: str | None,
    learning_scope: Any | None = None,
) -> tuple[Any, BuiltinToolBundle | None]:
    """Construct an Agent and close acquired tools if construction fails."""
    blueprint = dict(harness._agent_blueprint)
    created_model = None
    if harness._model_factory is not None:
        prepared = harness._prepared_run_model.get()
        created_model = prepared[0] if prepared is not None else harness._model_factory.create()
        blueprint["model"] = created_model
    bundle = harness._builtin_tool_factory(None) if harness._builtin_tool_factory else None
    base_tools = list(harness._agent_blueprint.get("tools") or [])
    tools = [tool for tool in base_tools if id(tool) not in harness._base_default_tool_ids]
    if bundle is not None:
        harness._attach_tool_runtime_hooks(list(bundle.tools))
        tools = list(bundle.tools) + tools
    capability_names = {binding.tool_name for binding in harness._capability_bindings}
    tools = [tool for tool in tools if harness._single_tool_name(tool) not in capability_names]
    tools.extend(
        harness._materialize_capability_function(binding.spec, None)
        for binding in harness._capability_bindings
    )
    blueprint["tools"] = tools
    if harness._compression_manager_factory is not None:
        blueprint["compression_manager"] = harness._compression_manager_factory()
    if harness._session_summary_manager_factory is not None:
        blueprint["session_summary_manager"] = harness._session_summary_manager_factory()
    blueprint["dependencies"] = copy.deepcopy(harness._dependencies) or None
    blueprint["session_state"] = copy.deepcopy(harness._session_state) or None
    if isinstance(blueprint.get("output_schema"), dict):
        blueprint["output_schema"] = copy.deepcopy(blueprint["output_schema"])
    prompt_session_id = session_id
    if harness._learning_policy is not None:
        if learning_scope is None:  # pragma: no cover - internal invariant
            raise HarnessError(
                code="LEARNING_SCOPE_REQUIRED",
                category="learning",
                message="A run-scoped learning policy requires a resolved scope.",
                retryable=False,
            )
        from ..memory import build_learning_machine

        blueprint["learning"] = build_learning_machine(
            db=harness._learning_db,
            policy=harness._learning_policy,
            scope=learning_scope,
        )
        blueprint["add_learnings_to_context"] = False
        user_id = learning_scope.storage_user_id
        session_id = learning_scope.storage_session_id
    blueprint["system_message"] = harness._build_system_prompt(session_id=prompt_session_id)
    blueprint["session_id"] = session_id
    blueprint["user_id"] = user_id
    try:
        agent = harness._agent_constructor(**blueprint)
        run_id = harness._active_runtime_run_id.get()
        if harness._active_durable_model_loop.get():
            if run_id is None:  # pragma: no cover - internal context invariant
                raise HarnessError(
                    code="PROVIDER_GATEWAY_RUN_REQUIRED",
                    category="model",
                    message="Durable provider dispatch requires an active lifecycle run.",
                    retryable=False,
                )
            install_agno_provider_gateway(
                agent.model,
                store=harness._get_runtime_store(),
                artifact_store=harness._require_artifact_store(),
                worker_id=harness._runtime_worker_id,
                run_id=run_id,
                harness_spec_digest=harness._spec.settings_digest,
                result_cache_size=int(
                    getattr(harness.config, "runtime_operation_result_cache_size", 128)
                ),
            )
        return agent, bundle
    except BaseException:
        if bundle is not None:
            bundle.close()
        if created_model is not None and harness._prepared_run_model.get() is None:
            OwnedAgnoModelResource(created_model).close()
        raise


__all__ = ["materialize_run_agent"]
