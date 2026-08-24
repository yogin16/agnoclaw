"""Provider context-overflow classification and stable harness errors."""

from __future__ import annotations

from typing import Any

from agno.exceptions import (
    AgentRunException,
    ContextWindowExceededError,
    ModelProviderError,
)

from .runtime.errors import HarnessError


def is_context_overflow_exception(exc: BaseException) -> bool:
    """Recognize only Agno's typed provider-overflow exception graph."""
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending and len(seen) < 12:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ContextWindowExceededError):
            return True
        if isinstance(current, ModelProviderError):
            try:
                if isinstance(ModelProviderError.classify(current), ContextWindowExceededError):
                    return True
            except Exception:
                pass
        for nested in (current.__cause__, current.__context__):
            if isinstance(nested, BaseException):
                pending.append(nested)
        if isinstance(current, AgentRunException):
            pending.extend(item for item in current.args if isinstance(item, BaseException))
    return False


def is_context_overflow_signal(signal: dict[str, Any]) -> bool:
    """Classify a failed Agno RunError payload using its official classifier."""
    error_id = str(signal.get("error_id") or "").casefold()
    error_type = str(signal.get("error_type") or "").casefold()
    if "context_window_exceeded" in error_id or "context_window_exceeded" in error_type:
        return True
    message = _normalize_message(signal.get("message"))
    if not message or str(signal.get("status") or "").casefold() != "error":
        return False
    classified = ModelProviderError.classify(ModelProviderError(message=message))
    return isinstance(classified, ContextWindowExceededError)


def context_overflow_error(*, run_id: str, reason: str, source: str) -> HarnessError:
    """Return the stable fail-closed error for an unsafe or exhausted recovery."""
    codes = {
        "disabled": "CONTEXT_WINDOW_EXCEEDED",
        "stream": "CONTEXT_OVERFLOW_STREAM_UNSAFE",
        "tool_activity": "CONTEXT_OVERFLOW_RETRY_UNSAFE",
        "exhausted": "CONTEXT_OVERFLOW_RETRY_EXHAUSTED",
    }
    messages = {
        "disabled": "The provider rejected the model context; automatic recovery is disabled.",
        "stream": "Context-overflow retry is unsafe after a streaming request starts.",
        "tool_activity": "Context-overflow retry is unsafe after this run observed a tool call.",
        "exhausted": "Context compaction completed, but the one permitted retry also overflowed.",
    }
    return HarnessError(
        code=codes[reason],
        category="context",
        message=messages[reason],
        retryable=False,
        details={
            "run_id": run_id,
            "reason": reason,
            "source": source,
            "attempts": 2 if reason == "exhausted" else 1,
        },
    )


def _normalize_message(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return text
