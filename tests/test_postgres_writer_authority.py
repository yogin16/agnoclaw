"""Pure contracts for externally fenced PostgreSQL writer admission."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pytest

from agnoclaw import PostgresRuntimeStore
from agnoclaw.runtime import (
    PostgresWriterAuthorityError,
    PostgresWriterAuthorityGrant,
    PostgresWriterAuthorityProvider,
)
from agnoclaw.runtime.postgres_authority import PostgresWriterAuthorityGuard


@dataclass
class MutableAuthority(PostgresWriterAuthorityProvider):
    value: PostgresWriterAuthorityGrant | BaseException
    observed_timeouts: list[float]

    def current_grant(
        self,
        *,
        timeout_seconds: float,
    ) -> PostgresWriterAuthorityGrant:
        self.observed_timeouts.append(timeout_seconds)
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class FakeConnection:
    def __init__(
        self,
        *,
        server_id: str = "cluster-a",
        in_recovery: bool = False,
    ) -> None:
        self.server_id = server_id
        self.in_recovery = in_recovery
        self.timeout_values: list[str] = []

    def execute(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> FakeResult:
        if "pg_is_in_recovery" in query:
            return FakeResult(
                {"server_id": self.server_id, "in_recovery": self.in_recovery}
            )
        if "transaction_timeout" in query:
            assert params is not None
            self.timeout_values.append(str(params[0]))
            return FakeResult({"set_config": str(params[0])})
        raise AssertionError(f"unexpected query: {query}")


class BrokenConnection(FakeConnection):
    def execute(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> FakeResult:
        raise RuntimeError("secret database diagnostic")


class FakeResult:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    def fetchone(self) -> dict[str, object] | None:
        return self._row


class MalformedConnection(FakeConnection):
    def execute(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> FakeResult:
        return FakeResult({"unexpected": True})


class MissingIdentityConnection(FakeConnection):
    def execute(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> FakeResult:
        return FakeResult({"server_id": None, "in_recovery": False})


class NoIdentityRowConnection(FakeConnection):
    def execute(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> FakeResult:
        return EmptyResult()


class TimeoutConfigurationConnection(FakeConnection):
    def execute(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> FakeResult:
        if "transaction_timeout" in query:
            raise RuntimeError("secret server diagnostic")
        return super().execute(query, params)


class EmptyResult(FakeResult):
    def __init__(self) -> None:
        super().__init__({})

    def fetchone(self) -> None:
        return None


@dataclass
class SlowAuthority(PostgresWriterAuthorityProvider):
    value: PostgresWriterAuthorityGrant

    def current_grant(
        self,
        *,
        timeout_seconds: float,
    ) -> PostgresWriterAuthorityGrant:
        time.sleep(timeout_seconds + 0.01)
        return self.value


def _grant(
    *,
    authority_id: str = "control-plane",
    server_id: str = "cluster-a",
    fence_token: int = 1,
    remaining_seconds: float = 5.0,
) -> PostgresWriterAuthorityGrant:
    return PostgresWriterAuthorityGrant(
        authority_id=authority_id,
        server_id=server_id,
        fence_token=fence_token,
        remaining_seconds=remaining_seconds,
    )


def _guard(
    provider: MutableAuthority,
    *,
    margin: float = 1.0,
    maximum: float = 30.0,
) -> PostgresWriterAuthorityGuard:
    return PostgresWriterAuthorityGuard(
        provider,
        check_timeout_seconds=0.25,
        safety_margin_seconds=margin,
        max_transaction_seconds=maximum,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authority_id", ""),
        ("authority_id", None),
        ("server_id", "contains spaces"),
        ("server_id", None),
        ("fence_token", 0),
        ("fence_token", True),
        ("fence_token", 1.5),
        ("remaining_seconds", 0),
        ("remaining_seconds", "5"),
        ("remaining_seconds", float("inf")),
    ],
)
def test_writer_authority_grant_rejects_invalid_values(field: str, value: Any) -> None:
    values: dict[str, Any] = {
        "authority_id": "control-plane",
        "server_id": "cluster-a",
        "fence_token": 1,
        "remaining_seconds": 5.0,
    }
    values[field] = value

    with pytest.raises(ValueError):
        PostgresWriterAuthorityGrant(**values)


def test_guard_admits_exact_writer_and_bounds_transaction_inside_lease() -> None:
    provider = MutableAuthority(_grant(remaining_seconds=4.25), [])
    conn = FakeConnection()
    guard = _guard(provider, margin=1.0, maximum=2.0)

    admission = guard.admit(conn)
    guard.revalidate(conn, admission)

    assert admission.transaction_timeout_ms == 2_000
    assert conn.timeout_values == ["2000ms"]
    assert provider.observed_timeouts == [0.25, 0.25]


@pytest.mark.parametrize(
    ("connection", "reason"),
    [
        (FakeConnection(server_id="cluster-b"), "server_identity_mismatch"),
        (FakeConnection(in_recovery=True), "server_in_recovery"),
        (FakeConnection(server_id=""), "server_identity_mismatch"),
    ],
)
def test_guard_denies_wrong_or_recovering_server(
    connection: FakeConnection,
    reason: str,
) -> None:
    guard = _guard(MutableAuthority(_grant(), []))

    with pytest.raises(PostgresWriterAuthorityError) as denied:
        guard.admit(connection)

    assert denied.value.details == {"reason": reason}
    assert connection.timeout_values == []


def test_guard_denies_lease_that_cannot_cover_its_safety_margin() -> None:
    conn = FakeConnection()
    guard = _guard(MutableAuthority(_grant(remaining_seconds=0.5), []), margin=1.0)

    with pytest.raises(PostgresWriterAuthorityError) as denied:
        guard.admit(conn)

    assert denied.value.details == {"reason": "authority_lease_too_short"}
    assert conn.timeout_values == []


def test_guard_normalizes_server_identity_failure() -> None:
    for connection in (
        BrokenConnection(),
        MalformedConnection(),
        MissingIdentityConnection(),
        NoIdentityRowConnection(),
    ):
        with pytest.raises(PostgresWriterAuthorityError) as denied:
            _guard(MutableAuthority(_grant(), [])).admit(connection)

        assert denied.value.details == {"reason": "server_identity_unavailable"}
        assert "secret" not in str(denied.value)


def test_guard_normalizes_transaction_timeout_configuration_failure() -> None:
    with pytest.raises(PostgresWriterAuthorityError) as denied:
        _guard(MutableAuthority(_grant(), [])).admit(TimeoutConfigurationConnection())

    assert denied.value.details == {"reason": "transaction_timeout_unavailable"}
    assert "secret" not in str(denied.value)


def test_guard_normalizes_provider_failure_without_secret_text() -> None:
    provider = MutableAuthority(RuntimeError("secret external endpoint"), [])

    with pytest.raises(PostgresWriterAuthorityError) as denied:
        _guard(provider).admit(FakeConnection())

    assert denied.value.details == {"reason": "authority_provider_unavailable"}
    assert "secret" not in str(denied.value)


def test_guard_denies_provider_that_ignores_its_timeout() -> None:
    guard = PostgresWriterAuthorityGuard(
        SlowAuthority(_grant()),
        check_timeout_seconds=0.001,
        safety_margin_seconds=1,
        max_transaction_seconds=1,
    )

    with pytest.raises(PostgresWriterAuthorityError) as denied:
        guard.admit(FakeConnection())

    assert denied.value.details == {"reason": "authority_provider_timeout"}


def test_guard_denies_stale_conflicting_and_changed_generations() -> None:
    provider = MutableAuthority(_grant(fence_token=2), [])
    guard = _guard(provider)
    admission = guard.admit(FakeConnection())

    provider.value = _grant(fence_token=1)
    with pytest.raises(PostgresWriterAuthorityError) as stale:
        guard.revalidate(FakeConnection(), admission)
    assert stale.value.details == {"reason": "authority_generation_stale"}

    provider.value = _grant(server_id="cluster-b", fence_token=2)
    with pytest.raises(PostgresWriterAuthorityError) as conflict:
        guard.revalidate(FakeConnection(), admission)
    assert conflict.value.details == {"reason": "authority_generation_conflict"}

    provider.value = _grant(fence_token=3)
    with pytest.raises(PostgresWriterAuthorityError) as changed:
        guard.revalidate(FakeConnection(), admission)
    assert changed.value.details == {"reason": "authority_changed"}


def test_guard_denies_invalid_grant_and_changed_authority_identity() -> None:
    invalid = MutableAuthority(_grant(), [])
    invalid.value = object()  # type: ignore[assignment]
    with pytest.raises(PostgresWriterAuthorityError) as invalid_grant:
        _guard(invalid).admit(FakeConnection())
    assert invalid_grant.value.details == {"reason": "authority_grant_invalid"}

    provider = MutableAuthority(_grant(authority_id="authority-a"), [])
    guard = _guard(provider)
    admission = guard.admit(FakeConnection())
    provider.value = _grant(authority_id="authority-b", fence_token=2)
    with pytest.raises(PostgresWriterAuthorityError) as identity:
        guard.revalidate(FakeConnection(), admission)
    assert identity.value.details == {"reason": "authority_identity_changed"}


def test_guard_constructor_rejects_unbounded_policy_values() -> None:
    provider = MutableAuthority(_grant(), [])

    with pytest.raises(ValueError, match="check_timeout_seconds"):
        PostgresWriterAuthorityGuard(
            provider,
            check_timeout_seconds=0,
            safety_margin_seconds=1,
            max_transaction_seconds=1,
        )
    with pytest.raises(ValueError, match="safety_margin_seconds"):
        PostgresWriterAuthorityGuard(
            provider,
            check_timeout_seconds=1,
            safety_margin_seconds=86_400,
            max_transaction_seconds=1,
        )


def test_store_rejects_invalid_authority_policy_before_opening_a_pool() -> None:
    for value in (0, True, "1"):
        with pytest.raises(ValueError, match="check_timeout_seconds"):
            PostgresRuntimeStore(
                "postgresql://unused.invalid/unused",
                writer_authority_check_timeout_seconds=value,  # type: ignore[arg-type]
            )
