"""Contract-style tests for v0.2 harness runtime behavior."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.exceptions import AgentRunException, ContextWindowExceededError
from agno.tools.function import Function

from agnoclaw import (
    CapabilityConcurrency,
    CapabilityKind,
    CapabilityLifetime,
    CapabilityRecovery,
    CapabilitySpec,
    CapabilityTrust,
    EffectClass,
)
from agnoclaw.agent import _RESULT_REF_KEYS, AgentHarness, _merge_result_ref_keys
from agnoclaw.config import HarnessConfig, RuntimeProfile
from agnoclaw.context_locking import (
    ContextLockLostError,
    ContextLockMode,
    ContextLockUnavailableError,
    LocalFileContextLockProvider,
)
from agnoclaw.context_management import (
    ContextContinuationRecord,
    ContextItemKind,
    ContextQualityError,
    ContextScope,
    DeterministicTokenCounter,
)
from agnoclaw.runtime import (
    AgnoAuthError,
    ExecutionContext,
    HarnessError,
    IdentitySource,
    InMemoryEventSink,
    LocalArtifactStore,
    PolicyAction,
    PolicyDecision,
    RedactionRule,
    RunResultEnvelope,
    RunWaitError,
    SQLiteRuntimeStore,
)


def _make_harness(
    tmp_path,
    *,
    config=None,
    event_sink=None,
    policy_engine=None,
    pre_run_hooks=None,
    post_run_hooks=None,
    **harness_kwargs,
):
    mock_agent = MagicMock()

    def _agent_ctor(*args, **kwargs):
        mock_agent.system_message = kwargs.get("system_message")
        mock_agent.session_id = kwargs.get("session_id")
        mock_agent.tools = list(kwargs.get("tools") or [])
        mock_agent.learning = kwargs.get("learning")
        return mock_agent

    with patch("agnoclaw.agent.Agent", side_effect=_agent_ctor):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=tmp_path,
                config=config or HarnessConfig(),
                event_sink=event_sink,
                policy_engine=policy_engine,
                pre_run_hooks=pre_run_hooks,
                post_run_hooks=post_run_hooks,
                **harness_kwargs,
            )
    return harness, mock_agent


def test_execution_context_is_immutable():
    ctx = ExecutionContext.create(
        user_id="u-1",
        session_id="s-1",
        workspace_id="ws-1",
        roles=["developer"],
    )
    with pytest.raises(FrozenInstanceError):
        ctx.user_id = "u-2"


def test_run_rejects_explicit_identity_conflicting_with_context(tmp_path):
    harness, mock_agent = _make_harness(tmp_path)
    context = ExecutionContext.create(
        user_id="trusted-user",
        session_id="trusted-session",
        workspace_id=str(tmp_path),
    )

    with pytest.raises(HarnessError) as exc:
        harness.run("hello", context=context, user_id="other-user")

    assert exc.value.code == "IDENTITY_CONTEXT_CONFLICT"
    assert exc.value.details == {"field": "user_id"}
    mock_agent.run.assert_not_called()


def test_run_projects_one_frozen_admission_with_source_provenance(tmp_path):
    harness, mock_agent = _make_harness(tmp_path)
    mock_agent.run.return_value = SimpleNamespace(content="ok")
    context = ExecutionContext.create(
        user_id="user-1",
        session_id="session-1",
        workspace_id=str(tmp_path),
        tenant_id="tenant-1",
        roles=("developer",),
        scopes=("runs:write",),
        metadata={"trusted": {"source": "host"}},
        identity_source=IdentitySource.AUTHENTICATED_CLAIMS,
    )

    harness.run("hello", context=context, metadata={"client": {"source": "sdk"}})

    projected = mock_agent.run.call_args.kwargs["metadata"]["_agnoclaw_context"]
    admission = projected["admission"]
    assert projected["identity_source"] == "authenticated_claims"
    assert admission["identity"]["tenant_id"] == "tenant-1"
    assert admission["client_metadata"] == {"client": {"source": "sdk"}}
    assert admission["trusted_metadata"]["trusted"] == {"source": "host"}
    assert {item["field"]: item["source"] for item in admission["provenance"]}[
        "tenant_id"
    ] == "authenticated_claims"


def test_admission_snapshots_nested_metadata_before_hooks(tmp_path):
    harness, _ = _make_harness(tmp_path)
    metadata = {"nested": {"values": ["one"]}}

    _, _, context = harness._resolve_run_identity(
        context=None,
        user_id="user-1",
        session_id="session-1",
        metadata=metadata,
    )
    metadata["nested"]["values"].append("two")

    assert context.metadata["nested"]["values"] == ["one"]
    assert context.admission is not None
    assert context.admission.client_metadata["nested"]["values"] == ("one",)


def test_harness_copies_config_and_compiles_resource_inventory(tmp_path):
    config = HarnessConfig(workspace_dir=str(tmp_path), session_history_runs=2)
    harness, _ = _make_harness(tmp_path, config=config, include_default_tools=False)

    config.session_history_runs = 99

    assert harness.config.session_history_runs == 2
    assert harness._spec.settings["context"]["history_runs"] == 2
    assert harness._spec.settings_digest.startswith("sha256:")
    assert [item.resource_id for item in harness._spec.resources] == [
        "model",
        "agno_db",
    ]


def test_harness_spec_snapshots_dict_output_schema(tmp_path):
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
    }
    harness, _ = _make_harness(
        tmp_path,
        include_default_tools=False,
        output_schema=schema,
    )

    schema["properties"]["answer"]["type"] = "integer"

    assert harness._spec.settings["output"]["schema"]["properties"]["answer"]["type"] == "string"


def test_explicit_durable_profile_requires_recoverable_stores_before_side_effects(tmp_path):
    workspace = tmp_path / "workspace"
    with pytest.raises(HarnessError) as caught:
        AgentHarness(
            model="custom:model",
            profile="durable",
            workspace_dir=workspace,
            include_default_tools=False,
        )

    assert caught.value.code == "RUNTIME_PROFILE_STORE_REQUIRED"
    assert caught.value.details == {
        "profile": "durable",
        "missing": ["runtime_store", "artifact_store"],
    }
    assert not workspace.exists()


def test_explicit_profile_conflict_fails_closed(tmp_path):
    with pytest.raises(HarnessError) as caught:
        AgentHarness(
            model="custom:model",
            profile="quick",
            config=HarnessConfig.durable(),
            workspace_dir=tmp_path,
            include_default_tools=False,
        )

    assert caught.value.code == "RUNTIME_PROFILE_CONFLICT"


def test_invalid_profile_has_a_typed_preconstruction_error(tmp_path):
    workspace = tmp_path / "workspace"
    with pytest.raises(HarnessError) as caught:
        AgentHarness(
            model="custom:model",
            profile="unsafe-fast",  # type: ignore[arg-type]
            workspace_dir=workspace,
            include_default_tools=False,
        )

    assert caught.value.code == "RUNTIME_PROFILE_INVALID"
    assert not workspace.exists()


def test_service_profile_rejects_a_non_postgres_runtime_store(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    with pytest.raises(HarnessError) as caught:
        AgentHarness(
            model="custom:model",
            config=HarnessConfig.service(
                storage={
                    "backend": "postgres",
                    "postgres_url": "postgresql://redacted.invalid/database",
                }
            ),
            workspace_dir=tmp_path / "workspace",
            include_default_tools=False,
            runtime_store=store,
            artifact_store=artifacts,
            db=MagicMock(),
        )

    assert caught.value.code == "SERVICE_POSTGRES_RUNTIME_STORE_REQUIRED"
    store.close()


def test_durable_profile_drives_compiled_resource_classification(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    harness, _ = _make_harness(
        tmp_path / "workspace",
        config=HarnessConfig.durable(),
        include_default_tools=False,
        runtime_store=store,
        artifact_store=artifacts,
    )

    assert harness.profile is RuntimeProfile.DURABLE
    assert harness._spec.profile == "durable"
    resources = {item.resource_id: item for item in harness._spec.resources}
    assert resources["model"].trust.value == "explicit_immutable"
    assert resources["runtime_store"].concurrency.value == "host_managed_shared"
    assert resources["artifact_store"].recovery.value == "reconcilable"
    public = harness.runtime_manifest()
    payload = public.to_dict()
    assert public.profile == "durable"
    assert public.spec_digest == harness._spec.settings_digest
    assert payload["schema_version"] == "0.12a2"
    assert {item["resource_id"] for item in payload["resources"]} == set(resources)
    assert str(tmp_path) not in str(payload)
    harness.close()
    store.close()


def test_durable_profile_rejects_mutable_unclassified_dependencies(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    with pytest.raises(HarnessError) as caught:
        _make_harness(
            tmp_path / "workspace",
            config=HarnessConfig.durable(),
            include_default_tools=False,
            runtime_store=store,
            artifact_store=artifacts,
            dependencies={"mutable_client": object()},
        )

    assert caught.value.code == "UNCLASSIFIED_RUNTIME_RESOURCE"
    assert caught.value.details == {
        "parameter": "dependency:mutable_client",
        "profile": "durable",
    }
    store.close()


def test_model_only_sync_runs_materialize_distinct_agents_and_overlap(tmp_path):
    class ConcurrentAgent:
        instances: list[ConcurrentAgent] = []
        barrier = threading.Barrier(2)

        def __init__(self, **kwargs):
            self.system_message = kwargs["system_message"]
            self.session_id = kwargs.get("session_id")
            self.user_id = kwargs.get("user_id")
            self.tools = list(kwargs.get("tools") or [])
            self.db = kwargs.get("db")
            self.learning = kwargs.get("learning")
            self.__class__.instances.append(self)

        def run(self, _message, **kwargs):
            self.__class__.barrier.wait(timeout=2)
            return SimpleNamespace(
                content=f"{kwargs['session_id']}:{id(self)}",
            )

    with patch("agnoclaw.agent.Agent", ConcurrentAgent):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path,
                config=HarnessConfig(),
                include_default_tools=False,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(harness.run, "one", session_id="session-one")
        second = pool.submit(harness.run, "two", session_id="session-two")
        outputs = [first.result(timeout=5), second.result(timeout=5)]

    run_agents = ConcurrentAgent.instances[1:]
    assert len(run_agents) == 2
    assert run_agents[0] is not run_agents[1]
    assert {agent.session_id for agent in run_agents} == {
        "session-one",
        "session-two",
    }
    assert {output.content.split(":", 1)[0] for output in outputs} == {
        "session-one",
        "session-two",
    }
    assert harness._agent is harness._base_agent


@pytest.mark.asyncio
async def test_model_only_async_runs_materialize_distinct_agents_and_overlap(tmp_path):
    class ConcurrentAgent:
        instances: list[ConcurrentAgent] = []
        ready = asyncio.Event()
        entered = 0

        def __init__(self, **kwargs):
            self.system_message = kwargs["system_message"]
            self.session_id = kwargs.get("session_id")
            self.user_id = kwargs.get("user_id")
            self.tools = list(kwargs.get("tools") or [])
            self.db = kwargs.get("db")
            self.learning = kwargs.get("learning")
            self.__class__.instances.append(self)

        async def arun(self, _message, **kwargs):
            self.__class__.entered += 1
            if self.__class__.entered == 2:
                self.__class__.ready.set()
            await asyncio.wait_for(self.__class__.ready.wait(), timeout=2)
            return SimpleNamespace(
                content=f"{kwargs['session_id']}:{id(self)}",
            )

    with patch("agnoclaw.agent.Agent", ConcurrentAgent):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path,
                config=HarnessConfig(),
                include_default_tools=False,
            )

    outputs = await asyncio.gather(
        harness.arun("one", session_id="session-one"),
        harness.arun("two", session_id="session-two"),
    )

    run_agents = ConcurrentAgent.instances[1:]
    assert len(run_agents) == 2
    assert run_agents[0] is not run_agents[1]
    assert {agent.session_id for agent in run_agents} == {
        "session-one",
        "session-two",
    }
    assert {output.content.split(":", 1)[0] for output in outputs} == {
        "session-one",
        "session-two",
    }
    assert harness._agent is harness._base_agent


def test_harness_callbacks_keep_fresh_agents_while_disabling_parallel_fast_path(tmp_path):
    class CallbackAgent:
        instances: list[CallbackAgent] = []

        def __init__(self, **kwargs):
            self.system_message = kwargs["system_message"]
            self.session_id = kwargs.get("session_id")
            self.user_id = kwargs.get("user_id")
            self.tools = list(kwargs.get("tools") or [])
            self.__class__.instances.append(self)

        def run(self, _message, **kwargs):
            return SimpleNamespace(content=f"{kwargs['session_id']}:{id(self)}")

    observed: list[str] = []

    def pre_hook(run_input, _context):
        observed.append(run_input.run_id)
        return run_input

    with patch("agnoclaw.agent.Agent", CallbackAgent):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path,
                config=HarnessConfig(),
                include_default_tools=False,
                event_sink=InMemoryEventSink(),
                pre_run_hooks=[pre_hook],
            )

    assert harness._can_materialize_run_agent(stream=False, skill=None)
    assert not harness._can_materialize_isolated_run(stream=False, skill=None)
    first = harness.run("one", session_id="session-one")
    second = harness.run("two", session_id="session-two")

    base, first_agent, second_agent = CallbackAgent.instances
    assert first_agent is not base
    assert second_agent is not base
    assert first_agent is not second_agent
    assert first.content == f"session-one:{id(first_agent)}"
    assert second.content == f"session-two:{id(second_agent)}"
    assert len(observed) == 2
    assert harness._agent is harness._base_agent
    harness.close()


def test_explicit_skill_mutates_only_a_fresh_run_agent(tmp_path):
    class SkillAgent:
        instances: list[SkillAgent] = []

        def __init__(self, **kwargs):
            self.system_message = kwargs["system_message"]
            self.session_id = kwargs.get("session_id")
            self.user_id = kwargs.get("user_id")
            self.tools = list(kwargs.get("tools") or [])
            self.__class__.instances.append(self)

        def run(self, _message, **_kwargs):
            return SimpleNamespace(content=self.system_message)

    skill_dir = tmp_path / "skills" / "reviewer"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: reviewer\ndescription: Review safely\n---\n\n"
        "RUN-OWNED-SKILL-INSTRUCTION\n",
        encoding="utf-8",
    )
    with patch("agnoclaw.agent.Agent", SkillAgent):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path,
                config=HarnessConfig(),
                include_default_tools=False,
            )

    base_prompt = harness._base_agent.system_message
    assert harness._can_materialize_run_agent(stream=False, skill="reviewer")
    assert not harness._can_materialize_isolated_run(stream=False, skill="reviewer")

    result = harness.run("review", skill="reviewer", session_id="session-skill")

    assert len(SkillAgent.instances) == 2
    assert "RUN-OWNED-SKILL-INSTRUCTION" in result.content
    assert harness._base_agent.system_message == base_prompt
    assert harness._agent is harness._base_agent
    harness.close()


@pytest.mark.asyncio
async def test_capability_functions_are_rebuilt_per_overlapping_run(tmp_path):
    class CapabilityAgent:
        instances: list[CapabilityAgent] = []
        ready = asyncio.Event()
        entered = 0

        def __init__(self, **kwargs):
            self.system_message = kwargs["system_message"]
            self.session_id = kwargs.get("session_id")
            self.user_id = kwargs.get("user_id")
            self.tools = list(kwargs.get("tools") or [])
            self.db = kwargs.get("db")
            self.learning = kwargs.get("learning")
            self.__class__.instances.append(self)

        async def arun(self, _message, **_kwargs):
            self.__class__.entered += 1
            if self.__class__.entered == 2:
                self.__class__.ready.set()
            await asyncio.wait_for(self.__class__.ready.wait(), timeout=2)
            function = next(tool for tool in self.tools if isinstance(tool, Function))
            return SimpleNamespace(content=str(id(function)))

    capability = CapabilitySpec(
        name="lookup",
        version="1.0.0",
        kind=CapabilityKind.TOOL,
        effect_class=EffectClass.READ_ONLY,
        trust=CapabilityTrust.VERIFIED,
        lifetime=CapabilityLifetime.RUN,
        concurrency=CapabilityConcurrency.ISOLATED,
        recovery=CapabilityRecovery.RECREATABLE,
        implementation_digest="sha256:lookup-v1",
        input_schema={"type": "object", "properties": {}},
        factory=lambda: lambda: "ok",
    )
    with patch("agnoclaw.agent.Agent", CapabilityAgent):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path,
                config=HarnessConfig.quick(),
                include_default_tools=False,
                capabilities=[capability],
            )

    results = await asyncio.gather(
        harness.arun("one", session_id="session-one"),
        harness.arun("two", session_id="session-two"),
    )

    run_agents = CapabilityAgent.instances[1:]
    assert len(run_agents) == 2
    assert run_agents[0].tools[0] is not run_agents[1].tools[0]
    assert {result.content for result in results} == {
        str(id(run_agents[0].tools[0])),
        str(id(run_agents[1].tools[0])),
    }
    capability_resource = next(
        item for item in harness._spec.resources if item.resource_id == "capability:lookup@1.0.0"
    )
    assert capability_resource.trust.value == "factory"
    assert capability_resource.lifetime.value == "run"
    await harness.aclose()


@pytest.mark.asyncio
async def test_context_managers_and_data_containers_are_run_owned(tmp_path):
    class ManagedAgent:
        instances: list[ManagedAgent] = []
        ready = asyncio.Event()
        entered = 0

        def __init__(self, **kwargs):
            self.system_message = kwargs["system_message"]
            self.session_id = kwargs.get("session_id")
            self.user_id = kwargs.get("user_id")
            self.tools = list(kwargs.get("tools") or [])
            self.db = kwargs.get("db")
            self.learning = kwargs.get("learning")
            self.compression_manager = kwargs.get("compression_manager")
            self.session_summary_manager = kwargs.get("session_summary_manager")
            self.dependencies = kwargs.get("dependencies")
            self.session_state = kwargs.get("session_state")
            self.output_schema = kwargs.get("output_schema")
            self.__class__.instances.append(self)

        async def arun(self, _message, **_kwargs):
            self.__class__.entered += 1
            if self.__class__.entered == 2:
                self.__class__.ready.set()
            await asyncio.wait_for(self.__class__.ready.wait(), timeout=2)
            self.session_state["turn"] = self.session_id
            return SimpleNamespace(content=self.session_id)

    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    with patch("agnoclaw.agent.Agent", ManagedAgent):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path,
                config=HarnessConfig.quick(),
                include_default_tools=False,
                dependencies={"region": "local"},
                session_state={"turn": "seed"},
                output_schema=schema,
                enable_compression=True,
                enable_session_summary=True,
            )

    await asyncio.gather(
        harness.arun("one", session_id="session-one"),
        harness.arun("two", session_id="session-two"),
    )

    first, second = ManagedAgent.instances[1:]
    assert first.compression_manager is not second.compression_manager
    assert first.session_summary_manager is not second.session_summary_manager
    assert first.dependencies is not second.dependencies
    assert first.session_state is not second.session_state
    assert first.output_schema is not second.output_schema
    assert first.session_state == {"turn": "session-one"}
    assert second.session_state == {"turn": "session-two"}
    assert harness._session_state == {"turn": "seed"}
    resources = {item.resource_id: item for item in harness._spec.resources}
    assert resources["compression_manager"].trust.value == "factory"
    assert resources["session_summary_manager"].concurrency.value == "isolated"
    await harness.aclose()


def test_quick_host_builtins_are_rebuilt_and_released_per_run(tmp_path):
    class BuiltinAgent:
        instances: list[BuiltinAgent] = []

        def __init__(self, **kwargs):
            self.system_message = kwargs["system_message"]
            self.session_id = kwargs.get("session_id")
            self.user_id = kwargs.get("user_id")
            self.tools = list(kwargs.get("tools") or [])
            self.__class__.instances.append(self)

        def run(self, _message, **_kwargs):
            return SimpleNamespace(content="ok")

        async def arun(self, _message, **_kwargs):
            return SimpleNamespace(content="ok")

    class ClosingExecutor:
        instances: list[ClosingExecutor] = []

        def __init__(self, *, workspace_dir=None, **_kwargs):
            self.workspace_dir = str(workspace_dir) if workspace_dir is not None else None
            self.close_calls = 0
            self.__class__.instances.append(self)

        def close(self):
            self.close_calls += 1

    with patch("agnoclaw.agent.Agent", BuiltinAgent):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path,
                config=HarnessConfig.quick(),
            )

    assert harness._can_materialize_run_agent(stream=False, skill=None)
    assert not harness._can_materialize_isolated_run(stream=False, skill=None)
    resources = {item.resource_id: item for item in harness._spec.resources}
    assert resources["builtin_tools"].trust.value == "factory"
    assert resources["builtin_tools"].lifetime.value == "run"
    assert not any(resource_id.startswith("tool:") for resource_id in resources)

    with patch(
        "agnoclaw.runtime.builtin_materialization.LocalCommandExecutor",
        ClosingExecutor,
    ):
        harness.run("one", session_id="session-one")
        harness.run("two", session_id="session-two")

    base, first, second = BuiltinAgent.instances
    assert first is not second
    assert {id(tool) for tool in base.tools}.isdisjoint(id(tool) for tool in first.tools)
    assert {id(tool) for tool in first.tools}.isdisjoint(id(tool) for tool in second.tools)
    assert len(ClosingExecutor.instances) == 2
    assert all(executor.close_calls >= 1 for executor in ClosingExecutor.instances)
    harness.close()


def test_quick_configured_mcp_is_a_run_owned_builtin(tmp_path):
    config = HarnessConfig.quick(
        mcp_servers=[{"name": "catalog", "command": ["unused"]}],
    )
    harness, _ = _make_harness(
        tmp_path,
        model="model",
        provider="custom",
        config=config,
    )

    assert harness._can_materialize_run_agent(stream=False, skill=None)
    resources = {item.resource_id: item for item in harness._spec.resources}
    assert resources["builtin_tools"].trust.value == "factory"
    assert not any(item.startswith("tool:") for item in resources)

    first = harness._builtin_tool_factory(None)
    second = harness._builtin_tool_factory(None)
    first_mcp = next(tool for tool in first.tools if tool.name == "mcp")
    second_mcp = next(tool for tool in second.tools if tool.name == "mcp")
    assert first_mcp is not second_mcp
    first.close()
    second.close()
    harness.close()


def test_quick_host_builtin_effects_remain_single_flight(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingAgent:
        def __init__(self, **kwargs):
            self.system_message = kwargs["system_message"]
            self.session_id = kwargs.get("session_id")
            self.user_id = kwargs.get("user_id")
            self.tools = list(kwargs.get("tools") or [])

        def run(self, message, **_kwargs):
            if message == "first":
                entered.set()
                assert release.wait(timeout=5)
            return SimpleNamespace(content="ok")

        async def arun(self, message, **_kwargs):
            if message == "first":
                entered.set()
                assert await asyncio.to_thread(release.wait, 5)
            return SimpleNamespace(content="ok")

    with patch("agnoclaw.agent.Agent", BlockingAgent):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path,
                config=HarnessConfig.quick(),
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(harness.run, "first", session_id="session-one")
        assert entered.wait(timeout=5)
        try:
            with pytest.raises(RunWaitError) as caught:
                harness.run("overlap", session_id="session-two")
            assert caught.value.code == "RUN_FAILED"
            assert caught.value.safe_error["code"] == "HARNESS_RUN_IN_PROGRESS"
            assert harness._get_runtime_store().list_run_operations(
                caught.value.details["run_id"]
            ) == []
        finally:
            release.set()
        assert first.result(timeout=5).content == "ok"
    harness.close()


def test_durable_profile_rejects_ungoverned_default_tool_effects(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    with pytest.raises(HarnessError) as caught:
        _make_harness(
            tmp_path / "workspace",
            config=HarnessConfig.durable(),
            runtime_store=store,
            artifact_store=artifacts,
        )

    assert caught.value.code == "UNCLASSIFIED_RUNTIME_RESOURCE"
    assert str(caught.value.details["parameter"]).startswith("tool:")
    store.close()


def test_builtin_surface_configuration_changes_complete_spec_digest(tmp_path):
    first, _ = _make_harness(
        tmp_path / "workspace",
        model="model",
        provider="custom",
        config=HarnessConfig.quick(enable_web_fetch=True),
    )
    second, _ = _make_harness(
        tmp_path / "workspace",
        model="model",
        provider="custom",
        config=HarnessConfig.quick(enable_web_fetch=False),
    )

    assert first._spec.settings["builtin_tools"]["web_fetch"] is True
    assert second._spec.settings["builtin_tools"]["web_fetch"] is False
    assert first._spec.settings["builtin_tools"]["effect_manifest"]["read_file"] == {
        "effect_class": "read_only",
        "version": "1",
    }
    assert first._spec.settings_digest != second._spec.settings_digest
    first.close()
    second.close()


def test_mutable_runtime_extension_disables_isolated_fast_path(tmp_path):
    harness, _ = _make_harness(tmp_path, include_default_tools=False)
    assert harness._can_materialize_isolated_run(stream=False, skill=None)

    harness.set_event_sink(InMemoryEventSink())

    assert not harness._can_materialize_isolated_run(stream=False, skill=None)


@pytest.mark.asyncio
async def test_arun_resolves_missing_context_identity_once(tmp_path):
    harness, mock_agent = _make_harness(tmp_path)
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="ok"))
    context = ExecutionContext.create(
        user_id=None,
        session_id=None,
        workspace_id=str(tmp_path),
        tenant_id="tenant-1",
    )

    await harness.arun(
        "hello",
        context=context,
        user_id="user-1",
        session_id="session-1",
    )

    kwargs = mock_agent.arun.call_args.kwargs
    assert kwargs["user_id"] == "user-1"
    assert kwargs["session_id"] == "session-1"
    assert kwargs["metadata"]["_agnoclaw_context"]["user_id"] == "user-1"
    assert kwargs["metadata"]["_agnoclaw_context"]["session_id"] == "session-1"
    assert kwargs["metadata"]["_agnoclaw_context"]["tenant_id"] == "tenant-1"


def test_run_emits_lifecycle_events(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    harness.run("hello")

    event_types = [e.event_type for e in sink.events]
    assert event_types[0] == "run.started"
    assert "prompt.built" in event_types
    assert "model.request.started" in event_types
    assert "model.request.completed" in event_types
    assert "run.completed" in event_types
    assert event_types.count("policy.decision") >= 2
    first_event = sink.events[0]
    payload = first_event.to_dict()
    assert first_event.event_version == "0.2"
    assert payload["context"]["workspace_id"] == str(harness.workspace.path)


def test_run_raises_auth_error_for_auth_failures(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    mock_agent.run.return_value = SimpleNamespace(
        content=(
            '"Could not resolve authentication method. '
            'Expected either api_key or auth_token to be set."'
        ),
        status=SimpleNamespace(value="error"),
        events=[],
    )

    with pytest.raises(AgnoAuthError):
        harness.run("hello")

    event_types = [e.event_type for e in sink.events]
    assert "run.failed" in event_types
    assert "run.completed" not in event_types


def test_run_keeps_recoverable_error_as_output_and_marks_failed(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    model_output = SimpleNamespace(
        content="Rate limit exceeded, please retry later",
        status=SimpleNamespace(value="error"),
        events=[],
    )
    mock_agent.run.return_value = model_output

    result = harness.run("hello")

    assert result is model_output
    event_types = [e.event_type for e in sink.events]
    assert "model.request.failed" in event_types
    assert "run.failed" in event_types
    assert "run.completed" not in event_types


def test_policy_deny_blocks_run(tmp_path):
    class DenyPolicy:
        def before_run(self, run_input, context):
            del run_input, context
            return PolicyDecision.deny(reason_code="BLOCKED", message="run denied")

        def before_prompt_send(self, prompt, context):
            del prompt, context
            return PolicyDecision.allow()

        def before_skill_load(self, request, context):
            del request, context
            return PolicyDecision.allow()

    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink, policy_engine=DenyPolicy())
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    with pytest.raises(HarnessError, match="run denied") as exc:
        harness.run("hello")

    assert exc.value.code == "POLICY_DENIED"
    mock_agent.run.assert_not_called()
    assert "run.failed" in [e.event_type for e in sink.events]


def test_pre_and_post_hooks_are_ordered(tmp_path):
    order: list[str] = []

    def pre_hook(run_input, context):
        del context
        order.append("pre")
        run_input.message = f"{run_input.message} transformed"
        return run_input

    def post_hook(run_input, result, context):
        del run_input, context
        order.append("post")
        result.metadata["seen"] = True
        return result

    harness, mock_agent = _make_harness(
        tmp_path,
        pre_run_hooks=[pre_hook],
        post_run_hooks=[post_hook],
    )
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    harness.run("hello")

    call_args = mock_agent.run.call_args
    assert call_args.args[0] == "hello transformed"
    assert order == ["pre", "post"]


def test_stream_run_emits_content_events(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    mock_agent.run.return_value = iter(
        [
            SimpleNamespace(event="ToolCallStarted", content=""),
            SimpleNamespace(event="RunContent", content="A"),
            SimpleNamespace(event="ToolCallCompleted", content=""),
            SimpleNamespace(event="RunContent", content="B"),
        ]
    )

    list(harness.run("hello", stream=True, stream_events=True))

    event_types = [e.event_type for e in sink.events]
    assert event_types.count("run.content") == 2
    assert "agno.event" in event_types
    assert "run.completed" in event_types

    agno_events = [e.payload for e in sink.events if e.event_type == "agno.event"]
    assert [payload["source_event"] for payload in agno_events[:4]] == [
        "ToolCallStarted",
        "RunContent",
        "ToolCallCompleted",
        "RunContent",
    ]


def test_stream_emits_response_and_thinking_events_and_marks_failed(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    mock_agent.run.return_value = iter(
        [
            SimpleNamespace(
                event="ReasoningContentDelta", content="", reasoning_content="plan step"
            ),
            SimpleNamespace(event="RunContent", content="A"),
            SimpleNamespace(
                event="RunError",
                content="Rate limit exceeded",
                error_id="model_rate_limit_error",
                error_type="model_provider_error",
                additional_data={},
            ),
        ]
    )

    with pytest.raises(HarnessError) as exc:
        list(harness.run("hello", stream=True, stream_events=True))

    event_types = [e.event_type for e in sink.events]
    assert exc.value.code == "MODEL_STREAM_FAILED"
    assert "thinking" in event_types
    assert "response_chunk" in event_types
    assert "run.failed" in event_types
    assert "run.completed" not in event_types

    chunks = [e.payload for e in sink.events if e.event_type == "response_chunk"]
    assert chunks[0]["content"] == "A"
    assert chunks[-1]["is_final"] is True


@pytest.mark.asyncio
async def test_arun_stream_raises_on_stream_error_and_marks_failed(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)

    async def _stream():
        yield SimpleNamespace(event="RunContent", content="A")
        yield SimpleNamespace(
            event="RunError",
            content="Rate limit exceeded",
            error_id="model_rate_limit_error",
            error_type="model_provider_error",
            additional_data={},
        )

    mock_agent.arun = AsyncMock(return_value=_stream())

    stream = await harness.arun("hello", stream=True, stream_events=True)
    with pytest.raises(HarnessError) as exc:
        async for _ in stream:
            pass

    assert exc.value.code == "MODEL_STREAM_FAILED"
    event_types = [e.event_type for e in sink.events]
    assert "model.request.failed" in event_types
    assert "run.failed" in event_types
    assert "run.completed" not in event_types


@pytest.mark.asyncio
async def test_arun_stream_emits_single_response_chunk_per_text_delta(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)

    async def _stream():
        yield SimpleNamespace(event="RunContent", content="Hello!")

    mock_agent.arun = AsyncMock(return_value=_stream())

    stream = await harness.arun("hello", stream=True, stream_events=True)
    async for _ in stream:
        pass

    chunks = [e.payload for e in sink.events if e.event_type == "response_chunk"]
    assert chunks == [
        {"content": "Hello!", "cumulative": "Hello!", "is_final": False},
        {"content": "", "cumulative": "Hello!", "is_final": True},
    ]

    run_content_events = [e for e in sink.events if e.event_type == "run.content"]
    assert len(run_content_events) == 1
    assert run_content_events[0].payload["chars"] == len("Hello!")


def test_stream_tool_lifecycle_events_do_not_duplicate_hook_events(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    run_context = _tool_run_context(harness)
    fc = SimpleNamespace(
        function=SimpleNamespace(name="bash"),
        arguments={"command": "pwd"},
        result="/tmp/workspace",
        error=None,
        call_id="tc-stream-1",
    )

    def _stream():
        harness._handle_tool_pre_hook(fc=fc, run_context=run_context)
        yield SimpleNamespace(
            event="ToolCallStarted",
            tool=SimpleNamespace(
                tool_name="bash",
                tool_call_id="tc-stream-1",
                arguments={"command": "pwd"},
            ),
            content="",
        )
        harness._handle_tool_post_hook(fc=fc, run_context=run_context)
        yield SimpleNamespace(
            event="ToolCallCompleted",
            tool=SimpleNamespace(tool_name="bash", tool_call_id="tc-stream-1"),
            content="/tmp/workspace",
        )
        yield SimpleNamespace(event="RunContent", content="Done.")

    mock_agent.run.return_value = _stream()

    list(harness.run("hello", stream=True, stream_events=True))

    event_types = [e.event_type for e in sink.events]
    assert event_types.count("tool.call.started") == 1
    assert event_types.count("tool.call.completed") == 1

    agno_events = [e.payload for e in sink.events if e.event_type == "agno.event"]
    tool_events = [
        payload for payload in agno_events if payload["source_event"].startswith("ToolCall")
    ]
    assert [payload["source_event"] for payload in tool_events] == [
        "ToolCallStarted",
        "ToolCallCompleted",
    ]
    assert tool_events[0]["arguments"] == {"command": "pwd"}
    assert tool_events[0]["argument_keys"] == ["command"]
    assert tool_events[1]["result_preview"] == "/tmp/workspace"
    assert tool_events[1]["result_chars"] == len("/tmp/workspace")


def test_stream_response_chunk_excludes_tool_output_but_keeps_raw_agno_detail(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    mock_agent.run.return_value = iter(
        [
            SimpleNamespace(
                event="ToolCallCompleted",
                content="Ahmedabad Gujarat IN 23.0258,72.5873",
                tool=SimpleNamespace(tool_name="bash", tool_call_id="tc-weather-1"),
            ),
            SimpleNamespace(event="RunContent", content="The current weather is warm and clear."),
        ]
    )

    list(harness.run("hello", stream=True, stream_events=True))

    chunks = [e.payload for e in sink.events if e.event_type == "response_chunk"]
    assert chunks == [
        {
            "content": "The current weather is warm and clear.",
            "cumulative": "The current weather is warm and clear.",
            "is_final": False,
        },
        {
            "content": "",
            "cumulative": "The current weather is warm and clear.",
            "is_final": True,
        },
    ]

    agno_events = [e.payload for e in sink.events if e.event_type == "agno.event"]
    tool_event = next(
        payload for payload in agno_events if payload["source_event"] == "ToolCallCompleted"
    )
    assert tool_event["tool_name"] == "bash"
    assert tool_event["tool_call_id"] == "tc-weather-1"
    assert tool_event["details"]["content"] == "Ahmedabad Gujarat IN 23.0258,72.5873"


def test_subagent_events_flow_to_parent_sink_with_parent_child_linkage(tmp_path):
    from agnoclaw.tools.tasks import _run_subagent

    sink = InMemoryEventSink()
    parent_harness, _ = _make_harness(
        tmp_path,
        event_sink=sink,
        session_metadata={"client": "tests"},
    )
    parent_run_context = _tool_run_context(parent_harness)
    parent_fc = SimpleNamespace(
        function=SimpleNamespace(name="spawn_subagent"),
        arguments={"task": "child task"},
        result=None,
        error=None,
        call_id="tc-parent-subagent-1",
    )
    parent_harness._handle_tool_pre_hook(fc=parent_fc, run_context=parent_run_context)
    parent_harness._set_active_tool_runtime(parent_fc, parent_fc._agnoclaw_tool_runtime)

    class FakeChildAgent:
        def __init__(self, **kwargs):
            self.system_message = kwargs.get("system_message")
            self.session_id = kwargs.get("session_id")
            self.tools = kwargs.get("tools") or []

        def run(self, message, **kwargs):
            del message
            bash = next(tool for tool in self.tools if getattr(tool, "name", None) == "bash")
            child_fc = SimpleNamespace(
                function=SimpleNamespace(name="bash"),
                arguments={"command": "pwd"},
                result=str(tmp_path),
                error=None,
                call_id="tc-child-bash-1",
            )
            child_run_context = SimpleNamespace(
                run_id=kwargs["run_id"],
                session_id=kwargs["session_id"],
                user_id=kwargs["user_id"],
                metadata=kwargs["metadata"],
            )
            bash.pre_hook(run_context=child_run_context, fc=child_fc)
            bash.post_hook(run_context=child_run_context, fc=child_fc)
            return SimpleNamespace(content="child ok")

    with (
        patch("agnoclaw.agent.Agent", side_effect=lambda *args, **kwargs: FakeChildAgent(**kwargs)),
        patch("agnoclaw.agent._make_db", return_value=MagicMock()),
    ):
        result = _run_subagent(
            "child task",
            "Follow instructions",
            "anthropic:test-model",
            tool_names=["bash"],
            workspace_dir=tmp_path,
        )

    parent_harness._handle_tool_post_hook(fc=parent_fc, run_context=parent_run_context)
    parent_harness._clear_active_tool_runtime(parent_fc)

    assert result == "child ok"

    child_events = [
        event
        for event in sink.events
        if event.payload.get("parent_tool_call_id") == "tc-parent-subagent-1"
    ]
    assert child_events

    child_run_ids = {event.run_id for event in child_events if event.run_id != "run_tool_123"}
    assert len(child_run_ids) == 1

    child_event_types = [event.event_type for event in child_events]
    assert "run.started" in child_event_types
    assert "tool.call.started" in child_event_types
    assert "tool.call.completed" in child_event_types
    assert "run.completed" in child_event_types

    for event in child_events:
        assert event.payload["parent_run_id"] == "run_tool_123"
        assert event.payload["parent_tool_name"] == "spawn_subagent"
        assert event.payload["subagent_depth"] == 1
        assert event.payload["subagent_root_run_id"] == "run_tool_123"
        assert event.payload["client"] == "tests"


def test_skill_fork_resolves_model_using_active_provider(tmp_path):
    harness, mock_agent = _make_harness(tmp_path, model="openai:gpt-4o")
    harness.skills.load_skill = MagicMock(return_value="skill instructions")
    harness.skills._get_skill = MagicMock(
        return_value=SimpleNamespace(
            meta=SimpleNamespace(
                context="fork",
                model="gpt-4",
                allowed_tools=None,
                command_dispatch=None,
                command_tool=None,
            )
        )
    )

    with patch("agnoclaw.tools.tasks._run_subagent", return_value="fork ok") as mock_run:
        result = harness.run("hello", skill="forked-skill")

    assert result == "fork ok"
    assert mock_run.call_args[1]["model_id"] == "openai:gpt-4"
    mock_agent.run.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_profile_rejects_specialized_skill_dispatch_before_run_creation(tmp_path):
    harness, mock_agent = _make_harness(
        tmp_path,
        config=HarnessConfig.quick(),
        include_default_tools=False,
    )
    harness.skills._get_skill = MagicMock(
        return_value=SimpleNamespace(
            meta=SimpleNamespace(
                context="fork",
                command_dispatch=None,
            )
        )
    )

    with pytest.raises(HarnessError) as caught:
        await harness.start("hello", skill="forked-skill")

    assert caught.value.code == "SKILL_LIFECYCLE_DISPATCH_UNSUPPORTED"
    assert caught.value.details == {
        "skill": "forked-skill",
        "dispatch_mode": "fork",
    }
    mock_agent.arun.assert_not_called()


def test_explicit_profile_rejects_named_raw_subagents_at_construction(tmp_path):
    with pytest.raises(HarnessError) as caught:
        _make_harness(
            tmp_path,
            config=HarnessConfig.quick(),
            subagents={"reviewer": object()},
        )

    assert caught.value.code == "RAW_SUBAGENT_LIFECYCLE_UNSUPPORTED"


def test_context_override_propagates_to_model_call(tmp_path):
    harness, mock_agent = _make_harness(tmp_path)
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    context = ExecutionContext.create(
        user_id="ctx-user",
        session_id="ctx-session",
        workspace_id="ws-id",
    )
    harness.run("hello", context=context)

    call_kwargs = mock_agent.run.call_args.kwargs
    assert call_kwargs["user_id"] == "ctx-user"
    assert call_kwargs["session_id"] == "ctx-session"
    assert isinstance(call_kwargs["run_id"], str)
    assert call_kwargs["run_id"].startswith("run_")
    assert "_agnoclaw_context" in call_kwargs["metadata"]


def test_fail_closed_event_sink_raises(tmp_path):
    class FailingSink:
        def emit(self, event):
            del event
            raise RuntimeError("sink down")

    harness, mock_agent = _make_harness(
        tmp_path,
        event_sink=FailingSink(),
        event_sink_mode="fail_closed",
    )
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    with pytest.raises(HarnessError) as exc:
        harness.run("hello")
    assert exc.value.code == "EVENT_SINK_FAILED"


@pytest.mark.asyncio
async def test_sync_emit_from_worker_thread_routes_to_owning_loop(tmp_path):
    """Regression (issue #57): when a sync tool's lifecycle hook fires from an
    agno ThreadPoolExecutor worker thread (no running loop), an async event
    sink's ``emit`` must run on the harness's owning loop — not a throwaway
    ``asyncio.run`` loop that would corrupt loop-bound resources under load."""
    recorded_loops: list[int] = []

    class LoopRecordingSink:
        async def emit(self, event):
            del event
            recorded_loops.append(id(asyncio.get_running_loop()))

    harness, _ = _make_harness(tmp_path, event_sink=LoopRecordingSink())
    owning_loop = asyncio.get_running_loop()
    harness._owning_loop = owning_loop

    ctx = ExecutionContext.create(
        user_id="u", session_id="s", workspace_id="ws", roles=["developer"]
    )

    # Mirror agno's sync-tool thread hop: emit from a worker thread that has no
    # running loop of its own.
    await asyncio.to_thread(
        harness._emit_event_sync,
        event_type="tool.started",
        run_id="run_x",
        context=ctx,
    )

    # Best-effort mode is fire-and-forget; let the owning loop drain the task.
    for _ in range(100):
        if recorded_loops:
            break
        await asyncio.sleep(0.01)

    assert recorded_loops == [id(owning_loop)]


def test_sync_emit_falls_back_when_owning_loop_not_running(tmp_path):
    """A stale-but-open owning loop (from a prior run, no longer running) must
    NOT be routed to — routing there would drop the event or hang. The sync
    fallback (asyncio.run) still delivers the emit (issue #57 hardening)."""
    delivered: list[str] = []

    class AsyncSink:
        async def emit(self, event):
            delivered.append(event.event_type)

    harness, _ = _make_harness(tmp_path, event_sink=AsyncSink())

    stale_loop = asyncio.new_event_loop()  # open but never run
    harness._owning_loop = stale_loop
    try:
        assert not stale_loop.is_running()
        ctx = ExecutionContext.create(
            user_id="u", session_id="s", workspace_id="ws", roles=["developer"]
        )
        harness._emit_event_sync(event_type="tool.started", run_id="run_x", context=ctx)
    finally:
        stale_loop.close()

    assert delivered == ["tool.started"]


@pytest.mark.asyncio
async def test_sync_emit_fail_closed_from_worker_thread_propagates(tmp_path):
    """A failing async sink driven from a worker thread must surface as a
    HarnessError under fail-closed mode (issue #57 routing path)."""

    class FailingAsyncSink:
        async def emit(self, event):
            del event
            raise RuntimeError("sink down")

    harness, _ = _make_harness(
        tmp_path,
        event_sink=FailingAsyncSink(),
        event_sink_mode="fail_closed",
    )
    harness._owning_loop = asyncio.get_running_loop()

    ctx = ExecutionContext.create(
        user_id="u", session_id="s", workspace_id="ws", roles=["developer"]
    )

    with pytest.raises(HarnessError) as exc:
        await asyncio.to_thread(
            harness._emit_event_sync,
            event_type="tool.started",
            run_id="run_x",
            context=ctx,
        )
    assert exc.value.code == "EVENT_SINK_FAILED"


@pytest.mark.asyncio
async def test_sync_emit_best_effort_worker_thread_swallows_sink_failure(tmp_path, caplog):
    """Best-effort mode: a failing async sink routed from a worker thread must
    be swallowed and logged on the owning loop, never raised (issue #57)."""

    class FailingAsyncSink:
        async def emit(self, event):
            del event
            raise RuntimeError("sink down")

    harness, _ = _make_harness(tmp_path, event_sink=FailingAsyncSink())
    harness._owning_loop = asyncio.get_running_loop()

    ctx = ExecutionContext.create(
        user_id="u", session_id="s", workspace_id="ws", roles=["developer"]
    )

    with caplog.at_level("WARNING", logger="agnoclaw.agent"):
        # Fire-and-forget: emit returns without raising even though the sink fails.
        await asyncio.to_thread(
            harness._emit_event_sync,
            event_type="tool.started",
            run_id="run_x",
            context=ctx,
        )
        # Let the owning loop drain the scheduled coroutine + done-callback.
        for _ in range(100):
            if any("Async event sink failure" in r.message for r in caplog.records):
                break
            await asyncio.sleep(0.01)

    assert any("Async event sink failure" in r.message for r in caplog.records)


def test_sync_emit_falls_back_when_owning_loop_closed(tmp_path):
    """A closed owning loop must take the asyncio.run fallback (the is_closed()
    guard sub-branch), still delivering the event (issue #57).

    Sync test on purpose: with no running loop, ``_emit_event_sync`` reaches the
    no-running-loop branch where the ``_owning_loop`` guard lives. An async test
    would supply a running loop and take the create_task path instead.
    """
    delivered: list[str] = []

    class AsyncSink:
        async def emit(self, event):
            delivered.append(event.event_type)

    harness, _ = _make_harness(tmp_path, event_sink=AsyncSink())

    closed_loop = asyncio.new_event_loop()
    closed_loop.close()
    harness._owning_loop = closed_loop
    assert closed_loop.is_closed()

    ctx = ExecutionContext.create(
        user_id="u", session_id="s", workspace_id="ws", roles=["developer"]
    )
    harness._emit_event_sync(event_type="tool.started", run_id="run_x", context=ctx)

    assert delivered == ["tool.started"]


@pytest.mark.asyncio
async def test_sync_emit_fail_closed_worker_thread_success_delivers(tmp_path):
    """Fail-closed mode: a healthy async sink routed from a worker thread must
    deliver on the owning loop and not raise (issue #57 success path)."""
    delivered: list[int] = []

    class LoopRecordingSink:
        async def emit(self, event):
            del event
            delivered.append(id(asyncio.get_running_loop()))

    harness, _ = _make_harness(
        tmp_path,
        event_sink=LoopRecordingSink(),
        event_sink_mode="fail_closed",
    )
    owning_loop = asyncio.get_running_loop()
    harness._owning_loop = owning_loop

    ctx = ExecutionContext.create(
        user_id="u", session_id="s", workspace_id="ws", roles=["developer"]
    )

    # future.result() blocks the worker thread until the owning loop delivers.
    await asyncio.to_thread(
        harness._emit_event_sync,
        event_type="tool.started",
        run_id="run_x",
        context=ctx,
    )

    assert delivered == [id(owning_loop)]


@pytest.mark.asyncio
async def test_async_hooks_and_events(tmp_path):
    sink = InMemoryEventSink()
    order: list[str] = []

    async def pre_hook(run_input, context):
        del context
        order.append("pre")
        run_input.message = f"{run_input.message} async"
        return run_input

    async def post_hook(run_input, result, context):
        del run_input, context
        order.append("post")
        return RunResultEnvelope(
            run_id=result.run_id,
            content=result.content,
            raw_output=result.raw_output,
            metadata=result.metadata,
        )

    harness, mock_agent = _make_harness(
        tmp_path,
        event_sink=sink,
        pre_run_hooks=[pre_hook],
        post_run_hooks=[post_hook],
    )
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="ok"))

    await harness.arun("hello")

    call_args = mock_agent.arun.call_args
    assert call_args.args[0] == "hello async"
    assert order == ["pre", "post"]
    assert "run.completed" in [e.event_type for e in sink.events]


@pytest.mark.asyncio
async def test_arun_raises_auth_error_for_auth_failures(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    mock_agent.arun = AsyncMock(
        return_value=SimpleNamespace(
            content=(
                "Could not resolve authentication method. "
                "Expected either api_key or auth_token to be set."
            ),
            status=SimpleNamespace(value="error"),
            events=[],
        )
    )

    with pytest.raises(AgnoAuthError):
        await harness.arun("hello")

    event_types = [e.event_type for e in sink.events]
    assert "run.failed" in event_types
    assert "run.completed" not in event_types


@pytest.mark.asyncio
async def test_arun_keeps_recoverable_error_as_output_and_marks_failed(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    model_output = SimpleNamespace(
        content="Rate limit exceeded, please retry later",
        status=SimpleNamespace(value="error"),
        events=[],
    )
    mock_agent.arun = AsyncMock(return_value=model_output)

    result = await harness.arun("hello")

    assert result is model_output
    event_types = [e.event_type for e in sink.events]
    assert "model.request.failed" in event_types
    assert "run.failed" in event_types
    assert "run.completed" not in event_types


@pytest.mark.asyncio
async def test_arun_passes_max_turns_to_underlying_agent(tmp_path):
    harness, mock_agent = _make_harness(tmp_path)
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="ok"))

    await harness.arun("hello", max_turns=4)

    assert mock_agent.arun.call_args.kwargs["max_turns"] == 4


def _tool_run_context(harness: AgentHarness):
    ctx = ExecutionContext.create(
        user_id="tool-user",
        session_id="tool-session",
        workspace_id=str(harness.workspace.path),
    )
    return SimpleNamespace(
        run_id="run_tool_123",
        session_id="tool-session",
        user_id="tool-user",
        metadata={"_agnoclaw_context": harness._context_to_metadata(ctx)},
    )


def test_before_tool_policy_denies_call(tmp_path):
    class ToolDenyPolicy:
        def before_run(self, run_input, context):
            del run_input, context
            return PolicyDecision.allow()

        def before_prompt_send(self, prompt, context):
            del prompt, context
            return PolicyDecision.allow()

        def before_skill_load(self, request, context):
            del request, context
            return PolicyDecision.allow()

        def before_tool_call(self, request, context):
            del request, context
            return PolicyDecision.deny(reason_code="TOOL_BLOCK", message="tool denied")

        def after_tool_call(self, result, context):
            del result, context
            return PolicyDecision.allow()

    harness, _ = _make_harness(tmp_path, policy_engine=ToolDenyPolicy())
    fc = SimpleNamespace(
        function=SimpleNamespace(name="web_fetch"),
        arguments={"url": "https://example.com"},
        result=None,
        error=None,
        call_id="tc-1",
    )

    with pytest.raises(AgentRunException) as exc:
        harness._handle_tool_pre_hook(fc=fc, run_context=_tool_run_context(harness))

    inner = exc.value.args[0]
    assert isinstance(inner, HarnessError)
    assert inner.code == "POLICY_DENIED"


def test_tool_policy_redacts_input_and_output(tmp_path):
    sink = InMemoryEventSink()

    class RedactPolicy:
        def before_run(self, run_input, context):
            del run_input, context
            return PolicyDecision.allow()

        def before_prompt_send(self, prompt, context):
            del prompt, context
            return PolicyDecision.allow()

        def before_skill_load(self, request, context):
            del request, context
            return PolicyDecision.allow()

        def before_tool_call(self, request, context):
            del request, context
            return PolicyDecision(
                action=PolicyAction.ALLOW_WITH_REDACTION,
                reason_code="REDACT_PRE",
                redactions=(RedactionRule(target="secret"),),
            )

        def after_tool_call(self, result, context):
            del result, context
            return PolicyDecision(
                action=PolicyAction.ALLOW_WITH_REDACTION,
                reason_code="REDACT_POST",
                redactions=(RedactionRule(target="secret"),),
            )

    harness, _ = _make_harness(tmp_path, policy_engine=RedactPolicy(), event_sink=sink)
    fc = SimpleNamespace(
        function=SimpleNamespace(name="web_fetch"),
        arguments={"url": "https://example.com?q=secret"},
        result="secret output",
        error=None,
        call_id="tc-2",
    )
    run_context = _tool_run_context(harness)

    harness._handle_tool_pre_hook(fc=fc, run_context=run_context)
    assert "[REDACTED]" in fc.arguments["url"]
    started = [e for e in sink.events if e.event_type == "tool.call.started"][-1]
    assert started.payload["arguments"] == {"url": "https://example.com?q=[REDACTED]"}
    assert started.payload["argument_keys"] == ["url"]

    harness._handle_tool_post_hook(fc=fc, run_context=run_context)
    assert fc.result == "[REDACTED] output"
    completed = [e for e in sink.events if e.event_type == "tool.call.completed"][-1]
    assert completed.payload["arguments"] == {"url": "https://example.com?q=[REDACTED]"}
    assert completed.payload["argument_keys"] == ["url"]


def test_tool_events_include_step_progress_and_result_preview(tmp_path):
    sink = InMemoryEventSink()
    harness, _ = _make_harness(tmp_path, event_sink=sink)
    run_context = _tool_run_context(harness)
    fc = SimpleNamespace(
        function=SimpleNamespace(name="web_fetch"),
        arguments={"url": "https://example.com"},
        result="line one\nline two",
        error=None,
        call_id="tc-step-1",
    )

    harness._handle_tool_pre_hook(fc=fc, run_context=run_context)
    harness._handle_tool_post_hook(fc=fc, run_context=run_context)

    event_types = [e.event_type for e in sink.events]
    assert "step_started" in event_types
    assert "step_completed" in event_types
    assert "tool.call.completed" in event_types

    completed = [e for e in sink.events if e.event_type == "tool.call.completed"][-1]
    assert completed.payload["step_id"] == "tc-step-1"
    assert completed.payload["duration_ms"] >= 0
    assert completed.payload["arguments"] == {"url": "https://example.com"}
    assert completed.payload["argument_keys"] == ["url"]
    assert completed.payload["result_preview"] == "line one line two"
    # Scalar/string result yields no structured identity.
    assert completed.payload["result_ref"] is None


def test_result_identity_extracts_generic_keys_only():
    ref = AgentHarness._result_identity(
        {
            "id": "art_123",
            "title": "Quarterly Report",
            "type": "document",
            "content": "the full body that must never be carried",
            "extra": {"nested": "ignored"},
            # App-specific keys are NOT recognized without explicit configuration —
            # the harness stays agnostic about any one consumer's schema.
            "artifact_type": "report",
        }
    )
    assert ref == {"id": "art_123", "title": "Quarterly Report", "type": "document"}
    assert "content" not in ref
    assert "artifact_type" not in ref


def test_result_identity_honors_configured_extra_keys():
    ref_keys = _merge_result_ref_keys(["artifact_title", "document_title"])
    ref = AgentHarness._result_identity(
        {"id": "a1", "artifact_title": "Design Doc", "unknown_key": "ignored"},
        ref_keys,
    )
    assert ref == {"id": "a1", "artifact_title": "Design Doc"}
    assert "unknown_key" not in ref


def test_result_identity_from_pydantic_model():
    from pydantic import BaseModel

    class Artifact(BaseModel):
        id: str
        title: str
        version: int

    ref = AgentHarness._result_identity(Artifact(id="a1", title="Spec", version=3))
    assert ref == {"id": "a1", "title": "Spec", "version": 3}


def test_result_identity_from_json_string():
    ref = AgentHarness._result_identity('{"id": "d9", "name": "notes.txt", "type": "file"}')
    assert ref == {"id": "d9", "name": "notes.txt", "type": "file"}


def test_result_identity_none_for_scalars_and_non_identifying():
    assert AgentHarness._result_identity(None) is None
    assert AgentHarness._result_identity(42) is None
    assert AgentHarness._result_identity("plain text output") is None
    assert AgentHarness._result_identity([1, 2, 3]) is None
    # A dict with no identity-bearing keys yields None, not an empty dict.
    assert AgentHarness._result_identity({"foo": "bar", "count": 5}) is None
    # Blank/whitespace identity values are ignored.
    assert AgentHarness._result_identity({"title": "   "}) is None


def test_result_identity_excludes_bool_and_accepts_float():
    # bool is an int subclass but is meaningless as identity — it must not leak in.
    ref = AgentHarness._result_identity({"id": "a1", "type": True})
    assert ref == {"id": "a1"}
    assert "type" not in ref
    # Numeric identifiers expressed as floats are preserved (e.g. a float version).
    ref = AgentHarness._result_identity({"id": "a1", "version": 1.5})
    assert ref == {"id": "a1", "version": 1.5}


def test_result_identity_skips_parsing_non_object_strings():
    # A large non-object string (HTML/plain text) is not JSON-parsed; yields None.
    assert AgentHarness._result_identity("<html><body>" + "x" * 10000) is None
    assert AgentHarness._result_identity("[1, 2, 3]") is None  # JSON array, not identity
    # Object-shaped JSON (even with leading whitespace) is still parsed.
    assert AgentHarness._result_identity('  {"id": "z1"}') == {"id": "z1"}


def test_result_identity_pydantic_include_limits_dump(monkeypatch):
    from pydantic import BaseModel

    class Doc(BaseModel):
        id: str
        title: str

    doc = Doc(id="d1", title="Spec")
    captured = {}
    original = Doc.model_dump

    def spy(self, **kwargs):
        captured.update(kwargs)
        return original(self, **kwargs)

    monkeypatch.setattr(Doc, "model_dump", spy)
    ref = AgentHarness._result_identity(doc, _RESULT_REF_KEYS)
    assert ref == {"id": "d1", "title": "Spec"}
    # Only identity fields are dumped, not the whole model graph.
    assert captured.get("include") == set(_RESULT_REF_KEYS)


def test_merge_result_ref_keys_dedups_and_ignores_blanks():
    merged = _merge_result_ref_keys(["artifact_title", "id", "  ", "artifact_title", 5])
    # Generic keys come first, unchanged.
    assert merged[: len(_RESULT_REF_KEYS)] == _RESULT_REF_KEYS
    # "id" already generic -> not duplicated; blanks/non-strings dropped.
    assert merged == _RESULT_REF_KEYS + ("artifact_title",)


def test_tool_events_emit_result_ref_for_dict_result(tmp_path):
    sink = InMemoryEventSink()
    harness, _ = _make_harness(tmp_path, event_sink=sink)
    run_context = _tool_run_context(harness)
    fc = SimpleNamespace(
        function=SimpleNamespace(name="load_artifact"),
        arguments={"artifact_id": "art_9"},
        result={"id": "art_9", "title": "Design Doc", "type": "document"},
        error=None,
        call_id="tc-ref-1",
    )

    harness._handle_tool_pre_hook(fc=fc, run_context=run_context)
    harness._handle_tool_post_hook(fc=fc, run_context=run_context)

    completed = [e for e in sink.events if e.event_type == "tool.call.completed"][-1]
    assert completed.payload["result_ref"] == {
        "id": "art_9",
        "title": "Design Doc",
        "type": "document",
    }
    # result_preview is still emitted unchanged alongside result_ref.
    assert "result_preview" in completed.payload


def test_tool_events_result_ref_honors_configured_keys(tmp_path):
    config = HarnessConfig(result_ref_keys=["artifact_title", "artifact_type"])
    sink = InMemoryEventSink()
    harness, _ = _make_harness(tmp_path, config=config, event_sink=sink)
    run_context = _tool_run_context(harness)
    fc = SimpleNamespace(
        function=SimpleNamespace(name="load_artifact"),
        arguments={"artifact_id": "art_9"},
        result={"id": "art_9", "artifact_title": "Design Doc", "artifact_type": "document"},
        error=None,
        call_id="tc-ref-cfg-1",
    )

    harness._handle_tool_pre_hook(fc=fc, run_context=run_context)
    harness._handle_tool_post_hook(fc=fc, run_context=run_context)

    completed = [e for e in sink.events if e.event_type == "tool.call.completed"][-1]
    assert completed.payload["result_ref"] == {
        "id": "art_9",
        "artifact_title": "Design Doc",
        "artifact_type": "document",
    }


def test_stream_event_summary_result_ref_honors_configured_keys(tmp_path):
    # The streaming path (used by the TUI/async REPL renderers, too) must honor the
    # consumer-configured keys — not just the sync tool-hook path.
    config = HarnessConfig(result_ref_keys=["artifact_title"])
    harness, _ = _make_harness(tmp_path, config=config)
    event = SimpleNamespace(
        result={"id": "art_9", "artifact_title": "Design Doc", "content": "big body"},
        tool=None,
    )

    summary = harness._stream_event_summary(event)

    assert summary["result_ref"] == {"id": "art_9", "artifact_title": "Design Doc"}


def test_stream_event_summary_result_ref_generic_by_default(tmp_path):
    harness, _ = _make_harness(tmp_path)
    event = SimpleNamespace(
        result={"id": "art_9", "title": "Doc", "artifact_title": "dropped"},
        tool=None,
    )

    summary = harness._stream_event_summary(event)

    # Without configuration, only generic keys are recognized.
    assert summary["result_ref"] == {"id": "art_9", "title": "Doc"}


def test_tool_events_emit_failed_for_tool_errors(tmp_path):
    sink = InMemoryEventSink()
    harness, _ = _make_harness(tmp_path, event_sink=sink)
    run_context = _tool_run_context(harness)
    fc = SimpleNamespace(
        function=SimpleNamespace(name="bash"),
        arguments={"command": "pwd"},
        result=None,
        error="Failed to execute command: cwd missing",
        call_id="tc-step-failed-1",
    )

    harness._handle_tool_pre_hook(fc=fc, run_context=run_context)
    harness._handle_tool_post_hook(fc=fc, run_context=run_context)

    event_types = [e.event_type for e in sink.events]
    assert "tool.call.failed" in event_types
    assert "tool.call.completed" not in event_types

    failed = [e for e in sink.events if e.event_type == "tool.call.failed"][-1]
    assert failed.payload["step_id"] == "tc-step-failed-1"
    assert failed.payload["error"] == "Failed to execute command: cwd missing"
    assert failed.payload["result_chars"] == 0


def test_guardrails_block_path_outside_workspace(tmp_path):
    harness, _ = _make_harness(tmp_path)
    fc = SimpleNamespace(
        function=SimpleNamespace(name="read_file"),
        arguments={"path": "/etc/passwd"},
        result=None,
        error=None,
        call_id="tc-3",
    )

    with pytest.raises(AgentRunException) as exc:
        harness._handle_tool_pre_hook(fc=fc, run_context=_tool_run_context(harness))

    inner = exc.value.args[0]
    assert isinstance(inner, HarnessError)
    assert inner.code == "GUARDRAIL_DENIED"


def test_guardrails_block_private_network_host(tmp_path):
    harness, _ = _make_harness(tmp_path)
    fc = SimpleNamespace(
        function=SimpleNamespace(name="web_fetch"),
        arguments={"url": "https://localhost/internal"},
        result=None,
        error=None,
        call_id="tc-4",
    )

    with pytest.raises(AgentRunException) as exc:
        harness._handle_tool_pre_hook(fc=fc, run_context=_tool_run_context(harness))

    inner = exc.value.args[0]
    assert isinstance(inner, HarnessError)
    assert inner.code == "GUARDRAIL_DENIED"


def test_guardrails_block_hostname_resolving_to_private_network(tmp_path):
    import socket

    harness, _ = _make_harness(tmp_path)
    fc = SimpleNamespace(
        function=SimpleNamespace(name="web_fetch"),
        arguments={"url": "https://public-looking.example/internal"},
        result=None,
        error=None,
        call_id="tc-dns-private",
    )

    with patch(
        "agnoclaw.runtime.guardrails.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443))],
    ):
        with pytest.raises(AgentRunException) as exc:
            harness._handle_tool_pre_hook(fc=fc, run_context=_tool_run_context(harness))

    inner = exc.value.args[0]
    assert isinstance(inner, HarnessError)
    assert inner.code == "GUARDRAIL_DENIED"
    assert inner.details["violations"][0]["code"] == "NETWORK_PRIVATE_HOST_BLOCKED"


@pytest.mark.parametrize("host", ["127.1", "2130706433", "0x7f000001"])
def test_guardrails_block_legacy_loopback_address_spellings(tmp_path, host):
    harness, _ = _make_harness(tmp_path)
    fc = SimpleNamespace(
        function=SimpleNamespace(name="web_fetch"),
        arguments={"url": f"https://{host}/internal"},
        result=None,
        error=None,
        call_id="tc-legacy-loopback",
    )

    with pytest.raises(AgentRunException):
        harness._handle_tool_pre_hook(fc=fc, run_context=_tool_run_context(harness))


def test_guardrails_block_browser_when_network_disabled(tmp_path):
    harness, _ = _make_harness(tmp_path, config=HarnessConfig(network_enabled=False))
    fc = SimpleNamespace(
        function=SimpleNamespace(name="browser_navigate"),
        arguments={"url": "https://example.com"},
        result=None,
        error=None,
        call_id="tc-browser-1",
    )

    with pytest.raises(AgentRunException) as exc:
        harness._handle_tool_pre_hook(fc=fc, run_context=_tool_run_context(harness))

    inner = exc.value.args[0]
    assert isinstance(inner, HarnessError)
    assert inner.code == "GUARDRAIL_DENIED"


def test_permission_plan_mode_blocks_mutating_tools(tmp_path):
    harness, _ = _make_harness(tmp_path, permission_mode="plan")
    fc = SimpleNamespace(
        function=SimpleNamespace(name="write_file"),
        arguments={"path": "note.txt", "content": "x"},
        result=None,
        error=None,
        call_id="tc-plan-1",
    )

    with pytest.raises(AgentRunException) as exc:
        harness._handle_tool_pre_hook(fc=fc, run_context=_tool_run_context(harness))

    inner = exc.value.args[0]
    assert isinstance(inner, HarnessError)
    assert inner.code == "POLICY_DENIED"
    assert "Plan mode is read-only" in inner.message


def test_permission_dont_ask_denies_without_preapproval(tmp_path):
    harness, _ = _make_harness(tmp_path, permission_mode="dont_ask")
    fc = SimpleNamespace(
        function=SimpleNamespace(name="bash"),
        arguments={"command": "echo hi"},
        result=None,
        error=None,
        call_id="tc-pa-1",
    )

    with pytest.raises(AgentRunException) as exc:
        harness._handle_tool_pre_hook(fc=fc, run_context=_tool_run_context(harness))

    inner = exc.value.args[0]
    assert isinstance(inner, HarnessError)
    assert inner.code == "POLICY_DENIED"
    assert "dont_ask" in inner.message


def test_permission_preapproved_tool_allows_in_dont_ask_mode(tmp_path):
    harness, _ = _make_harness(tmp_path, permission_mode="dont_ask")
    ctx = ExecutionContext.create(
        user_id="tool-user",
        session_id="tool-session",
        workspace_id=str(harness.workspace.path),
        trusted_permission_tools=("bash",),
    )
    run_context = SimpleNamespace(
        run_id="run_tool_123",
        session_id="tool-session",
        user_id="tool-user",
        metadata={"_agnoclaw_context": harness._context_to_metadata(ctx)},
    )
    fc = SimpleNamespace(
        function=SimpleNamespace(name="bash"),
        arguments={"command": "echo hi"},
        result=None,
        error=None,
        call_id="tc-pa-2",
    )

    harness._handle_tool_pre_hook(fc=fc, run_context=run_context)


def test_permission_metadata_cannot_self_preapprove_in_dont_ask_mode(tmp_path):
    harness, _ = _make_harness(tmp_path, permission_mode="dont_ask")
    run_context = _tool_run_context(harness)
    run_context.metadata["_agnoclaw_context"]["metadata"]["permission_preapproved_tools"] = ["bash"]
    fc = SimpleNamespace(
        function=SimpleNamespace(name="bash"),
        arguments={"command": "echo hi"},
        result=None,
        error=None,
        call_id="tc-pa-untrusted",
    )

    with pytest.raises(AgentRunException):
        harness._handle_tool_pre_hook(fc=fc, run_context=run_context)


def test_guardrails_apply_to_bash_start_network_calls(tmp_path):
    harness, _ = _make_harness(tmp_path)
    fc = SimpleNamespace(
        function=SimpleNamespace(name="bash_start"),
        arguments={"command": "curl https://localhost/internal"},
        result=None,
        error=None,
        call_id="tc-net-1",
    )

    with pytest.raises(AgentRunException) as exc:
        harness._handle_tool_pre_hook(fc=fc, run_context=_tool_run_context(harness))

    inner = exc.value.args[0]
    assert isinstance(inner, HarnessError)
    assert inner.code == "GUARDRAIL_DENIED"


# ── session_metadata on events ────────────────────────────────────────


def test_session_metadata_merged_into_events(tmp_path):
    """session_metadata dict is merged into every emitted event's payload."""
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(
        tmp_path,
        event_sink=sink,
        session_metadata={"deal_id": "acme-123", "fund_id": "fund-iii"},
    )
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    harness.run("hello")

    for event in sink.events:
        assert event.payload.get("deal_id") == "acme-123", (
            f"Event {event.event_type} missing deal_id"
        )
        assert event.payload.get("fund_id") == "fund-iii", (
            f"Event {event.event_type} missing fund_id"
        )


def test_session_metadata_does_not_clobber_event_payload(tmp_path):
    """Event-specific payload keys take precedence over session_metadata."""
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(
        tmp_path,
        event_sink=sink,
        session_metadata={"stream": "should-be-overridden"},
    )
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    harness.run("hello")

    started = [e for e in sink.events if e.event_type == "run.started"][0]
    # The run.started payload sets stream=False, which should win
    assert started.payload["stream"] is False


@pytest.mark.asyncio
async def test_session_metadata_on_async_events(tmp_path):
    """session_metadata works in the async path too."""
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(
        tmp_path,
        event_sink=sink,
        session_metadata={"tenant": "t-1"},
    )
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="ok"))

    await harness.arun("hello")

    for event in sink.events:
        assert event.payload.get("tenant") == "t-1", (
            f"Async event {event.event_type} missing tenant"
        )


# ── on_compaction callback ────────────────────────────────────────────


def _stored_compaction_session() -> object:
    from agno.models.message import Message
    from agno.run.agent import RunOutput
    from agno.run.base import RunStatus
    from agno.session.agent import AgentSession

    return AgentSession(
        session_id="s-1",
        user_id="u-1",
        agent_id="agnoclaw",
        session_data={},
        runs=[
            RunOutput(
                run_id="run-1",
                agent_id="agnoclaw",
                session_id="s-1",
                user_id="u-1",
                content="evidence " * 200,
                messages=[
                    Message(role="user", content="Investigate the incident " * 100),
                    Message(role="assistant", content="Evidence collected " * 100),
                ],
                status=RunStatus.completed,
            )
        ],
    )


def _configure_overflow_context(harness, mock_agent, tmp_path) -> None:
    del harness, tmp_path
    mock_agent.model.count_tokens.return_value = 100
    mock_agent.get_chat_history.return_value = [
        SimpleNamespace(role="user", content="Continue the incident investigation."),
    ]
    mock_agent.aget_session = AsyncMock(return_value=_stored_compaction_session())
    mock_agent.asave_session = AsyncMock()


def test_sync_context_overflow_compacts_once_and_retries_exact_model_request(tmp_path):
    sink = InMemoryEventSink()
    pre_calls: list[str] = []

    def pre_hook(run_input, context):
        del context
        pre_calls.append(run_input.run_id)

    harness, mock_agent = _make_harness(
        tmp_path,
        event_sink=sink,
        pre_run_hooks=[pre_hook],
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        max_context_tokens=2_000,
        auto_compact_context=True,
    )
    _configure_overflow_context(harness, mock_agent, tmp_path)
    fenced: list[str] = []

    def invoke(*args, **kwargs):
        del args, kwargs
        if mock_agent.run.call_count == 1:
            raise ContextWindowExceededError("maximum context length exceeded")
        with pytest.raises(HarnessError) as blocked:
            harness._context_automation.admit_run("s-1")
        fenced.append(blocked.value.code)
        return SimpleNamespace(content="recovered")

    mock_agent.run.side_effect = invoke

    result = harness.run("continue")

    assert result.content == "recovered"
    assert mock_agent.run.call_count == 2
    assert mock_agent.run.call_args_list[0].args == mock_agent.run.call_args_list[1].args
    assert (
        mock_agent.run.call_args_list[0].kwargs["run_id"]
        == mock_agent.run.call_args_list[1].kwargs["run_id"]
    )
    assert mock_agent.asave_session.await_count == 1
    assert len(pre_calls) == 1
    assert fenced == ["CONTEXT_MAINTENANCE_IN_PROGRESS"]
    assert any(event.event_type == "context.overflow.retrying" for event in sink.events)


def test_sync_context_overflow_upgrades_and_releases_sole_cross_process_reader(tmp_path):
    provider = LocalFileContextLockProvider(tmp_path / "locks")
    harness, mock_agent = _make_harness(
        tmp_path / "harness",
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        max_context_tokens=2_000,
        auto_compact_context=True,
        context_lock_provider=provider,
    )
    _configure_overflow_context(harness, mock_agent, tmp_path)
    mock_agent.run.side_effect = [
        ContextWindowExceededError("maximum context length exceeded"),
        SimpleNamespace(content="recovered"),
    ]

    result = harness.run("continue")

    assert result.content == "recovered"
    assert mock_agent.asave_session.await_count == 1
    writer = provider.acquire(
        ContextScope(session_id="s-1", user_id="u-1"),
        mode=ContextLockMode.EXCLUSIVE,
    )
    writer.release()


@pytest.mark.asyncio
async def test_async_context_overflow_run_output_compacts_once_and_retries(tmp_path):
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        max_context_tokens=2_000,
        auto_compact_context=True,
    )
    _configure_overflow_context(harness, mock_agent, tmp_path)
    overflow = SimpleNamespace(
        content="context_length_exceeded: prompt is too long",
        status=SimpleNamespace(value="error"),
        events=[],
    )
    mock_agent.arun = AsyncMock(side_effect=[overflow, SimpleNamespace(content="recovered")])

    result = await harness.arun("continue")

    assert result.content == "recovered"
    assert mock_agent.arun.await_count == 2
    assert mock_agent.asave_session.await_count == 1


def test_context_overflow_does_not_retry_after_tool_activity(tmp_path):
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        max_context_tokens=2_000,
        auto_compact_context=True,
    )
    _configure_overflow_context(harness, mock_agent, tmp_path)

    def overflow_after_tool(*args, **kwargs):
        del args
        harness._tool_step_state[kwargs["run_id"]] = {"next_index": 2, "active": {}}
        raise ContextWindowExceededError("maximum context length exceeded")

    mock_agent.run.side_effect = overflow_after_tool

    with pytest.raises(HarnessError) as caught:
        harness.run("continue")

    assert caught.value.code == "CONTEXT_OVERFLOW_RETRY_UNSAFE"
    assert mock_agent.run.call_count == 1
    mock_agent.asave_session.assert_not_awaited()


def test_tool_hook_identity_blocks_overflow_retry_when_agno_run_id_differs(tmp_path):
    harness, _mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        max_context_tokens=2_000,
        auto_compact_context=True,
    )
    context = ExecutionContext.create(
        session_id="s-1",
        user_id="u-1",
        workspace_id=str(tmp_path),
    )
    run_context = SimpleNamespace(
        run_id="agno-inner-run",
        metadata={"_agnoclaw_harness_run_id": "harness-outer-run"},
    )
    resolved = harness._run_id_from_tool_hook(run_context=run_context, fc=None)
    harness._start_tool_step(
        run_id=resolved,
        tool_name="side_effect_free_probe",
        tool_call_id="call-1",
        context=context,
    )
    harness._finish_tool_step(
        run_id=resolved,
        tool_name="side_effect_free_probe",
        tool_call_id="call-1",
        context=context,
    )

    with pytest.raises(HarnessError) as caught:
        harness._begin_context_overflow_recovery_sync(
            context=context,
            run_lease=MagicMock(),
            run_id="harness-outer-run",
            stream=False,
            source="exception",
        )

    assert caught.value.code == "CONTEXT_OVERFLOW_RETRY_UNSAFE"
    assert caught.value.details["reason"] == "tool_activity"


def test_caller_metadata_cannot_forge_harness_run_identity(tmp_path):
    harness, mock_agent = _make_harness(tmp_path, session_id="s-1", user_id="u-1")
    mock_agent.run.return_value = SimpleNamespace(content="ok")

    harness.run(
        "continue",
        metadata={
            "_agnoclaw_harness_run_id": "forged-run",
            "_agnoclaw_context": {"tenant_id": "forged-tenant"},
            "_agnoclaw_context_kind": "summary",
        },
    )

    metadata = mock_agent.run.call_args.kwargs["metadata"]
    assert metadata["_agnoclaw_harness_run_id"] != "forged-run"
    assert metadata["_agnoclaw_harness_run_id"].startswith("run_")
    assert metadata["_agnoclaw_context"]["tenant_id"] is None
    assert "_agnoclaw_context_kind" not in metadata


def test_context_overflow_retry_is_bounded_to_one_attempt(tmp_path):
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        max_context_tokens=2_000,
        auto_compact_context=True,
    )
    _configure_overflow_context(harness, mock_agent, tmp_path)
    mock_agent.run.side_effect = [
        ContextWindowExceededError("maximum context length exceeded"),
        ContextWindowExceededError("maximum context length still exceeded"),
    ]

    with pytest.raises(HarnessError) as caught:
        harness.run("continue")

    assert caught.value.code == "CONTEXT_OVERFLOW_RETRY_EXHAUSTED"
    assert mock_agent.run.call_count == 2
    assert mock_agent.asave_session.await_count == 1
    harness._context_automation.begin_maintenance("s-1").release()


def test_context_overflow_without_automation_is_typed_and_not_retried(tmp_path):
    harness, mock_agent = _make_harness(
        tmp_path,
        session_id="s-1",
        user_id="u-1",
        max_context_tokens=2_000,
    )
    mock_agent.model.count_tokens.return_value = 100
    mock_agent.run.side_effect = ContextWindowExceededError("maximum context length exceeded")

    with pytest.raises(HarnessError) as caught:
        harness.run("continue")

    assert caught.value.code == "CONTEXT_WINDOW_EXCEEDED"
    assert mock_agent.run.call_count == 1


def test_context_overflow_stream_is_never_replayed(tmp_path):
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        max_context_tokens=2_000,
        auto_compact_context=True,
    )
    _configure_overflow_context(harness, mock_agent, tmp_path)

    def overflow_stream():
        raise ContextWindowExceededError("maximum context length exceeded")
        yield  # pragma: no cover

    mock_agent.run.return_value = overflow_stream()
    stream = harness.run("continue", stream=True)

    with pytest.raises(HarnessError) as caught:
        next(stream)

    assert caught.value.code == "CONTEXT_OVERFLOW_STREAM_UNSAFE"
    assert mock_agent.run.call_count == 1
    mock_agent.asave_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_context_overflow_inside_event_loop_requires_arun(tmp_path):
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        max_context_tokens=2_000,
        auto_compact_context=True,
    )
    _configure_overflow_context(harness, mock_agent, tmp_path)
    mock_agent.run.side_effect = ContextWindowExceededError("maximum context length exceeded")

    with pytest.raises(HarnessError) as caught:
        harness.run("continue")

    assert caught.value.code == "CONTEXT_AUTOMATION_ASYNC_REQUIRED"
    assert mock_agent.run.call_count == 1
    mock_agent.asave_session.assert_not_awaited()


def test_automatic_context_compaction_requires_budget_and_archive(tmp_path):
    archive = LocalArtifactStore(tmp_path / "artifacts")
    with pytest.raises(HarnessError) as missing_budget:
        _make_harness(
            tmp_path / "missing-budget",
            auto_compact_context=True,
            artifact_store=archive,
        )
    with pytest.raises(HarnessError) as missing_archive:
        _make_harness(
            tmp_path / "missing-archive",
            auto_compact_context=True,
            max_context_tokens=2_000,
        )

    assert missing_budget.value.code == "CONTEXT_BUDGET_REQUIRED"
    assert missing_archive.value.code == "CONTEXT_ARTIFACT_STORE_REQUIRED"


def test_context_budget_treats_an_uncreated_agno_session_as_empty(tmp_path):
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="new-session",
        user_id="u-1",
        max_context_tokens=2_000,
        auto_compact_context=True,
    )
    mock_agent.get_chat_history.side_effect = Exception("Session not found")

    budget = harness.inspect_context_budget()

    assert budget is not None
    assert budget.used_tokens == 0
    assert budget.action.value == "none"


def test_context_budget_does_not_hide_agno_database_failure(tmp_path):
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="new-session",
        user_id="u-1",
        max_context_tokens=2_000,
        auto_compact_context=True,
    )
    mock_agent.get_chat_history.side_effect = RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        harness.inspect_context_budget()


def test_context_automation_config_is_snapshotted_and_explicit_arg_wins(tmp_path):
    archive = LocalArtifactStore(tmp_path / "artifacts")
    config = HarnessConfig(max_context_tokens=2_000, auto_compact_context=True)
    configured, _ = _make_harness(
        tmp_path / "configured",
        config=config,
        artifact_store=archive,
    )
    overridden, _ = _make_harness(
        tmp_path / "overridden",
        config=config,
        artifact_store=archive,
        auto_compact_context=False,
        max_context_tokens=4_000,
    )

    assert configured._max_context_tokens == 2_000
    assert configured._auto_compact_context is True
    assert configured._spec.settings["context"]["auto_compact"] is True
    assert overridden._max_context_tokens == 4_000
    assert overridden._auto_compact_context is False


@pytest.mark.asyncio
async def test_auto_context_stays_read_only_below_compaction_threshold(tmp_path):
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        max_context_tokens=2_000,
        auto_compact_context=True,
    )
    mock_agent.model.count_tokens.return_value = 1_799
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="ok"))
    mock_agent.asave_session = AsyncMock()

    result = await harness.arun("continue")

    assert result.content == "ok"
    mock_agent.asave_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_context_compacts_at_threshold_then_releases_below_hysteresis(tmp_path):
    session = _stored_compaction_session()
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        max_context_tokens=2_000,
        auto_compact_context=True,
    )
    mock_agent.model.count_tokens.side_effect = [1_800, 1_800, 1_200]
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="ok"))
    mock_agent.aget_session = AsyncMock(return_value=session)
    mock_agent.asave_session = AsyncMock()
    harness.summarize_session = AsyncMock(return_value="Incident response remains active.")
    triggered: list[object] = []
    harness.add_lifecycle_hook(
        "session.compaction.auto_triggered",
        lambda request, context: triggered.append((request, context)),
    )

    assert (await harness.arun("continue")).content == "ok"
    assert (await harness.arun("continue again")).content == "ok"

    assert mock_agent.asave_session.await_count == 1
    checkpoint = mock_agent.asave_session.await_args.args[0].runs[0]
    assert checkpoint.run_id.startswith("context-compaction-")
    assert len(triggered) == 1
    # Verified continuation synthesis, preservation flush, and two caller runs.
    assert mock_agent.arun.await_count == 4


@pytest.mark.asyncio
async def test_emergency_auto_context_uses_no_extra_model_calls(tmp_path):
    session = _stored_compaction_session()
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        max_context_tokens=2_000,
        auto_compact_context=True,
    )
    mock_agent.model.count_tokens.return_value = 1_940
    mock_agent.get_chat_history.return_value = [
        SimpleNamespace(role="user", content="Keep investigating the outage."),
        SimpleNamespace(role="assistant", content="The database is the current lead."),
    ]
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="ok"))
    mock_agent.aget_session = AsyncMock(return_value=session)
    mock_agent.asave_session = AsyncMock()
    harness.summarize_session = AsyncMock(side_effect=AssertionError("must not summarize"))

    result = await harness.arun("continue")

    assert result.content == "ok"
    assert mock_agent.arun.await_count == 1
    harness.summarize_session.assert_not_awaited()
    saved = mock_agent.asave_session.await_args.args[0]
    assert saved.summary.summary.startswith("Emergency context checkpoint.")


def test_automatic_summary_is_bounded_to_release_without_dropping_checkpoint() -> None:
    live_user = "checkpoint identifiers and latest intent"
    target = 220
    fitted = AgentHarness._fit_summary_to_release_boundary(
        "provider narrative " * 1_000,
        live_user=live_user,
        target_release_tokens=target,
    )
    counter = DeterministicTokenCounter()
    total = counter.count(f"user: {live_user}") + counter.count(f"assistant: {fitted}")

    assert fitted.startswith("Bounded checkpoint summary;")
    assert total <= target


def test_automatic_summary_refuses_when_required_checkpoint_alone_exceeds_release() -> None:
    with pytest.raises(ContextQualityError) as caught:
        AgentHarness._fit_summary_to_release_boundary(
            "short summary",
            live_user="required checkpoint " * 1_000,
            target_release_tokens=100,
        )

    assert caught.value.details["reason"] == "replacement_above_hysteresis_release"
    assert caught.value.details["required_checkpoint_only"] is True


@pytest.mark.asyncio
async def test_auto_context_rejects_replacement_above_release_boundary(tmp_path):
    session = _stored_compaction_session()
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        max_context_tokens=500,
        auto_compact_context=True,
    )
    mock_agent.model.count_tokens.return_value = 450
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="flushed"))
    mock_agent.aget_session = AsyncMock(return_value=session)
    mock_agent.asave_session = AsyncMock()
    harness.summarize_session = AsyncMock(return_value="Too little context reduction.")

    with pytest.raises(ContextQualityError) as caught:
        await harness.arun("continue")

    assert caught.value.details["reason"] == "replacement_above_hysteresis_release"
    mock_agent.asave_session.assert_not_awaited()
    harness._context_automation.begin_maintenance("s-1").release()


@pytest.mark.asyncio
async def test_context_maintenance_cannot_race_open_stream_and_is_session_local(tmp_path):
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
    )
    mock_agent.run.return_value = iter(())

    stream = harness.run("stream", stream=True)
    with pytest.raises(HarnessError) as caught:
        await harness.compact_session(summary="Must not race the stream.")
    other = harness._context_automation.begin_maintenance("s-2")
    other.release()

    assert caught.value.code == "CONTEXT_SESSION_BUSY"
    stream.close()
    harness._context_automation.begin_maintenance("s-1").release()


def test_cross_process_context_writer_blocks_harness_run_before_model_dispatch(tmp_path):
    provider = LocalFileContextLockProvider(tmp_path / "locks")
    scope = ContextScope(session_id="s-1", user_id="u-1", tenant_id="t-1")
    writer = provider.acquire(scope, mode=ContextLockMode.EXCLUSIVE)
    harness, mock_agent = _make_harness(
        tmp_path / "harness",
        session_id="s-1",
        user_id="u-1",
        tenant_id="t-1",
        context_lock_provider=provider,
    )
    assert harness._spec.settings["context"]["lock_provider_digest"] == (provider.identity_digest)
    assert any(
        item.resource_id == "context_lock_provider" for item in harness.runtime_manifest().resources
    )

    with pytest.raises(ContextLockUnavailableError):
        harness.run("must not dispatch")

    mock_agent.run.assert_not_called()
    writer.release()


def test_cross_process_reader_makes_overflow_compaction_fail_before_session_write(tmp_path):
    provider = LocalFileContextLockProvider(tmp_path / "locks")
    first, first_agent = _make_harness(
        tmp_path / "first",
        session_id="s-1",
        user_id="u-1",
        context_lock_provider=provider,
    )
    first_agent.run.return_value = iter(())
    open_stream = first.run("hold shared session activity", stream=True)

    second, second_agent = _make_harness(
        tmp_path / "second",
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        max_context_tokens=2_000,
        auto_compact_context=True,
        context_lock_provider=provider,
    )
    _configure_overflow_context(second, second_agent, tmp_path)
    second_agent.run.side_effect = ContextWindowExceededError("maximum context length exceeded")

    with pytest.raises(ContextLockUnavailableError):
        second.run("overflow while another process-equivalent reader is active")

    assert second_agent.run.call_count == 1
    second_agent.asave_session.assert_not_awaited()
    open_stream.close()


@pytest.mark.asyncio
async def test_cross_process_lock_loss_refuses_final_compaction_write(tmp_path):
    class LostLease:
        def __init__(self, scope: ContextScope) -> None:
            self.scope = scope
            self.mode = ContextLockMode.EXCLUSIVE

        def validate(self) -> None:
            raise ContextLockLostError(self.scope, mode=self.mode)

        def upgrade(self) -> None:
            self.mode = ContextLockMode.EXCLUSIVE

        def release(self) -> None:
            return None

    class LostProvider:
        @property
        def identity_digest(self) -> str:
            return "sha256:" + "c" * 64

        def acquire(self, scope: ContextScope, *, mode: ContextLockMode):
            assert mode is ContextLockMode.EXCLUSIVE
            return LostLease(scope)

    harness, mock_agent = _make_harness(
        tmp_path,
        on_compaction=None,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        context_lock_provider=LostProvider(),
    )
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="flushed"))
    mock_agent.aget_session = AsyncMock(return_value=_stored_compaction_session())
    mock_agent.asave_session = AsyncMock()

    with pytest.raises(ContextLockLostError):
        await harness.compact_session(summary="Session summary text")

    mock_agent.asave_session.assert_not_awaited()


def test_sync_auto_context_compacts_outside_an_event_loop(tmp_path):
    session = _stored_compaction_session()
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        max_context_tokens=2_000,
        auto_compact_context=True,
    )
    mock_agent.model.count_tokens.side_effect = [1_800, 1_800]
    mock_agent.run.return_value = SimpleNamespace(content="ok")
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="flushed"))
    mock_agent.aget_session = AsyncMock(return_value=session)
    mock_agent.asave_session = AsyncMock()
    harness.summarize_session = AsyncMock(return_value="Incident response remains active.")

    assert harness.run("continue").content == "ok"
    assert mock_agent.asave_session.await_count == 1


@pytest.mark.asyncio
async def test_sync_auto_context_inside_event_loop_requires_arun(tmp_path):
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        max_context_tokens=2_000,
        auto_compact_context=True,
    )
    mock_agent.model.count_tokens.return_value = 1_800

    with pytest.raises(HarnessError) as caught:
        harness.run("continue")

    assert caught.value.code == "CONTEXT_AUTOMATION_ASYNC_REQUIRED"
    mock_agent.run.assert_not_called()


@pytest.mark.asyncio
async def test_on_compaction_callback_fires(tmp_path):
    """on_compaction is called with the summary after compact_session()."""
    received: list[str] = []

    async def on_compaction(summary: str) -> None:
        received.append(summary)

    harness, mock_agent = _make_harness(
        tmp_path,
        on_compaction=on_compaction,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
    )
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="flushed"))
    mock_agent.aget_session = AsyncMock(return_value=_stored_compaction_session())
    mock_agent.asave_session = AsyncMock()

    await harness.compact_session(summary="Session summary text")

    assert received == ["Session summary text"]
    saved = mock_agent.asave_session.await_args.args[0]
    assert len(saved.runs) == 1
    assert saved.runs[0].run_id.startswith("context-compaction-")
    assert saved.session_data["_agnoclaw_context_manifest"]["revision"] == 1


@pytest.mark.asyncio
async def test_summarize_only_does_not_fire_compaction_callback(tmp_path):
    """Summary-only compatibility does not claim that replacement occurred."""
    received: list[str] = []

    async def on_compaction(summary: str) -> None:
        received.append(summary)

    harness, mock_agent = _make_harness(tmp_path, on_compaction=on_compaction)
    mock_agent.get_chat_history.return_value = []
    mock_agent.session_summary_manager = None

    assert await harness.summarize_session() is None

    assert received == []


@pytest.mark.asyncio
async def test_summary_only_targets_the_explicit_trusted_session(tmp_path):
    harness, mock_agent = _make_harness(tmp_path, session_id="default", user_id="u-1")
    mock_agent.session_summary_manager = None
    mock_agent.get_chat_history.return_value = [
        SimpleNamespace(role="user", content="Scoped request"),
    ]
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="Scoped summary"))

    summary = await harness.summarize_session(session_id="target", user_id="u-1")

    assert summary == "Scoped summary"
    mock_agent.get_chat_history.assert_called_with("target")
    assert mock_agent.arun.await_args.kwargs["session_id"] == "target"
    assert mock_agent.arun.await_args.kwargs["user_id"] == "u-1"


@pytest.mark.asyncio
async def test_on_compaction_callback_exception_is_swallowed(tmp_path):
    """compact_session should not raise when on_compaction callback fails."""

    async def on_compaction(summary: str) -> None:
        del summary
        raise RuntimeError("callback failed")

    harness, mock_agent = _make_harness(
        tmp_path,
        on_compaction=on_compaction,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
    )
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="flushed"))
    mock_agent.aget_session = AsyncMock(return_value=_stored_compaction_session())
    mock_agent.asave_session = AsyncMock()

    await harness.compact_session(summary="Session summary text")


@pytest.mark.asyncio
async def test_compacted_context_is_searchable_and_selectively_rehydratable(tmp_path):
    session = _stored_compaction_session()
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
    )
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="flushed"))
    mock_agent.aget_session = AsyncMock(return_value=session)
    mock_agent.asave_session = AsyncMock()

    checkpoint = await harness.compact_session(summary="Incident response remains active.")
    hits = await harness.search_session_context("incident", limit=5)
    restored = await harness.rehydrate_session_context([hits[0].item_id])
    injected = await harness.rehydrate_session_context([hits[0].item_id], inject=True)

    assert checkpoint.saved_tokens > 0
    assert hits[0].source.value == "trajectory"
    assert restored.items[0].item_id == hits[0].item_id
    assert restored.injected is False
    assert injected.injected is True
    assert session.runs[-1].run_id.startswith("context-rehydration-")
    assert session.runs[-1].metadata["agnoclaw_context_rehydration"]["sources"] == [
        "trajectory",
        "artifact",
    ]


@pytest.mark.asyncio
async def test_compaction_retains_typed_continuation_fields_in_live_and_archive(tmp_path):
    session = _stored_compaction_session()
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
    )
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="flushed"))
    mock_agent.aget_session = AsyncMock(return_value=session)
    mock_agent.asave_session = AsyncMock()
    record = ContextContinuationRecord(
        summary="Incident recovery remains active.",
        goal="continuation-goal-needle",
        plan=("continuation-plan-needle",),
        progress=("continuation-progress-needle",),
        decisions=("continuation-decision-needle",),
        approvals=("continuation-approval-needle",),
        open_questions=("continuation-question-needle",),
        tests=("continuation-test-needle",),
        files=("continuation-file-needle",),
        citations=("continuation-citation-needle",),
    )

    with pytest.raises(HarnessError) as conflict:
        await harness.compact_session(summary="ambiguous", continuation=record)
    assert conflict.value.code == "CONTEXT_CONTINUATION_CONFLICT"
    mock_agent.arun.assert_not_awaited()

    checkpoint = await harness.compact_session(continuation=record)
    manifest = await harness.context_manifest()
    live_checkpoint = session.runs[0].messages[0].content
    expected = {
        "continuation-goal-needle": ContextItemKind.GOAL,
        "continuation-plan-needle": ContextItemKind.PLAN,
        "continuation-progress-needle": ContextItemKind.PROGRESS,
        "continuation-decision-needle": ContextItemKind.DECISION,
        "continuation-approval-needle": ContextItemKind.APPROVAL,
        "continuation-question-needle": ContextItemKind.OPEN_QUESTION,
        "continuation-test-needle": ContextItemKind.TEST_RESULT,
        "continuation-file-needle": ContextItemKind.FILE_REFERENCE,
        "continuation-citation-needle": ContextItemKind.CITATION,
    }

    assert len(checkpoint.retained_item_ids) == 10  # latest intent plus nine typed fields
    assert checkpoint.summary == record.summary
    assert manifest.segments[0].source_tokens == checkpoint.before_tokens
    assert manifest.segments[0].total_tokens > manifest.segments[0].source_tokens
    for needle, kind in expected.items():
        hits = await harness.search_session_context(needle, limit=2)
        assert len(hits) == 1
        assert hits[0].kind is kind
        assert needle in live_checkpoint
        restored = await harness.rehydrate_session_context([hits[0].item_id])
        assert restored.items[0].content == needle
        assert restored.items[0].provenance["continuation"]["record_id"] == record.record_id


@pytest.mark.asyncio
async def test_automatic_compaction_captures_source_bound_initial_goal(
    tmp_path,
):
    session = _stored_compaction_session()
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
    )
    mock_agent.aget_session = AsyncMock(return_value=session)
    mock_agent.asave_session = AsyncMock()
    scope = ContextScope(session_id="s-1", user_id="u-1")

    checkpoint = await harness._run_context_compaction(
        summary="The incident investigation remains active.",
        continuation=None,
        scope=scope,
        skip_memory_flush=True,
        capture_initial_goal=True,
    )
    manifest = await harness.context_manifest()
    retained = await harness._context_archive.load_latest_retained_items(
        manifest,
        scope=scope,
    )
    goals = [item for item in retained if item.kind is ContextItemKind.GOAL]

    assert len(goals) == 1
    assert goals[0].content == ("Investigate the incident " * 100).strip()
    provenance = goals[0].provenance["continuation"]
    assert provenance["origin"] == "source_bound_initial_user_goal"
    assert provenance["source"]["source_ordinal"] == 0
    assert provenance["source"]["normalization"] == "trim_outer_whitespace"
    assert provenance["source"]["source_content_digest"].startswith("sha256:")
    checkpoint_metadata = session.runs[0].metadata["agnoclaw_context_checkpoint"]
    assert checkpoint_metadata["continuation_record_id"] == provenance["record_id"]
    assert checkpoint_metadata["continuation_origin"] == "source_bound_initial_user_goal"
    assert checkpoint_metadata["continuation_entry_count"] == 1
    assert goals[0].item_id in checkpoint.retained_item_ids


@pytest.mark.asyncio
async def test_automatic_compaction_archives_generated_verified_continuation(tmp_path):
    session = _stored_compaction_session()
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
    )
    mock_agent.aget_session = AsyncMock(return_value=session)
    mock_agent.asave_session = AsyncMock()
    generated = ContextContinuationRecord(
        summary="Incident evidence collection remains active.",
        goal="Investigate the incident safely.",
        plan=("Inspect the archived evidence.",),
    )
    harness._generate_verified_continuation = AsyncMock(
        return_value=(
            generated.summary,
            generated,
            {
                ("goal", 0): {"extraction": "deterministic_initial_goal_v1"},
                ("plan", 0): {"extraction": "model_proposed_exact_span_v1"},
            },
        )
    )
    harness.summarize_session = AsyncMock(return_value="must-not-be-used")
    scope = ContextScope(session_id="s-1", user_id="u-1")

    checkpoint = await harness._run_context_compaction(
        summary=None,
        continuation=None,
        scope=scope,
        skip_memory_flush=True,
        capture_initial_goal=True,
    )
    manifest = await harness.context_manifest()
    retained = await harness._context_archive.load_latest_retained_items(
        manifest,
        scope=scope,
    )
    structured = [item for item in retained if "continuation" in item.provenance]

    assert checkpoint.summary == generated.summary
    assert harness._generate_verified_continuation.await_count == 1
    harness.summarize_session.assert_not_awaited()
    assert {item.content for item in structured} == {
        "Investigate the incident safely.",
        "Inspect the archived evidence.",
    }
    assert all(
        item.provenance["continuation"]["origin"] == "model_verified_exact_span"
        for item in structured
    )


@pytest.mark.asyncio
async def test_compaction_carries_reviewed_continuation_and_explicit_record_supersedes(
    tmp_path,
):
    from agno.models.message import Message
    from agno.run.agent import RunOutput
    from agno.run.base import RunStatus

    session = _stored_compaction_session()
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
    )
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="flushed"))
    mock_agent.aget_session = AsyncMock(return_value=session)
    mock_agent.asave_session = AsyncMock()
    original = ContextContinuationRecord(
        summary="Original structured state.",
        goal="preserve-reviewed-goal",
        approvals=("preserve-reviewed-approval",),
        tests=("preserve-reviewed-test",),
    )

    await harness.compact_session(continuation=original)
    first_manifest = await harness.context_manifest()
    first_retained = await harness._context_archive.load_latest_retained_items(
        first_manifest,
        scope=ContextScope(session_id="s-1", user_id="u-1"),
    )
    first_structured = {
        item.content: item.item_id for item in first_retained if "continuation" in item.provenance
    }
    session.runs.append(
        RunOutput(
            run_id="run-after-first-compaction",
            agent_id="agnoclaw",
            session_id="s-1",
            user_id="u-1",
            content="New evidence " * 200,
            messages=[
                Message(role="user", content="Continue with the new evidence " * 100),
                Message(role="assistant", content="New evidence verified " * 100),
            ],
            status=RunStatus.completed,
        )
    )

    await harness.compact_session(summary="Updated narrative state.")
    second_manifest = await harness.context_manifest()
    second_retained = await harness._context_archive.load_latest_retained_items(
        second_manifest,
        scope=ContextScope(session_id="s-1", user_id="u-1"),
    )
    carried = [
        item
        for item in second_retained
        if item.kind
        in {
            ContextItemKind.GOAL,
            ContextItemKind.APPROVAL,
            ContextItemKind.TEST_RESULT,
        }
    ]

    assert {item.content for item in carried} == set(first_structured)
    assert all(
        item.provenance["continuation"]["origin"] == "checkpoint_carry_forward" for item in carried
    )
    assert {item.provenance["continuation"]["source"]["source_item_id"] for item in carried} == set(
        first_structured.values()
    )
    assert "preserve-reviewed-goal" in session.runs[0].messages[0].content
    assert "preserve-reviewed-approval" in session.runs[0].messages[0].content

    session.runs.append(
        RunOutput(
            run_id="run-before-supersession",
            agent_id="agnoclaw",
            session_id="s-1",
            user_id="u-1",
            content="Supersession evidence " * 200,
            messages=[
                Message(role="user", content="Adopt the replacement goal " * 100),
                Message(role="assistant", content="Replacement reviewed " * 100),
            ],
            status=RunStatus.completed,
        )
    )
    replacement = ContextContinuationRecord(
        summary="Replacement state.",
        goal="replacement-reviewed-goal",
    )
    await harness.compact_session(continuation=replacement)
    third_manifest = await harness.context_manifest()
    third_retained = await harness._context_archive.load_latest_retained_items(
        third_manifest,
        scope=ContextScope(session_id="s-1", user_id="u-1"),
    )
    active_structured = [item for item in third_retained if "continuation" in item.provenance]

    assert [item.content for item in active_structured] == ["replacement-reviewed-goal"]
    assert active_structured[0].provenance["continuation"]["origin"] == "explicit"
    assert "preserve-reviewed-goal" not in session.runs[0].messages[0].content
    assert "replacement-reviewed-goal" in session.runs[0].messages[0].content


@pytest.mark.asyncio
async def test_continuation_entries_cannot_inflate_source_token_savings(tmp_path):
    from agno.models.message import Message

    session = _stored_compaction_session()
    session.runs[0].messages = [
        Message(role="user", content="short intent"),
        Message(role="assistant", content="short response"),
    ]
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
    )
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="flushed"))
    mock_agent.aget_session = AsyncMock(return_value=session)
    mock_agent.asave_session = AsyncMock()
    continuation = ContextContinuationRecord(
        summary="Short summary.",
        goal="continuation-derived-state-" + ("x" * 15_000),
    )

    with pytest.raises(ContextQualityError) as caught:
        await harness.compact_session(continuation=continuation)

    assert caught.value.details["reason"] == "replacement_does_not_reduce_context"
    assert caught.value.details["before_tokens"] < continuation.entry_count * 100
    mock_agent.asave_session.assert_not_awaited()


def test_continuation_live_index_is_priority_ordered_and_character_bounded(tmp_path):
    harness, _mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
    )
    assert harness._context_archive is not None
    continuation = ContextContinuationRecord(
        summary="Bounded continuation.",
        goal="priority-goal-must-survive",
        progress=tuple(f"progress-{index:02d}-" + ("x" * 900) for index in range(64)),
    )
    items = harness._context_archive.items_from_messages(
        [SimpleNamespace(role="user", content="latest intent")],
        scope=ContextScope(session_id="s-1", user_id="u-1"),
        continuation=continuation,
    )

    index = harness._context_invariant_index(items)

    assert len(index) <= 4_000
    assert len(index.splitlines()) <= 16
    assert "priority-goal-must-survive" in index
    assert "progress-63" in index  # most recent low-priority evidence wins its class


@pytest.mark.asyncio
async def test_compaction_retains_spilled_output_index_search_and_provenance(tmp_path):
    from agno.models.message import Message

    session = _stored_compaction_session()
    artifact_id = "artifact:v1:" + ":".join(("a" * 64, "b" * 64, "c" * 64))
    spill = {
        "type": "agnoclaw.spilled_output",
        "id": artifact_id,
        "artifact": {
            "artifact_id": artifact_id,
            "checksum": "sha256:" + "d" * 64,
            "media_type": "application/json",
            "size_bytes": 5_002,
        },
        "rendered_chars": 5_000,
        "preview": "stored-output-needle",
        "read": {
            "tool": "read_spilled_output",
            "artifact_id": artifact_id,
            "offset": 0,
        },
    }
    messages = list(session.runs[0].messages or [])
    messages.insert(
        1,
        Message(
            role="tool",
            tool_name="inventory",
            content=json.dumps(spill, separators=(",", ":"), sort_keys=True),
        ),
    )
    session.runs[0].messages = messages
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
    )
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="flushed"))
    mock_agent.aget_session = AsyncMock(return_value=session)
    mock_agent.asave_session = AsyncMock()

    checkpoint = await harness.compact_session(summary="Incident response remains active.")
    manifest = await harness.context_manifest()
    hits = await harness.search_session_context("stored-output-needle")
    restored = await harness.rehydrate_session_context([hits[0].item_id])
    live_checkpoint = session.runs[0].messages[0].content

    assert len(checkpoint.retained_item_ids) == 2
    assert manifest.checkpoints[0].retained_item_ids == checkpoint.retained_item_ids
    assert artifact_id in live_checkpoint
    assert "read_tool=read_spilled_output" in live_checkpoint
    assert hits[0].kind is ContextItemKind.ARTIFACT_REFERENCE
    assert restored.items[0].provenance["spilled_output"]["artifact_id"] == artifact_id


@pytest.mark.asyncio
async def test_context_admin_identity_filters_cannot_override_trusted_scope(tmp_path):
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        tenant_id="t-1",
    )

    with pytest.raises(HarnessError) as conflict:
        await harness.search_session_context("incident", user_id="other")

    assert conflict.value.code == "CONTEXT_IDENTITY_CONFLICT"
    mock_agent.aget_session.assert_not_called()


@pytest.mark.asyncio
async def test_compaction_replacement_preserves_provable_tenant_ownership(tmp_path):
    session = _stored_compaction_session()
    session.runs[0].metadata = {
        "_agnoclaw_context": {
            "tenant_id": "t-1",
            "user_id": "u-1",
            "session_id": "s-1",
        }
    }
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
        tenant_id="t-1",
    )
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="flushed"))
    mock_agent.aget_session = AsyncMock(return_value=session)
    mock_agent.asave_session = AsyncMock()

    await harness.compact_session(summary="Tenant incident response remains active.")
    manifest = await harness.context_manifest()

    assert manifest.scope.tenant_id == "t-1"
    assert session.runs[0].metadata["_agnoclaw_context"]["tenant_id"] == "t-1"


@pytest.mark.asyncio
async def test_compaction_finishes_final_write_and_reports_cancellation_truth(tmp_path):
    session = _stored_compaction_session()
    write_started = asyncio.Event()
    allow_write = asyncio.Event()

    async def delayed_save(value) -> None:
        assert value is not session
        assert value.session_id == session.session_id
        write_started.set()
        await allow_write.wait()

    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
    )
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="flushed"))
    mock_agent.aget_session = AsyncMock(return_value=session)
    mock_agent.asave_session = AsyncMock(side_effect=delayed_save)

    task = asyncio.create_task(harness.compact_session(summary="Incident remains active."))
    await write_started.wait()
    task.cancel()
    allow_write.set()

    with pytest.raises(HarnessError) as outcome:
        await task
    assert outcome.value.code == "CONTEXT_COMPACTION_COMMITTED_AFTER_CANCELLATION"
    assert len(session.runs) == 1
    assert session.runs[0].run_id.startswith("context-compaction-")


@pytest.mark.asyncio
async def test_failed_compaction_write_does_not_mutate_live_session_cache(tmp_path):
    session = _stored_compaction_session()
    original_run = session.runs[0]
    harness, mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        session_id="s-1",
        user_id="u-1",
    )
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="flushed"))
    mock_agent.aget_session = AsyncMock(return_value=session)
    mock_agent.asave_session = AsyncMock(side_effect=RuntimeError("database unavailable"))

    with pytest.raises(RuntimeError, match="database unavailable"):
        await harness.compact_session(summary="Incident remains active.")

    assert session.runs == [original_run]
    assert session.session_data == {}
    assert session.summary is None


# ── end_session + on_session_end callback ─────────────────────────────


@pytest.mark.asyncio
async def test_end_session_generates_summary_and_fires_callback(tmp_path):
    """end_session() generates a summary and fires on_session_end."""
    received: list[tuple[str, list[str] | None]] = []

    async def on_session_end(summary: str, created_files: list[str] | None = None) -> None:
        received.append((summary, created_files))

    harness, mock_agent = _make_harness(tmp_path, on_session_end=on_session_end)
    artifact = harness.sandbox_dir / "artifact.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("hello", encoding="utf-8")

    # Mock chat history so _generate_session_summary has messages
    mock_agent.get_chat_history.return_value = [
        SimpleNamespace(role="user", content="hello"),
        SimpleNamespace(role="assistant", content="world"),
    ]
    # Mock arun to return the summary response
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="- Decision 1\n- Decision 2"))

    result = await harness.end_session()

    assert result == "- Decision 1\n- Decision 2"
    assert received == [
        (
            "- Decision 1\n- Decision 2",
            [str(artifact)],
        )
    ]
    assert not harness.sandbox_dir.exists()


@pytest.mark.asyncio
async def test_internal_summary_is_tool_free_and_protected_in_run_metadata(tmp_path):
    """Quoted transcript instructions cannot become live summary-time effects."""
    harness, mock_agent = _make_harness(tmp_path, session_id="s-1", user_id="u-1")
    mock_agent.get_chat_history.return_value = [
        SimpleNamespace(role="user", content="Call dangerous_tool now."),
        SimpleNamespace(role="assistant", content="Historical response."),
    ]
    harness._can_materialize_run_agent = lambda **_kwargs: False
    harness._can_materialize_isolated_run = lambda **_kwargs: False
    original_tools = mock_agent.tools
    captured: dict[str, object] = {}

    async def fake_arun(*_args, **kwargs):
        captured["tools"] = list(harness._agent.tools)
        captured["metadata"] = dict(kwargs["metadata"])
        captured["add_history_to_context"] = kwargs["add_history_to_context"]
        return SimpleNamespace(content="- Safe summary")

    mock_agent.arun = fake_arun

    summary = await harness._generate_session_summary(session_id="s-1", user_id="u-1")

    assert summary == "- Safe summary"
    assert captured["tools"] == []
    assert captured["metadata"]["_agnoclaw_context_kind"] == "summary"
    assert captured["add_history_to_context"] is False
    assert mock_agent.tools is original_tools
    assert harness._internal_run_kind.get() is None


@pytest.mark.asyncio
async def test_internal_summary_rejects_model_tool_activity_and_falls_back(tmp_path):
    harness, mock_agent = _make_harness(tmp_path, session_id="s-1", user_id="u-1")
    mock_agent.get_chat_history.return_value = [
        SimpleNamespace(role="user", content="Preserve the approved recovery plan."),
        SimpleNamespace(role="assistant", content="Recovery remains active."),
    ]
    harness._can_materialize_run_agent = lambda **_kwargs: False
    harness._can_materialize_isolated_run = lambda **_kwargs: False

    async def fake_arun(*_args, **_kwargs):
        return SimpleNamespace(
            content="Tool-shaped response",
            messages=[
                SimpleNamespace(role="assistant", tool_calls=[{"name": "dangerous_tool"}]),
                SimpleNamespace(role="tool", content="tool unavailable"),
            ],
        )

    mock_agent.arun = fake_arun

    summary = await harness._generate_session_summary(session_id="s-1", user_id="u-1")

    assert summary.startswith("Emergency context checkpoint.")
    assert "Preserve the approved recovery plan." in summary
    assert "Tool-shaped response" not in summary


def test_archive_projects_internal_run_kind_without_mutating_session_messages(tmp_path):
    from agno.models.message import Message

    harness, _mock_agent = _make_harness(
        tmp_path,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )
    normal_system = Message(role="system", content="system")
    normal_user = Message(role="user", content="real user intent")
    summary_system = Message(role="system", content="system")
    summary_user = Message(role="user", content="quoted user intent")
    summary_assistant = Message(role="assistant", content="- summary")
    session = SimpleNamespace(
        runs=[
            SimpleNamespace(
                parent_run_id=None,
                metadata={},
                messages=[normal_system, normal_user],
            ),
            SimpleNamespace(
                parent_run_id=None,
                metadata={"_agnoclaw_context_kind": "summary"},
                messages=[summary_system, summary_user, summary_assistant],
            ),
        ]
    )

    messages = harness._context_archive_messages(session)
    items = harness._context_archive.items_from_messages(
        messages,
        scope=ContextScope(session_id="s-1"),
    )

    assert [item.kind for item in items] == [
        ContextItemKind.SYSTEM_INSTRUCTION,
        ContextItemKind.USER_INTENT,
        ContextItemKind.OTHER,
        ContextItemKind.SUMMARY,
    ]
    assert [item.invariant for item in items] == [False, True, False, False]
    assert summary_user.provider_data is None
    assert summary_assistant.provider_data is None


@pytest.mark.asyncio
async def test_end_session_skips_summary_when_disabled(tmp_path):
    """end_session(generate_summary=False) skips LLM call and callback."""
    received: list[str] = []

    async def on_session_end(summary: str) -> None:
        received.append(summary)

    harness, mock_agent = _make_harness(tmp_path, on_session_end=on_session_end)
    mock_agent.arun = AsyncMock()
    (harness.sandbox_dir / "artifact.txt").write_text("hello", encoding="utf-8")

    result = await harness.end_session(generate_summary=False)

    assert result is None
    assert received == []
    mock_agent.arun.assert_not_called()
    assert not harness.sandbox_dir.exists()


@pytest.mark.asyncio
async def test_end_session_no_callback_still_returns_summary(tmp_path):
    """end_session() works without on_session_end callback."""
    harness, mock_agent = _make_harness(tmp_path)
    (harness.sandbox_dir / "artifact.txt").write_text("hello", encoding="utf-8")

    mock_agent.get_chat_history.return_value = [
        SimpleNamespace(role="user", content="hi"),
    ]
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="summary"))

    result = await harness.end_session()

    assert result == "summary"
    assert not harness.sandbox_dir.exists()


@pytest.mark.asyncio
async def test_end_session_callback_exception_is_swallowed(tmp_path):
    """end_session should still return summary when callback raises."""

    async def on_session_end(summary: str) -> None:
        del summary
        raise RuntimeError("callback failed")

    harness, mock_agent = _make_harness(tmp_path, on_session_end=on_session_end)
    (harness.sandbox_dir / "artifact.txt").write_text("hello", encoding="utf-8")
    mock_agent.get_chat_history.return_value = [
        SimpleNamespace(role="user", content="hi"),
    ]
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="summary"))

    result = await harness.end_session()

    assert result == "summary"
    assert not harness.sandbox_dir.exists()


@pytest.mark.asyncio
async def test_end_session_supports_legacy_summary_only_callback(tmp_path):
    received: list[str] = []

    async def on_session_end(summary: str) -> None:
        received.append(summary)

    harness, mock_agent = _make_harness(tmp_path, on_session_end=on_session_end)
    mock_agent.get_chat_history.return_value = [
        SimpleNamespace(role="user", content="hi"),
    ]
    mock_agent.arun = AsyncMock(return_value=SimpleNamespace(content="summary"))

    result = await harness.end_session()

    assert result == "summary"
    assert received == ["summary"]
