"""External writer-authority admission for PostgreSQL service deployments."""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import HarnessError

_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def validate_postgres_writer_authority_policy(
    *,
    check_timeout_seconds: float,
    safety_margin_seconds: float,
    max_transaction_seconds: float,
) -> None:
    """Validate bounded writer-authority policy values before opening a pool."""

    values = {
        "check_timeout_seconds": check_timeout_seconds,
        "safety_margin_seconds": safety_margin_seconds,
        "max_transaction_seconds": max_transaction_seconds,
    }
    for name, value in values.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be finite and positive")
    if safety_margin_seconds >= 86_400:
        raise ValueError("safety_margin_seconds must be less than 86400")


class PostgresWriterAuthorityError(HarnessError):
    """A PostgreSQL access was denied because writer authority was not safe."""

    def __init__(self, *, reason: str):
        super().__init__(
            code="POSTGRES_WRITER_AUTHORITY_DENIED",
            category="runtime_store",
            message=(
                "PostgreSQL writer authority is unavailable or no longer matches this "
                "server; the transaction was not committed."
            ),
            retryable=True,
            details={"reason": _safe_reason(reason)},
        )


@dataclass(frozen=True)
class PostgresWriterAuthorityGrant:
    """A freshly verified, short-lived external grant for one PostgreSQL writer.

    ``remaining_seconds`` is deliberately relative. The provider must derive it from
    its own authoritative lease observation and subtract any uncertainty before
    returning; agnoclaw never compares clocks with the external control plane.
    """

    authority_id: str
    server_id: str
    fence_token: int
    remaining_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.authority_id, str) or not _IDENTITY_RE.fullmatch(
            self.authority_id
        ):
            raise ValueError("authority_id must be a bounded opaque identity")
        if not isinstance(self.server_id, str) or not _IDENTITY_RE.fullmatch(
            self.server_id
        ):
            raise ValueError("server_id must be a bounded opaque identity")
        if not isinstance(self.fence_token, int) or isinstance(self.fence_token, bool):
            raise ValueError("fence_token must be a positive integer")
        if self.fence_token <= 0:
            raise ValueError("fence_token must be a positive integer")
        if (
            isinstance(self.remaining_seconds, bool)
            or not isinstance(self.remaining_seconds, (int, float))
            or not math.isfinite(self.remaining_seconds)
            or not 0 < self.remaining_seconds <= 86_400
        ):
            raise ValueError("remaining_seconds must be finite and between 0 and 86400")


class PostgresWriterAuthorityProvider(Protocol):
    """Deployment adapter for a linearizable external leader lease.

    Implementations must finish within ``timeout_seconds`` or raise. A successful call
    must freshly verify the holder, monotonically fenced generation, and conservative
    remaining TTL in the external authority; cached or last-known-good grants are not
    valid.
    """

    def current_grant(
        self,
        *,
        timeout_seconds: float,
    ) -> PostgresWriterAuthorityGrant: ...


def _safe_reason(reason: str) -> str:
    if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
        return "authority_denied"
    return reason


def _validate_authority_identity(value: str, *, name: str) -> None:
    if not isinstance(value, str) or not _IDENTITY_RE.fullmatch(value):
        raise ValueError(f"{name} must be a bounded opaque identity")


@dataclass(frozen=True)
class _AuthorityAdmission:
    grant: PostgresWriterAuthorityGrant
    transaction_timeout_ms: int


class PostgresWriterAuthorityGuard:
    """Fail-closed transaction admission around an external fenced writer lease."""

    def __init__(
        self,
        provider: PostgresWriterAuthorityProvider,
        *,
        check_timeout_seconds: float,
        safety_margin_seconds: float,
        max_transaction_seconds: float,
    ) -> None:
        validate_postgres_writer_authority_policy(
            check_timeout_seconds=check_timeout_seconds,
            safety_margin_seconds=safety_margin_seconds,
            max_transaction_seconds=max_transaction_seconds,
        )
        self._provider = provider
        self._check_timeout_seconds = check_timeout_seconds
        self._safety_margin_seconds = safety_margin_seconds
        self._max_transaction_seconds = max_transaction_seconds
        self._generation_lock = threading.Lock()
        self._latest_generation: tuple[str, int, str] | None = None

    def _grant(self) -> PostgresWriterAuthorityGrant:
        started = time.monotonic()
        try:
            grant = self._provider.current_grant(
                timeout_seconds=self._check_timeout_seconds,
            )
        except PostgresWriterAuthorityError:
            raise
        except Exception as exc:
            raise PostgresWriterAuthorityError(
                reason="authority_provider_unavailable",
            ) from exc
        if time.monotonic() - started > self._check_timeout_seconds:
            raise PostgresWriterAuthorityError(reason="authority_provider_timeout")
        if not isinstance(grant, PostgresWriterAuthorityGrant):
            raise PostgresWriterAuthorityError(reason="authority_grant_invalid")
        generation = (grant.authority_id, grant.fence_token, grant.server_id)
        with self._generation_lock:
            latest = self._latest_generation
            if latest is not None:
                if grant.authority_id != latest[0]:
                    raise PostgresWriterAuthorityError(
                        reason="authority_identity_changed",
                    )
                if grant.fence_token < latest[1]:
                    raise PostgresWriterAuthorityError(
                        reason="authority_generation_stale",
                    )
                if grant.fence_token == latest[1] and grant.server_id != latest[2]:
                    raise PostgresWriterAuthorityError(
                        reason="authority_generation_conflict",
                    )
            if latest is None or grant.fence_token > latest[1]:
                self._latest_generation = generation
        return grant

    @staticmethod
    def _server_identity(conn: Any) -> tuple[str, bool]:
        try:
            row = conn.execute(
                """
                SELECT current_setting('cluster_name', true) AS server_id,
                       pg_is_in_recovery() AS in_recovery
                """
            ).fetchone()
        except Exception as exc:
            raise PostgresWriterAuthorityError(
                reason="server_identity_unavailable",
            ) from exc
        if row is None:
            raise PostgresWriterAuthorityError(reason="server_identity_unavailable")
        try:
            server_id = row["server_id"]
            in_recovery = row["in_recovery"]
        except (KeyError, TypeError) as exc:
            raise PostgresWriterAuthorityError(
                reason="server_identity_unavailable",
            ) from exc
        if not isinstance(server_id, str) or not isinstance(in_recovery, bool):
            raise PostgresWriterAuthorityError(reason="server_identity_unavailable")
        return server_id, in_recovery

    def _verify_server(
        self,
        conn: Any,
        grant: PostgresWriterAuthorityGrant,
    ) -> None:
        server_id, in_recovery = self._server_identity(conn)
        if in_recovery:
            raise PostgresWriterAuthorityError(reason="server_in_recovery")
        if not server_id or server_id != grant.server_id:
            raise PostgresWriterAuthorityError(reason="server_identity_mismatch")

    def _transaction_timeout_ms(self, grant: PostgresWriterAuthorityGrant) -> int:
        usable_seconds = grant.remaining_seconds - self._safety_margin_seconds
        bounded_seconds = min(usable_seconds, self._max_transaction_seconds)
        timeout_ms = math.floor(bounded_seconds * 1000)
        if timeout_ms < 1:
            raise PostgresWriterAuthorityError(reason="authority_lease_too_short")
        return timeout_ms

    def admit(self, conn: Any) -> _AuthorityAdmission:
        grant = self._grant()
        self._verify_server(conn, grant)
        timeout_ms = self._transaction_timeout_ms(grant)
        try:
            conn.execute(
                "SELECT set_config('transaction_timeout', %s, true)",
                (f"{timeout_ms}ms",),
            )
        except Exception as exc:
            raise PostgresWriterAuthorityError(
                reason="transaction_timeout_unavailable",
            ) from exc
        return _AuthorityAdmission(
            grant=grant,
            transaction_timeout_ms=timeout_ms,
        )

    def revalidate(self, conn: Any, admission: _AuthorityAdmission) -> None:
        grant = self._grant()
        if (
            grant.authority_id != admission.grant.authority_id
            or grant.server_id != admission.grant.server_id
            or grant.fence_token != admission.grant.fence_token
        ):
            raise PostgresWriterAuthorityError(reason="authority_changed")
        self._verify_server(conn, grant)
        self._transaction_timeout_ms(grant)


__all__ = [
    "PostgresWriterAuthorityError",
    "PostgresWriterAuthorityGrant",
    "PostgresWriterAuthorityProvider",
]
