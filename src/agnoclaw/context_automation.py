"""Cross-run admission for session context maintenance."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .runtime.errors import HarnessError


@dataclass
class ContextActivityLease:
    coordinator: ContextAutomationCoordinator = field(repr=False)
    session_id: str
    maintenance: bool = False
    _released: bool = field(default=False, init=False, repr=False)
    _release_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
            self.coordinator._release(self.session_id, maintenance=self.maintenance)


class ContextAutomationCoordinator:
    """Prevent replacement from racing any direct run in the same session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, int] = {}
        self._maintenance: set[str] = set()

    def admit_run(
        self,
        session_id: str | None,
        *,
        maintenance_owned: bool = False,
    ) -> ContextActivityLease | None:
        if not session_id:
            return None
        with self._lock:
            if session_id in self._maintenance and not maintenance_owned:
                raise HarnessError(
                    code="CONTEXT_MAINTENANCE_IN_PROGRESS",
                    category="context",
                    message="This session is being compacted; retry the run after it settles.",
                    retryable=True,
                    details={"session_id": session_id},
                )
            self._active[session_id] = self._active.get(session_id, 0) + 1
        return ContextActivityLease(self, session_id)

    def begin_maintenance(self, session_id: str) -> ContextActivityLease:
        with self._lock:
            if session_id in self._maintenance:
                code = "CONTEXT_MAINTENANCE_IN_PROGRESS"
                message = "This session already has context maintenance in progress."
            elif self._active.get(session_id, 0):
                code = "CONTEXT_SESSION_BUSY"
                message = "Context replacement cannot race an active session run."
            else:
                self._maintenance.add(session_id)
                return ContextActivityLease(self, session_id, maintenance=True)
        raise HarnessError(
            code=code,
            category="context",
            message=message,
            retryable=True,
            details={"session_id": session_id},
        )

    def begin_owned_maintenance(self, session_id: str) -> ContextActivityLease:
        """Fence maintenance performed by the session's sole admitted run."""
        with self._lock:
            if session_id in self._maintenance:
                code = "CONTEXT_MAINTENANCE_IN_PROGRESS"
                message = "This session already has context maintenance in progress."
            elif self._active.get(session_id, 0) != 1:
                code = "CONTEXT_SESSION_BUSY"
                message = "Overflow recovery requires sole ownership of the session."
            else:
                self._maintenance.add(session_id)
                return ContextActivityLease(self, session_id, maintenance=True)
        raise HarnessError(
            code=code,
            category="context",
            message=message,
            retryable=True,
            details={"session_id": session_id},
        )

    def _release(self, session_id: str, *, maintenance: bool) -> None:
        with self._lock:
            if maintenance:
                self._maintenance.discard(session_id)
                return
            remaining = self._active.get(session_id, 0) - 1
            if remaining > 0:
                self._active[session_id] = remaining
            else:
                self._active.pop(session_id, None)


__all__ = ["ContextActivityLease", "ContextAutomationCoordinator"]
