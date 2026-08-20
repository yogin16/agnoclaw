from agno.exceptions import (
    AgentRunException,
    ContextWindowExceededError,
    ModelProviderError,
)

from agnoclaw.context_overflow import (
    context_overflow_error,
    is_context_overflow_exception,
    is_context_overflow_signal,
)


def test_exception_classifier_traverses_typed_agno_wrappers() -> None:
    overflow = ContextWindowExceededError("maximum context length exceeded")
    wrapped = AgentRunException(overflow, user_message="request failed")

    assert is_context_overflow_exception(wrapped) is True


def test_exception_classifier_uses_agno_provider_classification() -> None:
    provider_error = ModelProviderError("context_length_exceeded: prompt is too long")

    assert is_context_overflow_exception(provider_error) is True


def test_exception_classifier_does_not_guess_from_arbitrary_errors() -> None:
    generic = RuntimeError("maximum context length exceeded")

    assert is_context_overflow_exception(generic) is False


def test_signal_classifier_requires_failed_status_or_typed_identity() -> None:
    message_only = {
        "status": "completed",
        "message": "maximum context length exceeded",
    }
    typed_identity = {
        "status": "completed",
        "error_id": "context_window_exceeded",
    }

    assert is_context_overflow_signal(message_only) is False
    assert is_context_overflow_signal(typed_identity) is True


def test_overflow_error_contract_is_stable_and_nonretryable() -> None:
    error = context_overflow_error(
        run_id="run-1",
        reason="exhausted",
        source="provider_exception",
    )

    assert error.code == "CONTEXT_OVERFLOW_RETRY_EXHAUSTED"
    assert error.retryable is False
    assert error.details == {
        "run_id": "run-1",
        "reason": "exhausted",
        "source": "provider_exception",
        "attempts": 2,
    }
