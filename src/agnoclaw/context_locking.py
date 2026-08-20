"""Cooperative cross-process reader/writer fencing for Agno session context.

Context replacement is a writer operation while ordinary model runs are readers.  The
protocol is intentionally small so deployments can supply a lock with the same failure
semantics without coupling context management to the lifecycle run ledger.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import threading
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .context_management import ContextScope
from .runtime.errors import HarnessError


class ContextLockMode(StrEnum):
    """Shared model activity or exclusive session replacement."""

    SHARED = "shared"
    EXCLUSIVE = "exclusive"


class ContextLockUnavailableError(HarnessError):
    """Another cooperating process owns a conflicting session-context lock."""

    def __init__(self, scope: ContextScope, *, mode: ContextLockMode) -> None:
        super().__init__(
            code="CONTEXT_CROSS_PROCESS_LOCK_UNAVAILABLE",
            category="context",
            message="Another process currently owns conflicting session context activity.",
            retryable=True,
            details={"scope_digest": scope.digest, "mode": mode.value},
        )


class ContextLockLostError(HarnessError):
    """The process can no longer prove ownership at the session write boundary."""

    def __init__(self, scope: ContextScope, *, mode: ContextLockMode) -> None:
        super().__init__(
            code="CONTEXT_CROSS_PROCESS_LOCK_LOST",
            category="context",
            message="Cross-process session context ownership was lost before commit.",
            retryable=False,
            details={"scope_digest": scope.digest, "mode": mode.value},
        )


@runtime_checkable
class ContextLockLease(Protocol):
    """One exact-scope lock held until explicit idempotent release."""

    @property
    def scope(self) -> ContextScope: ...

    @property
    def mode(self) -> ContextLockMode: ...

    def validate(self) -> None: ...

    def upgrade(self) -> None: ...

    def release(self) -> None: ...


@runtime_checkable
class ContextLockProvider(Protocol):
    """Non-blocking context activity lock provider."""

    @property
    def identity_digest(self) -> str: ...

    def acquire(
        self,
        scope: ContextScope,
        *,
        mode: ContextLockMode,
    ) -> ContextLockLease: ...


class _LocalFileContextLockLease:
    __slots__ = ("_fd", "_lock", "_mode", "_released", "_scope")

    def __init__(
        self,
        fd: int,
        *,
        scope: ContextScope,
        mode: ContextLockMode,
    ) -> None:
        self._fd = fd
        self._scope = scope
        self._mode = mode
        self._released = False
        self._lock = threading.Lock()

    @property
    def scope(self) -> ContextScope:
        return self._scope

    @property
    def mode(self) -> ContextLockMode:
        return self._mode

    @staticmethod
    def _fcntl_module() -> Any:
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - Windows is not certified yet
            raise HarnessError(
                code="CONTEXT_FILE_LOCK_UNSUPPORTED",
                category="configuration",
                message="Local file context locking requires POSIX flock support.",
                retryable=False,
            ) from exc
        return fcntl

    def validate(self) -> None:
        with self._lock:
            if self._released:
                raise ContextLockLostError(self._scope, mode=self._mode)
            try:
                os.fstat(self._fd)
            except OSError as exc:
                raise ContextLockLostError(self._scope, mode=self._mode) from exc

    def upgrade(self) -> None:
        """Convert the sole reader into a writer or fail without doing work.

        POSIX flock conversion is not atomic. If a competing reader prevents the
        conversion, this method reacquires the shared lock before returning a retryable
        conflict. If even that is lost, it closes the lease and reports non-retryable
        ownership loss to the in-flight run.
        """

        fcntl = self._fcntl_module()
        with self._lock:
            if self._released:
                raise ContextLockLostError(self._scope, mode=self._mode)
            if self._mode is ContextLockMode.EXCLUSIVE:
                try:
                    os.fstat(self._fd)
                except OSError as exc:
                    raise ContextLockLostError(self._scope, mode=self._mode) from exc
                return
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    self._release_unlocked(fcntl)
                    raise ContextLockLostError(self._scope, mode=self._mode) from exc
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except OSError as reacquire_error:
                    self._release_unlocked(fcntl)
                    raise ContextLockLostError(
                        self._scope,
                        mode=ContextLockMode.SHARED,
                    ) from reacquire_error
                raise ContextLockUnavailableError(
                    self._scope,
                    mode=ContextLockMode.EXCLUSIVE,
                ) from exc
            self._mode = ContextLockMode.EXCLUSIVE

    def _release_unlocked(self, fcntl: Any) -> None:
        if self._released:
            return
        self._released = True
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(self._fd)
            except OSError:
                pass

    def release(self) -> None:
        fcntl = self._fcntl_module()
        with self._lock:
            self._release_unlocked(fcntl)

    def __enter__(self) -> _LocalFileContextLockLease:
        self.validate()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.release()

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass


class LocalFileContextLockProvider:
    """Crash-releasing reader/writer locks shared by local host processes.

    Every cooperating harness must use the same trusted lock directory. Lock filenames
    contain only the exact context-scope digest; tenant, user, and session values are
    never written to the filesystem or error text.
    """

    def __init__(self, directory: str | Path) -> None:
        root = Path(directory).expanduser()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = root.resolve()
        if not resolved.is_dir():
            raise ValueError("context lock directory must be a directory")
        self.directory = resolved
        self._identity_digest = f"sha256:{hashlib.sha256(str(resolved).encode()).hexdigest()}"

    @property
    def identity_digest(self) -> str:
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self._identity_digest) is None:
            raise AssertionError("invalid local context lock provider identity")
        return self._identity_digest

    def acquire(
        self,
        scope: ContextScope,
        *,
        mode: ContextLockMode,
    ) -> ContextLockLease:
        if not isinstance(scope, ContextScope):
            raise TypeError("scope must be a ContextScope")
        mode = ContextLockMode(mode)
        fcntl = _LocalFileContextLockLease._fcntl_module()
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        path = self.directory / f"{scope.digest}.lock"
        fd = os.open(path, flags, 0o600)
        try:
            descriptor = os.fstat(fd)
            if not stat.S_ISREG(descriptor.st_mode):
                raise ValueError("context lock target must be a regular file")
            operation = fcntl.LOCK_SH if mode is ContextLockMode.SHARED else fcntl.LOCK_EX
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise ContextLockUnavailableError(scope, mode=mode) from exc
            raise
        except BaseException:
            os.close(fd)
            raise
        return _LocalFileContextLockLease(fd, scope=scope, mode=mode)


__all__ = [
    "ContextLockLease",
    "ContextLockLostError",
    "ContextLockMode",
    "ContextLockProvider",
    "ContextLockUnavailableError",
    "LocalFileContextLockProvider",
]
