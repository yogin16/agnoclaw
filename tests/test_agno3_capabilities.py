"""Token-free contracts for the Agno 3 capability adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from agno.models.base import Model
from agno.models.response import ModelResponse
from agno.session.agent import AgentSession

from agnoclaw.agent import AgentHarness
from agnoclaw.agno3 import LazyLocalMediaStorage, resolve_code_mode
from agnoclaw.backends import RuntimeBackend
from agnoclaw.compat import AgnoFeature, AgnoLane, inspect_agno_compatibility
from agnoclaw.config import HarnessConfig, StorageConfig
from agnoclaw.runtime import HarnessError
from agnoclaw.tools.backends import CommandResult


class OfflineModel(Model):
    """A construction-only model that makes accidental provider use fail loudly."""

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        raise AssertionError("the Agno 3 adapter tests must not call a model")

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        raise AssertionError("the Agno 3 adapter tests must not call a model")

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        raise AssertionError("the Agno 3 adapter tests must not call a model")
        yield  # pragma: no cover

    async def ainvoke_stream(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[ModelResponse]:
        raise AssertionError("the Agno 3 adapter tests must not call a model")
        yield  # pragma: no cover

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return ModelResponse(content=str(response))

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return ModelResponse(content=str(response))


class TokenFreeResponseModel(OfflineModel):
    """A deterministic local model response for real Agno persistence contracts."""

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return ModelResponse(content="offline-ok")

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return ModelResponse(content="offline-ok")


def _config(tmp_path, **overrides: Any) -> HarnessConfig:
    values: dict[str, Any] = {
        "storage": StorageConfig(sqlite_path=str(tmp_path / "sessions.db")),
        "agno_media_storage_path": str(tmp_path / "media"),
        "enable_plugins": False,
    }
    values.update(overrides)
    return HarnessConfig(**values)


def _requires_v3() -> None:
    if inspect_agno_compatibility().lane is not AgnoLane.STABLE_V3:
        pytest.skip("Agno 3 capability contract")


def test_agno3_report_exposes_adopted_capabilities() -> None:
    report = inspect_agno_compatibility()
    if report.lane is not AgnoLane.STABLE_V3:
        assert not report.has(AgnoFeature.V3_TOOL_RESULT_OFFLOADING)
        return

    assert report.has(AgnoFeature.V3_TOOL_RESULT_OFFLOADING)
    assert report.has(AgnoFeature.V3_MEDIA_OFFLOADING)
    assert report.has(AgnoFeature.V3_INCREMENTAL_HISTORY)
    assert report.has(AgnoFeature.V3_CODE_MODE)


def test_agno3_defaults_enable_result_and_lazy_media_offloading(tmp_path) -> None:
    _requires_v3()
    config = _config(tmp_path)
    harness = AgentHarness(
        model=OfflineModel(id="offline"),
        include_default_tools=False,
        config=config,
    )
    try:
        result_store = harness._agent.offload_tool_results
        assert result_store.threshold_chars == 16_000
        assert isinstance(harness._agent.media_storage, LazyLocalMediaStorage)
        assert not (tmp_path / "media").exists()
        assert harness._spec.settings["output"]["agno_tool_result_offloading"] is True
        assert harness._spec.settings["output"]["agno_media_offloading"] is True
        assert harness._spec.settings["context"]["agno_incremental_history"] is True
    finally:
        harness.close()


def test_lazy_media_storage_round_trips_without_touching_model(tmp_path) -> None:
    _requires_v3()
    storage = LazyLocalMediaStorage(tmp_path / "media")
    assert not (tmp_path / "media").exists()

    key = storage.upload("media-1", b"image-bytes", mime_type="image/png")

    assert (tmp_path / "media").is_dir()
    assert storage.exists(key)
    assert storage.download(key) == b"image-bytes"


def test_default_result_store_offloads_and_reads_large_native_tool_output(tmp_path) -> None:
    _requires_v3()
    harness = AgentHarness(
        model=OfflineModel(id="offline"),
        include_default_tools=False,
        session_id="session-1",
        config=_config(tmp_path),
    )
    payload = "large-result-line\n" * 1_100
    try:
        store = harness._agent.result_store
        assert store is not None
        envelope = store.offload_for_model(
            session_id="session-1",
            run_id="run-1",
            tool_call_id="call-1",
            tool_name="large_tool",
            tool_args={"query": "bounded"},
            output=payload,
            user_id="user-1",
        )
        refs = store.live_ids("session-1")

        assert len(envelope) < len(payload)
        assert '<result id="res_' in envelope
        assert len(refs) == 1
        assert store.payload(refs[0].result_id) == payload
    finally:
        harness.close()


def test_auto_result_offload_defers_to_compression(tmp_path) -> None:
    _requires_v3()
    harness = AgentHarness(
        model=OfflineModel(id="offline"),
        include_default_tools=False,
        config=_config(tmp_path, enable_compression=True),
    )
    try:
        assert harness._agent.offload_tool_results is None
        assert harness._spec.settings["output"]["agno_tool_result_offloading"] is False
    finally:
        harness.close()


def test_explicit_result_offload_rejects_duplicate_compression_owner(tmp_path) -> None:
    _requires_v3()
    with pytest.raises(HarnessError) as caught:
        AgentHarness(
            model=OfflineModel(id="offline"),
            include_default_tools=False,
            offload_tool_results=True,
            config=_config(tmp_path, enable_compression=True),
        )

    assert caught.value.code == "AGNO_TOOL_RESULT_OFFLOAD_CONFLICT"


def test_code_mode_collapses_tools_and_executes_in_bounded_kernel(tmp_path) -> None:
    _requires_v3()
    from agno.run import RunContext
    from agno.tools.code import CodeMode

    def double(value: int) -> int:
        return value * 2

    harness = AgentHarness(
        model=OfflineModel(id="offline"),
        include_default_tools=False,
        tools=[double],
        session_id="code-session",
        sandbox_dir=tmp_path / "sandbox",
        code_mode=True,
        config=_config(
            tmp_path,
            agno_media_offloading="disabled",
            agno_tool_result_offloading="disabled",
        ),
    )
    try:
        assert len(harness._agent.tools) == 1
        code = harness._agent.tools[0]
        assert isinstance(code, CodeMode)
        assert code.allow_shell is False
        assert code.handles == ["double"]
        result = code.execute(
            RunContext(run_id="run-1", session_id="code-session", user_id="alice"),
            "answer = await double(value=21)\nowner_marker = 'alice'\nanswer",
        )
        assert "42" in str(result.content)
        isolated = code.execute(
            RunContext(run_id="run-2", session_id="code-session", user_id="bob"),
            "'owner_marker' in globals()",
        )
        assert "False" in str(isolated.content)
        assert harness._spec.settings["code_mode"] == {
            "enabled": True,
            "allow_shell": False,
            "snapshot": True,
            "kernel_plane": "host_process",
            "tool_handle_plane": "runtime_backend",
            "security_sandbox": False,
        }
    finally:
        harness.close()


def test_code_mode_shell_disable_survives_sibling_magic_materialization(tmp_path) -> None:
    _requires_v3()
    from agno.run import RunContext

    harness = AgentHarness(
        model=OfflineModel(id="offline"),
        include_default_tools=False,
        session_id="shell-disabled-session",
        sandbox_dir=tmp_path / "sandbox",
        code_mode=True,
        config=_config(
            tmp_path,
            agno_media_offloading="disabled",
            agno_tool_result_offloading="disabled",
            code_mode_snapshot=False,
        ),
    )
    try:
        code = harness._agent.tools[0]
        context = RunContext(
            run_id="run-shell-disabled",
            session_id="shell-disabled-session",
            user_id="alice",
        )
        sibling = code.execute(
            context,
            "get_ipython().find_cell_magic('sh') is not None",
        )
        bash_missing = code.execute(
            context,
            "get_ipython().find_cell_magic('bash') is None",
        )
        indirect = code.execute(
            context,
            (
                "get_ipython().run_cell_magic("
                "'bash', '', \"printf leaked > shell-leak.txt\")"
            ),
        )
        direct = code.execute(
            context,
            "%%bash\nprintf leaked > shell-leak-direct.txt",
        )

        assert "True" in str(sibling.content)
        assert "True" in str(bash_missing.content)
        assert "not found" in str(indirect.content)
        assert "disabled" in str(direct.content)
        assert not (tmp_path / "sandbox" / "shell-leak.txt").exists()
        assert not (tmp_path / "sandbox" / "shell-leak-direct.txt").exists()
    finally:
        harness.close()


def _seed_forged_agno_metadata(harness: AgentHarness, *, session_id: str) -> Any:
    db = harness._agent.db
    harness._agent.metadata = {
        "_agnoclaw_context": {"user_id": "component-forgery"},
        "source": "component",
    }
    db.upsert_session(
        AgentSession(
            session_id=session_id,
            agent_id=harness._agent.id,
            user_id="trusted-user",
            metadata={
                "_agnoclaw_context": {"user_id": "session-forgery"},
                "source": "session",
            },
        )
    )
    return db


def _assert_trusted_run_metadata(db: Any, harness: AgentHarness, *, session_id: str) -> None:
    stored = db.get_session(session_id, user_id="trusted-user")
    assert stored is not None and stored.runs
    metadata = stored.runs[-1].metadata
    assert metadata["source"] == "caller"
    assert metadata["_agnoclaw_context"]["user_id"] == "trusted-user"
    assert metadata["_agnoclaw_context"]["metadata"]["_agnoclaw_context"] == {
        "user_id": "caller-forgery"
    }
    assert harness._agent.metadata == {
        "_agnoclaw_context": {"user_id": "component-forgery"},
        "source": "component",
    }


def test_agno3_sync_run_metadata_keeps_call_site_trusted_context(tmp_path) -> None:
    _requires_v3()
    session_id = "metadata-sync"
    harness = AgentHarness(
        model=TokenFreeResponseModel(id="offline-response"),
        include_default_tools=False,
        session_id=session_id,
        user_id="trusted-user",
        config=_config(
            tmp_path,
            agno_media_offloading="disabled",
            agno_tool_result_offloading="disabled",
        ),
    )
    db = _seed_forged_agno_metadata(harness, session_id=session_id)
    try:
        result = harness.run(
            "offline metadata contract",
            metadata={
                "source": "caller",
                "_agnoclaw_context": {"user_id": "caller-forgery"},
            },
        )
        assert result.content == "offline-ok"
        _assert_trusted_run_metadata(db, harness, session_id=session_id)
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_agno3_async_run_metadata_keeps_call_site_trusted_context(tmp_path) -> None:
    _requires_v3()
    session_id = "metadata-async"
    harness = AgentHarness(
        model=TokenFreeResponseModel(id="offline-response"),
        include_default_tools=False,
        session_id=session_id,
        user_id="trusted-user",
        config=_config(
            tmp_path,
            agno_media_offloading="disabled",
            agno_tool_result_offloading="disabled",
        ),
    )
    db = _seed_forged_agno_metadata(harness, session_id=session_id)
    try:
        result = await harness.arun(
            "offline metadata contract",
            metadata={
                "source": "caller",
                "_agnoclaw_context": {"user_id": "caller-forgery"},
            },
        )
        assert result.content == "offline-ok"
        _assert_trusted_run_metadata(db, harness, session_id=session_id)
    finally:
        await harness.aclose()


def test_code_mode_rejects_durable_profile_until_kernels_are_recoverable(tmp_path) -> None:
    _requires_v3()
    with pytest.raises(HarnessError) as caught:
        resolve_code_mode(
            explicit=True,
            enabled=True,
            tools=[],
            db=None,
            profile="durable",
            scope_key="test-scope",
            cwd=tmp_path,
            allow_shell=False,
            snapshot=True,
            timeout_seconds=300,
            idle_ttl_seconds=1_800,
            max_kernels=4,
            compatibility=inspect_agno_compatibility(),
        )

    assert caught.value.code == "AGNO_CODE_MODE_PROFILE_UNSUPPORTED"


def test_code_mode_routes_handles_to_backend_but_direct_python_stays_host_side(
    tmp_path,
) -> None:
    _requires_v3()
    from agno.run import RunContext

    executor = MagicMock()
    executor.run.return_value = CommandResult(stdout="backend-plane", exit_code=0)
    workspace = MagicMock()
    workspace.workspace_dir = tmp_path / "backend-workspace"
    backend = RuntimeBackend(command_executor=executor, workspace_adapter=workspace)
    kernel_cwd = tmp_path / "host-kernel"
    harness = AgentHarness(
        model=OfflineModel(id="offline"),
        backend=backend,
        workspace_dir=tmp_path / "workspace",
        sandbox_dir=kernel_cwd,
        session_id="composed-session",
        code_mode=True,
        config=_config(
            tmp_path,
            agno_media_offloading="disabled",
            agno_tool_result_offloading="disabled",
            code_mode_snapshot=False,
        ),
    )
    kernel_loop = None
    try:
        code = harness._agent.tools[0]
        context = RunContext(
            run_id="run-composed",
            session_id="composed-session",
            user_id="alice",
        )
        backend_result = code.execute(
            context,
            "result = await bash(command='printf backend')\nresult",
        )
        direct_result = code.execute(
            context,
            (
                "from pathlib import Path\n"
                "Path('direct-host.txt').write_text('host-plane')\n"
                "Path('direct-host.txt').read_text()"
            ),
        )

        assert "backend-plane" in str(backend_result.content)
        assert executor.run.call_args_list[-1].kwargs["command"] == "printf backend"
        assert "host-plane" in str(direct_result.content)
        assert (kernel_cwd / "direct-host.txt").read_text() == "host-plane"
        workspace.write_file.assert_not_called()
        kernel_loop = code._runner._loop
    finally:
        harness.close()
    assert not code._runner.started
    assert kernel_loop is not None and kernel_loop.is_closed()


@pytest.mark.asyncio
async def test_code_mode_async_harness_close_stops_kernel_loop(tmp_path) -> None:
    _requires_v3()
    from agno.run import RunContext

    harness = AgentHarness(
        model=OfflineModel(id="offline"),
        include_default_tools=False,
        code_mode=True,
        config=_config(
            tmp_path,
            agno_media_offloading="disabled",
            agno_tool_result_offloading="disabled",
            code_mode_snapshot=False,
        ),
    )
    code = harness._agent.tools[0]
    result = await code.aexecute(
        RunContext(run_id="run-async", session_id="session-async", user_id="alice"),
        "20 + 22",
    )
    kernel_loop = code._runner._loop

    assert "42" in str(result.content)
    await harness.aclose()
    assert not code._runner.started
    assert kernel_loop is not None and kernel_loop.is_closed()
