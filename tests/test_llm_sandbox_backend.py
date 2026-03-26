from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from agnoclaw.config import HarnessConfig
from agnoclaw.integrations import LLMSandboxBackend
from agnoclaw.tools import get_default_tools
from agnoclaw.tools.files import FilesToolkit


@dataclass
class _ConsoleOutput:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


class FakeLLMSandboxSession:
    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root
        self.is_open = False
        self.calls: list[tuple[str, str | None]] = []
        self._next_pid = 4000
        self.background_tasks: dict[int, dict[str, object]] = {}

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def execute_command(
        self,
        command: str,
        workdir: str | None = None,
        on_stdout=None,
        on_stderr=None,
    ) -> _ConsoleOutput:
        del on_stdout, on_stderr
        self.calls.append((command, workdir))

        if "nohup sh -lc" in command:
            match = re.search(
                (
                    r"> (?P<log>[^ ]+) 2>&1 < /dev/null & "
                    r"pid=\$!; printf '%s' \"\$pid\" > (?P<pid_path>.+)$"
                ),
                command,
            )
            assert match is not None
            log_path = self._unquote(match.group("log"))
            pid_path = self._unquote(match.group("pid_path"))
            exit_path = str(Path(log_path).with_suffix(".exit"))
            pid = self._next_pid
            self._next_pid += 1
            self.background_tasks[pid] = {
                "status": "running",
                "exit_code": None,
                "log_path": log_path,
                "exit_path": exit_path,
                "pid_path": pid_path,
                "log_text": "",
            }
            self._runtime_path(pid_path).parent.mkdir(parents=True, exist_ok=True)
            self._runtime_path(pid_path).write_text(str(pid), encoding="utf-8")
            self._runtime_path(log_path).parent.mkdir(parents=True, exist_ok=True)
            self._runtime_path(log_path).write_text("", encoding="utf-8")
            return _ConsoleOutput(stdout=str(pid))

        if command.startswith("mkdir -p "):
            target = re.match(r"^mkdir -p '?(.*?)'?$", command)
            assert target is not None
            self._runtime_path(target.group(1)).mkdir(parents=True, exist_ok=True)
            return _ConsoleOutput()

        if command.startswith("if [ -f "):
            exit_match = re.search(r"\[ -f (?P<exit_path>[^ ]+) \]", command)
            pid_match = re.search(r"kill -0 (?P<pid>\d+)", command)
            assert exit_match is not None
            assert pid_match is not None
            pid = int(pid_match.group("pid"))
            task = self.background_tasks[pid]
            if task["status"] == "running":
                return _ConsoleOutput(stdout="status=running\n")
            if task["status"] == "exited":
                return _ConsoleOutput(
                    stdout=f"status=exited\nexit_code={task['exit_code']}\n"
                )
            return _ConsoleOutput(stdout="status=terminated\n")

        if command.startswith("if kill -0 "):
            pid_match = re.search(r"if kill -0 (?P<pid>\d+)", command)
            assert pid_match is not None
            pid = int(pid_match.group("pid"))
            task = self.background_tasks[pid]
            if task["status"] == "running":
                task["status"] = "terminated"
                return _ConsoleOutput(stdout="terminated")
            return _ConsoleOutput(stdout="not-running")

        return _ConsoleOutput(stdout=f"ran:{command}:{workdir}")

    def copy_to_runtime(self, src: str, dest: str) -> None:
        src_path = Path(src)
        dest_path = self._runtime_path(dest)
        if src_path.is_dir():
            if dest_path.exists():
                shutil.rmtree(dest_path)
            shutil.copytree(src_path, dest_path)
            return
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_path)

    def copy_from_runtime(self, src: str, dest: str) -> None:
        src_path = self._runtime_path(src)
        dest_path = Path(dest)
        if not src_path.exists():
            raise FileNotFoundError(src)
        if src_path.is_dir():
            dest_path.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_path, dest_path / src_path.name)
            return
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_path)

    def mark_task_exited(self, pid: int, *, exit_code: int = 0, log_text: str = "") -> None:
        task = self.background_tasks[pid]
        task["status"] = "exited"
        task["exit_code"] = exit_code
        task["log_text"] = log_text
        log_path = self._runtime_path(str(task["log_path"]))
        exit_path = self._runtime_path(str(task["exit_path"]))
        log_path.write_text(log_text, encoding="utf-8")
        exit_path.write_text(str(exit_code), encoding="utf-8")

    def _runtime_path(self, path: str) -> Path:
        return self.runtime_root / Path(path).relative_to("/")

    @staticmethod
    def _unquote(text: str) -> str:
        return text.strip("'")


def test_llm_sandbox_backend_requires_binding_before_explicit_sync(tmp_path):
    backend = LLMSandboxBackend(session=FakeLLMSandboxSession(tmp_path / "runtime"))

    with pytest.raises(RuntimeError, match="must be bound to a workspace first"):
        backend.sync_to_runtime("workspace/inputs")


def test_llm_sandbox_backend_syncs_only_selected_paths(tmp_path):
    workspace = tmp_path / "workspace"
    inputs = workspace / "workspace" / "inputs"
    inputs.mkdir(parents=True)
    (inputs / "prompt.txt").write_text("hello", encoding="utf-8")
    ignored = workspace / "workspace" / "private"
    ignored.mkdir(parents=True)
    (ignored / "secret.txt").write_text("nope", encoding="utf-8")

    session = FakeLLMSandboxSession(tmp_path / "runtime")
    backend = LLMSandboxBackend(session=session, sync_paths=["workspace/inputs"])

    backend.bind(workspace)

    runtime_inputs = session._runtime_path(str(inputs))
    runtime_ignored = session._runtime_path(str(ignored))
    assert (runtime_inputs / "prompt.txt").read_text(encoding="utf-8") == "hello"
    assert not runtime_ignored.exists()


def test_llm_sandbox_backend_rejects_rebinding_to_different_workspace(tmp_path):
    session = FakeLLMSandboxSession(tmp_path / "runtime")
    backend = LLMSandboxBackend(session=session)

    backend.bind(tmp_path / "workspace-a")

    with pytest.raises(RuntimeError, match="already bound"):
        backend.bind(tmp_path / "workspace-b")


def test_llm_sandbox_backend_files_stay_in_sandbox_until_pulled(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = FakeLLMSandboxSession(tmp_path / "runtime")
    backend = LLMSandboxBackend(session=session).bind(workspace)
    resolved = backend.resolve(workspace_dir=workspace)
    files = FilesToolkit(adapter=resolved.workspace_adapter)

    target = workspace / "workspace" / "outputs" / "result.txt"
    result = files.write_file(str(target), "sandbox-only")

    assert "Written" in result
    assert not target.exists()
    assert session._runtime_path(str(target)).read_text(encoding="utf-8") == "sandbox-only"

    backend.sync_from_runtime("workspace/outputs")

    assert target.read_text(encoding="utf-8") == "sandbox-only"


def test_llm_sandbox_backend_command_execution_uses_bound_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = FakeLLMSandboxSession(tmp_path / "runtime")
    backend = LLMSandboxBackend(session=session)

    executor = backend.resolve(workspace_dir=workspace).command_executor
    result = executor.run(command="pwd", workdir=None, timeout_seconds=5)

    assert result.stdout == f"ran:pwd:{workspace}"


def test_llm_sandbox_backend_background_command_lifecycle(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = FakeLLMSandboxSession(tmp_path / "runtime")
    backend = LLMSandboxBackend(session=session)
    executor = backend.resolve(workspace_dir=workspace).command_executor

    handle = executor.start(command="sleep 10", workdir=None, description="test")
    running = executor.output(task_id=handle.task_id)
    assert running.status == "running"
    assert running.pid == handle.pid

    assert handle.pid is not None
    session.mark_task_exited(handle.pid, exit_code=0, log_text="done\n")

    exited = executor.output(task_id=handle.task_id)
    assert exited.status == "exited"
    assert exited.exit_code == 0
    assert "done" in exited.output


def test_llm_sandbox_backend_kill_updates_running_task(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = FakeLLMSandboxSession(tmp_path / "runtime")
    backend = LLMSandboxBackend(session=session)
    executor = backend.resolve(workspace_dir=workspace).command_executor

    handle = executor.start(command="sleep 20", workdir=None, description="test")
    message = executor.kill(task_id=handle.task_id)

    assert "SIGTERM" in message
    assert handle.pid is not None
    assert session.background_tasks[handle.pid]["status"] == "terminated"


def test_llm_sandbox_default_tools_route_relative_ops_to_session_sandbox(tmp_path):
    workspace = tmp_path / "workspace"
    sandbox = tmp_path / "sandbox"
    workspace.mkdir()

    session = FakeLLMSandboxSession(tmp_path / "runtime")
    backend = LLMSandboxBackend(session=session)

    tools = get_default_tools(
        HarnessConfig(),
        workspace_dir=workspace,
        sandbox_dir=sandbox,
        backend=backend,
    )
    files = next(t for t in tools if isinstance(t, FilesToolkit))
    bash = next(t for t in tools if getattr(t, "name", None) == "bash")

    files.write_file("scratch.txt", "sandbox-only")
    files.write_file(str(workspace / "workspace.txt"), "workspace-only")

    assert files.workspace_dir == sandbox.resolve()
    assert not (sandbox / "scratch.txt").exists()
    assert session._runtime_path(str(sandbox / "scratch.txt")).read_text(encoding="utf-8") == "sandbox-only"
    assert session._runtime_path(str(workspace / "workspace.txt")).read_text(encoding="utf-8") == "workspace-only"

    assert bash.entrypoint("pwd") == f"ran:pwd:{sandbox.resolve()}"
    assert (
        bash.entrypoint("pwd", working_dir=str(workspace.resolve()))
        == f"ran:pwd:{workspace.resolve()}"
    )
