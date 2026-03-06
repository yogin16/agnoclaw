"""Tests for runtime error types."""

from agnoclaw.runtime.errors import HarnessError, from_exception


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
    exc = IOError("disk full")
    result = from_exception(exc, details={"disk": "/dev/sda1"})
    assert result.details["disk"] == "/dev/sda1"
    assert result.details["exception_type"] == "OSError"
