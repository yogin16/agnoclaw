"""Backend abstractions for the built-in workspace tool family."""

from __future__ import annotations

import fnmatch
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

_MAX_READ_SIZE = 50 * 1024 * 1024  # 50MB guard for read_file


@dataclass(frozen=True)
class CommandResult:
    """Normalized result for a foreground command execution."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration_ms: int | None = None


@dataclass(frozen=True)
class BackgroundCommandHandle:
    """Metadata returned when a background command starts."""

    task_id: str
    pid: int | None = None
    status: str = "running"
    log_path: str | None = None


@dataclass(frozen=True)
class BackgroundCommandOutput:
    """Snapshot of background command output and lifecycle state."""

    task_id: str
    status: str
    output: str
    exit_code: int | None = None
    pid: int | None = None


class CommandExecutor(Protocol):
    """Backend interface for shell execution."""

    def run(
        self,
        *,
        command: str,
        workdir: str | None,
        timeout_seconds: int | None,
    ) -> CommandResult:
        ...

    def start(
        self,
        *,
        command: str,
        workdir: str | None,
        description: str | None = None,
    ) -> BackgroundCommandHandle:
        ...

    def output(
        self,
        *,
        task_id: str,
        max_chars: int = 8000,
        tail: bool = True,
    ) -> BackgroundCommandOutput:
        ...

    def kill(self, *, task_id: str, force: bool = False) -> str:
        ...


class WorkspaceAdapter(Protocol):
    """Backend interface for workspace/file operations."""

    workspace_dir: Path

    def read_file(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        ...

    def write_file(self, path: str, content: str) -> str:
        ...

    def edit_file(self, path: str, old_string: str, new_string: str) -> str:
        ...

    def multi_edit_file(self, path: str, edits: list[dict[str, str]]) -> str:
        ...

    def glob_files(
        self,
        pattern: str,
        base_dir: str | None = None,
        path: str | None = None,
    ) -> str:
        ...

    def grep_files(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        case_insensitive: bool = False,
        context_lines: int = 0,
        max_results: int = 50,
    ) -> str:
        ...

    def list_dir(self, path: str | None = None) -> str:
        ...


@dataclass
class _BackgroundTask:
    task_id: str
    process: subprocess.Popen
    command: str
    output_path: Path
    output_file: Any
    working_dir: str | None
    started_at: float
    description: str | None = None


class LocalCommandExecutor:
    """Host-local subprocess implementation for command execution."""

    def __init__(
        self,
        *,
        workspace_dir: str | Path | None = None,
        max_background_tasks: int = 16,
    ) -> None:
        self.workspace_dir = (
            str(Path(workspace_dir).expanduser().resolve())
            if workspace_dir is not None
            else None
        )
        self.max_background_tasks = max_background_tasks
        self._tasks: dict[str, _BackgroundTask] = {}
        self._tasks_dir = Path.home() / ".agnoclaw" / "tmp" / "bash_tasks"
        self._tasks_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_cwd(self, working_dir: str | None) -> str | None:
        candidate = working_dir or self.workspace_dir
        if candidate is None:
            return None
        return str(Path(candidate).expanduser().resolve())

    @staticmethod
    def _tail_text(text: str, max_chars: int, tail: bool) -> str:
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        if tail:
            return f"... [truncated to last {max_chars} chars]\n{text[-max_chars:]}"
        return text[:max_chars] + f"\n... [truncated to first {max_chars} chars]"

    @staticmethod
    def _cleanup_task(task: _BackgroundTask) -> None:
        try:
            if task.output_file and not task.output_file.closed:
                task.output_file.close()
        except Exception:
            pass

    def _prune_finished_tasks(self) -> None:
        if len(self._tasks) <= self.max_background_tasks:
            return
        finished = [task for task in self._tasks.values() if task.process.poll() is not None]
        if not finished:
            return
        finished.sort(key=lambda task: task.started_at)
        for task in finished:
            if len(self._tasks) <= self.max_background_tasks:
                break
            self._cleanup_task(task)
            self._tasks.pop(task.task_id, None)

    def run(
        self,
        *,
        command: str,
        workdir: str | None,
        timeout_seconds: int | None,
    ) -> CommandResult:
        cwd = self._resolve_cwd(workdir)
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired as exc:
            timeout = timeout_seconds if timeout_seconds is not None else "unknown"
            raise RuntimeError(f"Command timed out after {timeout} seconds: {command}") from exc
        except Exception as exc:
            raise RuntimeError(f"Failed to execute command: {exc}") from exc

        return CommandResult(
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            exit_code=int(result.returncode),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def start(
        self,
        *,
        command: str,
        workdir: str | None,
        description: str | None = None,
    ) -> BackgroundCommandHandle:
        self._prune_finished_tasks()
        if len(self._tasks) >= self.max_background_tasks:
            raise RuntimeError(
                f"[error] Too many background tasks ({len(self._tasks)}). "
                "Use bash_kill or wait for tasks to finish."
            )

        task_id = f"task_{uuid4().hex[:12]}"
        output_path = self._tasks_dir / f"{task_id}.log"
        cwd = self._resolve_cwd(workdir)
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
        except Exception as exc:
            if output_file is not None and not output_file.closed:
                output_file.close()
            raise RuntimeError(f"Failed to start background command: {exc}") from exc

        self._tasks[task_id] = _BackgroundTask(
            task_id=task_id,
            process=process,
            command=command,
            output_path=output_path,
            output_file=output_file,
            working_dir=cwd,
            started_at=time.time(),
            description=description,
        )
        return BackgroundCommandHandle(
            task_id=task_id,
            pid=process.pid,
            status="running",
            log_path=str(output_path),
        )

    def output(
        self,
        *,
        task_id: str,
        max_chars: int = 8000,
        tail: bool = True,
    ) -> BackgroundCommandOutput:
        task = self._tasks.get(task_id)
        if task is None:
            raise RuntimeError(f"Unknown task id: {task_id}")

        exit_code = task.process.poll()
        if exit_code is None:
            status = "running"
        else:
            status = "exited"
            self._cleanup_task(task)

        if not task.output_path.exists():
            body = "[no output yet]"
        else:
            body = task.output_path.read_text(encoding="utf-8", errors="replace")
            body = self._tail_text(body, max_chars=max_chars, tail=tail)
            if not body.strip():
                body = "[no output yet]"

        return BackgroundCommandOutput(
            task_id=task_id,
            status=status,
            output=body,
            exit_code=int(exit_code) if exit_code is not None else None,
            pid=task.process.pid,
        )

    def kill(self, *, task_id: str, force: bool = False) -> str:
        task = self._tasks.get(task_id)
        if task is None:
            raise RuntimeError(f"Unknown task id: {task_id}")

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
        except Exception as exc:
            raise RuntimeError(f"Failed to kill task {task_id}: {exc}") from exc


class LocalWorkspaceAdapter:
    """Host-local pathlib implementation for file operations."""

    def __init__(self, workspace_dir: str | Path | None = None) -> None:
        self.workspace_dir = (
            Path(workspace_dir).expanduser().resolve()
            if workspace_dir is not None
            else Path.cwd().resolve()
        )

    def read_file(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        file_path = Path(path).expanduser()
        if not file_path.exists():
            return f"[error] File not found: {path}"
        if not file_path.is_file():
            return f"[error] Not a file: {path}"

        try:
            file_size = file_path.stat().st_size
            if file_size > _MAX_READ_SIZE:
                return (
                    f"[error] File too large ({file_size // (1024 * 1024)}MB). "
                    f"Max readable size is {_MAX_READ_SIZE // (1024 * 1024)}MB."
                )
            lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(0, offset - 1) if offset > 0 else 0
            end = start + limit
            selected = lines[start:end]
            numbered = [f"{start + i + 1:6}\t{line}" for i, line in enumerate(selected)]
            result = "\n".join(numbered)
            if len(lines) > end:
                result += f"\n... ({len(lines) - end} more lines)"
            return result
        except Exception as exc:
            return f"[error] Could not read {path}: {exc}"

    def write_file(self, path: str, content: str) -> str:
        file_path = Path(path).expanduser()
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            lines = content.count("\n") + (0 if content.endswith("\n") else 1)
            return f"Written {lines} lines to {path}"
        except Exception as exc:
            return f"[error] Could not write {path}: {exc}"

    def edit_file(self, path: str, old_string: str, new_string: str) -> str:
        file_path = Path(path).expanduser()
        if not file_path.exists():
            return f"[error] File not found: {path}. Read the file first."

        try:
            content = file_path.read_text(encoding="utf-8")
            count = content.count(old_string)
            if count == 0:
                return (
                    f"[error] old_string not found in {path}.\n"
                    f"Make sure to read the file first and copy the exact text including whitespace."
                )
            if count > 1:
                return (
                    f"[error] old_string appears {count} times in {path}. "
                    f"Provide more surrounding context to make it unique."
                )
            new_content = content.replace(old_string, new_string, 1)
            file_path.write_text(new_content, encoding="utf-8")
            return f"Edited {path}: replaced 1 occurrence."
        except Exception as exc:
            return f"[error] Could not edit {path}: {exc}"

    def multi_edit_file(self, path: str, edits: list[dict[str, str]]) -> str:
        file_path = Path(path).expanduser()
        if not file_path.exists():
            return f"[error] File not found: {path}. Read the file first."
        if not edits:
            return "[error] No edits provided."

        try:
            content = file_path.read_text(encoding="utf-8")
            for i, edit in enumerate(edits):
                old_str = edit.get("old_string", "")
                if not old_str:
                    return f"[error] Edit {i}: old_string is empty."
                count = content.count(old_str)
                if count == 0:
                    return (
                        f"[error] Edit {i}: old_string not found in {path}.\n"
                        f"Make sure to read the file first and copy the exact text including whitespace."
                    )
                if count > 1:
                    return (
                        f"[error] Edit {i}: old_string appears {count} times in {path}. "
                        f"Provide more surrounding context to make it unique."
                    )

            for edit in edits:
                content = content.replace(edit["old_string"], edit.get("new_string", ""), 1)

            file_path.write_text(content, encoding="utf-8")
            return f"Edited {path}: applied {len(edits)} replacements."
        except Exception as exc:
            return f"[error] Could not edit {path}: {exc}"

    def glob_files(
        self,
        pattern: str,
        base_dir: str | None = None,
        path: str | None = None,
    ) -> str:
        directory = base_dir or path
        search_dir = Path(directory).expanduser() if directory else self.workspace_dir
        if not search_dir.exists():
            return f"[error] Directory not found: {search_dir}"

        try:
            matches = list(search_dir.glob(pattern))

            def _safe_mtime(candidate: Path) -> float:
                try:
                    return candidate.stat().st_mtime
                except OSError:
                    return 0.0

            matches.sort(key=_safe_mtime, reverse=True)
            if not matches:
                return f"[no matches] Pattern '{pattern}' found no files in {search_dir}"
            return "\n".join(str(match) for match in matches)
        except Exception as exc:
            return f"[error] Glob failed: {exc}"

    def grep_files(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        case_insensitive: bool = False,
        context_lines: int = 0,
        max_results: int = 50,
    ) -> str:
        search_path = Path(path).expanduser() if path else self.workspace_dir
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return f"[error] Invalid regex pattern: {exc}"

        results: list[str] = []
        count = 0

        def search_file(file_path: Path) -> None:
            nonlocal count
            if count >= max_results:
                return
            try:
                lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
                for i, line in enumerate(lines):
                    if compiled.search(line):
                        if context_lines > 0:
                            start = max(0, i - context_lines)
                            end = min(len(lines), i + context_lines + 1)
                            for j in range(start, end):
                                prefix = ">" if j == i else " "
                                results.append(f"{file_path}:{j + 1}{prefix} {lines[j]}")
                            results.append("---")
                        else:
                            results.append(f"{file_path}:{i + 1}: {line}")
                        count += 1
                        if count >= max_results:
                            break
            except Exception:
                pass

        if search_path.is_file():
            search_file(search_path)
        else:
            for file_path in search_path.rglob("*"):
                if not file_path.is_file():
                    continue
                if glob and not fnmatch.fnmatch(file_path.name, glob):
                    continue
                if any(part.startswith(".") for part in file_path.parts):
                    continue
                search_file(file_path)
                if count >= max_results:
                    break

        if not results:
            return f"[no matches] Pattern '{pattern}' not found"

        output = "\n".join(results)
        if count >= max_results:
            output += f"\n... [truncated at {max_results} matches]"
        return output

    def list_dir(self, path: str | None = None) -> str:
        dir_path = Path(path).expanduser() if path else self.workspace_dir
        if not dir_path.exists():
            return f"[error] Directory not found: {dir_path}"
        if not dir_path.is_dir():
            return f"[error] Not a directory: {dir_path}"

        try:
            entries = sorted(dir_path.iterdir(), key=lambda candidate: (candidate.is_file(), candidate.name))
            lines = []
            for entry in entries:
                if entry.is_dir():
                    lines.append(f"d  {entry.name}/")
                else:
                    size = entry.stat().st_size
                    size_str = f"{size:>8}" if size < 1024 else f"{size // 1024:>7}K"
                    lines.append(f"f {size_str}  {entry.name}")
            return f"{dir_path}:\n" + "\n".join(lines) if lines else f"{dir_path}: (empty)"
        except Exception as exc:
            return f"[error] Could not list {dir_path}: {exc}"
