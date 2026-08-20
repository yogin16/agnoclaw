"""Fail-closed etcd v3 adapter for PostgreSQL writer authority."""

from __future__ import annotations

import base64
import json
import math
import threading
import time
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Final, NamedTuple, Self
from urllib.parse import urlsplit

import httpx

from .postgres_authority import (
    PostgresWriterAuthorityError,
    PostgresWriterAuthorityGrant,
    _validate_authority_identity,
)

ETCD_POSTGRES_WRITER_AUTHORITY_SCHEMA: Final = (
    "agnoclaw.postgres-writer-authority.v1"
)
_RECORD_FIELDS = frozenset({"schema", "authority_id", "server_id"})
_MAX_KEY_BYTES = 512
_MAX_VALUE_BYTES = 2_048
_MAX_AUTH_RESPONSE_BYTES = 16_384


class EtcdGatewayCredentials:
    """Endpoint-bound credentials for etcd JSON-gateway token authentication.

    etcd's JSON gRPC gateway does not use a client-certificate Common Name for RBAC.
    The authority adapter exchanges these credentials for a bounded endpoint-local
    token, caches it, and refreshes it once after a 401 response. This object is not
    an ``httpx.Auth`` implementation: authentication must remain inside the
    adapter's one deadline and bounded streamed-response contract.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        username: str,
        password: str,
        allow_insecure_loopback: bool = False,
    ) -> None:
        validated = _validate_endpoint(
            endpoint,
            allow_insecure_loopback=allow_insecure_loopback,
        )
        self._endpoint = validated
        self._origin = _origin(httpx.URL(validated))
        self._username = _credential_text(username, name="username", maximum=256)
        self._password = _credential_text(password, name="password", maximum=4_096)
        self._authenticate_body = json.dumps(
            {"name": self._username, "password": self._password},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        self._token: str | None = None
        self._lock = threading.Lock()

    def clear_token(self) -> None:
        """Forget the cached token, for example after server-side token revocation."""

        with self._lock:
            self._token = None

    def _matches_endpoint(self, endpoint: str) -> bool:
        return _origin(httpx.URL(endpoint)) == self._origin

    def _authorization_header(
        self,
        client: httpx.Client,
        deadline: float,
    ) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._lock.acquire(
            timeout=min(remaining, threading.TIMEOUT_MAX)
        ):
            raise PostgresWriterAuthorityError(reason="etcd_timeout")
        try:
            if self._token is None:
                self._token = self._authenticate(client, deadline)
            return self._token
        finally:
            self._lock.release()

    def _invalidate(self, token: str, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._lock.acquire(
            timeout=min(remaining, threading.TIMEOUT_MAX)
        ):
            raise PostgresWriterAuthorityError(reason="etcd_timeout")
        try:
            if self._token == token:
                self._token = None
        finally:
            self._lock.release()

    def _authenticate(
        self,
        client: httpx.Client,
        deadline: float,
    ) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PostgresWriterAuthorityError(reason="etcd_timeout")
        try:
            with client.stream(
                "POST",
                f"{self._endpoint}/v3/auth/authenticate",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                content=self._authenticate_body,
                timeout=remaining,
                follow_redirects=False,
                auth=None,
            ) as response:
                raw = _read_bounded(
                    response,
                    maximum=_MAX_AUTH_RESPONSE_BYTES,
                    too_large_reason="etcd_authentication_response_too_large",
                    transport_reason="etcd_authentication_failed",
                )
                if response.status_code != 200:
                    raise PostgresWriterAuthorityError(
                        reason="etcd_authentication_failed"
                    )
        except PostgresWriterAuthorityError:
            raise
        except httpx.TimeoutException as exc:
            raise PostgresWriterAuthorityError(reason="etcd_timeout") from exc
        except httpx.HTTPError as exc:
            raise PostgresWriterAuthorityError(
                reason="etcd_authentication_failed"
            ) from exc
        try:
            payload = json.loads(raw, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise PostgresWriterAuthorityError(
                reason="etcd_authentication_invalid"
            ) from exc
        payload = _mapping(payload, reason="etcd_authentication_invalid")
        token = payload.get("token")
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 8_192
            or any(ord(char) < 33 or ord(char) > 126 for char in token)
        ):
            raise PostgresWriterAuthorityError(
                reason="etcd_authentication_invalid"
            )
        return token


class EtcdPostgresWriterAuthority:
    """Read one dedicated, lease-backed etcd key as writer authority.

    The adapter never creates, renews, or transfers authority. It brackets a lease
    TTL lookup with two linearizable reads and uses the key's ``mod_revision`` as the
    fencing token. The supplied HTTP client is not owned or closed by this object.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        key: str,
        authority_id: str,
        cluster_id: int | str,
        http_client: httpx.Client | None = None,
        ttl_uncertainty_seconds: float = 1.0,
        max_response_bytes: int = 32_768,
        allow_insecure_loopback: bool = False,
        gateway_credentials: EtcdGatewayCredentials | None = None,
    ) -> None:
        self._endpoint = _validate_endpoint(
            endpoint,
            allow_insecure_loopback=allow_insecure_loopback,
        )
        self._key = _validate_key(key)
        self._encoded_key = _b64(self._key)
        _validate_authority_identity(authority_id, name="authority_id")
        self._authority_id = authority_id
        self._cluster_id = _positive_decimal(cluster_id, name="cluster_id")
        if (
            isinstance(ttl_uncertainty_seconds, bool)
            or not isinstance(ttl_uncertainty_seconds, (int, float))
            or not math.isfinite(ttl_uncertainty_seconds)
            or ttl_uncertainty_seconds < 0
            or ttl_uncertainty_seconds >= 86_400
        ):
            raise ValueError(
                "ttl_uncertainty_seconds must be finite and between 0 and 86400"
            )
        if (
            not isinstance(max_response_bytes, int)
            or isinstance(max_response_bytes, bool)
            or not 1 <= max_response_bytes <= 1_048_576
        ):
            raise ValueError("max_response_bytes must be between 1 and 1048576")
        self._ttl_uncertainty_seconds = float(ttl_uncertainty_seconds)
        self._max_response_bytes = max_response_bytes
        if gateway_credentials is not None and not isinstance(
            gateway_credentials, EtcdGatewayCredentials
        ):
            raise TypeError("gateway_credentials must be EtcdGatewayCredentials")
        if gateway_credentials is not None and not gateway_credentials._matches_endpoint(
            self._endpoint
        ):
            raise ValueError("gateway_credentials must match the exact etcd origin")
        self._client = http_client or httpx.Client(trust_env=False)
        self._owns_client = http_client is None
        if gateway_credentials is not None and "Authorization" in self._client.headers:
            if self._owns_client:
                self._client.close()
            raise ValueError(
                "gateway_credentials require a client without a global Authorization header"
            )
        self._gateway_credentials = gateway_credentials

    def close(self) -> None:
        """Close the internally created HTTP client, if any."""

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def current_grant(
        self,
        *,
        timeout_seconds: float,
    ) -> PostgresWriterAuthorityGrant:
        """Return a fresh grant or fail closed without a last-known-good fallback."""

        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        started = time.monotonic()
        deadline = started + timeout_seconds
        first = self._range(deadline)
        ttl = self._lease_ttl(first.lease_id, deadline)
        second = self._range(deadline)
        elapsed = time.monotonic() - started
        if elapsed > timeout_seconds:
            raise PostgresWriterAuthorityError(reason="etcd_timeout")
        if first != second:
            raise PostgresWriterAuthorityError(reason="etcd_authority_changed")
        remaining = min(float(ttl), 86_400.0)
        remaining -= elapsed
        remaining -= self._ttl_uncertainty_seconds
        if remaining <= 0:
            raise PostgresWriterAuthorityError(reason="etcd_lease_too_short")
        return PostgresWriterAuthorityGrant(
            authority_id=self._authority_id,
            server_id=first.server_id,
            fence_token=first.mod_revision,
            remaining_seconds=remaining,
        )

    def _range(self, deadline: float) -> _EtcdRecord:
        payload = self._post(
            "/v3/kv/range",
            {"key": self._encoded_key, "serializable": False},
            deadline,
        )
        self._verify_header(payload)
        if payload.get("count") != "1" or payload.get("more") not in {None, False}:
            raise PostgresWriterAuthorityError(reason="etcd_authority_absent")
        kvs = payload.get("kvs")
        if not isinstance(kvs, list) or len(kvs) != 1:
            raise PostgresWriterAuthorityError(reason="etcd_authority_absent")
        kv = _mapping(kvs[0], reason="etcd_authority_malformed")
        if kv.get("key") != self._encoded_key:
            raise PostgresWriterAuthorityError(reason="etcd_authority_malformed")
        lease_id = _positive_decimal(
            kv.get("lease"),
            name="lease",
            reason="etcd_lease_absent",
            maximum=2**63 - 1,
        )
        mod_revision = _positive_decimal(
            kv.get("mod_revision"),
            name="mod_revision",
            reason="etcd_authority_malformed",
            maximum=2**63 - 1,
        )
        encoded_value = kv.get("value")
        if not isinstance(encoded_value, str):
            raise PostgresWriterAuthorityError(reason="etcd_authority_malformed")
        try:
            raw_value = base64.b64decode(encoded_value, validate=True)
        except (ValueError, TypeError) as exc:
            raise PostgresWriterAuthorityError(
                reason="etcd_authority_malformed"
            ) from exc
        if len(raw_value) > _MAX_VALUE_BYTES:
            raise PostgresWriterAuthorityError(reason="etcd_authority_malformed")
        try:
            record = json.loads(raw_value, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise PostgresWriterAuthorityError(
                reason="etcd_authority_malformed"
            ) from exc
        record = _mapping(record, reason="etcd_authority_malformed")
        if set(record) != _RECORD_FIELDS:
            raise PostgresWriterAuthorityError(reason="etcd_authority_malformed")
        if (
            record.get("schema") != ETCD_POSTGRES_WRITER_AUTHORITY_SCHEMA
            or record.get("authority_id") != self._authority_id
        ):
            raise PostgresWriterAuthorityError(reason="etcd_authority_malformed")
        server_id = record.get("server_id")
        if not isinstance(server_id, str):
            raise PostgresWriterAuthorityError(reason="etcd_authority_malformed")
        try:
            _validate_authority_identity(server_id, name="server_id")
        except ValueError as exc:
            raise PostgresWriterAuthorityError(
                reason="etcd_authority_malformed"
            ) from exc
        return _EtcdRecord(
            server_id=server_id,
            lease_id=lease_id,
            mod_revision=mod_revision,
            encoded_value=encoded_value,
        )

    def _lease_ttl(self, lease_id: int, deadline: float) -> int:
        payload = self._post(
            "/v3/lease/timetolive",
            {"ID": str(lease_id), "keys": True},
            deadline,
        )
        self._verify_header(payload)
        if _decimal(payload.get("ID")) != lease_id:
            raise PostgresWriterAuthorityError(reason="etcd_lease_expired")
        ttl = _decimal(payload.get("TTL"))
        if ttl is None or ttl <= 0:
            raise PostgresWriterAuthorityError(reason="etcd_lease_expired")
        keys = payload.get("keys")
        if not isinstance(keys, list) or keys != [self._encoded_key]:
            raise PostgresWriterAuthorityError(reason="etcd_lease_detached")
        return ttl

    def _post(
        self,
        path: str,
        body: Mapping[str, Any],
        deadline: float,
    ) -> Mapping[str, Any]:
        token: str | None = None
        if self._gateway_credentials is not None:
            token = self._gateway_credentials._authorization_header(
                self._client,
                deadline,
            )
        status, raw = self._post_once(
            path,
            body,
            deadline,
            token=token,
        )
        if status == 401 and self._gateway_credentials is not None and token is not None:
            self._gateway_credentials._invalidate(token, deadline)
            refreshed = self._gateway_credentials._authorization_header(
                self._client,
                deadline,
            )
            status, raw = self._post_once(
                path,
                body,
                deadline,
                token=refreshed,
            )
            if status == 401:
                self._gateway_credentials._invalidate(refreshed, deadline)
        if status == 401:
            raise PostgresWriterAuthorityError(reason="etcd_authentication_failed")
        try:
            payload = json.loads(raw, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise PostgresWriterAuthorityError(reason="etcd_response_invalid") from exc
        return _mapping(payload, reason="etcd_response_invalid")

    def _post_once(
        self,
        path: str,
        body: Mapping[str, Any],
        deadline: float,
        *,
        token: str | None,
    ) -> tuple[int, bytes]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PostgresWriterAuthorityError(reason="etcd_timeout")
        headers = {"Authorization": token} if token is not None else None
        try:
            with self._client.stream(
                "POST",
                f"{self._endpoint}{path}",
                json=body,
                headers=headers,
                timeout=remaining,
                follow_redirects=False,
                auth=None if token is not None else httpx.USE_CLIENT_DEFAULT,
            ) as response:
                if response.status_code == 401:
                    return response.status_code, b""
                response.raise_for_status()
                raw = _read_bounded(
                    response,
                    maximum=self._max_response_bytes,
                    too_large_reason="etcd_response_too_large",
                    transport_reason="etcd_unavailable",
                )
        except PostgresWriterAuthorityError:
            raise
        except httpx.TimeoutException as exc:
            raise PostgresWriterAuthorityError(reason="etcd_timeout") from exc
        except httpx.HTTPError as exc:
            raise PostgresWriterAuthorityError(reason="etcd_unavailable") from exc
        return response.status_code, raw

    def _verify_header(self, payload: Mapping[str, Any]) -> None:
        header = _mapping(payload.get("header"), reason="etcd_response_invalid")
        if _decimal(header.get("cluster_id")) != self._cluster_id:
            raise PostgresWriterAuthorityError(reason="etcd_cluster_mismatch")


class _EtcdRecord(NamedTuple):
    """Comparable immutable authority observation without exposing record contents."""

    server_id: str
    lease_id: int
    mod_revision: int
    encoded_value: str


def _validate_endpoint(endpoint: str, *, allow_insecure_loopback: bool) -> str:
    if not isinstance(endpoint, str):
        raise ValueError("endpoint must be an HTTPS origin")
    parsed = urlsplit(endpoint)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("endpoint must be an HTTPS origin") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.scheme not in {"http", "https"}
    ):
        raise ValueError("endpoint must be an HTTPS origin")
    if parsed.scheme == "http" and not (
        allow_insecure_loopback and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("endpoint must use HTTPS except explicit test-only loopback")
    return endpoint.rstrip("/")


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    port = url.port
    if port is None:
        port = 443 if url.scheme == "https" else 80
    return (url.scheme, url.host, port)


def _credential_text(value: object, *, name: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value.encode("utf-8")) <= maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{name} must be bounded printable UTF-8 text")
    return value


def _read_bounded(
    response: httpx.Response,
    *,
    maximum: int,
    too_large_reason: str,
    transport_reason: str,
) -> bytes:
    raw = bytearray()
    try:
        for chunk in response.iter_bytes():
            if len(raw) + len(chunk) > maximum:
                response.close()
                raise PostgresWriterAuthorityError(reason=too_large_reason)
            raw.extend(chunk)
    except httpx.HTTPError as exc:
        raise PostgresWriterAuthorityError(reason=transport_reason) from exc
    return bytes(raw)


def _validate_key(key: str) -> bytes:
    if not isinstance(key, str) or not key.startswith("/") or any(
        ord(char) < 32 or ord(char) == 127 for char in key
    ):
        raise ValueError("key must be an absolute printable UTF-8 etcd key")
    encoded = key.encode()
    if not 1 <= len(encoded) <= _MAX_KEY_BYTES:
        raise ValueError("key must be between 1 and 512 UTF-8 bytes")
    return encoded


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _mapping(value: object, *, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PostgresWriterAuthorityError(reason=reason)
    return value


def _decimal(value: object) -> int | None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 20
        or not value.isascii()
        or not value.isdecimal()
    ):
        return None
    parsed = int(value)
    return parsed if parsed <= 2**64 - 1 else None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _positive_decimal(
    value: object,
    *,
    name: str,
    reason: str | None = None,
    maximum: int = 2**64 - 1,
) -> int:
    candidate = (
        str(value)
        if isinstance(value, int) and not isinstance(value, bool)
        else value
    )
    parsed = _decimal(candidate)
    if parsed is None or parsed <= 0 or parsed > maximum:
        if reason is not None:
            raise PostgresWriterAuthorityError(reason=reason)
        raise ValueError(f"{name} must be a positive decimal integer")
    return parsed


__all__ = [
    "ETCD_POSTGRES_WRITER_AUTHORITY_SCHEMA",
    "EtcdGatewayCredentials",
    "EtcdPostgresWriterAuthority",
]
