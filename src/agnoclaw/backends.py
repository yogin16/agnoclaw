"""Single coherent runtime backend for host and sandboxed deployments."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .skills.backends import (
    CommandExecutorSkillRuntimeBackend,
    LocalSkillRuntimeBackend,
    SkillRuntimeBackend,
)
from .tools.backends import (
    CommandExecutor,
    LocalCommandExecutor,
    LocalWorkspaceAdapter,
    WorkspaceAdapter,
)
from .tools.browser_backends import BrowserBackend


@dataclass(frozen=True)
class ResolvedRuntimeBackend:
    """Resolved runtime capabilities for one concrete workspace."""

    command_executor: CommandExecutor
    workspace_adapter: WorkspaceAdapter
    skill_runtime: SkillRuntimeBackend
    browser_backend: BrowserBackend | None = None


class RuntimeBackend:
    """
    Single runtime backend override for shell, files, skills, and browser tools.

    Consumers should pass one backend object to `AgentHarness(..., backend=...)`.
    Advanced integrations can either subclass this type around a sandbox/session
    object, or compose one from lower-level adapters in tests and embeddings.
    """

    def __init__(
        self,
        *,
        command_executor: CommandExecutor | None = None,
        workspace_adapter: WorkspaceAdapter | None = None,
        browser_backend: BrowserBackend | None = None,
    ) -> None:
        if (command_executor is None) != (workspace_adapter is None):
            raise ValueError(
                "RuntimeBackend requires both command_executor and "
                "workspace_adapter together, or neither."
            )
        self._command_executor = command_executor
        self._workspace_adapter = workspace_adapter
        self._browser_backend = browser_backend

    def uses_host_runtime(self) -> bool:
        """Return True only for the default host-local backend mode."""
        return (
            type(self) is RuntimeBackend
            and self._command_executor is None
            and self._workspace_adapter is None
            and self._browser_backend is None
        )

    def resolve(
        self,
        *,
        workspace_dir: str | Path,
    ) -> ResolvedRuntimeBackend:
        workspace_path = Path(workspace_dir).expanduser().resolve()
        command_executor = self.resolve_command_executor(workspace_dir=workspace_path)
        workspace_adapter = self.resolve_workspace_adapter(workspace_dir=workspace_path)
        return ResolvedRuntimeBackend(
            command_executor=command_executor,
            workspace_adapter=workspace_adapter,
            skill_runtime=self.resolve_skill_runtime(
                workspace_dir=workspace_path,
                command_executor=command_executor,
            ),
            browser_backend=self.resolve_browser_backend(),
        )

    def resolve_command_executor(self, *, workspace_dir: str | Path) -> CommandExecutor:
        if self._command_executor is not None:
            return self._command_executor
        if self.uses_host_runtime():
            return LocalCommandExecutor(workspace_dir=workspace_dir)
        raise RuntimeError(
            "This backend must provide command execution. "
            "Override resolve_command_executor() or pass command_executor and "
            "workspace_adapter together."
        )

    def resolve_workspace_adapter(self, *, workspace_dir: str | Path) -> WorkspaceAdapter:
        if self._workspace_adapter is not None:
            return self._workspace_adapter
        if self.uses_host_runtime():
            return LocalWorkspaceAdapter(workspace_dir=workspace_dir)
        raise RuntimeError(
            "This backend must provide workspace file access. "
            "Override resolve_workspace_adapter() or pass command_executor and "
            "workspace_adapter together."
        )

    def resolve_skill_runtime(
        self,
        *,
        workspace_dir: str | Path,
        command_executor: CommandExecutor | None = None,
    ) -> SkillRuntimeBackend:
        if self.uses_host_runtime():
            return LocalSkillRuntimeBackend(working_dir=workspace_dir)
        resolved_command_executor = command_executor or self.resolve_command_executor(
            workspace_dir=workspace_dir
        )
        return CommandExecutorSkillRuntimeBackend(
            resolved_command_executor,
            working_dir=workspace_dir,
        )

    def resolve_browser_backend(self) -> BrowserBackend | None:
        return self._browser_backend
