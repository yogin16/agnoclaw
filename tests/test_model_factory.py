"""Public fresh-model factory contracts for explicit runtime profiles."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from agno.models.base import Model
from agno.models.response import ModelResponse

from agnoclaw import (
    AgentHarness,
    AgnoModelFactory,
    HarnessConfig,
    HarnessError,
    LocalArtifactStore,
    SQLiteRuntimeStore,
)
from agnoclaw.runtime import RunWaitError

_DIGEST = "sha256:" + "1" * 64


@dataclass
class _TrackingModel(Model):
    closed: int = 0

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        del args, kwargs
        return ModelResponse(content="factory-ready")

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return self.invoke(*args, **kwargs)

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        yield self.invoke(*args, **kwargs)

    async def ainvoke_stream(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[ModelResponse]:
        yield self.invoke(*args, **kwargs)

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        del kwargs
        return ModelResponse(content=str(response))

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return ModelResponse(content=str(response))

    def close(self) -> None:
        self.closed += 1


def _stores(root: Path) -> tuple[SQLiteRuntimeStore, LocalArtifactStore]:
    return SQLiteRuntimeStore(root / "runtime.db"), LocalArtifactStore(root / "artifacts")


def test_factory_validates_digest_result_and_declared_identity() -> None:
    with pytest.raises(HarnessError) as digest_error:
        AgnoModelFactory(model_id="model", implementation_digest="not-a-digest", factory=lambda: 1)  # type: ignore[arg-type,return-value]
    assert digest_error.value.code == "MODEL_FACTORY_DIGEST_INVALID"

    invalid_result = AgnoModelFactory(
        model_id="model",
        implementation_digest=_DIGEST,
        factory=lambda: object(),  # type: ignore[arg-type,return-value]
    )
    with pytest.raises(HarnessError) as result_error:
        invalid_result.create()
    assert result_error.value.code == "MODEL_FACTORY_RESULT_INVALID"

    mismatch = AgnoModelFactory(
        model_id="declared",
        implementation_digest=_DIGEST,
        factory=lambda: _TrackingModel(id="actual"),
    )
    with pytest.raises(HarnessError) as identity_error:
        mismatch.create()
    assert identity_error.value.code == "MODEL_FACTORY_IDENTITY_MISMATCH"

    missing_identity = AgnoModelFactory(
        model_id="declared",
        implementation_digest=_DIGEST,
        factory=lambda: _TrackingModel(id=""),
    )
    with pytest.raises(HarnessError) as missing_identity_error:
        missing_identity.create()
    assert missing_identity_error.value.code == "MODEL_FACTORY_IDENTITY_MISMATCH"


@pytest.mark.asyncio
async def test_durable_factory_materializes_fresh_models_and_closes_them(tmp_path: Path) -> None:
    created: list[_TrackingModel] = []

    def create_model() -> _TrackingModel:
        model = _TrackingModel(id="journey-model", provider="custom")
        created.append(model)
        return model

    factory = AgnoModelFactory(
        model_id="journey-model",
        provider="custom",
        implementation_digest=_DIGEST,
        factory=create_model,
    )
    runtime, artifacts = _stores(tmp_path)
    harness = AgentHarness(
        factory,
        config=HarnessConfig.durable(storage={"sqlite_path": str(tmp_path / "agno.db")}),
        workspace_dir=tmp_path / "workspace",
        include_default_tools=False,
        runtime_store=runtime,
        artifact_store=artifacts,
    )
    try:
        first = await harness.start("first", session_id="session-one")
        second = await harness.start("second", session_id="session-two")
        assert (await first.wait()).content == "factory-ready"
        assert (await second.wait()).content == "factory-ready"

        manifest = harness.runtime_manifest().to_dict()
        model_resource = next(
            item for item in manifest["resources"] if item["resource_id"] == "model"
        )
        expected_type = (
            f"{__name__}.test_durable_factory_materializes_fresh_models_and_closes_them"
            ".<locals>.create_model"
        )
        assert model_resource == {
            "resource_id": "model",
            "resource_type": expected_type,
            "trust": "factory",
            "lifetime": "run",
            "concurrency": "isolated",
            "recovery": "recreatable",
        }
        assert harness.model_name == "custom:journey-model"
        assert harness._spec.settings["model_factory_digest"] == _DIGEST
        assert len({id(model) for model in created}) == 3
        assert [model.closed for model in created] == [0, 1, 1]
    finally:
        await harness.aclose()
        runtime.close()
    assert [model.closed for model in created] == [1, 1, 1]


@pytest.mark.asyncio
async def test_factory_rejects_a_shared_instance_before_model_dispatch(tmp_path: Path) -> None:
    shared = _TrackingModel(id="shared")
    factory = AgnoModelFactory(
        model_id="shared",
        implementation_digest=_DIGEST,
        factory=lambda: shared,
    )
    runtime, artifacts = _stores(tmp_path)
    harness = AgentHarness(
        factory,
        config=HarnessConfig.durable(storage={"sqlite_path": str(tmp_path / "agno.db")}),
        workspace_dir=tmp_path / "workspace",
        include_default_tools=False,
        runtime_store=runtime,
        artifact_store=artifacts,
    )
    try:
        run = await harness.start("must not dispatch")
        with pytest.raises(RunWaitError) as caught:
            await run.wait()
        assert caught.value.safe_error["code"] == "MODEL_FACTORY_SHARED_INSTANCE"
        operations = runtime.list_run_operations(str(run.run_id))
        assert operations
        assert all(record.intent.kind.value != "model" for record in operations)
    finally:
        await harness.aclose()
        runtime.close()


def test_factory_owns_provider_options_without_duplicated_authority(tmp_path: Path) -> None:
    factory = AgnoModelFactory(
        model_id="model",
        provider="custom",
        implementation_digest=_DIGEST,
        factory=lambda: _TrackingModel(id="model", provider="custom"),
    )
    with pytest.raises(HarnessError) as provider_error:
        AgentHarness(factory, provider="other", workspace_dir=tmp_path / "provider")
    assert provider_error.value.code == "MODEL_FACTORY_PROVIDER_CONFLICT"

    with pytest.raises(HarnessError) as option_error:
        AgentHarness(factory, effort="high", workspace_dir=tmp_path / "effort")
    assert option_error.value.code == "MODEL_FACTORY_OPTION_CONFLICT"
