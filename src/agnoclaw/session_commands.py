"""Session command routing with explicit executor ownership."""

from __future__ import annotations

from typing import Any

from .runtime import ElevatedSessionMode
from .tools.backends import (
    BackgroundCommandHandle,
    BackgroundCommandOutput,
    CommandExecutor,
    CommandResult,
)


class _ElevatedSessionCommandExecutor:
    """Route session bash calls through the elevated host path when enabled."""

    def __init__(
        self,
        *,
        harness: Any,
        sandbox_executor: CommandExecutor,
        host_executor: CommandExecutor,
        owns_sandbox_executor: bool,
    ) -> None:
        self._harness = harness
        self._sandbox_executor = sandbox_executor
        self._host_executor = host_executor
        self._owns_sandbox_executor = owns_sandbox_executor
        self._closed = False
        self._elevated_task_ids: set[str] = set()
        self.workspace_dir = getattr(sandbox_executor, "workspace_dir", None)

    def _mode(self) -> ElevatedSessionMode:
        return self._harness._elevated_session_mode

    def _skip_approval(self) -> bool:
        return self._mode() == ElevatedSessionMode.FULL

    def _metadata(self) -> dict[str, Any]:
        return {
            "source": "elevated_session",
            "elevated_mode": self._mode().value,
        }

    def run(
        self,
        *,
        command: str,
        workdir: str | None,
        timeout_seconds: int | None,
    ) -> CommandResult:
        if self._mode() == ElevatedSessionMode.OFF:
            return self._sandbox_executor.run(
                command=command,
                workdir=workdir,
                timeout_seconds=timeout_seconds,
            )

        result = self._harness.run_elevated_command(
            command,
            reason="Session elevated bash tool call.",
            working_dir=workdir,
            timeout_seconds=timeout_seconds,
            metadata=self._metadata(),
            _skip_approval=self._skip_approval(),
        )
        return CommandResult(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
        )

    def start(
        self,
        *,
        command: str,
        workdir: str | None,
        description: str | None = None,
    ) -> BackgroundCommandHandle:
        if self._mode() == ElevatedSessionMode.OFF:
            return self._sandbox_executor.start(
                command=command,
                workdir=workdir,
                description=description,
            )

        request, context = self._harness._build_elevated_command_request(
            command,
            reason=description or "Session elevated background bash tool call.",
            working_dir=workdir,
            timeout_seconds=None,
            context=None,
            metadata={**self._metadata(), "background": True},
        )
        tool_request = self._harness._elevated_tool_request(request)
        payload = self._harness._elevated_request_payload(request)
        self._harness._authorize_elevated_command_sync(
            request=request,
            tool_request=tool_request,
            context=context,
            payload=payload,
            skip_approval=self._skip_approval(),
        )
        self._harness._emit_event_sync(
            event_type="elevated.command.started",
            run_id=request.run_id,
            context=context,
            payload={**payload, "background": True},
        )
        try:
            handle = self._host_executor.start(
                command=request.command,
                workdir=request.working_dir,
                description=description,
            )
        except Exception as exc:
            self._harness._emit_event_sync(
                event_type="elevated.command.failed",
                run_id=request.run_id,
                context=context,
                payload={**payload, "background": True, "error": str(exc)},
            )
            raise
        self._elevated_task_ids.add(handle.task_id)
        self._harness._emit_event_sync(
            event_type="elevated.command.completed",
            run_id=request.run_id,
            context=context,
            payload={
                **payload,
                "background": True,
                "task_id": handle.task_id,
                "status": handle.status,
                "log_path": handle.log_path,
            },
        )
        return handle

    def output(
        self,
        *,
        task_id: str,
        max_chars: int = 8000,
        tail: bool = True,
    ) -> BackgroundCommandOutput:
        if task_id in self._elevated_task_ids:
            return self._host_executor.output(
                task_id=task_id,
                max_chars=max_chars,
                tail=tail,
            )
        return self._sandbox_executor.output(
            task_id=task_id,
            max_chars=max_chars,
            tail=tail,
        )

    def kill(self, *, task_id: str, force: bool = False) -> str:
        if task_id in self._elevated_task_ids:
            return self._host_executor.kill(task_id=task_id, force=force)
        return self._sandbox_executor.kill(task_id=task_id, force=force)

    def close(self) -> None:
        """Close only command executors owned by this harness."""
        if self._closed:
            return
        self._closed = True
        resources = [self._host_executor]
        if self._owns_sandbox_executor:
            resources.append(self._sandbox_executor)
        seen: set[int] = set()
        for resource in reversed(resources):
            identity = id(resource)
            if identity in seen:
                continue
            seen.add(identity)
            closer = getattr(resource, "close", None)
            if callable(closer):
                closer()
