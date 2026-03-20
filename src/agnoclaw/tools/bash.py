"""
Shell execution toolkit — sync bash plus background task lifecycle.

Background flow (Claude/OpenClaw style):
  bash_start -> bash_output -> bash_kill
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from agno.tools import tool
from agno.tools.toolkit import Toolkit

from .backends import (
    BackgroundCommandOutput,
    CommandExecutor,
    LocalCommandExecutor,
)


logger = logging.getLogger("agnoclaw.tools.bash")


class BashToolError(RuntimeError):
    """Raised when a bash tool call fails before producing a command result."""


class BashToolkit(Toolkit):
    """Toolkit exposing foreground and background shell tools."""

    def __init__(
        self,
        timeout: int = 120,
        workspace_dir: Optional[str | Path] = None,
        max_background_tasks: int = 16,
        executor: CommandExecutor | None = None,
    ):
        super().__init__(name="bash")
        self.timeout = timeout
        self.workspace_dir = (
            str(Path(workspace_dir).expanduser().resolve())
            if workspace_dir is not None
            else getattr(executor, "workspace_dir", None)
        )
        self.max_background_tasks = max_background_tasks
        self.executor = executor or LocalCommandExecutor(
            workspace_dir=self.workspace_dir,
            max_background_tasks=max_background_tasks,
        )

        self.register(self.bash)
        self.register(self.bash_start)
        self.register(self.bash_output)
        self.register(self.bash_kill)

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
        timeout = int(timeout_seconds) if timeout_seconds is not None else self.timeout

        try:
            result = self.executor.run(
                command=command,
                workdir=working_dir,
                timeout_seconds=timeout,
            )
            output = result.stdout
            if result.exit_code != 0:
                stderr = result.stderr.strip()
                if stderr:
                    output += f"\n[stderr]\n{stderr}"
                output += f"\n[exit code: {result.exit_code}]"
            return output.strip() if output.strip() else "[no output]"
        except Exception as exc:
            message = str(exc)
            if message.startswith("Command timed out after "):
                raise BashToolError(message) from exc
            raise BashToolError(f"Failed to execute command: {message}") from exc

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
        try:
            handle = self.executor.start(
                command=command,
                workdir=working_dir,
                description=description,
            )
        except Exception as exc:
            raise BashToolError(f"Failed to start background command: {exc}") from exc

        parts = [
            f"Started background task {handle.task_id}",
            f"pid: {handle.pid}",
            f"status: {handle.status}",
        ]
        if handle.log_path:
            parts.append(f"log: {handle.log_path}")
        return "\n".join(parts)

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
        try:
            result = self.executor.output(task_id=task_id, max_chars=max_chars, tail=tail)
        except Exception as exc:
            raise BashToolError(str(exc)) from exc
        return self._format_output(result)

    @tool(
        name="bash_kill",
        description="Terminate a background shell task by id.",
        show_result=True,
    )
    def bash_kill(self, task_id: str, force: bool = False) -> str:
        """Terminate a background task."""
        try:
            return self.executor.kill(task_id=task_id, force=force)
        except Exception as exc:
            raise BashToolError(f"Failed to kill task {task_id}: {exc}") from exc

    @staticmethod
    def _format_output(result: BackgroundCommandOutput) -> str:
        code_text = str(result.exit_code) if result.exit_code is not None else "n/a"
        pid_text = result.pid if result.pid is not None else "n/a"
        body = result.output if result.output.strip() else "[no output yet]"
        return (
            f"[task {result.task_id}] status={result.status} exit_code={code_text} pid={pid_text}\n"
            f"{body}"
        )


def make_bash_tool(
    timeout: int = 120,
    workspace_dir: Optional[str | Path] = None,
    executor: CommandExecutor | None = None,
):
    """
    Backward-compatible helper returning only the `bash` function tool.

    New code should prefer BashToolkit for background task support.
    """
    toolkit = BashToolkit(timeout=timeout, workspace_dir=workspace_dir, executor=executor)
    return toolkit.functions["bash"]
