"""Backend abstractions for skill runtime execution and installation."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from agnoclaw.tools.backends import CommandExecutor, LocalCommandExecutor

if TYPE_CHECKING:
    from .loader import Skill, SkillInstaller


@dataclass(frozen=True)
class SkillInstallResult:
    """Normalized result for a skill installation attempt."""

    success: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    message: str = ""


class SkillRuntimeBackend(Protocol):
    """Runtime backend for skill inline commands, probes, and installs."""

    def run_inline_command(
        self,
        *,
        command: str,
        timeout_seconds: int = 10,
        working_dir: str | None = None,
    ) -> str:
        ...

    def has_binary(self, name: str) -> bool:
        ...

    def has_env_var(self, name: str) -> bool:
        ...

    def has_python_distribution(self, name: str) -> bool:
        ...

    def run_install(
        self,
        *,
        installer_type: str,
        package_spec: str,
        timeout_seconds: int = 120,
    ) -> SkillInstallResult:
        ...


class SkillInstallApprover(Protocol):
    """Approval backend for skill dependency installs."""

    def approve(self, skill: Skill, pending: list[tuple[SkillInstaller, str]]) -> bool:
        ...


def build_install_command(
    installer_type: str,
    package_spec: str,
    *,
    python_command: str | None = None,
) -> list[str] | None:
    """Build an install command for the local/command-executor runtime backends."""
    if installer_type == "uv":
        python_bin = python_command or sys.executable
        return [python_bin, "-m", "uv", "pip", "install", package_spec]
    if installer_type == "pip":
        python_bin = python_command or sys.executable
        return [python_bin, "-m", "pip", "install", "--quiet", package_spec]
    if installer_type == "brew":
        return ["brew", "install", package_spec]
    if installer_type == "npm":
        return ["npm", "install", "-g", package_spec]
    if installer_type == "go":
        return ["go", "install", package_spec]
    return None


class AutoApproveSkillInstallApprover:
    """Install approver that always returns True."""

    def approve(self, skill: Skill, pending: list[tuple[SkillInstaller, str]]) -> bool:
        del skill, pending
        return True


class InteractiveSkillInstallApprover:
    """Interactive terminal approver for skill installs."""

    def approve(self, skill: Skill, pending: list[tuple[SkillInstaller, str]]) -> bool:
        print(f"\nSkill '{skill.name}' requires the following installations:")
        for installer, package_spec in pending:
            print(f"  {installer.type}: {package_spec}")
        print()
        try:
            answer = input("Proceed with installation? [y/N] ").strip().lower()
            return answer in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False


class LocalSkillRuntimeBackend:
    """Host-local backend for skill execution, probes, and installs."""

    def __init__(self, *, working_dir: str | Path | None = None) -> None:
        self.working_dir = (
            str(Path(working_dir).expanduser().resolve())
            if working_dir is not None
            else None
        )

    def run_inline_command(
        self,
        *,
        command: str,
        timeout_seconds: int = 10,
        working_dir: str | None = None,
    ) -> str:
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=working_dir or self.working_dir,
            )
            return result.stdout.strip()
        except Exception as exc:
            return f"[error running `{command}`: {exc}]"

    def has_binary(self, name: str) -> bool:
        return shutil.which(name) is not None

    def has_env_var(self, name: str) -> bool:
        return bool(os.environ.get(name))

    def has_python_distribution(self, name: str) -> bool:
        try:
            from importlib.metadata import distribution

            distribution(name)
            return True
        except Exception:
            return False

    def run_install(
        self,
        *,
        installer_type: str,
        package_spec: str,
        timeout_seconds: int = 120,
    ) -> SkillInstallResult:
        command = build_install_command(installer_type, package_spec)
        if command is None:
            return SkillInstallResult(
                success=False,
                message=f"unknown installer type '{installer_type}'",
            )

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=self.working_dir,
            )
            return SkillInstallResult(
                success=result.returncode == 0,
                exit_code=result.returncode,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )
        except FileNotFoundError:
            return SkillInstallResult(
                success=False,
                message=f"installer '{installer_type}' not found on PATH",
            )
        except subprocess.TimeoutExpired:
            return SkillInstallResult(
                success=False,
                message=f"install timed out for {package_spec}",
            )
        except Exception as exc:
            return SkillInstallResult(
                success=False,
                message=str(exc),
            )


class CommandExecutorSkillRuntimeBackend:
    """Skill runtime backend that probes and executes through a CommandExecutor."""

    _SAFE_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def __init__(
        self,
        command_executor: CommandExecutor,
        *,
        working_dir: str | Path | None = None,
        python_candidates: tuple[str, ...] = ("python3", "python"),
    ) -> None:
        self.command_executor = command_executor
        self.working_dir = (
            str(Path(working_dir).expanduser().resolve())
            if working_dir is not None
            else None
        )
        self.python_candidates = python_candidates

    def run_inline_command(
        self,
        *,
        command: str,
        timeout_seconds: int = 10,
        working_dir: str | None = None,
    ) -> str:
        try:
            result = self.command_executor.run(
                command=command,
                workdir=working_dir or self.working_dir,
                timeout_seconds=timeout_seconds,
            )
            return result.stdout.strip()
        except Exception as exc:
            return f"[error running `{command}`: {exc}]"

    def has_binary(self, name: str) -> bool:
        quoted = shlex.quote(name)
        return self._run_probe(f"command -v {quoted} >/dev/null 2>&1")

    def has_env_var(self, name: str) -> bool:
        if not self._SAFE_ENV_NAME_RE.match(name):
            return False
        return self._run_probe(f'test -n "${{{name}+x}}"')

    def has_python_distribution(self, name: str) -> bool:
        python_bin = self._resolve_python_command()
        if python_bin is None:
            return False
        quoted_name = name.replace("\\", "\\\\").replace("'", "\\'")
        command = (
            f"{python_bin} -c "
            f"\"import importlib.metadata, sys; "
            f"sys.exit(0 if importlib.metadata.distribution('{quoted_name}') else 1)\""
        )
        return self._run_probe(command)

    def run_install(
        self,
        *,
        installer_type: str,
        package_spec: str,
        timeout_seconds: int = 120,
    ) -> SkillInstallResult:
        python_bin = self._resolve_python_command() if installer_type in ("uv", "pip") else None
        command = build_install_command(
            installer_type,
            package_spec,
            python_command=python_bin,
        )
        if command is None:
            return SkillInstallResult(
                success=False,
                message=f"unknown installer type '{installer_type}'",
            )

        command_str = " ".join(shlex.quote(part) for part in command)
        try:
            result = self.command_executor.run(
                command=command_str,
                workdir=self.working_dir,
                timeout_seconds=timeout_seconds,
            )
            return SkillInstallResult(
                success=result.exit_code == 0,
                exit_code=result.exit_code,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )
        except Exception as exc:
            return SkillInstallResult(success=False, message=str(exc))

    def _run_probe(self, command: str) -> bool:
        try:
            result = self.command_executor.run(
                command=command,
                workdir=self.working_dir,
                timeout_seconds=10,
            )
            return result.exit_code == 0
        except Exception:
            return False

    def _resolve_python_command(self) -> str | None:
        for candidate in self.python_candidates:
            if self.has_binary(candidate):
                return candidate
        if isinstance(self.command_executor, LocalCommandExecutor):
            return sys.executable
        return None
