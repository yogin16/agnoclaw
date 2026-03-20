from __future__ import annotations

import subprocess

import pytest

from agnoclaw.integrations import LLMSandboxBackend

pytest.importorskip("llm_sandbox", reason="llm-sandbox extra is not installed")


def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_available(), reason="Docker daemon is not available"),
]


def test_llm_sandbox_backend_live_round_trip(tmp_path):
    workspace = tmp_path / "workspace"
    inputs_dir = workspace / "workspace" / "inputs"
    outputs_dir = workspace / "workspace" / "outputs"
    inputs_dir.mkdir(parents=True)
    inputs_file = inputs_dir / "note.txt"
    inputs_file.write_text("hello sandbox\n", encoding="utf-8")

    backend = LLMSandboxBackend(sync_paths=["workspace/inputs"])
    try:
        backend.bind(workspace)
        resolved = backend.resolve(workspace_dir=workspace)

        command_result = resolved.command_executor.run(
            command="cat workspace/inputs/note.txt",
            workdir=None,
            timeout_seconds=10,
        )
        assert command_result.exit_code == 0
        assert command_result.stdout == "hello sandbox\n"

        runtime_output = outputs_dir / "report.txt"
        write_result = resolved.workspace_adapter.write_file(
            str(runtime_output),
            "generated in sandbox\n",
        )
        assert "Written" in write_result
        assert not runtime_output.exists()

        backend.sync_from_runtime("workspace/outputs")

        assert runtime_output.exists()
        assert runtime_output.read_text(encoding="utf-8") == "generated in sandbox\n"
    finally:
        backend.close()
