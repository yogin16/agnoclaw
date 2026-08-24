"""Tests for runtime error types."""

import pytest

from agnoclaw.runtime import PostgresWriterAuthorityError
from agnoclaw.runtime.errors import AgnoAuthError, AgnoConfigError, HarnessError, from_exception
from agnoclaw.runtime.store import RuntimeStoreConnectionLostError


def test_harness_error_fields():
    err = HarnessError(
        code="TEST_001",
        category="test",
        message="Test error",
        retryable=False,
    )
    assert err.code == "TEST_001"
    assert err.category == "test"
    assert err.message == "Test error"
    assert err.retryable is False
    assert err.details is None


def test_harness_error_str():
    err = HarnessError(code="ERR_X", category="x", message="broke", retryable=True)
    assert str(err) == "[ERR_X] broke"


def test_harness_error_with_details():
    err = HarnessError(
        code="DETAIL",
        category="test",
        message="detailed",
        retryable=False,
        details={"key": "value"},
    )
    assert err.details == {"key": "value"}


def test_from_exception_wraps_standard_exception():
    exc = ValueError("bad value")
    result = from_exception(exc, code="VALIDATION_ERROR", category="input")
    assert isinstance(result, HarnessError)
    assert result.code == "VALIDATION_ERROR"
    assert result.category == "input"
    assert result.message == "bad value"
    assert result.details["exception_type"] == "ValueError"


def test_from_exception_passthrough_harness_error():
    original = HarnessError(
        code="ORIG",
        category="orig",
        message="original error",
        retryable=True,
    )
    result = from_exception(original)
    assert result is original  # same object, not wrapped


def test_from_exception_default_code():
    exc = RuntimeError("boom")
    result = from_exception(exc)
    assert result.code == "INTERNAL_UNEXPECTED"
    assert result.category == "internal"
    assert result.retryable is False


def test_from_exception_with_custom_details():
    exc = OSError("disk full")
    result = from_exception(exc, details={"disk": "/dev/sda1"})
    assert result.details["disk"] == "/dev/sda1"
    assert result.details["exception_type"] == "OSError"


def test_agno_config_error_shape():
    err = AgnoConfigError("bad model config")
    assert isinstance(err, HarnessError)
    assert err.code == "AGNO_CONFIG_ERROR"
    assert err.category == "config"
    assert err.retryable is False


def test_agno_auth_error_shape():
    err = AgnoAuthError("missing key")
    assert isinstance(err, HarnessError)
    assert err.code == "AGNO_AUTH_ERROR"
    assert err.category == "auth"
    assert err.retryable is False


def test_runtime_store_connection_loss_is_safe_and_not_blindly_retryable():
    err = RuntimeStoreConnectionLostError(backend="postgres")

    assert err.code == "RUNTIME_STORE_CONNECTION_LOST"
    assert err.category == "runtime_store"
    assert err.retryable is False
    assert err.details == {
        "backend": "postgres",
        "reconciliation_required": True,
    }


@pytest.mark.parametrize(
    "reason",
    [
        "authority_provider_unavailable",
        "authority_provider_timeout",
        "server_identity_mismatch",
        "authority_changed",
        "transaction_timeout",
    ],
)
def test_postgres_writer_authority_denial_has_safe_stable_shape(reason: str) -> None:
    err = PostgresWriterAuthorityError(reason=reason)

    assert err.code == "POSTGRES_WRITER_AUTHORITY_DENIED"
    assert err.category == "runtime_store"
    assert err.retryable is True
    assert err.details == {"reason": reason}
    assert reason not in str(err)


def test_postgres_writer_authority_denial_normalizes_untrusted_reason() -> None:
    for reason in ("secret endpoint diagnostic: token=abc", None):
        err = PostgresWriterAuthorityError(reason=reason)  # type: ignore[arg-type]

        assert err.details == {"reason": "authority_denied"}
        assert "secret" not in str(err)
