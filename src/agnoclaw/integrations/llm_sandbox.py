"""Optional llm-sandbox runtime backend integration."""

from __future__ import annotations

import shlex
import shutil
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from agnoclaw.backends import RuntimeBackend
from agnoclaw.tools.backends import (
    BackgroundCommandHandle,
    BackgroundCommandOutput,
    CommandResult,
    LocalWorkspaceAdapter,
)
from agnoclaw.tools.browser_backends import BrowserBackend


@dataclass(frozen=True)
class _BackgroundTask:
    task_id: str
    pid: int
    log_path: str
    exit_path: str
    pid_path: str
    description: str | None = None


def _load_llm_sandbox() -> tuple[Any, Any]:
    try:
        from llm_sandbox import SandboxSession
        from llm_sandbox.const import SandboxBackend
    except ImportError as exc:  # pragma: no cover - exercised via tests
        raise ImportError(
            "LLMSandboxBackend requires the optional llm-sandbox integration. "
            "Install it with `pip install 'agnoclaw[llm-sandbox]'`."
        ) from exc
    return SandboxSession, SandboxBackend


class LLMSandboxCommandExecutor:
    """Command executor backed by one llm-sandbox session."""

    def __init__(
        self,
        *,
        session: Any,
        workspace_dir: Path,
        task_dir: str,
        max_background_tasks: int = 16,
    ) -> None:
        self._session = session
        self.workspace_dir = str(workspace_dir)
        self._task_dir = task_dir
        self._max_background_tasks = max_background_tasks
        self._tasks: dict[str, _BackgroundTask] = {}

    def run(
        self,
        *,
        command: str,
        workdir: str | None,
        timeout_seconds: int | None,
    ) -> CommandResult:
        del timeout_seconds
        started = time.monotonic()
        result = self._session.execute_command(
            command=command,
            workdir=self._resolve_workdir(workdir),
        )
        return CommandResult(
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            exit_code=int(result.exit_code),
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
        if len(self._tasks) >= self._max_background_tasks:
            raise RuntimeError(
                f"[error] Too many background tasks ({len(self._tasks)}). "
                "Use bash_kill or wait for tasks to finish."
            )

        task_id = f"task_{uuid4().hex[:12]}"
        log_path = f"{self._task_dir}/{task_id}.log"
        exit_path = f"{self._task_dir}/{task_id}.exit"
        pid_path = f"{self._task_dir}/{task_id}.pid"
        inner = self._build_background_script(
            command=command,
            workdir=self._resolve_workdir(workdir),
            exit_path=exit_path,
        )
        shell_command = (
            f"mkdir -p {shlex.quote(self._task_dir)} && "
            f"nohup sh -lc {shlex.quote(inner)} > {shlex.quote(log_path)} 2>&1 < /dev/null & "
            f"pid=$!; printf '%s' \"$pid\" > {shlex.quote(pid_path)}; printf '%s' \"$pid\""
        )
        result = self._session.execute_command(command=shell_command, workdir=None)
        if int(result.exit_code) != 0:
            raise RuntimeError(result.stderr or result.stdout or "failed to start background task")
        try:
            pid = int((result.stdout or "").strip().splitlines()[-1])
        except Exception as exc:
            raise RuntimeError("failed to capture background task pid") from exc

        self._tasks[task_id] = _BackgroundTask(
            task_id=task_id,
            pid=pid,
            log_path=log_path,
            exit_path=exit_path,
            pid_path=pid_path,
            description=description,
        )
        return BackgroundCommandHandle(
            task_id=task_id,
            pid=pid,
            status="running",
            log_path=log_path,
        )

    def output(
        self,
        *,
        task_id: str,
        max_chars: int = 8000,
        tail: bool = True,
    ) -> BackgroundCommandOutput:
        task = self._require_task(task_id)
        status, exit_code = self._task_status(task)
        output = self._read_runtime_text(task.log_path)
        return BackgroundCommandOutput(
            task_id=task.task_id,
            status=status,
            output=self._truncate_output(output, max_chars=max_chars, tail=tail),
            exit_code=exit_code,
            pid=task.pid,
        )

    def kill(self, *, task_id: str, force: bool = False) -> str:
        task = self._require_task(task_id)
        signal = "-9" if force else "-TERM"
        command = (
            f"if kill -0 {task.pid} 2>/dev/null; then "
            f"kill {signal} {task.pid} 2>/dev/null || true; "
            f"if command -v pkill >/dev/null 2>&1; then "
            f"pkill {signal} -P {task.pid} 2>/dev/null || true; "
            f"fi; "
            f"printf 'terminated'; "
            f"else printf 'not-running'; fi"
        )
        result = self._session.execute_command(command=command, workdir=None)
        outcome = (result.stdout or "").strip() or "not-running"
        if outcome == "terminated":
            return f"Sent {'SIGKILL' if force else 'SIGTERM'} to task {task_id} (pid {task.pid})"
        return f"Task {task_id} is not running"

    def _resolve_workdir(self, workdir: str | None) -> str:
        if workdir is None:
            return self.workspace_dir
        return str(Path(workdir).expanduser().resolve(strict=False))

    @staticmethod
    def _build_background_script(
        *,
        command: str,
        workdir: str,
        exit_path: str,
    ) -> str:
        return (
            f"cd {shlex.quote(workdir)} || exit $?; "
            f"{command}; "
            f"code=$?; "
            f"printf '%s' \"$code\" > {shlex.quote(exit_path)}; "
            f"exit $code"
        )

    @staticmethod
    def _truncate_output(text: str, *, max_chars: int, tail: bool) -> str:
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        if tail:
            return f"... [truncated to last {max_chars} chars]\n{text[-max_chars:]}"
        return text[:max_chars] + f"\n... [truncated to first {max_chars} chars]"

    def _require_task(self, task_id: str) -> _BackgroundTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise RuntimeError(f"Unknown background task: {task_id}")
        return task

    def _task_status(self, task: _BackgroundTask) -> tuple[str, int | None]:
        command = (
            f"if [ -f {shlex.quote(task.exit_path)} ]; then "
            f"printf 'status=exited\\n'; "
            f"printf 'exit_code='; cat {shlex.quote(task.exit_path)}; printf '\\n'; "
            f"elif kill -0 {task.pid} 2>/dev/null; then "
            f"printf 'status=running\\n'; "
            f"else printf 'status=terminated\\n'; fi"
        )
        result = self._session.execute_command(command=command, workdir=None)
        status = "terminated"
        exit_code: int | None = None
        for line in (result.stdout or "").splitlines():
            if line.startswith("status="):
                status = line.partition("=")[2].strip() or "terminated"
            if line.startswith("exit_code="):
                value = line.partition("=")[2].strip()
                if value:
                    try:
                        exit_code = int(value)
                    except ValueError:
                        exit_code = None
        return status, exit_code

    def _read_runtime_text(self, runtime_path: str) -> str:
        with tempfile.TemporaryDirectory() as tmp_dir:
            local_path = Path(tmp_dir) / Path(runtime_path).relative_to("/")
            try:
                self._session.copy_from_runtime(runtime_path, str(local_path))
            except FileNotFoundError:
                return ""
            return local_path.read_text(encoding="utf-8", errors="replace")

    def _prune_finished_tasks(self) -> None:
        if len(self._tasks) < self._max_background_tasks:
            return
        finished = [
            task_id
            for task_id, task in self._tasks.items()
            if self._task_status(task)[0] != "running"
        ]
        for task_id in finished:
            if len(self._tasks) < self._max_background_tasks:
                break
            self._tasks.pop(task_id, None)


class LLMSandboxWorkspaceAdapter:
    """Workspace adapter that copies files to and from one sandbox session."""

    def __init__(self, *, session: Any, workspace_dir: Path) -> None:
        self._session = session
        self.workspace_dir = workspace_dir
        self._local = LocalWorkspaceAdapter(workspace_dir=workspace_dir)

    def read_file(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        runtime_path = self._resolve_path(path)
        try:
            with self._copied_from_runtime(runtime_path) as local_path:
                return self._local.read_file(str(local_path), offset=offset, limit=limit)
        except FileNotFoundError:
            return f"[error] File not found: {runtime_path}"

    def write_file(self, path: str, content: str) -> str:
        runtime_path = self._resolve_path(path)
        with tempfile.TemporaryDirectory() as tmp_dir:
            local_path = Path(tmp_dir) / runtime_path.relative_to("/")
            result = self._local.write_file(str(local_path), content)
            self._copy_to_runtime(local_path, runtime_path)
            return self._rewrite_paths(result, local_path, runtime_path)

    def edit_file(self, path: str, old_string: str, new_string: str) -> str:
        runtime_path = self._resolve_path(path)
        try:
            with self._copied_from_runtime(runtime_path) as local_path:
                result = self._local.edit_file(str(local_path), old_string, new_string)
                if result.startswith("[error]"):
                    return self._rewrite_paths(result, local_path, runtime_path)
                self._copy_to_runtime(local_path, runtime_path)
                return self._rewrite_paths(result, local_path, runtime_path)
        except FileNotFoundError:
            return f"[error] File not found: {runtime_path}. Read the file first."

    def multi_edit_file(self, path: str, edits: list[dict[str, str]]) -> str:
        runtime_path = self._resolve_path(path)
        try:
            with self._copied_from_runtime(runtime_path) as local_path:
                result = self._local.multi_edit_file(str(local_path), edits)
                if result.startswith("[error]"):
                    return self._rewrite_paths(result, local_path, runtime_path)
                self._copy_to_runtime(local_path, runtime_path)
                return self._rewrite_paths(result, local_path, runtime_path)
        except FileNotFoundError:
            return f"[error] File not found: {runtime_path}. Read the file first."

    def glob_files(
        self,
        pattern: str,
        base_dir: str | None = None,
        path: str | None = None,
    ) -> str:
        if base_dir or path:
            runtime_path = self._resolve_path(base_dir or path)
        else:
            runtime_path = self.workspace_dir
        try:
            with self._copied_from_runtime(runtime_path) as local_path:
                result = self._local.glob_files(pattern=pattern, path=str(local_path))
                return self._rewrite_paths(result, local_path, runtime_path)
        except FileNotFoundError:
            return f"[error] Directory not found: {runtime_path}"

    def grep_files(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        case_insensitive: bool = False,
        context_lines: int = 0,
        max_results: int = 50,
    ) -> str:
        runtime_path = self._resolve_path(path) if path else self.workspace_dir
        try:
            with self._copied_from_runtime(runtime_path) as local_path:
                result = self._local.grep_files(
                    pattern=pattern,
                    path=str(local_path),
                    glob=glob,
                    case_insensitive=case_insensitive,
                    context_lines=context_lines,
                    max_results=max_results,
                )
                return self._rewrite_paths(result, local_path, runtime_path)
        except FileNotFoundError:
            return f"[error] Directory not found: {runtime_path}"

    def list_dir(self, path: str | None = None) -> str:
        runtime_path = self._resolve_path(path) if path else self.workspace_dir
        try:
            with self._copied_from_runtime(runtime_path) as local_path:
                result = self._local.list_dir(str(local_path))
                return self._rewrite_paths(result, local_path, runtime_path)
        except FileNotFoundError:
            return f"[error] Directory not found: {runtime_path}"

    def _resolve_path(self, path: str | None) -> Path:
        if path is None:
            return self.workspace_dir
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_dir / candidate
        return candidate.resolve(strict=False)

    def _copied_from_runtime(self, runtime_path: Path):
        adapter = self

        class _RuntimeCopyContext:
            def __enter__(self_nonlocal) -> Path:
                self_nonlocal._tmp_dir = tempfile.TemporaryDirectory()
                local_path = Path(self_nonlocal._tmp_dir.name) / runtime_path.relative_to("/")
                copied_path = _copy_from_runtime_path(
                    adapter._session,
                    runtime_path=runtime_path,
                    local_path=local_path,
                )
                self_nonlocal._local_path = copied_path
                return copied_path

            def __exit__(self_nonlocal, exc_type, exc, tb) -> None:
                self_nonlocal._tmp_dir.cleanup()

        return _RuntimeCopyContext()

    def _copy_to_runtime(self, local_path: Path, runtime_path: Path) -> None:
        self._session.execute_command(
            command=f"mkdir -p {shlex.quote(str(runtime_path.parent))}",
            workdir=None,
        )
        self._session.copy_to_runtime(str(local_path), str(runtime_path))

    @staticmethod
    def _rewrite_paths(result: str, local_path: Path, runtime_path: Path) -> str:
        return result.replace(str(local_path), str(runtime_path))


class LLMSandboxBackend(RuntimeBackend):
    """
    Docker-first llm-sandbox integration with explicit workspace sync.

    By default this backend creates an llm-sandbox Docker session, mirrors the
    same absolute workspace path inside the sandbox, and keeps later sync
    decisions explicit through `sync_to_runtime()` and `sync_from_runtime()`.
    """

    def __init__(
        self,
        *,
        session: Any | None = None,
        sandbox_backend: str = "docker",
        lang: str = "python",
        sync_paths: Iterable[str | Path] = (),
        browser_backend: BrowserBackend | None = None,
        session_kwargs: dict[str, Any] | None = None,
        max_background_tasks: int = 16,
    ) -> None:
        super().__init__(browser_backend=browser_backend)
        self._session = session
        self._owns_session = session is None
        self._sandbox_backend = sandbox_backend
        self._lang = lang
        self._sync_paths = tuple(sync_paths)
        self._session_kwargs = dict(session_kwargs or {})
        self._max_background_tasks = max_background_tasks
        self._bound_workspace_dir: Path | None = None
        self._command_executor: LLMSandboxCommandExecutor | None = None
        self._workspace_adapter: LLMSandboxWorkspaceAdapter | None = None

    @property
    def workspace_dir(self) -> Path | None:
        return self._bound_workspace_dir

    def bind(self, workspace_dir: str | Path) -> LLMSandboxBackend:
        self._bind_workspace(workspace_dir)
        return self

    def close(self) -> None:
        if self._owns_session and self._session is not None:
            self._session.close()

    def __enter__(self) -> LLMSandboxBackend:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def resolve_command_executor(self, *, workspace_dir: str | Path):
        self._bind_workspace(workspace_dir)
        assert self._command_executor is not None
        return self._command_executor

    def resolve_workspace_adapter(self, *, workspace_dir: str | Path):
        self._bind_workspace(workspace_dir)
        assert self._workspace_adapter is not None
        return self._workspace_adapter

    def sync_to_runtime(self, *paths: str | Path) -> None:
        workspace = self._require_workspace_binding()
        session = self._ensure_session()
        for host_path in self._resolve_sync_paths(paths, workspace):
            if not host_path.exists():
                raise FileNotFoundError(f"Host path not found: {host_path}")
            session.execute_command(
                command=f"mkdir -p {shlex.quote(str(host_path.parent))}",
                workdir=None,
            )
            session.copy_to_runtime(str(host_path), str(host_path))

    def sync_from_runtime(self, *paths: str | Path) -> None:
        workspace = self._require_workspace_binding()
        session = self._ensure_session()
        for host_path in self._resolve_sync_paths(paths, workspace):
            _copy_from_runtime_path(
                session,
                runtime_path=host_path,
                local_path=host_path,
            )

    def _bind_workspace(self, workspace_dir: str | Path) -> None:
        resolved_workspace = Path(workspace_dir).expanduser().resolve(strict=False)
        if (
            self._bound_workspace_dir is not None
            and self._bound_workspace_dir != resolved_workspace
        ):
            raise RuntimeError(
                "LLMSandboxBackend is already bound to "
                f"{self._bound_workspace_dir}; create a new backend for {resolved_workspace}."
            )
        if self._bound_workspace_dir is not None:
            return

        session = self._ensure_session()
        session.execute_command(
            command=f"mkdir -p {shlex.quote(str(resolved_workspace))}",
            workdir=None,
        )
        task_dir = f"/tmp/agnoclaw-llm-sandbox/{uuid4().hex[:12]}"
        session.execute_command(command=f"mkdir -p {shlex.quote(task_dir)}", workdir=None)
        self._bound_workspace_dir = resolved_workspace
        self._command_executor = LLMSandboxCommandExecutor(
            session=session,
            workspace_dir=resolved_workspace,
            task_dir=task_dir,
            max_background_tasks=self._max_background_tasks,
        )
        self._workspace_adapter = LLMSandboxWorkspaceAdapter(
            session=session,
            workspace_dir=resolved_workspace,
        )
        if self._sync_paths:
            self.sync_to_runtime(*self._sync_paths)

    def _ensure_session(self) -> Any:
        if self._session is None:
            session_factory, sandbox_backend_enum = _load_llm_sandbox()
            backend_value = self._coerce_sandbox_backend(sandbox_backend_enum)
            self._session = session_factory(
                backend=backend_value,
                lang=self._lang,
                **self._session_kwargs,
            )
        if not bool(getattr(self._session, "is_open", False)):
            self._session.open()
        return self._session

    def _coerce_sandbox_backend(self, sandbox_backend_enum: Any) -> Any:
        try:
            return getattr(sandbox_backend_enum, str(self._sandbox_backend).upper())
        except AttributeError as exc:
            supported = ", ".join(member.name.lower() for member in sandbox_backend_enum)
            raise ValueError(
                f"Unsupported llm-sandbox backend '{self._sandbox_backend}'. "
                f"Choose one of: {supported}."
            ) from exc

    def _require_workspace_binding(self) -> Path:
        if self._bound_workspace_dir is None:
            raise RuntimeError(
                "LLMSandboxBackend must be bound to a workspace first. "
                "Pass it to AgentHarness(..., backend=...) or call bind(workspace_dir)."
            )
        return self._bound_workspace_dir

    @staticmethod
    def _resolve_sync_paths(
        paths: Iterable[str | Path],
        workspace_dir: Path,
    ) -> list[Path]:
        resolved: list[Path] = []
        for raw_path in paths:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                candidate = workspace_dir / candidate
            resolved.append(candidate.resolve(strict=False))
        return resolved


def _copy_from_runtime_path(
    session: Any,
    *,
    runtime_path: Path,
    local_path: Path,
) -> Path:
    """Normalize llm-sandbox archive extraction for file and directory copies."""
    session.copy_from_runtime(str(runtime_path), str(local_path))
    nested_path = local_path / runtime_path.name
    if local_path.exists() and not nested_path.exists():
        return local_path

    if not nested_path.exists():
        raise FileNotFoundError(runtime_path)

    local_path.mkdir(parents=True, exist_ok=True)
    for child in nested_path.iterdir():
        shutil.move(str(child), str(local_path / child.name))
    nested_path.rmdir()
    return local_path
