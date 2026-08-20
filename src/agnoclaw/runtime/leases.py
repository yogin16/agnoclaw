"""Store-issued run/session ownership leases and fencing identities."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from .errors import HarnessError

LEASE_SCHEMA_VERSION = "1.0"


def _require_id(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > 512:
        raise ValueError(f"{field_name} cannot exceed 512 characters")
    return normalized


class LeaseKind(StrEnum):
    RUN = "run"
    SESSION = "session"


@dataclass(frozen=True)
class RuntimeLease:
    lease_key: str
    kind: LeaseKind
    run_id: str
    worker_id: str
    claim_id: str
    lease_token: str
    fence_token: int
    acquired_at: str
    expires_at: str
    schema_version: str = LEASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "lease_key",
            "run_id",
            "worker_id",
            "claim_id",
            "lease_token",
            "acquired_at",
            "expires_at",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_id(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(self, "kind", LeaseKind(self.kind))
        if self.fence_token <= 0:
            raise ValueError("fence_token must be positive")
        if self.schema_version != LEASE_SCHEMA_VERSION:
            raise ValueError(f"unsupported lease schema version '{self.schema_version}'")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "schema_version": self.schema_version,
            "lease_key": self.lease_key,
            "kind": self.kind.value,
            "run_id": self.run_id,
            "worker_id": self.worker_id,
            "claim_id": self.claim_id,
            "lease_token": self.lease_token,
            "fence_token": self.fence_token,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class RunLeaseClaim:
    run: RuntimeLease
    session: RuntimeLease

    def __post_init__(self) -> None:
        if self.run.kind is not LeaseKind.RUN:
            raise ValueError("run lease must have kind=run")
        if self.session.kind is not LeaseKind.SESSION:
            raise ValueError("session lease must have kind=session")
        for field_name in ("run_id", "worker_id", "claim_id"):
            if getattr(self.run, field_name) != getattr(self.session, field_name):
                raise ValueError(f"run/session lease {field_name} must match")

    @property
    def run_id(self) -> str:
        return self.run.run_id

    @property
    def worker_id(self) -> str:
        return self.run.worker_id

    @property
    def claim_id(self) -> str:
        return self.run.claim_id


@dataclass(frozen=True)
class LeaseReleaseDecision:
    run_id: str
    claim_id: str
    released: bool
    idempotent: bool = False


class RuntimeLeaseUnavailableError(HarnessError):
    def __init__(self, *, run_id: str, kind: LeaseKind, retry_after_seconds: float):
        super().__init__(
            code="RUNTIME_LEASE_UNAVAILABLE",
            category="runtime_lease",
            message="Run ownership is currently leased by another worker.",
            retryable=True,
            details={
                "run_id": run_id,
                "lease_kind": kind.value,
                "retry_after_seconds": max(0.0, retry_after_seconds),
            },
        )


class RuntimeLeaseLostError(HarnessError):
    def __init__(self, *, run_id: str, kind: LeaseKind):
        super().__init__(
            code="RUNTIME_LEASE_LOST",
            category="runtime_lease",
            message="The worker no longer owns the exact unexpired lease.",
            retryable=False,
            details={"run_id": run_id, "lease_kind": kind.value},
        )


class RuntimeLeaseClaimReleasedError(HarnessError):
    def __init__(self, *, run_id: str):
        super().__init__(
            code="RUNTIME_LEASE_CLAIM_RELEASED",
            category="runtime_lease",
            message="This lease claim was already released and cannot be reopened.",
            retryable=False,
            details={"run_id": run_id},
        )


class RuntimeLeaseTerminalRunError(HarnessError):
    def __init__(self, *, run_id: str):
        super().__init__(
            code="RUNTIME_LEASE_RUN_TERMINAL",
            category="runtime_lease",
            message="A terminal run cannot acquire execution ownership.",
            retryable=False,
            details={"run_id": run_id},
        )


def _framed_digest(parts: tuple[str | None, ...]) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_lease_key(run_id: str) -> str:
    return f"run:{_require_id(run_id, field_name='run_id')}"


def session_lease_key(
    *,
    tenant_id: str | None,
    session_id: str | None,
    run_id: str,
) -> str:
    # Anonymous sessions are deliberately unique to the run. Named sessions serialize
    # by the exact tenant/session tuple without delimiter-collision risk or raw values in
    # the lease key.
    parts = (
        (
            "named",
            tenant_id,
            session_id,
        )
        if session_id is not None
        else ("anonymous", run_id)
    )
    return f"session:sha256:{_framed_digest(parts)}"


def lease_token(*, claim_id: str, kind: LeaseKind, lease_key: str) -> str:
    claim_id = _require_id(claim_id, field_name="claim_id")
    digest = _framed_digest((claim_id, kind.value, lease_key))
    return f"lease_{digest}"


__all__ = [
    "LEASE_SCHEMA_VERSION",
    "LeaseKind",
    "LeaseReleaseDecision",
    "RunLeaseClaim",
    "RuntimeLease",
    "RuntimeLeaseClaimReleasedError",
    "RuntimeLeaseLostError",
    "RuntimeLeaseTerminalRunError",
    "RuntimeLeaseUnavailableError",
    "lease_token",
    "run_lease_key",
    "session_lease_key",
]
