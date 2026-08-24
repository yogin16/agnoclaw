"""Shared outbound HTTP primitives with DNS-pinned policy enforcement."""

from __future__ import annotations

from typing import Protocol

import httpcore
import httpx


class NetworkURLPolicy(Protocol):
    """Minimal URL policy enforced at request and TCP connection boundaries."""

    def validate_network_url(
        self,
        url: str,
        *,
        tool_name: str = "network_request",
        arg_key: str = "url",
    ) -> tuple[object, ...]: ...

    def resolve_network_host(self, host: str, port: int) -> tuple[str, ...]: ...


class NetworkPolicyError(ValueError):
    """Raised when a concrete outbound request violates host network policy."""


class PinnedNetworkBackend(httpcore.NetworkBackend):
    """Resolve through policy and connect to the exact approved address."""

    def __init__(self, policy: NetworkURLPolicy) -> None:
        self._policy = policy
        self._backend = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.NetworkStream:
        addresses = self._policy.resolve_network_host(host, port)
        last_error: Exception | None = None
        for address in addresses:
            try:
                return self._backend.connect_tcp(
                    host=address,
                    port=port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError(f"No approved address for {host!r}")

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options=None,
    ) -> httpcore.NetworkStream:
        raise httpcore.ConnectError("Unix sockets are forbidden for outbound HTTP")

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class PinnedHTTPTransport(httpx.HTTPTransport):
    """HTTPX transport whose TCP destination is the policy-approved DNS result."""

    def __init__(self, policy: NetworkURLPolicy) -> None:
        super().__init__(trust_env=False)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=5.0,
            network_backend=PinnedNetworkBackend(policy),
        )


def require_allowed_network_url(
    policy: NetworkURLPolicy,
    url: str,
    *,
    tool_name: str,
    arg_key: str = "url",
) -> None:
    """Raise a stable policy error when any URL guardrail rejects a request."""
    violations = policy.validate_network_url(
        url,
        tool_name=tool_name,
        arg_key=arg_key,
    )
    if not violations:
        return
    first = violations[0]
    code = getattr(first, "code", "NETWORK_POLICY_DENIED")
    message = getattr(first, "message", str(first))
    raise NetworkPolicyError(f"{code}: {message}")
