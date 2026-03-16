"""
Shell execution toolkit — sync bash plus background task lifecycle.

Background flow (Claude/OpenClaw style):
  bash_start -> bash_output -> bash_kill
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from agno.tools import tool
from agno.tools.toolkit import Toolkit


logger = logging.getLogger("agnoclaw.tools.bash")


class BashToolError(RuntimeError):
    """Raised when a bash tool call fails before producing a command result."""


@dataclass
class _BackgroundTask:
    task_id: str
    process: subprocess.Popen
    command: str
    output_path: Path
    output_file: Any  # open file handle — kept alive while process runs
    working_dir: Optional[str]
    started_at: float
    description: Optional[str] = None


class BashToolkit(Toolkit):
    """Toolkit exposing foreground and background shell tools."""

    def __init__(
        self,
        timeout: int = 120,
        workspace_dir: Optional[str | Path] = None,
        max_background_tasks: int = 16,
    ):
        super().__init__(name="bash")
        self.timeout = timeout
        self.workspace_dir = (
            str(Path(workspace_dir).expanduser().resolve())
            if workspace_dir is not None
            else None
        )
        self.max_background_tasks = max_background_tasks
        self._tasks: dict[str, _BackgroundTask] = {}
        self._tasks_dir = Path.home() / ".agnoclaw" / "tmp" / "bash_tasks"
        self._tasks_dir.mkdir(parents=True, exist_ok=True)

        self.register(self.bash)
        self.register(self.bash_start)
        self.register(self.bash_output)
        self.register(self.bash_kill)

    def _resolve_cwd(self, working_dir: Optional[str]) -> Optional[str]:
        candidate = working_dir or self.workspace_dir
        if candidate is None:
            return None
        return str(Path(candidate).expanduser().resolve())

    def _task_status(self, task: _BackgroundTask) -> tuple[str, Optional[int]]:
        code = task.process.poll()
        if code is None:
            return ("running", None)
        return ("exited", int(code))

    @staticmethod
    def _tail_text(text: str, max_chars: int, tail: bool) -> str:
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        if tail:
            return f"... [truncated to last {max_chars} chars]\n{text[-max_chars:]}"
        return text[:max_chars] + f"\n... [truncated to first {max_chars} chars]"

    @staticmethod
    def _cleanup_task(task: _BackgroundTask) -> None:
        """Close the output file handle for a finished task."""
        try:
            if task.output_file and not task.output_file.closed:
                task.output_file.close()
        except Exception:
            pass

    def _prune_finished_tasks(self) -> None:
        if len(self._tasks) <= self.max_background_tasks:
            return
        finished = [t for t in self._tasks.values() if t.process.poll() is not None]
        if not finished:
            return
        finished.sort(key=lambda t: t.started_at)
        for task in finished:
            if len(self._tasks) <= self.max_background_tasks:
                break
            self._cleanup_task(task)
            self._tasks.pop(task.task_id, None)

    @tool(
        name="bash",
        description=(
            "Execute a shell command synchronously. Use for git/npm/pip/test/build tasks. "
            "Use bash_start for long-running commands you want to check later."
        ),
        show_result=True,
    )
    def bash(
        self,
        command: str,
        description: Optional[str] = None,
        working_dir: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> str:
        """Run a bash command and return its output."""
        del description
        cwd = self._resolve_cwd(working_dir)
        timeout = int(timeout_seconds) if timeout_seconds is not None else self.timeout

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
            output = result.stdout
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if stderr:
                    output += f"\n[stderr]\n{stderr}"
                output += f"\n[exit code: {result.returncode}]"
            return output.strip() if output.strip() else "[no output]"
        except subprocess.TimeoutExpired:
            raise BashToolError(f"Command timed out after {timeout} seconds: {command}") from None
        except Exception as e:
            raise BashToolError(f"Failed to execute command: {e}") from e

    @tool(
        name="bash_start",
        description=(
            "Start a background shell command. Returns a task id for use with "
            "bash_output and bash_kill."
        ),
        show_result=True,
    )
    def bash_start(
        self,
        command: str,
        description: Optional[str] = None,
        working_dir: Optional[str] = None,
    ) -> str:
        """Start a background command and return task metadata."""
        self._prune_finished_tasks()
        if len(self._tasks) >= self.max_background_tasks:
            raise BashToolError(
                f"[error] Too many background tasks ({len(self._tasks)}). "
                "Use bash_kill or wait for tasks to finish."
            )

        task_id = f"task_{uuid4().hex[:12]}"
        output_path = self._tasks_dir / f"{task_id}.log"
        cwd = self._resolve_cwd(working_dir)
        output_file = None
        try:
            output_file = output_path.open("w", encoding="utf-8")
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=output_file,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                text=True,
            )
            # NOTE: output_file must stay open while the process runs.
            # It is closed in _cleanup_task() when the process exits.
        except Exception as e:
            if output_file is not None and not output_file.closed:
                output_file.close()
            raise BashToolError(f"Failed to start background command: {e}") from e

        task = _BackgroundTask(
            task_id=task_id,
            process=process,
            command=command,
            output_path=output_path,
            output_file=output_file,
            working_dir=cwd,
            started_at=time.time(),
            description=description,
        )
        self._tasks[task_id] = task
        return (
            f"Started background task {task_id}\n"
            f"pid: {process.pid}\n"
            f"status: running\n"
            f"log: {output_path}"
        )

    @tool(
        name="bash_output",
        description="Fetch output and status for a background shell task id.",
        show_result=True,
    )
    def bash_output(
        self,
        task_id: str,
        max_chars: int = 8000,
        tail: bool = True,
    ) -> str:
        """Read output for a background task."""
        task = self._tasks.get(task_id)
        if task is None:
            raise BashToolError(f"Unknown task id: {task_id}")

        status, code = self._task_status(task)
        if code is not None:
            self._cleanup_task(task)
        if not task.output_path.exists():
            body = "[no output yet]"
        else:
            body = task.output_path.read_text(encoding="utf-8", errors="replace")
            body = self._tail_text(body, max_chars=max_chars, tail=tail)
            if not body.strip():
                body = "[no output yet]"

        code_text = str(code) if code is not None else "n/a"
        return (
            f"[task {task_id}] status={status} exit_code={code_text} pid={task.process.pid}\n"
            f"{body}"
        )

    @tool(
        name="bash_kill",
        description="Terminate a background shell task by id.",
        show_result=True,
    )
    def bash_kill(self, task_id: str, force: bool = False) -> str:
        """Terminate a background task."""
        task = self._tasks.get(task_id)
        if task is None:
            raise BashToolError(f"Unknown task id: {task_id}")

        code = task.process.poll()
        if code is not None:
            return f"Task {task_id} already exited with code {code}."

        try:
            if force:
                task.process.kill()
            else:
                task.process.terminate()
            try:
                task.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                task.process.kill()
                task.process.wait(timeout=2.0)
            code = task.process.poll()
            self._cleanup_task(task)
            return f"Killed task {task_id} (exit code {code})."
        except Exception as e:
            raise BashToolError(f"Failed to kill task {task_id}: {e}") from e


def make_bash_tool(timeout: int = 120, workspace_dir: Optional[str | Path] = None):
    """
    Backward-compatible helper returning only the `bash` function tool.

    New code should prefer BashToolkit for background task support.
    """
    toolkit = BashToolkit(timeout=timeout, workspace_dir=workspace_dir)
    return toolkit.functions["bash"]
