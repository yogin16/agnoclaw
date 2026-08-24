"""Pure contracts for the first-party etcd PostgreSQL authority adapter."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from agnoclaw.runtime import (
    ETCD_POSTGRES_WRITER_AUTHORITY_SCHEMA,
    EtcdGatewayCredentials,
    EtcdPostgresWriterAuthority,
    PostgresWriterAuthorityError,
)
from agnoclaw.runtime.postgres_authority import PostgresWriterAuthorityGuard

KEY = "/agnoclaw/postgres/cluster-a/writer"
ENCODED_KEY = base64.b64encode(KEY.encode()).decode()
VALUE = {
    "schema": ETCD_POSTGRES_WRITER_AUTHORITY_SCHEMA,
    "authority_id": "cluster-a",
    "server_id": "postgres-a",
}


def _range(
    *,
    cluster_id: str = "42",
    revision: str = "7",
    lease: str = "11",
    value: object = VALUE,
) -> dict[str, Any]:
    encoded_value = base64.b64encode(
        json.dumps(value, separators=(",", ":")).encode()
    ).decode()
    return {
        "header": {"cluster_id": cluster_id, "revision": revision},
        "count": "1",
        "kvs": [
            {
                "key": ENCODED_KEY,
                "value": encoded_value,
                "lease": lease,
                "mod_revision": revision,
            }
        ],
    }


def _ttl(
    *,
    cluster_id: str = "42",
    lease: str = "11",
    ttl: str = "20",
    keys: object = None,
) -> dict[str, Any]:
    return {
        "header": {"cluster_id": cluster_id, "revision": "7"},
        "ID": lease,
        "TTL": ttl,
        "keys": [ENCODED_KEY] if keys is None else keys,
    }


def _client(
    responses: list[dict[str, Any] | httpx.Response | BaseException],
    requests: list[httpx.Request] | None = None,
) -> httpx.Client:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        response = queue.pop(0)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _provider(
    responses: list[dict[str, Any] | httpx.Response | BaseException],
    **overrides: Any,
) -> EtcdPostgresWriterAuthority:
    values: dict[str, Any] = {
        "endpoint": "https://etcd.internal:2379",
        "key": KEY,
        "authority_id": "cluster-a",
        "cluster_id": 42,
        "http_client": _client(responses),
        "ttl_uncertainty_seconds": 1,
    }
    values.update(overrides)
    return EtcdPostgresWriterAuthority(**values)


def _reason(call: Callable[[], object]) -> str:
    with pytest.raises(PostgresWriterAuthorityError) as denied:
        call()
    assert "secret" not in str(denied.value)
    return str(denied.value.details["reason"])


def _credential_provider(
    responses: list[dict[str, Any] | httpx.Response | BaseException],
    requests: list[httpx.Request],
) -> tuple[EtcdPostgresWriterAuthority, EtcdGatewayCredentials, httpx.Client]:
    credentials = EtcdGatewayCredentials(
        endpoint="https://etcd.internal:2379",
        username="agnoclaw-reader",
        password="not-a-real-secret",
    )
    client = _client(responses, requests)
    return (
        _provider(
            [],
            http_client=client,
            gateway_credentials=credentials,
        ),
        credentials,
        client,
    )


def test_gateway_credentials_exchange_reuse_refresh_and_explicit_clear() -> None:
    requests: list[httpx.Request] = []
    provider, credentials, client = _credential_provider(
        [
            {"token": "token-one"},
            _range(),
            _ttl(),
            _range(),
            _range(),
            _ttl(),
            _range(),
            httpx.Response(401, json={"code": 16}),
            {"token": "token-two"},
            _range(),
            _ttl(),
            _range(),
            {"token": "token-three"},
            _range(),
            _ttl(),
            _range(),
        ],
        requests,
    )

    assert provider.current_grant(timeout_seconds=2).fence_token == 7
    assert provider.current_grant(timeout_seconds=2).fence_token == 7
    assert provider.current_grant(timeout_seconds=2).fence_token == 7
    credentials.clear_token()
    assert provider.current_grant(timeout_seconds=2).fence_token == 7

    paths = [request.url.path for request in requests]
    assert paths.count("/v3/auth/authenticate") == 3
    assert paths.count("/v3/kv/range") == 9
    assert requests[1].headers["Authorization"] == "token-one"
    assert requests[9].headers["Authorization"] == "token-two"
    assert requests[-1].headers["Authorization"] == "token-three"
    assert json.loads(requests[0].content) == {
        "name": "agnoclaw-reader",
        "password": "not-a-real-secret",
    }
    provider.close()
    client.close()


def test_gateway_credentials_reject_cross_origin_before_transport() -> None:
    requests: list[httpx.Request] = []
    client = _client([], requests)
    credentials = EtcdGatewayCredentials(
        endpoint="https://etcd.internal:2379",
        username="reader",
        password="secret",
    )

    with pytest.raises(ValueError, match="exact etcd origin"):
        _provider(
            [],
            endpoint="https://other.internal:2379",
            http_client=client,
            gateway_credentials=credentials,
        )
    assert requests == []
    client.close()


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(401, json={"message": "bad secret"}), "etcd_authentication_failed"),
        (httpx.Response(200, content=b"not-json"), "etcd_authentication_invalid"),
        (httpx.Response(200, content=b'{"token":"a","token":"b"}'), "etcd_authentication_invalid"),
        (httpx.Response(200, json={"token": "bad token"}), "etcd_authentication_invalid"),
        (
            httpx.Response(200, content=b"x" * 16_385),
            "etcd_authentication_response_too_large",
        ),
    ],
)
def test_gateway_credentials_fail_closed_with_content_free_reasons(
    response: httpx.Response,
    reason: str,
) -> None:
    requests: list[httpx.Request] = []
    provider, _credentials, client = _credential_provider([response], requests)

    observed = _reason(lambda: provider.current_grant(timeout_seconds=1))

    assert observed == reason
    provider.close()
    client.close()


def test_gateway_credentials_retry_only_once_after_unauthorized() -> None:
    requests: list[httpx.Request] = []
    provider, _credentials, client = _credential_provider(
        [
            {"token": "token-one"},
            httpx.Response(401),
            {"token": "token-two"},
            httpx.Response(401),
        ],
        requests,
    )

    assert _reason(lambda: provider.current_grant(timeout_seconds=1)) == (
        "etcd_authentication_failed"
    )
    assert [request.url.path for request in requests] == [
        "/v3/auth/authenticate",
        "/v3/kv/range",
        "/v3/auth/authenticate",
        "/v3/kv/range",
    ]
    provider.close()
    client.close()


def test_gateway_credentials_lock_wait_obeys_total_deadline() -> None:
    requests: list[httpx.Request] = []
    provider, credentials, client = _credential_provider([], requests)
    credentials._lock.acquire()
    try:
        assert _reason(lambda: provider.current_grant(timeout_seconds=0.01)) == (
            "etcd_timeout"
        )
    finally:
        credentials._lock.release()

    assert requests == []
    provider.close()
    client.close()


def test_gateway_credentials_reject_client_global_authorization_header() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        headers={"Authorization": "unscoped-secret"},
    )
    credentials = EtcdGatewayCredentials(
        endpoint="https://etcd.internal:2379",
        username="reader",
        password="secret",
    )

    with pytest.raises(ValueError, match="global Authorization"):
        _provider([], http_client=client, gateway_credentials=credentials)

    assert not client.is_closed
    client.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"endpoint": "http://etcd.internal:2379"},
        {"username": ""},
        {"username": "bad\nuser"},
        {"password": ""},
        {"password": "bad\x00secret"},
    ],
)
def test_gateway_credentials_reject_unsafe_configuration(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "endpoint": "https://etcd.internal:2379",
        "username": "reader",
        "password": "secret",
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        EtcdGatewayCredentials(**values)  # type: ignore[arg-type]


def test_etcd_adapter_returns_revision_fence_after_bracketed_linearizable_reads() -> None:
    requests: list[httpx.Request] = []
    client = _client([_range(), _ttl(), _range()], requests)
    provider = _provider([], http_client=client)

    grant = provider.current_grant(timeout_seconds=2)

    assert grant.authority_id == "cluster-a"
    assert grant.server_id == "postgres-a"
    assert grant.fence_token == 7
    assert 18 <= grant.remaining_seconds < 19
    assert [request.url.path for request in requests] == [
        "/v3/kv/range",
        "/v3/lease/timetolive",
        "/v3/kv/range",
    ]
    assert json.loads(requests[0].content) == {
        "key": ENCODED_KEY,
        "serializable": False,
    }
    assert json.loads(requests[1].content) == {"ID": "11", "keys": True}
    provider.close()
    assert not client.is_closed
    client.close()


def test_etcd_adapter_owns_and_closes_only_its_default_client() -> None:
    provider = EtcdPostgresWriterAuthority(
        endpoint="https://etcd.internal:2379",
        key=KEY,
        authority_id="cluster-a",
        cluster_id=42,
    )
    owned_client = provider._client

    with provider:
        assert not owned_client.is_closed

    assert owned_client.is_closed


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"endpoint": "http://etcd.internal:2379"}, "HTTPS"),
        ({"endpoint": "https://user:secret@etcd.internal"}, "HTTPS"),
        ({"endpoint": "https://etcd.internal/v3"}, "HTTPS"),
        ({"endpoint": "https://etcd.internal?secret=yes"}, "HTTPS"),
        ({"endpoint": "https://etcd.internal:bad"}, "HTTPS"),
        ({"key": "relative"}, "absolute"),
        ({"key": "/bad\nkey"}, "printable"),
        ({"key": "/" + "x" * 512}, "512"),
        ({"authority_id": "not allowed"}, "authority_id"),
        ({"cluster_id": 0}, "cluster_id"),
        ({"cluster_id": True}, "cluster_id"),
        ({"ttl_uncertainty_seconds": -1}, "ttl_uncertainty"),
        ({"ttl_uncertainty_seconds": float("inf")}, "ttl_uncertainty"),
        ({"max_response_bytes": 0}, "max_response_bytes"),
        ({"max_response_bytes": True}, "max_response_bytes"),
    ],
)
def test_etcd_adapter_rejects_unsafe_configuration(
    overrides: dict[str, object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        _provider([], **overrides)


def test_etcd_adapter_allows_only_explicit_insecure_loopback_for_live_tests() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        _provider([], endpoint="http://127.0.0.1:2379")

    provider = _provider(
        [],
        endpoint="http://127.0.0.1:2379",
        allow_insecure_loopback=True,
    )
    provider.close()


@pytest.mark.parametrize(
    ("responses", "reason"),
    [
        (
            [
                {"header": {"cluster_id": "42"}, "count": "0", "kvs": []},
            ],
            "etcd_authority_absent",
        ),
        ([{**_range(), "count": "2"}], "etcd_authority_absent"),
        ([{**_range(), "more": True}], "etcd_authority_absent"),
        ([{**_range(), "more": "false"}], "etcd_authority_absent"),
        ([{**_range(), "kvs": []}], "etcd_authority_absent"),
        ([{**_range(), "kvs": [{**_range()["kvs"][0], "key": "bad"}]}],
         "etcd_authority_malformed"),
        ([_range(lease="0")], "etcd_lease_absent"),
        ([_range(revision="bad")], "etcd_authority_malformed"),
        ([_range(revision=str(2**63))], "etcd_authority_malformed"),
        ([_range(value={**VALUE, "secret": "surplus"})], "etcd_authority_malformed"),
        ([_range(value={**VALUE, "schema": "other"})], "etcd_authority_malformed"),
        ([_range(value={**VALUE, "authority_id": "other"})], "etcd_authority_malformed"),
        ([_range(value={**VALUE, "server_id": "bad server"})], "etcd_authority_malformed"),
        ([_range(cluster_id="43")], "etcd_cluster_mismatch"),
        ([_range(), _ttl(lease="12")], "etcd_lease_expired"),
        ([_range(), _ttl(ttl="0")], "etcd_lease_expired"),
        ([_range(), _ttl(keys=[])], "etcd_lease_detached"),
        ([_range(), _ttl(keys=[ENCODED_KEY, "other"])], "etcd_lease_detached"),
        ([_range(), _ttl(cluster_id="43")], "etcd_cluster_mismatch"),
        ([_range(), _ttl(), _range(revision="8")], "etcd_authority_changed"),
        ([_range(), _ttl(ttl="1"), _range()], "etcd_lease_too_short"),
    ],
)
def test_etcd_adapter_fails_closed_on_invalid_authority_observations(
    responses: list[dict[str, Any]], reason: str
) -> None:
    provider = _provider(responses)

    assert _reason(lambda: provider.current_grant(timeout_seconds=1)) == reason

    provider.close()


def test_etcd_adapter_rejects_invalid_base64_json_and_oversize_values() -> None:
    invalid_base64 = _range()
    invalid_base64["kvs"][0]["value"] = "***secret***"
    invalid_json = _range()
    invalid_json["kvs"][0]["value"] = base64.b64encode(b"secret{").decode()
    oversize = _range()
    oversize["kvs"][0]["value"] = base64.b64encode(b"x" * 2_049).decode()

    for response in (invalid_base64, invalid_json, oversize):
        provider = _provider([response])
        with pytest.raises(PostgresWriterAuthorityError) as denied:
            provider.current_grant(timeout_seconds=1)
        assert denied.value.details == {"reason": "etcd_authority_malformed"}
        provider.close()


def test_etcd_adapter_rejects_duplicate_fields_in_record_and_gateway_json() -> None:
    record = _range()
    record["kvs"][0]["value"] = base64.b64encode(
        b'{"schema":"agnoclaw.postgres-writer-authority.v1",'
        b'"authority_id":"cluster-a","server_id":"postgres-a",'
        b'"server_id":"postgres-b"}'
    ).decode()
    duplicate_gateway = httpx.Response(
        200,
        content=b'{"header":{"cluster_id":"42"},"count":"1","count":"0"}',
    )
    for response in (record, duplicate_gateway):
        provider = _provider([response])
        with pytest.raises(PostgresWriterAuthorityError):
            provider.current_grant(timeout_seconds=1)
        provider.close()


def test_etcd_adapter_enforces_one_total_deadline_when_transport_ignores_timeout() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        time.sleep(0.02)
        return httpx.Response(200, json=_range())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = _provider([], http_client=client)

    assert _reason(lambda: provider.current_grant(timeout_seconds=0.001)) == (
        "etcd_timeout"
    )

    provider.close()
    client.close()


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(503, text="secret upstream"), "etcd_unavailable"),
        (httpx.ReadTimeout("secret timeout"), "etcd_timeout"),
        (httpx.Response(200, text="secret invalid json"), "etcd_response_invalid"),
        (httpx.Response(200, content=b"x" * 100), "etcd_response_too_large"),
    ],
)
def test_etcd_adapter_normalizes_transport_and_response_failures(
    response: httpx.Response | BaseException, reason: str
) -> None:
    provider = _provider([response], max_response_bytes=64)

    assert _reason(lambda: provider.current_grant(timeout_seconds=1)) == reason

    provider.close()


@pytest.mark.parametrize("timeout", [0, True, float("inf"), "1"])
def test_etcd_adapter_rejects_invalid_call_timeout(timeout: object) -> None:
    provider = _provider([])

    with pytest.raises(ValueError, match="timeout_seconds"):
        provider.current_grant(timeout_seconds=timeout)  # type: ignore[arg-type]

    provider.close()


def test_guard_preserves_safe_adapter_denial_reason() -> None:
    provider = _provider(
        [{"header": {"cluster_id": "42"}, "count": "0", "kvs": []}]
    )
    guard = PostgresWriterAuthorityGuard(
        provider,
        check_timeout_seconds=1,
        safety_margin_seconds=0.1,
        max_transaction_seconds=1,
    )

    assert _reason(lambda: guard.admit(object())) == "etcd_authority_absent"

    provider.close()
