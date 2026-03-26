from __future__ import annotations

import subprocess

import pytest

from agnoclaw.config import HarnessConfig
from agnoclaw.integrations import LLMSandboxBackend
from agnoclaw.tools import get_default_tools

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


def test_llm_sandbox_live_session_sandbox_boundary(tmp_path):
    workspace = tmp_path / "workspace"
    sandbox = tmp_path / "session-sandbox"
    workspace.mkdir()
    workspace_input = workspace / "input.txt"
    workspace_output = workspace / "output.txt"
    workspace_input.write_text("hello boundary\n", encoding="utf-8")

    backend = LLMSandboxBackend(sync_paths=[workspace_input])
    try:
        tools = get_default_tools(
            HarnessConfig(enable_bash=True),
            workspace_dir=workspace,
            sandbox_dir=sandbox,
            backend=backend,
        )
        files = next(t for t in tools if getattr(t, "name", None) == "files")
        bash = next(t for t in tools if getattr(t, "name", None) == "bash")

        script_path = "make_output.py"
        session_artifact = sandbox / "session-artifact.txt"
        script = (
            "from pathlib import Path\n"
            f"source = Path(r\"{workspace_input}\").read_text(encoding=\"utf-8\")\n"
            "Path(\"session-artifact.txt\").write_text(source.upper(), encoding=\"utf-8\")\n"
            f"Path(r\"{workspace_output}\").write_text(source + \"done\\n\", encoding=\"utf-8\")\n"
        )

        write_result = files.write_file(script_path, script)
        assert "Written" in write_result
        assert not (sandbox / script_path).exists()
        assert not session_artifact.exists()
        assert not workspace_output.exists()

        command_result = bash.entrypoint(f"python {script_path}")
        assert "[exit code:" not in command_result

        assert not (sandbox / script_path).exists()
        assert not session_artifact.exists()
        assert not workspace_output.exists()

        backend.sync_from_runtime(sandbox, workspace_output)

        assert (sandbox / script_path).read_text(encoding="utf-8") == script
        assert session_artifact.read_text(encoding="utf-8") == "HELLO BOUNDARY\n"
        assert workspace_output.read_text(encoding="utf-8") == "hello boundary\ndone\n"
    finally:
        backend.close()
