#!/usr/bin/env python3
"""Smoke the provider-neutral wheel without relying on any optional SDK."""

from __future__ import annotations

import asyncio
import importlib.metadata
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

# This probe expresses its contracts as assert statements; under -O/-OO they
# would vanish and the smoke would pass vacuously. Refuse optimized runs.
if sys.flags.optimize:
    raise SystemExit("smoke_core_install.py must run without -O/-OO")

from agno.agent import Agent
from agno.models.base import Model
from agno.models.response import ModelResponse
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from agnoclaw import (
    DOCKER_EVALUATION_POLICY_VERSION,
    OPERATION_RECONCILIATION_EVIDENCE_PURPOSE,
    PROCESS_EVALUATION_PROTOCOL_VERSION,
    AgentHarness,
    AgnoEvaluationSubject,
    DockerEvaluationPolicy,
    EvaluationArchiveQuery,
    EvaluationCase,
    EvaluationCaseExposure,
    EvaluationCorpusEntry,
    EvaluationCorpusManifest,
    EvaluationGatePolicy,
    EvaluationSlice,
    LearningOwner,
    LearningReconciliationWorkerConfig,
    ModelProviderDependencyError,
    OperationReconciliationObservation,
    OperationReconciliationVerdict,
    ProcessEvaluationSubject,
    RunReconciliationRequiredError,
    SQLiteLearningLedger,
)
from agnoclaw.config import HarnessConfig, StorageConfig
from agnoclaw.output_spill import model_output
from agnoclaw.runtime import (
    RUNTIME_SCHEMA_VERSION,
    ArtifactScope,
    HarnessError,
    LocalArtifactStore,
    RuntimeSchedulerBackend,
    SchedulerJob,
    SQLiteRuntimeStore,
)
from agnoclaw.runtime.tool_ingress import builtin_effect, toolkit_functions
from agnoclaw.tools import get_default_tools


class HostModel(Model):
    """Minimal host-supplied model proving core construction is provider-neutral."""

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return ModelResponse(content="host-model")

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return ModelResponse(content="host-model")

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        yield ModelResponse(content="host-model")

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        yield ModelResponse(content="host-model")

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return ModelResponse(content=str(response))

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return ModelResponse(content=str(response))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="agnoclaw-core-smoke-") as directory:
        root = Path(directory)
        config = HarnessConfig(
            workspace_dir=str(root / "workspace"),
            storage=StorageConfig(sqlite_path=str(root / "sessions.db")),
        )
        with AgentHarness(
            model=HostModel(id="host-model"),
            config=config,
            include_default_tools=False,
        ) as harness:
            assert harness.model_name == "host-model"
            assert config.permission_durable_approvals is True
        assert RUNTIME_SCHEMA_VERSION == 12
        assert OPERATION_RECONCILIATION_EVIDENCE_PURPOSE == (
            "operation.reconciliation.evidence"
        )
        assert OperationReconciliationObservation.__name__ == (
            "OperationReconciliationObservation"
        )
        assert OperationReconciliationVerdict.EFFECT_ABSENT.value == "effect_absent"
        assert issubclass(RunReconciliationRequiredError, HarnessError)
        evaluation = asyncio.run(
            AgnoEvaluationSubject(Agent(model=HostModel(id="evaluation-smoke")))(
                EvaluationCase(
                    case_id="installed-wheel-case",
                    slice=EvaluationSlice.HELD_IN,
                    task_class="installed-wheel-smoke",
                    payload="return the provider-neutral smoke response",
                )
            )
        )
        assert evaluation.output == "host-model"
        assert evaluation.tokens == 0

        async def process_smoke() -> None:
            worker = ProcessEvaluationSubject(
                (
                    sys.executable,
                    "-c",
                    "from agnoclaw import EvaluationRollout,run_process_evaluation_worker;"
                    "raise SystemExit(run_process_evaluation_worker("
                    "lambda case: EvaluationRollout(output={'case_id':case.case_id},"
                    "tokens=1,cost_usd=0.0)))",
                )
            )
            await worker.asetup()
            try:
                rollout = await worker(
                    EvaluationCase(
                        case_id="installed-process-case",
                        slice=EvaluationSlice.HELD_IN,
                        task_class="installed-process-smoke",
                        payload={"private": "must-stay-in-protocol"},
                    )
                )
            finally:
                await worker.aclose()
            assert rollout.output == {"case_id": "installed-process-case"}
            assert rollout.tokens == 1

        assert PROCESS_EVALUATION_PROTOCOL_VERSION.endswith(".v1")
        asyncio.run(process_smoke())
        docker_policy = DockerEvaluationPolicy(image="sha256:" + "d" * 64)
        assert DOCKER_EVALUATION_POLICY_VERSION.endswith(".v1")
        assert docker_policy.to_dict()["network"] == "none"
        assert docker_policy.to_dict()["root_filesystem"] == "read_only"
        assert docker_policy.digest.startswith("sha256:")
        corpus_cases = tuple(
            EvaluationCase(
                case_id=f"installed-{slice_name.value}",
                slice=slice_name,
                task_class=f"installed-{slice_name.value}",
                payload={"secret": f"wheel-secret-{slice_name.value}"},
            )
            for slice_name in EvaluationSlice
        )
        corpus_entries = tuple(
            EvaluationCorpusEntry.from_case(
                case,
                lineage_digest="sha256:" + str(index + 1) * 64,
                source_artifact_id="installed-corpus-source",
                exposure=(
                    EvaluationCaseExposure.DEVELOPMENT
                    if case.slice is EvaluationSlice.HELD_IN
                    else EvaluationCaseExposure.SEALED
                ),
            )
            for index, case in enumerate(corpus_cases)
        )
        corpus = EvaluationCorpusManifest(
            corpus_id="installed-corpus",
            version="1",
            entries=corpus_entries,
            selection_policy_digest="sha256:" + "4" * 64,
            sampling_seed_digest="sha256:" + "5" * 64,
            sealed_access_policy_digest="sha256:" + "6" * 64,
            decontamination_method_digest="sha256:" + "7" * 64,
            decontamination_artifact_id="installed-decontamination",
            curator_identity_digest="sha256:" + "8" * 64,
        )
        assert corpus.case_set_digest.startswith("sha256:")
        assert "wheel-secret" not in str(corpus.to_dict())
        assert EvaluationGatePolicy().require_governed_corpus is True
        archive_query = EvaluationArchiveQuery(limit=25)
        assert [item.value for item in archive_query.verdicts] == [
            "rejected",
            "inconclusive",
        ]
        worker_config = LearningReconciliationWorkerConfig(worker_id="installed-smoke")
        assert worker_config.heartbeat_seconds == 10

        learning_ledger = SQLiteLearningLedger(root / "learning.db")
        try:
            assert learning_ledger.schema_version == 6
            learning_owner = LearningOwner("tenant-smoke", "namespace-smoke")
            learning_lease = learning_ledger.claim_reconciliation_worker(
                owner=learning_owner,
                worker_id=worker_config.worker_id,
                lease_seconds=worker_config.lease_seconds,
            )
            assert learning_lease is not None
            assert "lease_token" not in learning_lease.to_dict()
            assert learning_ledger.release_reconciliation_worker(learning_lease) is True
        finally:
            learning_ledger.close()

        runtime_store = SQLiteRuntimeStore(root / "runtime.db")
        try:
            scheduler = RuntimeSchedulerBackend(runtime_store)
            scheduled = scheduler.upsert_job(
                SchedulerJob(
                    name="installed-smoke",
                    schedule="1h",
                    prompt="prove the installed durable scheduler surface",
                )
            )
            assert scheduled.revision == 1
            assert scheduled.next_run_at is not None
            assert scheduler.store is runtime_store
        finally:
            runtime_store.close()

        artifacts = LocalArtifactStore(root / "artifacts")
        reference = asyncio.run(
            artifacts.stage_json(
                "A" * 5_000,
                scope=ArtifactScope(run_id="installed-smoke"),
                purpose="operation_result",
            )
        )
        envelope, spilled_chars = model_output(
            "A" * 5_000,
            reference,
            maximum_inline_chars=1_024,
        )
        assert spilled_chars == 5_000
        assert envelope["id"] == reference.artifact_id
        assert "A" * 5_000 not in str(envelope)
        with AgentHarness(
            model=HostModel(id="host-model"),
            config=config,
            include_default_tools=False,
            artifact_store=artifacts,
            max_inline_output_chars=1_024,
        ) as spill_harness:
            model_tools = spill_harness.admin_harness_capabilities()["registry"]["model_tools"]
            assert any(item["reference"] == "read_spilled_output@1.0.0" for item in model_tools)

        first_party = get_default_tools(config, workspace_dir=root / "workspace")
        effects: dict[str, str] = {}
        for tool in first_party:
            functions = (
                [tool]
                if isinstance(tool, Function)
                else list(toolkit_functions(tool).values())
                if isinstance(tool, Toolkit)
                else []
            )
            for function in functions:
                effect = builtin_effect(function)
                assert effect is not None, function.name
                effects[function.name] = effect.effect_class.value
        assert effects["read_file"] == "read_only"
        assert effects["write_file"] == "non_repeatable"

        try:
            mcp_version = importlib.metadata.version("mcp")
        except importlib.metadata.PackageNotFoundError:
            pass
        else:
            assert mcp_version.startswith("2."), mcp_version
            mcp_config = config.model_copy(deep=True)
            mcp_config.mcp_servers = [{"name": "smoke", "command": ["unused"]}]
            mcp_tools = get_default_tools(mcp_config, workspace_dir=root / "workspace")
            mcp_names = {
                name
                for tool in mcp_tools
                if isinstance(tool, Toolkit)
                for name in toolkit_functions(tool)
            }
            assert {"search_mcp_tools", "call_mcp_tool"}.issubset(mcp_names)

        def legacy_tool(value: str) -> str:
            return value

        with AgentHarness(
            model=HostModel(id="host-model"),
            config=config,
            include_default_tools=False,
            tools=[legacy_tool],
        ) as compatibility:
            try:
                asyncio.run(compatibility.start("must reject opaque durable tools"))
            except HarnessError as error:
                assert error.code == "LEGACY_TOOL_DURABLE_UNSUPPORTED"
            else:  # pragma: no cover - a durable raw-tool bypass can only reach this
                raise AssertionError("core wheel allowed an opaque tools= lifecycle run")

        try:
            unexpected = AgentHarness(config=config, include_default_tools=False)
        except ModelProviderDependencyError as error:
            assert error.code == "MODEL_PROVIDER_DEPENDENCY_MISSING"
            assert error.details is not None
            assert error.details["install_extra"] == "agnoclaw[anthropic]"
        else:  # pragma: no cover - only optional-dependency leakage can reach this
            unexpected.close()
            raise AssertionError("core wheel unexpectedly contains the Anthropic SDK")

    requirements = importlib.metadata.requires("agnoclaw") or []
    core_requirements = [item for item in requirements if "extra ==" not in item]
    assert len(core_requirements) == 6, core_requirements
    print(
        "core wheel: host model, process/Docker evaluation policy, governed corpus, "
        "effects, and output spill constructed; extras are actionable"
    )


if __name__ == "__main__":
    main()
