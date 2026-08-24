"""Hardened Docker subjects for evidence-gated improvement evaluation.

This module deliberately exposes one narrow, network-disabled profile. It is stronger
than a fresh local process but remains container isolation, not a VM or a proof against
kernel/daemon compromise.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .improvement_corpus import EvaluationCase
from .improvement_process import (
    ProcessEvaluationCleanupError,
    ProcessEvaluationSubject,
)
from .improvement_runner import EvaluationRollout
from .runtime.security import freeze_data, thaw_data

DOCKER_EVALUATION_POLICY_VERSION = "agnoclaw.improvement.docker.v1"
_IMAGE_RE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9._/:@-]{0,447}@sha256:[0-9a-f]{64})$"
)
_USER_RE = re.compile(r"^[1-9][0-9]{0,9}:[1-9][0-9]{0,9}$")
_PLATFORM_RE = re.compile(r"^linux/(?:amd64|arm64)$")
_OWNER_LABEL = "agnoclaw.evaluation.owner"
_MAX_CLI_OUTPUT_BYTES = 4096


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        thaw_data(freeze_data(value)),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def _bounded_int(value: int, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_command(command: Sequence[str]) -> tuple[str, ...]:
    values = tuple(command)
    if not 1 <= len(values) <= 10:
        raise ValueError("container_command must contain between 1 and 10 arguments")
    if any(not isinstance(item, str) for item in values):
        raise TypeError("container command arguments must be strings")
    if any(not item or "\x00" in item or len(item) > 4096 for item in values):
        raise ValueError("container command arguments must be non-empty, bounded, and NUL-free")
    if sum(len(item.encode("utf-8")) for item in values) > 32 * 1024:
        raise ValueError("container command cannot exceed 32768 UTF-8 bytes")
    return values


@dataclass(frozen=True, slots=True)
class DockerEvaluationPolicy:
    """Immutable, no-network container policy shared by both sides of an experiment."""

    image: str
    platform: str = "linux/amd64"
    memory_bytes: int = 512 * 1024 * 1024
    cpu_limit: float = 1.0
    pids_limit: int = 64
    tmpfs_bytes: int = 64 * 1024 * 1024
    nofile_limit: int = 1024
    user: str = "65532:65532"

    def __post_init__(self) -> None:
        if not isinstance(self.image, str) or _IMAGE_RE.fullmatch(self.image) is None:
            raise ValueError("image must be an immutable sha256 image ID or digest reference")
        if not isinstance(self.platform, str) or _PLATFORM_RE.fullmatch(self.platform) is None:
            raise ValueError("platform must be linux/amd64 or linux/arm64")
        _bounded_int(
            self.memory_bytes,
            name="memory_bytes",
            minimum=16 * 1024 * 1024,
            maximum=64 * 1024 * 1024 * 1024,
        )
        if (
            isinstance(self.cpu_limit, bool)
            or not isinstance(self.cpu_limit, (int, float))
            or not math.isfinite(float(self.cpu_limit))
            or not 0.1 <= float(self.cpu_limit) <= 64
        ):
            raise ValueError("cpu_limit must be finite and between 0.1 and 64")
        object.__setattr__(self, "cpu_limit", float(self.cpu_limit))
        _bounded_int(self.pids_limit, name="pids_limit", minimum=8, maximum=4096)
        _bounded_int(
            self.tmpfs_bytes,
            name="tmpfs_bytes",
            minimum=1024 * 1024,
            maximum=self.memory_bytes,
        )
        _bounded_int(
            self.nofile_limit,
            name="nofile_limit",
            minimum=32,
            maximum=65536,
        )
        if not isinstance(self.user, str) or _USER_RE.fullmatch(self.user) is None:
            raise ValueError("user must be a non-root numeric uid:gid")

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": DOCKER_EVALUATION_POLICY_VERSION,
            "image": self.image,
            "network": "none",
            "root_filesystem": "read_only",
            "capabilities": "drop_all",
            "no_new_privileges": True,
            "seccomp": "builtin",
            "ipc": "none",
            "cgroup_namespace": "private",
            "init": True,
            "log_driver": "none",
            "pull": "never",
            "platform": self.platform,
            "image_declared_volumes": "forbidden",
            "image_entrypoint": "overridden",
            "healthcheck": "disabled",
            "bind_mounts": [],
            "host_environment_injection": [],
            "memory_bytes": self.memory_bytes,
            "memory_swap_bytes": self.memory_bytes,
            "cpu_limit": self.cpu_limit,
            "pids_limit": self.pids_limit,
            "tmpfs": {
                "path": "/tmp",
                "bytes": self.tmpfs_bytes,
                "options": ["nodev", "noexec", "nosuid"],
            },
            "nofile_limit": self.nofile_limit,
            "core_limit": 0,
            "user": self.user,
            "workdir": "/tmp",
        }


@dataclass(frozen=True, slots=True, repr=False)
class _DockerEvaluationConfig:
    docker_executable: str
    policy: DockerEvaluationPolicy
    container_command: tuple[str, ...]
    max_request_bytes: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    terminate_grace_seconds: float
    cleanup_timeout_seconds: float
    subject_contract_digest: str
    subject_isolation_digest: str


class DockerEvaluationImageError(RuntimeError):
    """The immutable image cannot satisfy the policy preflight."""


def _build_config(
    docker_executable: str | os.PathLike[str],
    policy: DockerEvaluationPolicy,
    container_command: Sequence[str],
    *,
    max_request_bytes: int,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    terminate_grace_seconds: float,
    cleanup_timeout_seconds: float,
) -> _DockerEvaluationConfig:
    executable = Path(docker_executable)
    if not executable.is_absolute():
        raise ValueError("docker_executable must be absolute")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("docker_executable must be an executable file")
    if not isinstance(policy, DockerEvaluationPolicy):
        raise TypeError("policy must be a DockerEvaluationPolicy")
    command = _bounded_command(container_command)
    for name, value, maximum in (
        ("max_request_bytes", max_request_bytes, 16 * 1024 * 1024),
        ("max_stdout_bytes", max_stdout_bytes, 16 * 1024 * 1024),
        ("max_stderr_bytes", max_stderr_bytes, 1024 * 1024),
    ):
        _bounded_int(value, name=name, minimum=1, maximum=maximum)
    for name, duration, max_duration in (
        ("terminate_grace_seconds", terminate_grace_seconds, 10.0),
        ("cleanup_timeout_seconds", cleanup_timeout_seconds, 30.0),
    ):
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or not 0 < float(duration) <= max_duration
        ):
            raise ValueError(f"{name} must be finite and between 0 and {max_duration:g}")
    isolation_contract = {
        "policy": policy.to_dict(),
        "host_process": {
            "protocol": "agnoclaw.improvement.process.v1",
            "environment": "empty",
            "working_directory": "fresh_temporary_directory",
            "max_request_bytes": max_request_bytes,
            "max_stdout_bytes": max_stdout_bytes,
            "max_stderr_bytes": max_stderr_bytes,
            "terminate_grace_seconds": float(terminate_grace_seconds),
            "cleanup_timeout_seconds": float(cleanup_timeout_seconds),
            "exact_label_verified_cleanup": True,
        },
    }
    isolation_digest = _digest(isolation_contract)
    return _DockerEvaluationConfig(
        docker_executable=str(executable),
        policy=policy,
        container_command=command,
        max_request_bytes=max_request_bytes,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
        terminate_grace_seconds=float(terminate_grace_seconds),
        cleanup_timeout_seconds=float(cleanup_timeout_seconds),
        subject_contract_digest=_digest(
            {
                "policy_version": DOCKER_EVALUATION_POLICY_VERSION,
                "subject_isolation_digest": isolation_digest,
                "container_command": list(command),
            }
        ),
        subject_isolation_digest=isolation_digest,
    )


async def _read_cli_stream(
    reader: asyncio.StreamReader,
    *,
    maximum: int = _MAX_CLI_OUTPUT_BYTES,
) -> bytes:
    value = await reader.read(maximum + 1)
    if len(value) > maximum:
        raise ProcessEvaluationCleanupError("Docker cleanup output exceeded its boundary.")
    return value


async def _run_cli(
    executable: str,
    arguments: Sequence[str],
    *,
    timeout_seconds: float,
) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        executable,
        *arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={},
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_task = asyncio.create_task(_read_cli_stream(process.stdout))
    stderr_task = asyncio.create_task(_read_cli_stream(process.stderr))
    try:
        async with asyncio.timeout(timeout_seconds):
            stdout, _stderr, returncode = await asyncio.gather(
                stdout_task,
                stderr_task,
                process.wait(),
            )
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    return returncode, stdout


class DockerEvaluationSubject:
    """One single-use, exactly cleaned Docker rollout subject."""

    __slots__ = (
        "_config",
        "_container_may_exist",
        "_inner",
        "_name",
        "_owner_token",
        "_setup_complete",
    )

    def __init__(self, config: _DockerEvaluationConfig) -> None:
        self._config = config
        nonce = uuid4().hex
        self._name = f"agnoclaw-eval-{nonce[:24]}"
        self._owner_token = nonce
        self._container_may_exist = False
        self._setup_complete = False
        self._inner = ProcessEvaluationSubject(
            self._command(),
            environment={},
            max_request_bytes=config.max_request_bytes,
            max_stdout_bytes=config.max_stdout_bytes,
            max_stderr_bytes=config.max_stderr_bytes,
            terminate_grace_seconds=config.terminate_grace_seconds,
        )

    @property
    def subject_contract_digest(self) -> str:
        return self._config.subject_contract_digest

    @property
    def subject_isolation_digest(self) -> str:
        return self._config.subject_isolation_digest

    def __repr__(self) -> str:
        return (
            "DockerEvaluationSubject("
            f"contract_digest={self.subject_contract_digest!r}, "
            f"policy_digest={self._config.policy.digest!r})"
        )

    def _command(self) -> tuple[str, ...]:
        policy = self._config.policy
        cpu = format(policy.cpu_limit, ".6g")
        tmpfs = f"/tmp:rw,noexec,nosuid,nodev,size={policy.tmpfs_bytes}"
        return (
            self._config.docker_executable,
            "run",
            "--rm",
            "--interactive",
            "--pull",
            "never",
            "--platform",
            policy.platform,
            "--name",
            self._name,
            "--label",
            f"{_OWNER_LABEL}={self._owner_token}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--security-opt",
            "seccomp=builtin",
            "--pids-limit",
            str(policy.pids_limit),
            "--memory",
            str(policy.memory_bytes),
            "--memory-swap",
            str(policy.memory_bytes),
            "--cpus",
            cpu,
            "--user",
            policy.user,
            "--workdir",
            "/tmp",
            "--tmpfs",
            tmpfs,
            "--ipc",
            "none",
            "--cgroupns",
            "private",
            "--init",
            "--no-healthcheck",
            "--log-driver",
            "none",
            "--hostname",
            "agnoclaw-eval",
            "--ulimit",
            "core=0:0",
            "--ulimit",
            f"nofile={policy.nofile_limit}:{policy.nofile_limit}",
            "--ulimit",
            f"nproc={policy.pids_limit}:{policy.pids_limit}",
            "--entrypoint",
            self._config.container_command[0],
            policy.image,
            *self._config.container_command[1:],
        )

    async def _verify_image(self) -> None:
        returncode, stdout = await _run_cli(
            self._config.docker_executable,
            (
                "image",
                "inspect",
                "--format",
                "{{json .Config.Volumes}}\n{{json .Os}}\n{{json .Architecture}}",
                self._config.policy.image,
            ),
            timeout_seconds=self._config.cleanup_timeout_seconds,
        )
        if returncode != 0:
            raise DockerEvaluationImageError(
                "The immutable Docker evaluation image is unavailable."
            )
        try:
            metadata_lines = stdout.decode("utf-8").strip().splitlines()
            if len(metadata_lines) != 3:
                raise ValueError("invalid image metadata line count")
            volumes_json, os_json, architecture_json = metadata_lines
            volumes = json.loads(volumes_json)
            image_os = json.loads(os_json)
            image_architecture = json.loads(architecture_json)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise DockerEvaluationImageError(
                "Docker returned invalid immutable-image metadata."
            ) from exc
        if (
            volumes not in (None, {})
            or f"{image_os}/{image_architecture}" != self._config.policy.platform
        ):
            raise DockerEvaluationImageError(
                "The immutable image platform must match and declare no writable volumes."
            )

    async def asetup(self) -> None:
        if self._setup_complete:
            raise RuntimeError("The Docker evaluation subject cannot be set up twice.")
        await self._verify_image()
        await self._inner.asetup()
        self._setup_complete = True

    async def __call__(self, case: EvaluationCase) -> EvaluationRollout:
        if not self._setup_complete:
            raise RuntimeError("asetup() must complete before Docker execution.")
        self._container_may_exist = True
        try:
            result = await self._inner(case)
        except BaseException as original:
            try:
                await self._cleanup_container()
            except BaseException as cleanup_error:
                raise cleanup_error from original
            raise
        self._container_may_exist = False
        return result

    async def _cleanup_container(self) -> None:
        if not self._container_may_exist:
            return
        inspect_format = f'{{{{ index .Config.Labels "{_OWNER_LABEL}" }}}}'
        returncode, stdout = await _run_cli(
            self._config.docker_executable,
            ("container", "inspect", "--format", inspect_format, self._name),
            timeout_seconds=self._config.cleanup_timeout_seconds,
        )
        if returncode == 0:
            try:
                owner = stdout.decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise ProcessEvaluationCleanupError(
                    "Docker cleanup ownership could not be verified."
                ) from exc
            if owner != self._owner_token:
                raise ProcessEvaluationCleanupError(
                    "Docker cleanup ownership could not be verified."
                )
            removed, _ = await _run_cli(
                self._config.docker_executable,
                ("container", "rm", "--force", self._name),
                timeout_seconds=self._config.cleanup_timeout_seconds,
            )
            if removed != 0:
                raise ProcessEvaluationCleanupError(
                    "The owned Docker evaluation container could not be removed."
                )
        else:
            reachable, _ = await _run_cli(
                self._config.docker_executable,
                ("version", "--format", "{{.Server.Version}}"),
                timeout_seconds=self._config.cleanup_timeout_seconds,
            )
            if reachable != 0:
                raise ProcessEvaluationCleanupError(
                    "Docker cleanup could not prove that the container is absent."
                )
        self._container_may_exist = False

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            await self._cleanup_container()


class DockerEvaluationSubjectFactory:
    """Redacted callable producing one hardened container per rollout."""

    __slots__ = ("_config",)

    def __init__(
        self,
        docker_executable: str | os.PathLike[str],
        policy: DockerEvaluationPolicy,
        container_command: Sequence[str],
        *,
        max_request_bytes: int = 1024 * 1024,
        max_stdout_bytes: int = 1024 * 1024,
        max_stderr_bytes: int = 64 * 1024,
        terminate_grace_seconds: float = 1.0,
        cleanup_timeout_seconds: float = 5.0,
    ) -> None:
        self._config = _build_config(
            docker_executable,
            policy,
            container_command,
            max_request_bytes=max_request_bytes,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            terminate_grace_seconds=terminate_grace_seconds,
            cleanup_timeout_seconds=cleanup_timeout_seconds,
        )

    @property
    def subject_contract_digest(self) -> str:
        return self._config.subject_contract_digest

    @property
    def subject_isolation_digest(self) -> str:
        return self._config.subject_isolation_digest

    def __call__(self) -> DockerEvaluationSubject:
        return DockerEvaluationSubject(self._config)

    def __repr__(self) -> str:
        return (
            "DockerEvaluationSubjectFactory("
            f"executable={Path(self._config.docker_executable).name!r}, "
            f"contract_digest={self.subject_contract_digest!r}, "
            f"policy_digest={self._config.policy.digest!r})"
        )


def docker_evaluation_subject_factory(
    docker_executable: str | os.PathLike[str],
    policy: DockerEvaluationPolicy,
    container_command: Sequence[str],
    *,
    max_request_bytes: int = 1024 * 1024,
    max_stdout_bytes: int = 1024 * 1024,
    max_stderr_bytes: int = 64 * 1024,
    terminate_grace_seconds: float = 1.0,
    cleanup_timeout_seconds: float = 5.0,
) -> DockerEvaluationSubjectFactory:
    """Build a strict no-network Docker subject factory."""
    return DockerEvaluationSubjectFactory(
        docker_executable,
        policy,
        container_command,
        max_request_bytes=max_request_bytes,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
        terminate_grace_seconds=terminate_grace_seconds,
        cleanup_timeout_seconds=cleanup_timeout_seconds,
    )


__all__ = [
    "DOCKER_EVALUATION_POLICY_VERSION",
    "DockerEvaluationPolicy",
    "DockerEvaluationImageError",
    "DockerEvaluationSubject",
    "DockerEvaluationSubjectFactory",
    "docker_evaluation_subject_factory",
]
