"""Fresh-process subjects for evidence-gated improvement evaluation.

The boundary is deliberately a tiny JSON stdin/stdout protocol. It isolates Python
state, crashes, environment, working-directory state, and process lifetime without
requiring factories or provider clients to be pickleable. It is not a filesystem,
network, kernel, or credential sandbox; hosts can place the exact command inside a
container/VM/sandbox while retaining the same protocol.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .improvement_corpus import EvaluationCase
from .improvement_runner import EvaluationRollout
from .runtime.security import freeze_data, thaw_data

PROCESS_EVALUATION_PROTOCOL_VERSION = "agnoclaw.improvement.process.v1"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SAFE_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024
_DEFAULT_MAX_STDOUT_BYTES = 1024 * 1024
_DEFAULT_MAX_STDERR_BYTES = 64 * 1024
_WINDOWS_CREATE_NEW_PROCESS_GROUP = int(
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        thaw_data(freeze_data(value)),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def _safe_error_type(error: BaseException) -> str:
    name = type(error).__name__
    return name if _SAFE_ERROR_TYPE_RE.fullmatch(name) else "ProcessWorkerError"


class ProcessEvaluationError(RuntimeError):
    """Base class whose messages never include child output or environment values."""


class ProcessEvaluationRequestLimitError(ProcessEvaluationError):
    """The canonical case request exceeded the configured process boundary."""


class ProcessEvaluationOutputLimitError(ProcessEvaluationError):
    """A child stream crossed its configured byte boundary."""

    def __init__(self, stream: str) -> None:
        super().__init__("The evaluation process exceeded a bounded output stream.")
        self.stream = stream


class ProcessEvaluationExitedError(ProcessEvaluationError):
    """The child exited without a successful protocol response."""

    def __init__(self, returncode: int) -> None:
        super().__init__("The evaluation process exited unsuccessfully.")
        self.returncode = returncode


class ProcessEvaluationProtocolError(ProcessEvaluationError):
    """The child response did not satisfy the exact protocol contract."""


class ProcessEvaluationRemoteError(ProcessEvaluationError):
    """The worker reported a bounded error type without an error message."""

    def __init__(self, remote_error_type: str) -> None:
        super().__init__("The evaluation worker reported a rollout failure.")
        self.remote_error_type = remote_error_type


class ProcessEvaluationCleanupError(ProcessEvaluationError):
    """A child or owned working directory could not be deterministically released."""


@dataclass(frozen=True, slots=True, repr=False)
class _ProcessEvaluationConfig:
    command: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    working_directory: str | None
    max_request_bytes: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    terminate_grace_seconds: float
    subject_contract_digest: str
    subject_isolation_digest: str


def _bounded_bytes(value: int, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def _build_config(
    command: Sequence[str],
    *,
    environment: Mapping[str, str] | None,
    working_directory: str | os.PathLike[str] | None,
    max_request_bytes: int,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    terminate_grace_seconds: float,
) -> _ProcessEvaluationConfig:
    values = tuple(command)
    if not 1 <= len(values) <= 64:
        raise ValueError("command must contain between 1 and 64 arguments")
    if any(not isinstance(item, str) for item in values):
        raise TypeError("command arguments must be strings")
    if any(not item or "\x00" in item or len(item) > 4096 for item in values):
        raise ValueError("command arguments must be non-empty, bounded, and NUL-free")
    if sum(len(item.encode("utf-8")) for item in values) > 64 * 1024:
        raise ValueError("command cannot exceed 65536 UTF-8 bytes")
    executable = Path(values[0])
    if not executable.is_absolute():
        raise ValueError("the process executable must be an absolute path")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("the process executable must be an executable file")

    supplied_environment = {} if environment is None else dict(environment)
    if len(supplied_environment) > 128:
        raise ValueError("environment cannot contain more than 128 entries")
    normalized_environment: list[tuple[str, str]] = []
    environment_bytes = 0
    for name, value in supplied_environment.items():
        if not isinstance(name, str) or _ENVIRONMENT_NAME_RE.fullmatch(name) is None:
            raise ValueError("environment names must use the portable identifier grammar")
        if not isinstance(value, str):
            raise TypeError("environment values must be strings")
        if "\x00" in value:
            raise ValueError("environment values must be NUL-free")
        environment_bytes += len(name.encode()) + len(value.encode())
        normalized_environment.append((name, value))
    if environment_bytes > 64 * 1024:
        raise ValueError("environment cannot exceed 65536 UTF-8 bytes")
    normalized_environment.sort()

    normalized_working_directory: str | None = None
    if working_directory is not None:
        path = Path(working_directory)
        if not path.is_absolute():
            raise ValueError("working_directory must be absolute")
        if not path.is_dir():
            raise ValueError("working_directory must be an existing directory")
        normalized_working_directory = str(path)

    request_limit = _bounded_bytes(
        max_request_bytes,
        name="max_request_bytes",
        maximum=16 * 1024 * 1024,
    )
    stdout_limit = _bounded_bytes(
        max_stdout_bytes,
        name="max_stdout_bytes",
        maximum=16 * 1024 * 1024,
    )
    stderr_limit = _bounded_bytes(
        max_stderr_bytes,
        name="max_stderr_bytes",
        maximum=1024 * 1024,
    )
    if (
        isinstance(terminate_grace_seconds, bool)
        or not isinstance(terminate_grace_seconds, (int, float))
        or not math.isfinite(float(terminate_grace_seconds))
        or not 0 < float(terminate_grace_seconds) <= 10
    ):
        raise ValueError("terminate_grace_seconds must be finite and between 0 and 10")

    isolation_contract = {
        "protocol_version": PROCESS_EVALUATION_PROTOCOL_VERSION,
        "environment_value_digests": {
            name: _digest(value) for name, value in normalized_environment
        },
        "working_directory": normalized_working_directory or "fresh_temporary_directory",
        "max_request_bytes": request_limit,
        "max_stdout_bytes": stdout_limit,
        "max_stderr_bytes": stderr_limit,
        "terminate_grace_seconds": float(terminate_grace_seconds),
        "process_group": os.name == "posix",
    }
    subject_isolation_digest = _digest(isolation_contract)
    contract = {
        "protocol_version": PROCESS_EVALUATION_PROTOCOL_VERSION,
        "command": list(values),
        "subject_isolation_digest": subject_isolation_digest,
    }
    return _ProcessEvaluationConfig(
        command=values,
        environment=tuple(normalized_environment),
        working_directory=normalized_working_directory,
        max_request_bytes=request_limit,
        max_stdout_bytes=stdout_limit,
        max_stderr_bytes=stderr_limit,
        terminate_grace_seconds=float(terminate_grace_seconds),
        subject_contract_digest=_digest(contract),
        subject_isolation_digest=subject_isolation_digest,
    )


async def _read_bounded(
    reader: asyncio.StreamReader,
    *,
    stream: str,
    maximum: int,
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := await reader.read(64 * 1024):
        size += len(chunk)
        if size > maximum:
            raise ProcessEvaluationOutputLimitError(stream)
        chunks.append(chunk)
    return b"".join(chunks)


async def _drain(reader: asyncio.StreamReader) -> None:
    while await reader.read(64 * 1024):
        pass


async def _await_rollout(value: Awaitable[EvaluationRollout]) -> EvaluationRollout:
    return await value


async def _stop_process(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float,
) -> None:
    if process.returncode is not None:
        await process.wait()
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows is not yet a certified release lane.
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        async with asyncio.timeout(grace_seconds):
            await asyncio.shield(process.wait())
        return
    except TimeoutError:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - Windows is not yet a certified release lane.
            process.kill()
    except ProcessLookupError:
        pass
    await asyncio.shield(process.wait())


class ProcessEvaluationSubject:
    """Execute one case in one fresh child process using a bounded JSON protocol."""

    __slots__ = (
        "_config",
        "_process",
        "_setup_complete",
        "_temporary_directory",
        "_used",
        "_working_directory",
    )

    def __init__(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
        working_directory: str | os.PathLike[str] | None = None,
        max_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
        max_stdout_bytes: int = _DEFAULT_MAX_STDOUT_BYTES,
        max_stderr_bytes: int = _DEFAULT_MAX_STDERR_BYTES,
        terminate_grace_seconds: float = 1.0,
        _config: _ProcessEvaluationConfig | None = None,
    ) -> None:
        self._config = _config or _build_config(
            command,
            environment=environment,
            working_directory=working_directory,
            max_request_bytes=max_request_bytes,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            terminate_grace_seconds=terminate_grace_seconds,
        )
        self._process: asyncio.subprocess.Process | None = None
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._working_directory: str | None = None
        self._setup_complete = False
        self._used = False

    @property
    def subject_contract_digest(self) -> str:
        return self._config.subject_contract_digest

    @property
    def subject_isolation_digest(self) -> str:
        return self._config.subject_isolation_digest

    def __repr__(self) -> str:
        return (
            f"ProcessEvaluationSubject(executable={Path(self._config.command[0]).name!r}, "
            f"contract_digest={self.subject_contract_digest!r})"
        )

    async def asetup(self) -> None:
        if self._setup_complete or self._used:
            raise ProcessEvaluationError("The evaluation subject cannot be set up twice.")
        if self._config.working_directory is None:
            self._temporary_directory = tempfile.TemporaryDirectory(
                prefix="agnoclaw-improvement-",
            )
            self._working_directory = self._temporary_directory.name
        else:
            self._working_directory = self._config.working_directory
        self._setup_complete = True

    async def __call__(self, case: EvaluationCase) -> EvaluationRollout:
        if not isinstance(case, EvaluationCase):
            raise TypeError("case must be an EvaluationCase")
        if not self._setup_complete or self._working_directory is None:
            raise ProcessEvaluationError("asetup() must complete before process execution.")
        if self._used:
            raise ProcessEvaluationError("A process evaluation subject is single-use.")
        self._used = True
        request_id = uuid4().hex
        request = _canonical_bytes(
            {
                "protocol_version": PROCESS_EVALUATION_PROTOCOL_VERSION,
                "request_id": request_id,
                "case": case.to_dict(),
            }
        )
        if len(request) > self._config.max_request_bytes:
            raise ProcessEvaluationRequestLimitError(
                "The evaluation case exceeded the process request boundary."
            )

        process_kwargs: dict[str, Any] = {}
        if os.name == "posix":
            process_kwargs["start_new_session"] = True
        elif os.name == "nt":  # pragma: no cover - Windows is not yet certified.
            process_kwargs["creationflags"] = _WINDOWS_CREATE_NEW_PROCESS_GROUP
        process = await asyncio.create_subprocess_exec(
            *self._config.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._working_directory,
            env=dict(self._config.environment),
            **process_kwargs,
        )
        self._process = process
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(
            _read_bounded(
                process.stdout,
                stream="stdout",
                maximum=self._config.max_stdout_bytes,
            )
        )
        stderr_task = asyncio.create_task(
            _read_bounded(
                process.stderr,
                stream="stderr",
                maximum=self._config.max_stderr_bytes,
            )
        )
        wait_task = asyncio.create_task(process.wait())
        try:
            try:
                process.stdin.write(request)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            stdout, _stderr, returncode = await asyncio.gather(
                stdout_task,
                stderr_task,
                wait_task,
            )
        except BaseException:
            await _stop_process(
                process,
                grace_seconds=self._config.terminate_grace_seconds,
            )
            for task in (stdout_task, stderr_task, wait_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                stdout_task,
                stderr_task,
                wait_task,
                return_exceptions=True,
            )
            await asyncio.gather(
                _drain(process.stdout),
                _drain(process.stderr),
            )
            raise
        finally:
            self._process = None

        if returncode != 0:
            raise ProcessEvaluationExitedError(returncode)
        try:
            response = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProcessEvaluationProtocolError(
                "The evaluation process returned invalid protocol JSON."
            ) from exc
        if not isinstance(response, dict):
            raise ProcessEvaluationProtocolError(
                "The evaluation process response must be an object."
            )
        if (
            response.get("protocol_version") != PROCESS_EVALUATION_PROTOCOL_VERSION
            or response.get("request_id") != request_id
            or not isinstance(response.get("ok"), bool)
        ):
            raise ProcessEvaluationProtocolError(
                "The evaluation process response did not match its request."
            )
        if response["ok"] is False:
            if set(response) != {"protocol_version", "request_id", "ok", "error_type"}:
                raise ProcessEvaluationProtocolError(
                    "The evaluation process error response has an invalid shape."
                )
            error_type = response.get("error_type")
            if not isinstance(error_type, str) or _SAFE_ERROR_TYPE_RE.fullmatch(
                error_type
            ) is None:
                raise ProcessEvaluationProtocolError(
                    "The evaluation process returned an invalid error type."
                )
            raise ProcessEvaluationRemoteError(error_type)
        if set(response) != {"protocol_version", "request_id", "ok", "rollout"}:
            raise ProcessEvaluationProtocolError(
                "The evaluation process success response has an invalid shape."
            )
        rollout = response.get("rollout")
        if not isinstance(rollout, dict) or set(rollout) != {
            "output",
            "tokens",
            "cost_usd",
        }:
            raise ProcessEvaluationProtocolError(
                "The evaluation process returned an invalid rollout shape."
            )
        try:
            return EvaluationRollout(
                output=rollout["output"],
                tokens=rollout["tokens"],
                cost_usd=rollout["cost_usd"],
            )
        except (TypeError, ValueError) as exc:
            raise ProcessEvaluationProtocolError(
                "The evaluation process returned an invalid rollout contract."
            ) from exc

    async def aclose(self) -> None:
        process = self._process
        if process is not None:
            try:
                await _stop_process(
                    process,
                    grace_seconds=self._config.terminate_grace_seconds,
                )
            except BaseException as exc:
                raise ProcessEvaluationCleanupError(
                    "The evaluation process could not be released."
                ) from exc
            finally:
                self._process = None
        temporary_directory = self._temporary_directory
        self._temporary_directory = None
        self._working_directory = None
        if temporary_directory is not None:
            try:
                await asyncio.to_thread(temporary_directory.cleanup)
            except BaseException as exc:
                raise ProcessEvaluationCleanupError(
                    "The evaluation working directory could not be released."
                ) from exc


class ProcessEvaluationSubjectFactory:
    """Validated, redacted callable that creates one configured subject per rollout."""

    __slots__ = ("_config",)

    def __init__(
        self,
        command: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
        working_directory: str | os.PathLike[str] | None = None,
        max_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
        max_stdout_bytes: int = _DEFAULT_MAX_STDOUT_BYTES,
        max_stderr_bytes: int = _DEFAULT_MAX_STDERR_BYTES,
        terminate_grace_seconds: float = 1.0,
    ) -> None:
        self._config = _build_config(
            command,
            environment=environment,
            working_directory=working_directory,
            max_request_bytes=max_request_bytes,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            terminate_grace_seconds=terminate_grace_seconds,
        )

    @property
    def subject_contract_digest(self) -> str:
        return self._config.subject_contract_digest

    @property
    def subject_isolation_digest(self) -> str:
        return self._config.subject_isolation_digest

    def __call__(self) -> ProcessEvaluationSubject:
        return ProcessEvaluationSubject(self._config.command, _config=self._config)

    def __repr__(self) -> str:
        return (
            "ProcessEvaluationSubjectFactory("
            f"executable={Path(self._config.command[0]).name!r}, "
            f"contract_digest={self.subject_contract_digest!r})"
        )


def process_evaluation_subject_factory(
    command: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    working_directory: str | os.PathLike[str] | None = None,
    max_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
    max_stdout_bytes: int = _DEFAULT_MAX_STDOUT_BYTES,
    max_stderr_bytes: int = _DEFAULT_MAX_STDERR_BYTES,
    terminate_grace_seconds: float = 1.0,
) -> ProcessEvaluationSubjectFactory:
    """Build a redacted factory for one fresh child process per runner rollout."""
    return ProcessEvaluationSubjectFactory(
        command,
        environment=environment,
        working_directory=working_directory,
        max_request_bytes=max_request_bytes,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
        terminate_grace_seconds=terminate_grace_seconds,
    )


ProcessEvaluationHandler = Callable[
    [EvaluationCase],
    EvaluationRollout | Awaitable[EvaluationRollout],
]


def run_process_evaluation_worker(
    handler: ProcessEvaluationHandler,
    *,
    max_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
) -> int:
    """Run one bounded worker request from stdin and emit one exact JSON response."""
    if not callable(handler):
        raise TypeError("handler must be callable")
    request_limit = _bounded_bytes(
        max_request_bytes,
        name="max_request_bytes",
        maximum=16 * 1024 * 1024,
    )
    raw_request = sys.stdin.buffer.read(request_limit + 1)
    if len(raw_request) > request_limit:
        return 64
    try:
        request = json.loads(raw_request)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 64
    if not isinstance(request, dict) or set(request) != {
        "protocol_version",
        "request_id",
        "case",
    }:
        return 64
    request_id = request.get("request_id")
    if (
        request.get("protocol_version") != PROCESS_EVALUATION_PROTOCOL_VERSION
        or not isinstance(request_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", request_id) is None
        or not isinstance(request.get("case"), dict)
    ):
        return 64
    case_value = request["case"]
    if set(case_value) != {"case_id", "slice", "task_class", "payload"}:
        return 64
    try:
        case = EvaluationCase(
            case_id=case_value["case_id"],
            slice=case_value["slice"],
            task_class=case_value["task_class"],
            payload=case_value["payload"],
        )
        result = handler(case)
        if inspect.isawaitable(result):
            result = asyncio.run(_await_rollout(result))
        if not isinstance(result, EvaluationRollout):
            raise TypeError("handler must return an EvaluationRollout")
        response = {
            "protocol_version": PROCESS_EVALUATION_PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": True,
            "rollout": result.to_dict(),
        }
    except Exception as exc:
        response = {
            "protocol_version": PROCESS_EVALUATION_PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": False,
            "error_type": _safe_error_type(exc),
        }
    sys.stdout.buffer.write(_canonical_bytes(response))
    sys.stdout.buffer.flush()
    return 0


__all__ = [
    "PROCESS_EVALUATION_PROTOCOL_VERSION",
    "ProcessEvaluationCleanupError",
    "ProcessEvaluationError",
    "ProcessEvaluationExitedError",
    "ProcessEvaluationHandler",
    "ProcessEvaluationOutputLimitError",
    "ProcessEvaluationProtocolError",
    "ProcessEvaluationRemoteError",
    "ProcessEvaluationRequestLimitError",
    "ProcessEvaluationSubject",
    "ProcessEvaluationSubjectFactory",
    "process_evaluation_subject_factory",
    "run_process_evaluation_worker",
]
